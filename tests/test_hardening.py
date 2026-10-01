"""Hardening & Resiliency Test Suite.

Tests system robustness against:
  1. Vendor duplicate message_id webhooks (Postgres UNIQUE constraint)
  2. Concurrent / repeated HTTP POST requests
  3. Payment retry with stable idempotency key after UNCERTAIN (timeout) status
  4. Process restart durability (closing DB connections & re-initializing state)
  5. Application crash recovery for pending/uncertain payments
  6. DB constraint verification & transaction rollback boundaries
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock
import pytest
import psycopg2
import httpx
from fastapi.testclient import TestClient

from app.config import settings
from app.ingest import run_ingestion, init_db
from app.domain.llm_schema import ParsedMessage, DisputeIntent
from app.domain.llm_adapter import LLMAdapter
from app.domain.orchestrator import (
    process_rider_message,
    get_trace_for_rider,
    recover_uncertain_decisions,
)
from app.payswift_client import PayoutStatus, execute_payout
from app.main import app


# ── Scripted Test LLM Adapter ────────────────────────────────────────────────

class ScriptedLLMAdapter(LLMAdapter):

    def __init__(self, responses: dict[str, ParsedMessage] | None = None):
        self.responses = responses or {}
        self.default_response = ParsedMessage(
            intent=DisputeIntent.SURGE_MISSING,
            trip_ids=["T926334"],
            claimed_date="20",
            needs_clarification=False,
            clarification_question=None,
            security_signal=False,
            security_note=None,
        )

    def _call_model(self, user_text: str, conversation_context: str) -> str:
        text_key = user_text.strip()
        parsed = self.responses.get(text_key, self.default_response)
        return parsed.model_dump_json()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def db_conn():
    """Module-level fixture ensuring database schema and clean tables."""
    run_ingestion()
    conn = psycopg2.connect(settings.database_url)
    init_db(conn)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE dispute_decisions, conversation_traces, payment_attempts RESTART IDENTITY;")
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def clean_tables(db_conn):
    """Clean tables before each test."""
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE dispute_decisions, conversation_traces, payment_attempts RESTART IDENTITY;")
    db_conn.commit()
    yield


MOCK_PAYSWIFT_URL = "http://localhost:8080"


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_duplicate_vendor_message_id_returns_existing_result(db_conn):
    """Duplicate webhook delivery with same message_id returns cached result without re-processing."""
    mock_resp = httpx.Response(201, json={"payout_id": "pay_hard123", "status": "processed"})

    scripted_adapter = ScriptedLLMAdapter({
        "Bhai surge missing": ParsedMessage(
            intent=DisputeIntent.SURGE_MISSING,
            trip_ids=["T926334"],
            claimed_date="20",
            needs_clarification=False,
            clarification_question=None,
            security_signal=False,
            security_note=None,
        )
    })

    with patch("httpx.Client.post", return_value=mock_resp) as mock_post:
        msg_id = "wamid.hard_dup001"

        # First request
        res1 = process_rider_message(
            conn=db_conn,
            rider_id="R003",
            text="Bhai surge missing",
            message_id=msg_id,
            received_at="2026-09-22T09:05:00+05:30",
            llm_adapter=scripted_adapter,
            payswift_base_url=MOCK_PAYSWIFT_URL,
        )

        # Second request with identical message_id
        res2 = process_rider_message(
            conn=db_conn,
            rider_id="R003",
            text="Bhai surge missing",
            message_id=msg_id,
            received_at="2026-09-22T09:05:00+05:30",
            llm_adapter=scripted_adapter,
            payswift_base_url=MOCK_PAYSWIFT_URL,
        )

        assert res1["reply"] == res2["reply"]
        # Verify PaySwift POST was called EXACTLY ONCE
        assert mock_post.call_count == 1

        # Verify DB has only 1 decision and 1 trace
        traces = get_trace_for_rider(db_conn, "R003")
        assert len(traces) == 1

        cur = db_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM dispute_decisions WHERE rider_id = 'R003';")
        assert cur.fetchone()[0] == 1


def test_db_unique_constraint_on_message_id(db_conn):
    """Database enforces UNIQUE(message_id) constraint on conversation_traces."""
    cur = db_conn.cursor()
    cur.execute(
        """
        INSERT INTO conversation_traces (rider_id, message_id, agent_reply)
        VALUES ('R003', 'wamid.unique_test', 'Reply 1');
        """
    )
    db_conn.commit()

    # Second insert with same message_id must raise UniqueViolation
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            """
            INSERT INTO conversation_traces (rider_id, message_id, agent_reply)
            VALUES ('R003', 'wamid.unique_test', 'Reply 2');
            """
        )
    db_conn.rollback()


def test_repeated_http_post_requests(db_conn):
    """Repeated HTTP POST /messages with same payload returns cached reply."""
    from app.payswift_client import PayoutResult, PayoutStatus

    client = TestClient(app)

    payload = {
        "message_id": "wamid.http_repeat001",
        "rider_id": "R003",
        "text": "Bhai order T926334 ka surge nahi mila, 20 tarikh wala",
        "received_at": "2026-09-22T09:05:00+05:30",
    }

    scripted_adapter = ScriptedLLMAdapter({
        "Bhai order T926334 ka surge nahi mila, 20 tarikh wala": ParsedMessage(
            intent=DisputeIntent.SURGE_MISSING,
            trip_ids=["T926334"],
            claimed_date="20",
            needs_clarification=False,
            clarification_question=None,
            security_signal=False,
            security_note=None,
        )
    })

    mock_payout_res = PayoutResult(
        status=PayoutStatus.CONFIRMED,
        payout_id="pay_http123",
        raw_status="processed",
        http_code=201,
        idempotency_key="idem-test",
    )

    with patch("app.domain.orchestrator.get_llm_adapter", return_value=scripted_adapter), \
         patch("app.domain.orchestrator.execute_payout", return_value=mock_payout_res) as mock_payout:
        resp1 = client.post("/messages", json=payload)
        assert resp1.status_code == 200

        resp2 = client.post("/messages", json=payload)
        assert resp2.status_code == 200

        assert resp1.json()["reply"] == resp2.json()["reply"]
        # Verify execute_payout was called EXACTLY ONCE across repeated HTTP requests
        assert mock_payout.call_count == 1




def test_uncertain_payswift_timeout_and_recovery(db_conn):
    """When PaySwift times out (UNCERTAIN), system preserves idempotency key and recovers on retry."""
    # Step 1: PaySwift times out on initial POST
    scripted_adapter = ScriptedLLMAdapter({
        "surge missing": ParsedMessage(
            intent=DisputeIntent.SURGE_MISSING,
            trip_ids=["T926334"],
            claimed_date="20",
            needs_clarification=False,
            clarification_question=None,
            security_signal=False,
            security_note=None,
        )
    })

    with patch("httpx.Client.post", side_effect=httpx.TimeoutException("Connection timed out")):
        res1 = process_rider_message(
            conn=db_conn,
            rider_id="R003",
            text="surge missing",
            message_id="wamid.timeout001",
            received_at="2026-09-22T09:05:00+05:30",
            llm_adapter=scripted_adapter,
            payswift_base_url=MOCK_PAYSWIFT_URL,
        )

    # Verify decision status in DB is 'uncertain' and has idempotency_key stored
    cur = db_conn.cursor()
    cur.execute(
        "SELECT status, idempotency_key FROM dispute_decisions WHERE rider_id = 'R003' AND decision_type = 'auto_pay';"
    )
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "uncertain"
    stored_key = row[1]
    assert stored_key.startswith("idem-")

    # Step 2: Retry with successful PaySwift response (HTTP 201 or 409)
    mock_resp_success = httpx.Response(201, json={"payout_id": "pay_recovered123", "status": "processed"})

    with patch("httpx.Client.post", return_value=mock_resp_success) as mock_post_retry:
        recover_uncertain_decisions(db_conn, "R003", payswift_base_url=MOCK_PAYSWIFT_URL)

        # Verify PaySwift retry used the EXACT SAME idempotency_key
        assert mock_post_retry.call_count == 1
        headers_used = mock_post_retry.call_args[1]["headers"]
        assert headers_used["Idempotency-Key"] == stored_key

    # Verify DB status updated to 'executed'
    cur.execute(
        "SELECT status, payout_id FROM dispute_decisions WHERE rider_id = 'R003' AND decision_type = 'auto_pay';"
    )
    row_after = cur.fetchone()
    assert row_after[0] == "executed"
    assert row_after[1] == "pay_recovered123"

    # Verify payment_attempts table logged both attempt 1 (timeout) and attempt 2 (recovery)
    cur.execute("SELECT attempt_number, status, idempotency_key FROM payment_attempts WHERE rider_id = 'R003' ORDER BY id ASC;")
    attempts = cur.fetchall()
    assert len(attempts) == 2
    assert attempts[0][0] == 1
    assert attempts[0][1] == "uncertain"
    assert attempts[1][0] == 2
    assert attempts[1][1] == "confirmed"
    assert attempts[0][2] == attempts[1][2] == stored_key


def test_process_restart_durability(db_conn):
    """System state and idempotency keys survive process restarts (closing DB connection)."""
    mock_resp = httpx.Response(201, json={"payout_id": "pay_restart123", "status": "processed"})

    scripted_adapter = ScriptedLLMAdapter({
        "surge missing": ParsedMessage(
            intent=DisputeIntent.SURGE_MISSING,
            trip_ids=["T926334"],
            claimed_date="20",
            needs_clarification=False,
            clarification_question=None,
            security_signal=False,
            security_note=None,
        )
    })

    with patch("httpx.Client.post", return_value=mock_resp):
        process_rider_message(
            conn=db_conn,
            rider_id="R003",
            text="surge missing",
            message_id="wamid.restart001",
            received_at="2026-09-22T09:05:00+05:30",
            llm_adapter=scripted_adapter,
            payswift_base_url=MOCK_PAYSWIFT_URL,
        )

    # SIMULATE PROCESS RESTART: Close current database connection completely
    db_conn.close()

    # Open fresh database connection (simulating new application process start)
    new_conn = psycopg2.connect(settings.database_url)

    # Verify that duplicate message_id returns cached result from Postgres without new payout
    with patch("httpx.Client.post", return_value=mock_resp) as mock_post2:
        res = process_rider_message(
            conn=new_conn,
            rider_id="R003",
            text="surge missing",
            message_id="wamid.restart001",
            received_at="2026-09-22T09:05:00+05:30",
            llm_adapter=scripted_adapter,
            payswift_base_url=MOCK_PAYSWIFT_URL,
        )

        assert "25" in res["reply"]
        assert mock_post2.call_count == 0  # Zero PaySwift calls after process restart!

    new_conn.close()
