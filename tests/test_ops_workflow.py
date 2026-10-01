"""Integration tests for Operations Approval, Rejection, and Escalation Workflow.

Tests required scenarios:
1. Approval: Backend revalidates payable dispute, executes payout, updates status, records trace.
2. Rejection: Rejects pending item, status becomes 'rejected', zero money moved.
3. Duplicate approval: Reject duplicate approval attempt on non-pending decision.
4. Stale approval: Revalidation fails if dispute calculated deficit is ₹0 or less than decision amount.
5. Already-paid cases: Revalidation fails if dispute has already been paid.
6. Escalation: Escalates pending item, status becomes 'escalated', zero money moved automatically.
7. Trace verification: Every transition writes persistent trace events to `trace_events`.
"""

import pytest
import psycopg2
from app.config import settings
from app.ingest import init_db, ingest_riders, ingest_trips, ingest_payout_lines
from app.domain.decision import record_decision, PolicyDecision, DecisionType
from app.domain.orchestrator import (
    approve_ops_decision,
    reject_ops_decision,
    escalate_ops_decision,
    get_pending_ops,
    OpsWorkflowError,
)
from app.domain.trace_logger import get_trace_events_for_rider
from app.payswift_client import PayoutStatus
from unittest.mock import patch, MagicMock


@pytest.fixture
def db_conn():
    conn = psycopg2.connect(settings.database_url)
    init_db(conn)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE dispute_decisions, conversation_traces, payment_attempts, trace_events RESTART IDENTITY CASCADE;")
        cur.execute("DELETE FROM raw_payout_lines WHERE rider_id = 'R999';")
        cur.execute("DELETE FROM raw_trips WHERE rider_id = 'R999';")
        cur.execute("INSERT INTO raw_riders (rider_id, name, city, joined_on) VALUES ('R999', 'Test Rider', 'Bengaluru', '2025-01-01') ON CONFLICT DO NOTHING;")
    conn.commit()
    yield conn
    with conn.cursor() as cur:
        cur.execute("DELETE FROM raw_payout_lines WHERE rider_id = 'R999';")
        cur.execute("DELETE FROM raw_trips WHERE rider_id = 'R999';")
        conn.commit()
    conn.close()



def test_ops_approval_success(db_conn, monkeypatch):
    """Test approving a valid pending ops decision with mock PaySwift confirmation."""
    # Setup trip & payout line showing ₹300 deficit for 2026-09-20
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO raw_trips (trip_id, rider_id, started_at, status, distance_km, surge_multiplier)
            VALUES ('T9991', 'R999', '2026-09-20 10:00:00+00', 'completed', 50.0, 1.0);
            INSERT INTO raw_payout_lines (line_id, payout_date, rider_id, line_type, trip_id, amount)
            VALUES ('L9991', '2026-09-20', 'R999', 'net_earnings', 'T9991', 0.0);
        """)
        db_conn.commit()

    # Create pending ops_approval decision for ₹300
    dec = PolicyDecision(
        rider_id="R999",
        dispute_date="2026-09-20",
        decision_type=DecisionType.OPS_APPROVAL,
        amount=300,
        reason="Owed ₹300 exceeds limit",
        reconciliation_evidence={},
    )
    dec_id = record_decision(db_conn, dec, message_id="wamid.ops.001")

    # Verify listed in GET /ops/pending
    pending = get_pending_ops(db_conn)
    assert any(p["id"] == dec_id and p["type"] == "approval" and p["amount"] == 300 for p in pending)

    # Mock PaySwift execute_payout
    mock_payout_res = MagicMock()
    mock_payout_res.is_confirmed = True
    mock_payout_res.status = PayoutStatus.CONFIRMED
    mock_payout_res.http_code = 200
    mock_payout_res.payout_id = "PAY_OPS_999"
    mock_payout_res.error = None

    with patch("app.domain.orchestrator.execute_payout", return_value=mock_payout_res):
        res = approve_ops_decision(db_conn, dec_id)

    assert res["status"] == "approved"
    assert res["payout_id"] == "PAY_OPS_999"
    assert res["amount"] == 300

    # Verify decision status updated to 'executed' in DB
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, payout_id FROM dispute_decisions WHERE id = %s;", (dec_id,))
        row = cur.fetchone()
        assert row[0] == "executed"
        assert row[1] == "PAY_OPS_999"

    # Verify trace logged transition
    events = get_trace_events_for_rider(db_conn, "R999")
    event_names = [e["name"] for e in events]
    assert "execute_payswift_payout" in event_names
    assert "ops_approve_executed" in event_names


def test_ops_rejection_no_money_moved(db_conn):
    """Test rejecting a pending decision moves ZERO money and does not call PaySwift."""
    dec = PolicyDecision(
        rider_id="R999",
        dispute_date="2026-09-20",
        decision_type=DecisionType.OPS_APPROVAL,
        amount=500,
        reason="Test rejection",
        reconciliation_evidence={},
    )
    dec_id = record_decision(db_conn, dec, message_id="wamid.ops.002")

    with patch("app.domain.orchestrator.execute_payout") as mock_pay:
        res = reject_ops_decision(db_conn, dec_id, reason="Rider invalid claim")
        mock_pay.assert_not_called()

    assert res["status"] == "rejected"
    assert res["amount"] == 500

    # Check DB status
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM dispute_decisions WHERE id = %s;", (dec_id,))
        assert cur.fetchone()[0] == "rejected"

    # Check trace
    events = get_trace_events_for_rider(db_conn, "R999")
    assert any(e["name"] == "ops_reject" for e in events)


def test_ops_escalation_no_money_moved(db_conn):
    """Test escalating a pending decision moves ZERO money automatically."""
    dec = PolicyDecision(
        rider_id="R999",
        dispute_date="2026-09-20",
        decision_type=DecisionType.ESCALATION,
        amount=0,
        reason="Complex dispute",
        reconciliation_evidence={},
    )
    dec_id = record_decision(db_conn, dec, message_id="wamid.ops.003")

    with patch("app.domain.orchestrator.execute_payout") as mock_pay:
        res = escalate_ops_decision(db_conn, dec_id, reason="Needs manual ledger investigation")
        mock_pay.assert_not_called()

    assert res["status"] == "escalated"

    # Check DB status
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM dispute_decisions WHERE id = %s;", (dec_id,))
        assert cur.fetchone()[0] == "escalated"

    # Check trace
    events = get_trace_events_for_rider(db_conn, "R999")
    assert any(e["name"] == "ops_escalate" for e in events)


def test_duplicate_approval_attempt_fails(db_conn):
    """Test attempting to approve a non-pending decision fails closed."""
    dec = PolicyDecision(
        rider_id="R999",
        dispute_date="2026-09-20",
        decision_type=DecisionType.OPS_APPROVAL,
        amount=300,
        reason="Already approved test",
        reconciliation_evidence={},
    )
    dec_id = record_decision(db_conn, dec, message_id="wamid.ops.004")

    # Mark status as already executed
    with db_conn.cursor() as cur:
        cur.execute("UPDATE dispute_decisions SET status = 'executed' WHERE id = %s;", (dec_id,))
        db_conn.commit()

    with pytest.raises(OpsWorkflowError) as exc_info:
        approve_ops_decision(db_conn, dec_id)

    assert exc_info.value.status_code == 400
    assert "is not pending" in exc_info.value.message


def test_stale_approval_revalidation_fails(db_conn):
    """Test backend revalidation fails if calculated deficit is ₹0 (stale dispute)."""
    # Trip & payout line with 0 deficit (expected ₹400, paid ₹400)
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO raw_trips (trip_id, rider_id, started_at, status, distance_km, surge_multiplier)
            VALUES ('T9992', 'R999', '2026-09-20 12:00:00+00', 'completed', 10.0, 1.0);
            INSERT INTO raw_payout_lines (line_id, payout_date, rider_id, line_type, trip_id, amount)
            VALUES ('L9992', '2026-09-20', 'R999', 'net_earnings', 'T9992', 400.0);
        """)
        db_conn.commit()

    dec = PolicyDecision(
        rider_id="R999",
        dispute_date="2026-09-20",
        decision_type=DecisionType.OPS_APPROVAL,
        amount=300,
        reason="Stale approval test",
        reconciliation_evidence={},
    )
    dec_id = record_decision(db_conn, dec, message_id="wamid.ops.005")

    with patch("app.domain.orchestrator.execute_payout") as mock_pay:
        with pytest.raises(OpsWorkflowError) as exc_info:
            approve_ops_decision(db_conn, dec_id)
        mock_pay.assert_not_called()

    assert exc_info.value.status_code == 400
    assert "no longer payable" in exc_info.value.message

    # Verify trace error event logged
    events = get_trace_events_for_rider(db_conn, "R999")
    assert any(e["name"] == "ops_approve_revalidation_failed" for e in events)


def test_already_paid_dispute_approval_fails(db_conn):
    """Test backend revalidation fails if a payment has already been executed for this dispute."""
    # Record an executed payment decision for R999 on 2026-09-20
    prior_dec = PolicyDecision(
        rider_id="R999",
        dispute_date="2026-09-20",
        decision_type=DecisionType.AUTO_PAY,
        amount=150,
        reason="Prior auto pay",
        reconciliation_evidence={},
        payout_id="PAY_PREV_123",
    )
    prior_id = record_decision(db_conn, prior_dec, message_id="wamid.ops.prior")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE dispute_decisions SET status = 'executed', payout_id = 'PAY_PREV_123' WHERE id = %s;", (prior_id,))
        db_conn.commit()

    # Now attempt ops approval for same rider and date
    pending_dec = PolicyDecision(
        rider_id="R999",
        dispute_date="2026-09-20",
        decision_type=DecisionType.OPS_APPROVAL,
        amount=250,
        reason="Second approval attempt",
        reconciliation_evidence={},
    )
    pending_id = record_decision(db_conn, pending_dec, message_id="wamid.ops.second")

    with patch("app.domain.orchestrator.execute_payout") as mock_pay:
        with pytest.raises(OpsWorkflowError) as exc_info:
            approve_ops_decision(db_conn, pending_id)
        mock_pay.assert_not_called()

    assert exc_info.value.status_code == 409
    assert "already been paid" in exc_info.value.message

    # Verify trace error event logged
    events = get_trace_events_for_rider(db_conn, "R999")
    assert any(e["name"] == "ops_approve_revalidation_failed" for e in events)
