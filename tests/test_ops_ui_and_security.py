"""Tests for Ops UI frontend endpoints, pending queue display, and backend policy security enforcement."""

import pytest
import psycopg2
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.ingest import init_db
from app.domain.decision import record_decision, PolicyDecision, DecisionType
from app.payswift_client import PayoutResult, PayoutStatus

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_test_db():
    """Reset database and insert base test data before each test."""
    conn = psycopg2.connect(settings.database_url)
    init_db(conn)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE dispute_decisions, conversation_traces, payment_attempts, trace_events RESTART IDENTITY CASCADE;")
        cur.execute("DELETE FROM raw_payout_lines WHERE line_id LIKE 'L_UI_%';")
        cur.execute("DELETE FROM raw_trips WHERE trip_id LIKE 'T_UI_%';")
        cur.execute("INSERT INTO raw_riders (rider_id, name, city, joined_on) VALUES ('R999', 'Test Rider', 'Bengaluru', '2025-01-01') ON CONFLICT DO NOTHING;")
        cur.execute("""
            INSERT INTO raw_trips (trip_id, rider_id, started_at, status, distance_km, surge_multiplier)
            VALUES ('T_UI_9991', 'R999', '2026-09-20 10:00:00+00', 'completed', 50.0, 1.0)
            ON CONFLICT DO NOTHING;
            INSERT INTO raw_payout_lines (line_id, payout_date, rider_id, line_type, trip_id, amount)
            VALUES ('L_UI_9991', '2026-09-20', 'R999', 'net_earnings', 'T_UI_9991', 0.0)
            ON CONFLICT DO NOTHING;
        """)
        conn.commit()
    yield
    with conn.cursor() as cur:
        cur.execute("DELETE FROM raw_payout_lines WHERE line_id LIKE 'L_UI_%';")
        cur.execute("DELETE FROM raw_trips WHERE trip_id LIKE 'T_UI_%';")
        conn.commit()
    conn.close()



def test_ops_ui_static_endpoints():
    """Verify that the Ops Portal HTML, CSS, and JS static assets are served correctly."""
    # GET /
    res_root = client.get("/")
    assert res_root.status_code == 200
    assert "Dispute Desk Ops Portal" in res_root.text
    assert "Backend Policy Gate Active" in res_root.text

    # GET /ops
    res_ops = client.get("/ops")
    assert res_ops.status_code == 200
    assert "Dispute Desk Ops Portal" in res_ops.text

    # GET /static/styles.css
    res_css = client.get("/static/styles.css")
    assert res_css.status_code == 200
    assert "--bg-dark" in res_css.text

    # GET /static/app.js
    res_js = client.get("/static/app.js")
    assert res_js.status_code == 200
    assert "loadPendingOps" in res_js.text


def test_ops_ui_displays_pending_items_and_trace():
    """Verify end-to-end that pending ops items appear in GET /ops/pending and trace works."""
    conn = psycopg2.connect(settings.database_url)
    dec = PolicyDecision(
        rider_id="R999",
        dispute_date="2026-09-20",
        decision_type=DecisionType.OPS_APPROVAL,
        amount=300,
        reason="Owed ₹300 exceeds auto-pay limit",
        reconciliation_evidence={},
    )
    dec_id = record_decision(conn, dec, message_id="wamid.ops.ui.001")
    conn.close()

    # Fetch /ops/pending
    pending_res = client.get("/ops/pending")
    assert pending_res.status_code == 200
    items = pending_res.json()
    assert len(items) >= 1

    item = next(i for i in items if i["id"] == dec_id)
    assert item["rider_id"] == "R999"
    assert item["type"] == "approval"
    assert item["amount"] == 300

    # Fetch audit trace for R999
    trace_res = client.get(f"/trace/{item['rider_id']}")
    assert trace_res.status_code == 200
    events = trace_res.json()
    assert isinstance(events, list)


def test_browser_manipulation_cannot_bypass_policy():
    """Verify that a rogue browser or malicious client manipulation cannot bypass policy."""
    # 1. Attacker attempts to approve a non-existent decision ID
    res1 = client.post("/ops/decisions/999999/approve")
    assert res1.status_code == 404
    assert "not found" in res1.json()["detail"].lower()

    # 2. Attacker attempts to send arbitrary payload (e.g. custom amount) to approval route
    res2 = client.post(
        "/ops/decisions/1/approve",
        json={"amount": 999999, "payout_to": "attacker_account"}
    )
    assert res2.status_code in (404, 400)

    # 3. Create a pending item for R999
    conn = psycopg2.connect(settings.database_url)
    dec = PolicyDecision(
        rider_id="R999",
        dispute_date="2026-09-20",
        decision_type=DecisionType.OPS_APPROVAL,
        amount=300,
        reason="Owed ₹300 exceeds limit",
        reconciliation_evidence={},
    )
    dec_id = record_decision(conn, dec, message_id="wamid.security.001")
    conn.close()

    # Reject the decision via backend API
    rej_res = client.post(f"/ops/decisions/{dec_id}/reject", json={"reason": "Rejected by ops manager"})
    assert rej_res.status_code == 200

    # Attacker tries to approve a rejected decision via browser API call
    res_stale = client.post(f"/ops/decisions/{dec_id}/approve")
    assert res_stale.status_code == 400
    assert "not pending" in res_stale.json()["detail"].lower()


@patch("app.domain.orchestrator.execute_payout")
def test_ops_approval_action_executes_payout_and_clears_queue(mock_execute_payout):
    """Verify approving an item via UI action endpoint triggers payout and clears queue item."""
    mock_execute_payout.return_value = PayoutResult(
        status=PayoutStatus.CONFIRMED,
        payout_id="PO_OPS_TEST_123",
        raw_status="SUCCESS",
        http_code=201,
        idempotency_key="idem_test_123",
    )

    conn = psycopg2.connect(settings.database_url)
    dec = PolicyDecision(
        rider_id="R999",
        dispute_date="2026-09-20",
        decision_type=DecisionType.OPS_APPROVAL,
        amount=300,
        reason="Owed ₹300 exceeds limit",
        reconciliation_evidence={},
    )
    dec_id = record_decision(conn, dec, message_id="wamid.ops.action.001")
    conn.close()

    # Call approval endpoint (same call triggered by UI "Approve Payout" button)
    res = client.post(f"/ops/decisions/{dec_id}/approve")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "approved"
    assert data["payout_id"] == "PO_OPS_TEST_123"

    # Verify queue is now cleared
    pending_res = client.get("/ops/pending")
    items = pending_res.json()
    assert not any(i["id"] == dec_id for i in items)

