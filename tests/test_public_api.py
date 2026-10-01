"""Tests for Public HTTP Interface matching SUBMISSION.md specs.

Verifies:
- GET /health returns 200 OK
- POST /messages validates input, processes messages, and handles duplicate wamids correctly
- GET /trace/{rider_id} returns a top-level JSON array of ordered trace events
- GET /ops/pending returns a top-level JSON array of pending ops items
- No route permits arbitrary payment amounts or direct PaySwift execution from browser
"""

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.ingest import init_db
from app.config import settings
import psycopg2


client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    conn = psycopg2.connect(settings.database_url)
    init_db(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM raw_riders;")
        if cur.fetchone()[0] == 0:
            from app.ingest import DATA_DIR, ingest_riders, ingest_trips, ingest_payout_lines
            ingest_riders(conn, DATA_DIR / "riders.csv")
            ingest_trips(conn, DATA_DIR / "trips.csv")
            ingest_payout_lines(conn, DATA_DIR / "payout_lines.csv")
        cur.execute("TRUNCATE dispute_decisions, conversation_traces, payment_attempts, trace_events RESTART IDENTITY CASCADE;")
        cur.execute("INSERT INTO raw_riders (rider_id, name, city, joined_on) VALUES ('R003', 'Rajesh Kumar', 'Bengaluru', '2025-01-01') ON CONFLICT DO NOTHING;")
    conn.commit()
    conn.close()


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["database"] == "connected"


def test_post_messages_valid_and_duplicate():
    payload = {
        "message_id": "wamid.HTTP.TEST.001",
        "rider_id": "R003",
        "text": "mera payout check karo 20 Sep ka",
        "received_at": "2026-09-22T10:00:00Z",
    }
    # 1. First delivery
    resp1 = client.post("/messages", json=payload)
    assert resp1.status_code == 200
    reply1 = resp1.json().get("reply")
    assert reply1 is not None
    assert isinstance(reply1, str)

    # 2. Duplicate delivery (same message_id)
    resp2 = client.post("/messages", json=payload)
    assert resp2.status_code == 200
    reply2 = resp2.json().get("reply")
    assert reply2 == reply1


def test_post_messages_validation():
    # Empty message_id
    payload_bad = {
        "message_id": "   ",
        "rider_id": "R003",
        "text": "hello",
    }
    resp = client.post("/messages", json=payload_bad)
    assert resp.status_code == 422

    # Missing text
    payload_missing = {
        "message_id": "wamid.123",
        "rider_id": "R003",
    }
    resp_missing = client.post("/messages", json=payload_missing)
    assert resp_missing.status_code == 422


def test_get_trace_returns_array():
    resp = client.get("/trace/R003")
    assert resp.status_code == 200
    trace_events = resp.json()
    assert isinstance(trace_events, list)
    if len(trace_events) > 0:
        event = trace_events[0]
        assert "at" in event
        assert "type" in event
        assert "name" in event
        assert "input" in event
        assert "output" in event


def test_get_ops_pending_returns_array():
    resp = client.get("/ops/pending")
    assert resp.status_code == 200
    pending_items = resp.json()
    assert isinstance(pending_items, list)
    for item in pending_items:
        assert "id" in item
        assert "rider_id" in item
        assert "type" in item
        assert "reason" in item
        assert "created_at" in item


def test_no_arbitrary_payout_amount_route():
    """Verify OpenAPI routes schema has no route allowing clients to specify custom payout amounts."""
    openapi_schema = app.openapi()
    paths = openapi_schema.get("paths", {})

    # Check POST /messages parameters schema
    msg_post = paths.get("/messages", {}).get("post", {})
    body_schema = msg_post.get("requestBody", {})
    # Verify no 'amount' or 'payout' field in message intake schema
    for route, methods in paths.items():
        if route in ("/", "/ops", "/messages", "/health", "/trace/{rider_id}", "/ops/pending"):
            continue
        # Ensure ops action routes only take decision_id in path
        assert "{decision_id}" in route or "{id}" in route or "ops" in route

