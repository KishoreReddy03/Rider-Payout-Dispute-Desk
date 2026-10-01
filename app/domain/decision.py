"""Deterministic policy decision engine.

Classifies a reconciliation result into one of four decision outcomes:
  - no_payment    : Rider is owed nothing (verified ₹0 deficit).
  - auto_pay      : Verified owed ≤ ₹200 AND no auto-pay issued for this rider today.
  - ops_approval  : Verified owed > ₹200 OR daily auto-pay already issued.
  - escalation    : Case cannot be resolved from source data (unknown trip, fraud signal, etc.).

INVARIANTS (enforced in deterministic code, never overridden by LLM or rider claim):
  1. Payment amount = min(calculated_amount_owed, AUTO_PAY_LIMIT) for auto_pay.
  2. Payment amount = calculated_amount_owed for ops_approval.
  3. Payment amount never exceeds calculated_amount_owed.
  4. Auto-pay ≤ ₹200 per dispute.
  5. At most one auto-pay per rider per IST calendar day (checked via DB).
  6. No payment is authorized from this module — only a structured decision is returned.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from typing import Optional, Dict, Any

from app.domain.reconciler import ReconciliationResult


AUTO_PAY_LIMIT = 200  # ₹ — hard ceiling on automatic payments


class DecisionType:
    NO_PAYMENT = "no_payment"
    AUTO_PAY = "auto_pay"
    OPS_APPROVAL = "ops_approval"
    ESCALATION = "escalation"


@dataclass
class PolicyDecision:
    """Structured, non-authoritative payment decision.

    The caller is responsible for executing payment; this object does not trigger
    any external API call.
    """

    rider_id: str
    dispute_date: Optional[str]   # IST calendar date the dispute concerns
    decision_type: str            # DecisionType constant
    amount: int                   # Rupees — 0 for no_payment / escalation
    reason: str                   # Human-readable audit reason
    reconciliation_evidence: Dict[str, Any]  # Full reconciliation result snapshot
    idempotency_key: Optional[str] = None    # Stable key derived from dispute identity
    payout_id: Optional[str] = None          # PaySwift returned payout ID if confirmed

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["reconciliation_evidence"] = self.reconciliation_evidence
        return d


def record_decision(
    conn,
    decision: PolicyDecision,
    message_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> int:
    """Persist a policy decision to dispute_decisions table.

    Returns the new row ID (or existing row ID if idempotency_key already exists).
    """
    key_to_use = idempotency_key or decision.idempotency_key
    cur = conn.cursor()

    if key_to_use:
        cur.execute("SELECT id FROM dispute_decisions WHERE idempotency_key = %s;", (key_to_use,))
        existing = cur.fetchone()
        if existing:
            return existing[0]

    cur.execute(
        """
        INSERT INTO dispute_decisions
            (rider_id, dispute_date, decision_type, amount, reason, status, message_id, idempotency_key, payout_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id;
        """,
        (
            decision.rider_id,
            decision.dispute_date,
            decision.decision_type,
            decision.amount,
            decision.reason,
            "pending",
            message_id,
            key_to_use,
            decision.payout_id,
        ),
    )
    row_id = cur.fetchone()[0]
    conn.commit()
    return row_id


def _already_auto_paid_today(conn, rider_id: str, dispute_date: date, reference_date: Optional[date] = None) -> bool:
    """Return True if an auto_pay decision has already been issued for a DIFFERENT dispute date for this rider today."""
    today_dt = reference_date or date.today()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*) FROM dispute_decisions
        WHERE rider_id = %s
          AND decision_type = 'auto_pay'
          AND status IN ('pending', 'executed')
          AND dispute_date != %s
          AND (created_at AT TIME ZONE 'Asia/Kolkata')::date = %s;
        """,
        (rider_id, str(dispute_date), today_dt),
    )
    return cur.fetchone()[0] > 0


def decide(
    conn,
    reconciliation: ReconciliationResult,
    dispute_date: date,
    message_id: Optional[str] = None,
    escalate_reason: Optional[str] = None,
    reference_date: Optional[date] = None,
) -> PolicyDecision:
    """Apply financial guardrails to a reconciliation result and return a policy decision.

    Args:
        conn:             Active DB connection (used only for read queries; no writes).
        reconciliation:   Output from reconcile_rider_dispute().
        dispute_date:     The IST calendar date the dispute concerns.
        message_id:       Incoming message's wamid (for trace correlation).
        escalate_reason:  If provided, forces an escalation regardless of amount.
        reference_date:   The date of incoming message intake (defaults to today).

    Returns:
        PolicyDecision with decision_type, amount, and reason. Does NOT call PaySwift.
    """
    rider_id = reconciliation.rider_id
    owed = reconciliation.calculated_amount_owed
    evidence = reconciliation.to_dict()

    # ── INVARIANT: amount must never exceed what the reconciler calculated ──────
    assert owed >= 0, f"calculated_amount_owed must be non-negative, got {owed}"

    # 1. Forced escalation (fraud signal, unknown trip, unresolvable dispute)
    if escalate_reason:
        return PolicyDecision(
            rider_id=rider_id,
            dispute_date=str(dispute_date),
            decision_type=DecisionType.ESCALATION,
            amount=0,
            reason=escalate_reason,
            reconciliation_evidence=evidence,
        )

    # 1.5. Old Dispute Staleness Rule: Disputes older than 7 days must be escalated
    ref_dt = reference_date or date.today()
    days_old = (ref_dt - dispute_date).days
    if days_old > 7:
        return PolicyDecision(
            rider_id=rider_id,
            dispute_date=str(dispute_date),
            decision_type=DecisionType.ESCALATION,
            amount=0,
            reason=f"Dispute date {dispute_date} is {days_old} days old (exceeds 7-day auto-pay limit). Escalated for manual review.",
            reconciliation_evidence=evidence,
        )

    # 2. No payment — rider is owed nothing according to source records

    if owed == 0:
        reasons = reconciliation.discrepancy_reasons
        reason_text = reasons[0] if reasons else "No discrepancy found. Payout records match expected policy amount."
        return PolicyDecision(
            rider_id=rider_id,
            dispute_date=str(dispute_date),
            decision_type=DecisionType.NO_PAYMENT,
            amount=0,
            reason=reason_text,
            reconciliation_evidence=evidence,
        )

    # 3. Amount exceeds auto-pay ceiling → always requires ops approval
    if owed > AUTO_PAY_LIMIT:
        return PolicyDecision(
            rider_id=rider_id,
            dispute_date=str(dispute_date),
            decision_type=DecisionType.OPS_APPROVAL,
            amount=owed,          # full verified owed amount — ops sees the exact figure
            reason=(
                f"Verified underpayment ₹{owed} exceeds auto-pay ceiling of ₹{AUTO_PAY_LIMIT}. "
                f"Requires ops approval."
            ),
            reconciliation_evidence=evidence,
        )

    # 4. Amount is within auto-pay ceiling (1 ≤ owed ≤ 200)
    #    Check once-per-rider-per-day auto-pay constraint
    if _already_auto_paid_today(conn, rider_id, dispute_date, reference_date=ref_dt):
        return PolicyDecision(
            rider_id=rider_id,
            dispute_date=str(dispute_date),
            decision_type=DecisionType.OPS_APPROVAL,
            amount=owed,
            reason=(
                f"Verified underpayment ₹{owed} is within ₹{AUTO_PAY_LIMIT} ceiling, but an "
                f"auto-pay has already been issued for rider {rider_id} today ({dispute_date}). "
                f"Requires ops approval."
            ),
            reconciliation_evidence=evidence,
        )

    # ── INVARIANT: auto-pay amount is strictly capped at min(owed, AUTO_PAY_LIMIT) ──
    auto_pay_amount = min(owed, AUTO_PAY_LIMIT)
    assert auto_pay_amount <= AUTO_PAY_LIMIT
    assert auto_pay_amount <= owed

    return PolicyDecision(
        rider_id=rider_id,
        dispute_date=str(dispute_date),
        decision_type=DecisionType.AUTO_PAY,
        amount=auto_pay_amount,
        reason=(
            f"Verified underpayment ₹{owed}. Auto-pay authorized for ₹{auto_pay_amount}."
        ),
        reconciliation_evidence=evidence,
    )


