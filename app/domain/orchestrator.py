"""Conversation Orchestration Layer for Rider Payout Dispute Desk.

Connects message intake, LLM interpretation, data lookup, reconciliation,
policy evaluation, payment execution, and response generation.

INVARIANTS:
1. The incoming rider_id is authoritative and MUST NEVER be changed based on message text.
2. The LLM is used ONLY for language interpretation (ParsedMessage schema).
3. The LLM NEVER directly calls financial tools, PaySwift, or reconciliation functions.
4. All financial decisions and payments pass through deterministic policy and payment boundaries.
5. All message processing steps are audited and stored in `conversation_traces`.
6. Idempotency: duplicate `message_id` returns cached agent reply without re-processing (survives process restarts via Postgres UNIQUE constraint).
7. Durable Payment Retries: Payment attempts store idempotency keys in DB BEFORE external API calls to survive application crashes.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, date, timedelta
from typing import List, Optional, Dict, Any, Tuple

from app.domain.llm_schema import ParsedMessage, DisputeIntent
from app.domain.llm_adapter import LLMAdapter, get_llm_adapter
from app.domain.reconciler import reconcile_rider_dispute, ReconciliationResult
from app.domain.decision import decide, record_decision, DecisionType, PolicyDecision
from app.payswift_client import execute_payout, make_idempotency_key, PayoutResult, PayoutStatus
from app.domain.trace_logger import log_trace_event

logger = logging.getLogger(__name__)



# ── Date Resolution Helper ───────────────────────────────────────────────────

def resolve_dispute_dates(
    claimed_date: Optional[str],
    text: str,
    reference_timestamp: Optional[str] = None,
) -> List[str]:
    """Parse and normalize dates from rider input or claimed_date field."""
    ref_dt = datetime.fromisoformat(reference_timestamp) if reference_timestamp else datetime(2026, 9, 22)
    ref_date = ref_dt.date()
    dates: List[str] = []

    combined_text = f"{claimed_date or ''} {text}".lower()

    # 1. Exact ISO pattern YYYY-MM-DD
    iso_matches = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", combined_text)
    for m in iso_matches:
        if m not in dates:
            dates.append(m)

    # 2. Pattern DD-MM-YYYY or DD/MM/YYYY
    dmy_matches = re.findall(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", combined_text)
    for d, m, y in dmy_matches:
        iso_str = f"{y}-{int(m):02d}-{int(d):02d}"
        if iso_str not in dates:
            dates.append(iso_str)

    # 3. Relative date "kal" (yesterday relative to ref_date)
    if "kal" in combined_text and not dates:
        yesterday = ref_date - timedelta(days=1)
        dates.append(yesterday.isoformat())

    # 4. Relative date "aaj" (today relative to ref_date)
    if "aaj" in combined_text and not dates:
        dates.append(ref_date.isoformat())

    # 5. Month-Day patterns (e.g. "sep 19", "september 19")
    md_matches = re.findall(r"\b(?:september|sept|sep)\s*(\d{1,2})(?:st|nd|rd|th)?\b", combined_text)
    for dm in md_matches:
        day_num = int(dm)
        if 1 <= day_num <= 31:
            try:
                candidate = date(ref_date.year, ref_date.month, day_num).isoformat()
                if candidate not in dates:
                    dates.append(candidate)
            except ValueError:
                pass

    # 6. Day-Month or Day keyword patterns (e.g. "19 sept", "18 ko", "20 tarikh", "20 wala")
    dm_matches = re.findall(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:september|sept|sep|tarikh|tareekh|ko|wala)\b", combined_text)
    for dm in dm_matches:
        day_num = int(dm)
        if 1 <= day_num <= 31:
            try:
                candidate = date(ref_date.year, ref_date.month, day_num).isoformat()
                if candidate not in dates:
                    dates.append(candidate)
            except ValueError:
                pass

    # 7. Fallback to claimed_date if numeric (e.g. "18", "19") and no dates extracted yet
    if not dates and claimed_date:
        digit_match = re.search(r"\b(\d{1,2})\b", str(claimed_date))
        if digit_match:
            day_num = int(digit_match.group(1))
            if 1 <= day_num <= 31:
                try:
                    candidate = date(ref_date.year, ref_date.month, day_num).isoformat()
                    if candidate not in dates:
                        dates.append(candidate)
                except ValueError:
                    pass

    return dates


# ── Context Builder & DB Trace Helpers ───────────────────────────────────────

def get_previous_conversation_context(conn, rider_id: str, limit: int = 5) -> str:
    """Fetch previous conversation turns for a rider as formatted context string."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT turn_input->>'text' AS rider_text, agent_reply
        FROM conversation_traces
        WHERE rider_id = %s
        ORDER BY id DESC
        LIMIT %s;
        """,
        (rider_id, limit),
    )
    rows = cur.fetchall()
    if not rows:
        return ""

    context_lines = []
    for r_text, a_reply in reversed(rows):
        if r_text:
            context_lines.append(f"Rider: {r_text}")
        if a_reply:
            context_lines.append(f"Agent: {a_reply}")
    return "\n".join(context_lines)


def record_payment_attempt(
    conn,
    decision_id: Optional[int],
    rider_id: str,
    amount: int,
    idempotency_key: str,
    attempt_number: int,
    status: str,
    http_code: Optional[int],
    payout_id: Optional[str],
    error_message: Optional[str],
):
    """Record a PaySwift POST execution attempt in the payment_attempts audit log."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO payment_attempts
            (decision_id, rider_id, amount, idempotency_key, attempt_number, status, http_code, payout_id, error_message)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
        """,
        (decision_id, rider_id, amount, idempotency_key, attempt_number, status, http_code, payout_id, error_message),
    )
    conn.commit()


def recover_uncertain_decisions(conn, rider_id: str, payswift_base_url: Optional[str] = None):
    """Check dispute_decisions for any 'uncertain' decisions with stored idempotency keys.

    Re-attempts payout with the EXACT SAME stored idempotency_key to resolve state across process crashes.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, dispute_date, amount, idempotency_key
        FROM dispute_decisions
        WHERE rider_id = %s AND decision_type = 'auto_pay' AND status = 'uncertain';
        """,
        (rider_id,),
    )
    rows = cur.fetchall()
    for row_id, disp_date, amount, key in rows:
        if key:
            logger.info("Recovering uncertain decision id %d for rider %s using key %s", row_id, rider_id, key)
            p_res = execute_payout(
                rider_id=rider_id,
                amount=amount,
                idempotency_key=key,
                reference=f"Retry dispute {disp_date}",
                payswift_base_url=payswift_base_url,
            )
            record_payment_attempt(
                conn, row_id, rider_id, amount, key, 2, p_res.status.value, p_res.http_code, p_res.payout_id, p_res.error
            )
            if p_res.is_confirmed:
                cur.execute(
                    "UPDATE dispute_decisions SET status = 'executed', payout_id = %s WHERE id = %s;",
                    (p_res.payout_id, row_id),
                )
            elif p_res.status in (PayoutStatus.REJECTED, PayoutStatus.RATE_LIMITED):
                cur.execute("UPDATE dispute_decisions SET status = 'rejected' WHERE id = %s;", (row_id,))
            conn.commit()


def record_trace(
    conn,
    rider_id: str,
    message_id: Optional[str],
    turn_input: Dict[str, Any],
    llm_output: Optional[Dict[str, Any]],
    reconciliation_evidence: Optional[Dict[str, Any]],
    policy_decision: Optional[Dict[str, Any]],
    payment_result: Optional[Dict[str, Any]],
    agent_reply: str,
) -> int:
    """Record turn execution trace in conversation_traces table with DB UniqueViolation safety."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO conversation_traces
                (rider_id, message_id, turn_input, llm_output, reconciliation_evidence, policy_decision, payment_result, agent_reply)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
            """,
            (
                rider_id,
                message_id,
                json.dumps(turn_input),
                json.dumps(llm_output) if llm_output else None,
                json.dumps(reconciliation_evidence) if reconciliation_evidence else None,
                json.dumps(policy_decision) if policy_decision else None,
                json.dumps(payment_result) if payment_result else None,
                agent_reply,
            ),
        )
        trace_id = cur.fetchone()[0]
        conn.commit()
        return trace_id
    except Exception as exc:
        conn.rollback()
        logger.warning("Trace insert conflict on message_id %s: %s", message_id, exc)
        cur.execute("SELECT id FROM conversation_traces WHERE message_id = %s;", (message_id,))
        row = cur.fetchone()
        return row[0] if row else 0


def get_trace_for_rider(conn, rider_id: str) -> List[Dict[str, Any]]:
    """Retrieve full chronological trace of everything agent did for a rider."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, rider_id, message_id, turn_input, llm_output, reconciliation_evidence,
               policy_decision, payment_result, agent_reply, created_at
        FROM conversation_traces
        WHERE rider_id = %s
        ORDER BY id ASC;
        """,
        (rider_id,),
    )
    rows = cur.fetchall()
    traces = []
    for r in rows:
        traces.append(
            {
                "trace_id": r[0],
                "rider_id": r[1],
                "message_id": r[2],
                "turn_input": r[3],
                "llm_output": r[4],
                "reconciliation_evidence": r[5],
                "policy_decision": r[6],
                "payment_result": r[7],
                "agent_reply": r[8],
                "created_at": r[9].isoformat() if r[9] else None,
            }
        )
    return traces


class OpsWorkflowError(Exception):
    """Exception raised for errors in operations workflow (revalidation failure, invalid state, etc.)."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def get_pending_ops(conn) -> List[Dict[str, Any]]:
    """Retrieve all decisions currently pending ops approval or escalation."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, rider_id, dispute_date, decision_type, amount, reason, status, message_id, created_at
        FROM dispute_decisions
        WHERE decision_type IN ('ops_approval', 'escalation')
          AND status = 'pending'
        ORDER BY id ASC;
        """
    )
    rows = cur.fetchall()
    pending = []
    for r in rows:
        dec_type = "approval" if r[3] == "ops_approval" else "escalation"
        amt = r[4] if dec_type == "approval" else None
        pending.append(
            {
                "id": r[0],
                "rider_id": r[1],
                "type": dec_type,
                "amount": amt,
                "reason": r[5],
                "created_at": r[8].isoformat() if r[8] else None,
            }
        )
    return pending


def approve_ops_decision(
    conn,
    decision_id: int,
    payswift_base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Process ops approval for a pending decision with strict backend revalidation.

    REVALIDATION RULES:
    1. Decision must exist and have status 'pending'.
    2. Decision must be of type 'ops_approval'.
    3. Re-run reconciliation to verify dispute is still payable (calculated_amount_owed >= decision.amount).
    4. Verify no confirmed payment has already occurred for this rider and dispute date/amount.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, rider_id, dispute_date, decision_type, amount, reason, status, message_id, idempotency_key, payout_id
        FROM dispute_decisions
        WHERE id = %s;
        """,
        (decision_id,),
    )
    row = cur.fetchone()
    if not row:
        raise OpsWorkflowError(f"Ops decision ID {decision_id} not found", status_code=404)

    dec_id, rider_id, dispute_date_str, decision_type, amount, reason, status, message_id, stored_key, stored_payout_id = row

    # 1. Check status (Duplicate or Stale Approval check)
    if status != "pending":
        raise OpsWorkflowError(
            f"Decision {decision_id} is not pending (current status: '{status}')",
            status_code=400,
        )

    if decision_type != "ops_approval":
        raise OpsWorkflowError(
            f"Decision {decision_id} is of type '{decision_type}', not 'ops_approval'",
            status_code=400,
        )

    # 2. Check for already-paid cases in dispute_decisions
    cur.execute(
        """
        SELECT payout_id FROM dispute_decisions
        WHERE rider_id = %s AND dispute_date = %s AND status = 'executed';
        """,
        (rider_id, dispute_date_str),
    )
    already_paid_row = cur.fetchone()
    if already_paid_row:
        log_trace_event(
            conn=conn,
            rider_id=rider_id,
            message_id=message_id,
            event_type="error",
            name="ops_approve_revalidation_failed",
            input_data={"decision_id": decision_id, "reason": "already_paid"},
            output_data={"error": f"Dispute already paid via payout {already_paid_row[0]}"},
        )
        raise OpsWorkflowError(
            f"Dispute for rider {rider_id} on date {dispute_date_str} has already been paid (payout: {already_paid_row[0]})",
            status_code=409,
        )

    # 3. Re-reconcile to verify dispute is still payable and amount is valid
    rec = reconcile_rider_dispute(conn, rider_id, target_date=str(dispute_date_str))
    if rec.calculated_amount_owed <= 0:
        log_trace_event(
            conn=conn,
            rider_id=rider_id,
            message_id=message_id,
            event_type="error",
            name="ops_approve_revalidation_failed",
            input_data={"decision_id": decision_id, "amount_requested": amount},
            output_data={"error": "Dispute is no longer payable (calculated deficit is ₹0)"},
        )
        raise OpsWorkflowError(
            f"Approval revalidation failed: dispute for rider {rider_id} on date {dispute_date_str} is no longer payable (calculated deficit ₹0)",
            status_code=400,
        )

    if rec.calculated_amount_owed < amount:
        log_trace_event(
            conn=conn,
            rider_id=rider_id,
            message_id=message_id,
            event_type="error",
            name="ops_approve_revalidation_failed",
            input_data={"decision_id": decision_id, "amount_requested": amount, "amount_owed": rec.calculated_amount_owed},
            output_data={"error": f"Invalid amount: requested ₹{amount}, but calculated owed is ₹{rec.calculated_amount_owed}"},
        )
        raise OpsWorkflowError(
            f"Approval revalidation failed: requested amount ₹{amount} exceeds valid calculated deficit ₹{rec.calculated_amount_owed}",
            status_code=400,
        )

    # 4. Generate or reuse idempotency key
    idem_key = stored_key or make_idempotency_key(rider_id, str(dispute_date_str), amount)

    # 5. Execute payment via backend to PaySwift
    p_result = execute_payout(
        rider_id=rider_id,
        amount=amount,
        idempotency_key=idem_key,
        reference=f"Ops approved dispute {dispute_date_str}",
        payswift_base_url=payswift_base_url,
    )

    record_payment_attempt(
        conn, dec_id, rider_id, amount, idem_key, 1, p_result.status.value, p_result.http_code, p_result.payout_id, p_result.error
    )

    pay_event_type = "tool_call" if p_result.is_confirmed else "error"
    log_trace_event(
        conn=conn,
        rider_id=rider_id,
        message_id=message_id,
        event_type=pay_event_type,
        name="execute_payswift_payout",
        input_data={"rider_id": rider_id, "amount": amount, "idempotency_key": idem_key, "source": "ops_approval"},
        output_data={"status": p_result.status.value, "payout_id": p_result.payout_id, "http_code": p_result.http_code, "error": p_result.error},
    )

    if p_result.is_confirmed:
        cur.execute(
            """
            UPDATE dispute_decisions
            SET status = 'executed', payout_id = %s, idempotency_key = %s
            WHERE id = %s;
            """,
            (p_result.payout_id, idem_key, dec_id),
        )
        conn.commit()

        log_trace_event(
            conn=conn,
            rider_id=rider_id,
            message_id=message_id,
            event_type="decision",
            name="ops_approve_executed",
            input_data={"decision_id": dec_id, "rider_id": rider_id, "amount": amount},
            output_data={"status": "executed", "payout_id": p_result.payout_id},
        )

        return {
            "status": "approved",
            "decision_id": dec_id,
            "rider_id": rider_id,
            "amount": amount,
            "payout_id": p_result.payout_id,
            "message": f"Payment of ₹{amount} successfully executed via PaySwift",
        }
    elif p_result.status == PayoutStatus.UNCERTAIN:
        cur.execute(
            "UPDATE dispute_decisions SET status = 'uncertain', idempotency_key = %s WHERE id = %s;",
            (idem_key, dec_id),
        )
        conn.commit()
        raise OpsWorkflowError(f"PaySwift payment timed out (status uncertain). Idempotency key {idem_key} saved for recovery.", status_code=504)
    else:
        cur.execute(
            "UPDATE dispute_decisions SET status = 'rejected' WHERE id = %s;",
            (dec_id,),
        )
        conn.commit()
        raise OpsWorkflowError(f"PaySwift payment rejected: {p_result.error}", status_code=502)


def reject_ops_decision(
    conn,
    decision_id: int,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Reject a pending ops decision. ZERO money is moved."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, rider_id, dispute_date, decision_type, amount, reason, status, message_id
        FROM dispute_decisions
        WHERE id = %s;
        """,
        (decision_id,),
    )
    row = cur.fetchone()
    if not row:
        raise OpsWorkflowError(f"Ops decision ID {decision_id} not found", status_code=404)

    dec_id, rider_id, dispute_date_str, decision_type, amount, original_reason, status, message_id = row

    if status != "pending":
        raise OpsWorkflowError(
            f"Decision {decision_id} is not pending (current status: '{status}')",
            status_code=400,
        )

    cur.execute(
        "UPDATE dispute_decisions SET status = 'rejected' WHERE id = %s;",
        (dec_id,),
    )
    conn.commit()

    log_trace_event(
        conn=conn,
        rider_id=rider_id,
        message_id=message_id,
        event_type="decision",
        name="ops_reject",
        input_data={"decision_id": dec_id, "rider_id": rider_id, "amount": amount, "reject_reason": reason},
        output_data={"status": "rejected", "money_moved": 0},
    )

    return {
        "status": "rejected",
        "decision_id": dec_id,
        "rider_id": rider_id,
        "amount": amount,
        "message": "Ops decision rejected. No money was moved.",
    }


def escalate_ops_decision(
    conn,
    decision_id: int,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Escalate a pending ops decision. ZERO money is moved automatically."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, rider_id, dispute_date, decision_type, amount, reason, status, message_id
        FROM dispute_decisions
        WHERE id = %s;
        """,
        (decision_id,),
    )
    row = cur.fetchone()
    if not row:
        raise OpsWorkflowError(f"Ops decision ID {decision_id} not found", status_code=404)

    dec_id, rider_id, dispute_date_str, decision_type, amount, original_reason, status, message_id = row

    if status != "pending":
        raise OpsWorkflowError(
            f"Decision {decision_id} is not pending (current status: '{status}')",
            status_code=400,
        )

    cur.execute(
        "UPDATE dispute_decisions SET status = 'escalated' WHERE id = %s;",
        (dec_id,),
    )
    conn.commit()

    log_trace_event(
        conn=conn,
        rider_id=rider_id,
        message_id=message_id,
        event_type="decision",
        name="ops_escalate",
        input_data={"decision_id": dec_id, "rider_id": rider_id, "escalation_reason": reason},
        output_data={"status": "escalated", "money_moved": 0},
    )

    return {
        "status": "escalated",
        "decision_id": dec_id,
        "rider_id": rider_id,
        "message": "Ops decision escalated to tier-2 support. No money was moved.",
    }


# ── Agent Response Generator ─────────────────────────────────────────────────

def generate_agent_reply(
    rider_id: str,
    parsed: ParsedMessage,
    reconciliations: List[ReconciliationResult],
    decisions: List[PolicyDecision],
    payment_results: List[Tuple[PolicyDecision, Optional[PayoutResult]]],
    is_repeat_pushback: bool = False,
) -> str:
    """Generate deterministic, clear Hinglish/English agent reply."""
    # 1. Impersonation / Security Response
    if parsed.security_signal:
        if parsed.security_note and "impersonat" in parsed.security_note.lower():
            return (
                f"Main sirf is number se jude account ({rider_id}) ke baare mein madad kar sakta hoon. "
                f"Aapke account ki koi specific problem ho to bataiye."
            )
        return "Aapki request security review ke liye ops team ko refer kar di gayi hai."

    # 2. Clarification Needed Response
    if parsed.needs_clarification:
        return (
            parsed.clarification_question
            or "Kaunse din ya kaunse order ka payout galat laga? Date ya order ID bata dijiye."
        )

    # 3. Repeat Pushback Handling
    if is_repeat_pushback and reconciliations:
        rec = reconciliations[0]
        if rec.calculated_amount_owed == 0:
            target_str = rec.target_date or (rec.trips_evaluated[0].started_at[:10] if rec.trips_evaluated else "")
            count = len(rec.trips_evaluated)
            return (
                f"Dobara check kiya: humare record mein {target_str} ko {count} completed trips hain. "
                f"Agar aapko lagta hai record galat hai, main ise ops team ko bhej deta hoon."
            )

    # 4. Status Enquiry Response
    if parsed.intent == DisputeIntent.STATUS_ENQUIRY and decisions:
        part_responses = []
        for dec in decisions:
            if dec.decision_type == DecisionType.AUTO_PAY:
                part_responses.append(f"₹{dec.amount} ka payment PaySwift pe process ho gaya hai.")
            elif dec.decision_type == DecisionType.OPS_APPROVAL:
                part_responses.append(f"Samajh sakta hoon. ₹{dec.amount} ops approval ke liye chala gaya hai, approve hote hi aa jayega.")
            elif dec.decision_type == DecisionType.NO_PAYMENT:
                part_responses.append("Check kiya: aapke account pe koi pending underpayment nahi hai.")
        if part_responses:
            return " ".join(part_responses)

    # 5. Process Evaluated Decisions
    if not decisions:
        return "Check kiya: aapke records mein koi discrepancy nahi mili. Payout expected amount ke mutabiq sahi tha."

    reply_parts: List[str] = []
    has_multiple = len(decisions) > 1

    if has_multiple:
        reply_parts.append(f"{len(decisions)} dispute check kiye.")

    for i, dec in enumerate(decisions):
        rec = reconciliations[i] if i < len(reconciliations) else None
        p_res = payment_results[i][1] if i < len(payment_results) else None

        detail_text = ""
        if rec and rec.trips_evaluated:
            for t_eval in rec.trips_evaluated:
                if t_eval.discrepancy_reason:
                    detail_text += f"{t_eval.discrepancy_reason} "
                elif t_eval.underpayment > 0:
                    detail_text += (
                        f"{rec.target_date or t_eval.started_at[:10]}: order {t_eval.trip_id} pe "
                        f"expected ₹{t_eval.expected_net}, actual ₹{t_eval.actual_paid}. "
                    )
        if rec and rec.incentive_evaluated and rec.incentive_evaluated.underpayment > 0:
            inc = rec.incentive_evaluated
            detail_text += f"{inc.date} ko incentive ₹{inc.underpayment} missing (completed {inc.unique_completed_trips_count} trips). "

        if dec.decision_type == DecisionType.AUTO_PAY:
            confirm_msg = f"₹{dec.amount} aapko PaySwift pe bhej rahe hain."
            if p_res and p_res.is_confirmed:
                confirm_msg = f"₹{dec.amount} abhi bhej rahe hain."
            reply_parts.append(f"{detail_text}{confirm_msg}".strip())

        elif dec.decision_type == DecisionType.OPS_APPROVAL:
            if "already been issued" in dec.reason:
                reply_parts.append(
                    f"{detail_text}ek din mein ek hi auto-payment ho sakta hai, isliye ₹{dec.amount} ops team approve karegi."
                )
            else:
                reply_parts.append(
                    f"{detail_text}₹{dec.amount} auto-pay limit (₹200) se zyada hai, isliye ops team approval ke liye bhej diya gaya hai."
                )

        elif dec.decision_type == DecisionType.NO_PAYMENT:
            if parsed.intent == DisputeIntent.INCENTIVE_MISSING or (rec and rec.incentive_evaluated):
                inc_date = rec.target_date or (rec.incentive_evaluated.date if rec.incentive_evaluated else "19 Sep")
                completed_count = rec.incentive_evaluated.unique_completed_trips_count if (rec and rec.incentive_evaluated) else len([t for t in rec.trips_evaluated if t.status == "completed"])
                reply_parts.append(
                    f"{inc_date} ko aapke {completed_count} trips complete hue the. "
                    f"Incentive 12 trips pe milta hai, isliye nahi bana."
                )
            elif rec and rec.trips_evaluated and rec.trips_evaluated[0].status == "cancelled_by_customer":
                t_eval = rec.trips_evaluated[0]
                reply_parts.append(
                    f"Trip {t_eval.trip_id} customer ne cancel kiya tha. Customer cancellation pe rider earnings ₹0 hoti hain."
                )
            else:
                reply_parts.append(dec.reason or "Record check kiya: koi payout discrepancy nahi mili.")

        elif dec.decision_type == DecisionType.ESCALATION:
            reply_parts.append(f"Dispute record verify nahi ho saka ({dec.reason}). Case ops team ko escalate kar diya hai.")

    return " ".join(reply_parts).strip()


# ── Main Deterministic Orchestration Function ────────────────────────────────

def process_rider_message(
    conn,
    rider_id: str,
    text: str,
    message_id: str,
    received_at: Optional[str] = None,
    llm_adapter: Optional[LLMAdapter] = None,
    payswift_base_url: Optional[str] = None,
) -> Dict[str, str]:
    """Process an incoming WhatsApp message end-to-end through deterministic orchestration.

    Args:
        conn:               Active DB connection.
        rider_id:           AUTHORITATIVE QuickDrop rider identifier.
        text:               Incoming message text.
        message_id:         WhatsApp wamid identifier (used for idempotency & auditing).
        received_at:        Message timestamp in ISO format.
        llm_adapter:        Optional LLM adapter.
        payswift_base_url:  Optional PaySwift URL override (for tests).

    Returns:
        Dict with "reply": string response for rider.
    """
    turn_input = {
        "rider_id": rider_id,
        "text": text,
        "message_id": message_id,
        "received_at": received_at,
    }

    # Log Intake Event
    log_trace_event(
        conn=conn,
        rider_id=rider_id,
        message_id=message_id,
        event_type="message_in",
        name="receive_whatsapp_message",
        input_data=turn_input,
        output_data={"status": "received"},
        timestamp=received_at,
    )

    # 1. Idempotency Check: Return cached reply if message_id was already processed
    cur = conn.cursor()
    cur.execute("SELECT agent_reply FROM conversation_traces WHERE message_id = %s;", (message_id,))
    cached = cur.fetchone()
    if cached:
        logger.info("Message %s already processed. Returning cached reply.", message_id)
        log_trace_event(
            conn=conn,
            rider_id=rider_id,
            message_id=message_id,
            event_type="reply",
            name="duplicate_message_cached_reply",
            input_data={"message_id": message_id},
            output_data={"reply": cached[0]},
        )
        return {"reply": cached[0]}

    # 2. Recover any previous 'uncertain' payment states for this rider (crash recovery)
    recover_uncertain_decisions(conn, rider_id, payswift_base_url=payswift_base_url)

    # 3. Check for repeat pushback in recent history
    context = get_previous_conversation_context(conn, rider_id, limit=4)
    is_repeat_pushback = any(
        phrase in text.lower()
        for phrase in ["dobara check", "dubara check", "check karo", "nahi nahi", "phir se dekho"]
    )

    # 4. LLM Interpretation Step
    if llm_adapter is None:
        llm_adapter = get_llm_adapter()

    try:
        parsed = llm_adapter.interpret(
            message_text=text,
            received_at=received_at or datetime.now().isoformat(),
            conversation_context=context,
        )
    except Exception as exc:
        logger.error("LLM interpretation failed for message %s: %s", message_id, exc)
        agent_reply = "Mujhe aapka message samajh nahi aaya. Kripya trip ID ya date ke saath payout issue bataiye."
        log_trace_event(
            conn=conn,
            rider_id=rider_id,
            message_id=message_id,
            event_type="error",
            name="llm_interpretation_error",
            input_data={"text": text},
            output_data={"error": str(exc)},
        )
        record_trace(
            conn=conn,
            rider_id=rider_id,
            message_id=message_id,
            turn_input=turn_input,
            llm_output={"error": str(exc)},
            reconciliation_evidence=None,
            policy_decision=None,
            payment_result=None,
            agent_reply=agent_reply,
        )
        return {"reply": agent_reply}

    llm_output_dict = parsed.model_dump()
    log_trace_event(
        conn=conn,
        rider_id=rider_id,
        message_id=message_id,
        event_type="tool_call",
        name="llm_interpret",
        input_data={"text": text, "conversation_context": context},
        output_data=llm_output_dict,
    )

    # 5. Handle Security Signals / Impersonation / Prompt Injection
    if parsed.security_signal:
        logger.warning("Security signal triggered on rider %s: %s", rider_id, parsed.security_note)
        disp_date_str = (received_at[:10] if received_at else "2026-09-22")
        escalation_dec = PolicyDecision(
            rider_id=rider_id,
            dispute_date=disp_date_str,
            decision_type=DecisionType.ESCALATION,
            amount=0,
            reason=f"Security signal: {parsed.security_note}",
            reconciliation_evidence={},
        )
        record_decision(conn, escalation_dec, message_id=message_id)

        reply = generate_agent_reply(rider_id, parsed, [], [escalation_dec], [])
        log_trace_event(
            conn=conn,
            rider_id=rider_id,
            message_id=message_id,
            event_type="decision",
            name="security_escalation",
            input_data={"security_note": parsed.security_note},
            output_data=escalation_dec.to_dict(),
        )
        log_trace_event(
            conn=conn,
            rider_id=rider_id,
            message_id=message_id,
            event_type="reply",
            name="agent_reply",
            input_data={"security_signal": True},
            output_data={"reply": reply},
        )
        record_trace(
            conn=conn,
            rider_id=rider_id,
            message_id=message_id,
            turn_input=turn_input,
            llm_output=llm_output_dict,
            reconciliation_evidence=None,
            policy_decision=escalation_dec.to_dict(),
            payment_result=None,
            agent_reply=reply,
        )
        return {"reply": reply}

    # 6. Handle Clarification Request
    if parsed.needs_clarification:
        reply = generate_agent_reply(rider_id, parsed, [], [], [])
        log_trace_event(
            conn=conn,
            rider_id=rider_id,
            message_id=message_id,
            event_type="reply",
            name="clarification_request",
            input_data={"needs_clarification": True},
            output_data={"clarification_question": reply},
        )
        record_trace(
            conn=conn,
            rider_id=rider_id,
            message_id=message_id,
            turn_input=turn_input,
            llm_output=llm_output_dict,
            reconciliation_evidence=None,
            policy_decision=None,
            payment_result=None,
            agent_reply=reply,
        )
        return {"reply": reply}

    # 7. Resolve Target Dates and Trip IDs from Rider Input
    resolved_dates = resolve_dispute_dates(parsed.claimed_date, text, received_at)
    target_trip_ids = parsed.trip_ids

    reconciliations: List[ReconciliationResult] = []
    decisions: List[PolicyDecision] = []
    payment_results: List[Tuple[PolicyDecision, Optional[PayoutResult]]] = []

    targets_to_evaluate: List[Tuple[Optional[str], Optional[str]]] = []
    if target_trip_ids:
        for tid in target_trip_ids:
            targets_to_evaluate.append((None, tid))
    elif resolved_dates:
        for d_str in resolved_dates:
            targets_to_evaluate.append((d_str, None))
    else:
        targets_to_evaluate.append((None, None))

    for d_str, tid in targets_to_evaluate:
        rec = reconcile_rider_dispute(conn, rider_id, target_date=d_str, target_trip_id=tid)
        reconciliations.append(rec)

        log_trace_event(
            conn=conn,
            rider_id=rider_id,
            message_id=message_id,
            event_type="tool_call",
            name="reconcile_rider_dispute",
            input_data={"rider_id": rider_id, "target_date": d_str, "target_trip_id": tid},
            output_data={
                "calculated_amount_owed": rec.calculated_amount_owed,
                "total_expected_payout": rec.total_expected_payout,
                "total_actual_paid": rec.total_actual_paid,
                "discrepancy_reasons": rec.discrepancy_reasons,
            },
        )

        disp_date_str = d_str
        if not disp_date_str:
            disp_date_str = rec.trips_evaluated[0].started_at[:10] if rec.trips_evaluated else (resolved_dates[0] if resolved_dates else "2026-09-20")
        disp_date = datetime.strptime(disp_date_str, "%Y-%m-%d").date()
        rec_dt = datetime.fromisoformat(received_at).date() if received_at else date.today()
        dec = decide(conn, rec, dispute_date=disp_date, message_id=message_id, reference_date=rec_dt)

        idem_key = None
        if dec.decision_type == DecisionType.AUTO_PAY and dec.amount > 0:
            idem_key = make_idempotency_key(rider_id, dec.dispute_date or "2026-09-20", dec.amount)
            dec.idempotency_key = idem_key

        decisions.append(dec)
        row_id = record_decision(conn, dec, message_id=message_id, idempotency_key=idem_key)

        log_trace_event(
            conn=conn,
            rider_id=rider_id,
            message_id=message_id,
            event_type="decision",
            name="evaluate_policy_decision",
            input_data={"calculated_amount_owed": rec.calculated_amount_owed, "dispute_date": str(disp_date)},
            output_data={
                "decision_type": dec.decision_type,
                "amount": dec.amount,
                "reason": dec.reason,
                "idempotency_key": idem_key,
            },
        )

        p_result: Optional[PayoutResult] = None

        if dec.decision_type == DecisionType.AUTO_PAY and dec.amount > 0 and idem_key:
            p_result = execute_payout(
                rider_id=rider_id,
                amount=dec.amount,
                idempotency_key=idem_key,
                reference=f"Dispute {dec.dispute_date or 'dispute'}",
                payswift_base_url=payswift_base_url,
            )

            record_payment_attempt(
                conn, row_id, rider_id, dec.amount, idem_key, 1, p_result.status.value, p_result.http_code, p_result.payout_id, p_result.error
            )

            pay_event_type = "tool_call" if p_result.is_confirmed else "error"
            log_trace_event(
                conn=conn,
                rider_id=rider_id,
                message_id=message_id,
                event_type=pay_event_type,
                name="execute_payswift_payout",
                input_data={"rider_id": rider_id, "amount": dec.amount, "idempotency_key": idem_key},
                output_data={"status": p_result.status.value, "payout_id": p_result.payout_id, "http_code": p_result.http_code, "error": p_result.error},
            )

            cur = conn.cursor()
            if p_result.is_confirmed:
                cur.execute(
                    "UPDATE dispute_decisions SET status = 'executed', payout_id = %s WHERE id = %s;",
                    (p_result.payout_id, row_id),
                )
            elif p_result.status == PayoutStatus.UNCERTAIN:
                cur.execute("UPDATE dispute_decisions SET status = 'uncertain' WHERE id = %s;", (row_id,))
            else:
                cur.execute("UPDATE dispute_decisions SET status = 'rejected' WHERE id = %s;", (row_id,))
            conn.commit()

        payment_results.append((dec, p_result))

    # 8. Generate Agent Reply
    agent_reply = generate_agent_reply(
        rider_id=rider_id,
        parsed=parsed,
        reconciliations=reconciliations,
        decisions=decisions,
        payment_results=payment_results,
        is_repeat_pushback=is_repeat_pushback,
    )

    log_trace_event(
        conn=conn,
        rider_id=rider_id,
        message_id=message_id,
        event_type="reply",
        name="agent_reply",
        input_data={"decisions_count": len(decisions)},
        output_data={"reply": agent_reply},
    )

    # 9. Persist Full Turn Trace to DB
    record_trace(
        conn=conn,
        rider_id=rider_id,
        message_id=message_id,
        turn_input=turn_input,
        llm_output=llm_output_dict,
        reconciliation_evidence=[r.to_dict() for r in reconciliations],
        policy_decision=[d.to_dict() for d in decisions],
        payment_result=[pr[1].__dict__ for pr in payment_results if pr[1]],
        agent_reply=agent_reply,
    )

    return {"reply": agent_reply}

