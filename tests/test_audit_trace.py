"""Tests for Audit & Trace System.

Verifies:
  1. Persistent trace events contain required fields: at, type, name, input, output
  2. Event types are correctly assigned: message_in, tool_call, decision, reply, error
  3. Secret sanitization redacts sensitive keys (api_key, token, secret)
  4. Errors are properly captured and represented as error events
  5. GET /trace/{rider_id} returns an understandable, ordered sequence
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
from app.domain.trace_logger import sanitize_data, get_trace_events_for_rider
from app.domain.orchestrator import process_rider_message
from app.payswift_client import PayoutStatus, PayoutResult
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
        cur.execute("TRUNCATE TABLE dispute_decisions, conversation_traces, payment_attempts, trace_events RESTART IDENTITY CASCADE;")
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def clean_tables(db_conn):
    """Clean tables before each test."""
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE dispute_decisions, conversation_traces, payment_attempts, trace_events RESTART IDENTITY CASCADE;")
    db_conn.commit()
    yield


MOCK_PAYSWIFT_URL = "http://localhost:8080"


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_secret_sanitization():
    """Verify that sensitive keys are redacted recursively."""
    sensitive_dict = {
        "rider_id": "R003",
        "api_key": "secret_gemini_key_123",
        "nested": {
            "token": "bearer_abc_xyz",
            "normal_field": "public_val",
        },
        "list_items": [
            {"password": "my_password_123"},
            {"safe_item": "ok"},
        ],
    }

    sanitized = sanitize_data(sensitive_dict)

    assert sanitized["rider_id"] == "R003"
    assert sanitized["api_key"] == "[REDACTED]"
    assert sanitized["nested"]["token"] == "[REDACTED]"
    assert sanitized["nested"]["normal_field"] == "public_val"
    assert sanitized["list_items"][0]["password"] == "[REDACTED]"
    assert sanitized["list_items"][1]["safe_item"] == "ok"


def test_ordered_trace_events_for_normal_dispute(db_conn):
    """Representative dispute generates complete, ordered trace events with required fields."""
    mock_resp = httpx.Response(201, json={"payout_id": "pay_trace123", "status": "processed"})

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

    with patch("httpx.Client.post", return_value=mock_resp):
        res = process_rider_message(
            conn=db_conn,
            rider_id="R003",
            text="Bhai surge missing",
            message_id="wamid.trace001",
            received_at="2026-09-22T09:05:00+05:30",
            llm_adapter=scripted_adapter,
            payswift_base_url=MOCK_PAYSWIFT_URL,
        )

    # Retrieve events via trace logger helper
    events = get_trace_events_for_rider(db_conn, "R003")

    assert len(events) >= 5

    # 1. Verify required fields on EVERY event
    required_keys = {"at", "type", "name", "input", "output"}
    for ev in events:
        assert required_keys.issubset(ev.keys())
        assert isinstance(ev["at"], str)
        assert isinstance(ev["type"], str)
        assert isinstance(ev["name"], str)
        assert isinstance(ev["input"], dict)
        assert isinstance(ev["output"], dict)

    # 2. Verify sequence of types and names
    event_types = [e["type"] for e in events]
    event_names = [e["name"] for e in events]

    assert "message_in" in event_types
    assert "tool_call" in event_types
    assert "decision" in event_types
    assert "reply" in event_types

    assert "receive_whatsapp_message" in event_names
    assert "llm_interpret" in event_names
    assert "reconcile_rider_dispute" in event_names
    assert "evaluate_policy_decision" in event_names
    assert "execute_payswift_payout" in event_names
    assert "agent_reply" in event_names


def test_error_event_representation(db_conn):
    """LLM or payment errors are explicitly captured as 'error' event type."""
    # Simulate LLM failure by providing broken adapter
    broken_adapter = MagicMock()
    broken_adapter.interpret.side_effect = RuntimeError("API key invalid or rate limited")

    res = process_rider_message(
        conn=db_conn,
        rider_id="R003",
        text="broken request",
        message_id="wamid.error001",
        received_at="2026-09-22T09:05:00+05:30",
        llm_adapter=broken_adapter,
        payswift_base_url=MOCK_PAYSWIFT_URL,
    )

    events = get_trace_events_for_rider(db_conn, "R003")

    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) >= 1
    assert error_events[0]["name"] == "llm_interpretation_error"
    assert "API key invalid" in str(error_events[0]["output"]["error"])


def test_get_rider_trace_api_endpoint(db_conn):
    """GET /trace/{rider_id} returns both turn traces and structured trace events."""
    mock_resp = httpx.Response(201, json={"payout_id": "pay_api123", "status": "processed"})

    scripted_adapter = ScriptedLLMAdapter()

    with patch("httpx.Client.post", return_value=mock_resp):
        process_rider_message(
            conn=db_conn,
            rider_id="R003",
            text="surge missing",
            message_id="wamid.api_trace001",
            received_at="2026-09-22T09:05:00+05:30",
            llm_adapter=scripted_adapter,
            payswift_base_url=MOCK_PAYSWIFT_URL,
        )

    client = TestClient(app)
    res = client.get("/trace/R003")
    assert res.status_code == 200

    data = res.json()
    assert isinstance(data, list)
    assert len(data) >= 5

    # Verify event fields in API response
    first_event = data[0]
    assert "at" in first_event
    assert "type" in first_event
    assert "name" in first_event
    assert "input" in first_event
    assert "output" in first_event
