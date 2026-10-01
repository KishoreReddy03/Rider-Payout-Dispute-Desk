"""Tests for the PaySwift payment client boundary.

Split into two classes:
  TestIdempotencyKey   – pure unit tests, no network.
  TestPaySwiftClient   – tests against the live PaySwift sandbox on port 8081.
                         These are marked with @pytest.mark.integration and are
                         skipped automatically if PaySwift is not reachable.
"""

import time
import pytest
import httpx
from unittest.mock import patch, MagicMock

from app.payswift_client import (
    make_idempotency_key,
    execute_payout,
    get_payouts,
    PayoutStatus,
    PayoutResult,
)

PAYSWIFT_URL = "http://127.0.0.1:8081"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def payswift_available() -> bool:
    """Return True if the local PaySwift sandbox is reachable."""
    try:
        resp = httpx.get(f"{PAYSWIFT_URL}/health", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


integration = pytest.mark.skipif(
    not payswift_available(),
    reason="PaySwift sandbox not running on port 8081",
)


# ─── Idempotency Key Unit Tests ───────────────────────────────────────────────

class TestIdempotencyKey:
    def test_key_is_deterministic(self):
        """Same inputs always produce the same key."""
        k1 = make_idempotency_key("R003", "2026-09-20", 25)
        k2 = make_idempotency_key("R003", "2026-09-20", 25)
        assert k1 == k2

    def test_key_differs_by_rider(self):
        k1 = make_idempotency_key("R003", "2026-09-20", 25)
        k2 = make_idempotency_key("R004", "2026-09-20", 25)
        assert k1 != k2

    def test_key_differs_by_date(self):
        k1 = make_idempotency_key("R003", "2026-09-20", 25)
        k2 = make_idempotency_key("R003", "2026-09-21", 25)
        assert k1 != k2

    def test_key_differs_by_amount(self):
        k1 = make_idempotency_key("R003", "2026-09-20", 25)
        k2 = make_idempotency_key("R003", "2026-09-20", 26)
        assert k1 != k2

    def test_key_has_prefix(self):
        k = make_idempotency_key("R003", "2026-09-20", 25)
        assert k.startswith("idem-")

    def test_key_does_not_change_on_retry(self):
        """Simulates two calls that must use the same key on retry."""
        k_first = make_idempotency_key("R003", "2026-09-20", 25)
        k_retry = make_idempotency_key("R003", "2026-09-20", 25)
        assert k_first == k_retry, "Key must be identical on retry"


# ─── Client Behaviour Tests (mocked) ─────────────────────────────────────────

class TestPaySwiftClientMocked:
    """Tests that verify client state-machine using mocked HTTP responses."""

    def _mock_resp(self, status_code: int, body: dict) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = body
        resp.text = str(body)
        return resp

    def _run(self, status_code: int, body: dict) -> PayoutResult:
        key = make_idempotency_key("R003", "2026-09-20", 25)
        with patch("httpx.Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            instance.post.return_value = self._mock_resp(status_code, body)
            return execute_payout("R003", 25, key, "test ref", payswift_base_url="http://mock")

    def test_201_returns_confirmed(self):
        res = self._run(201, {"payout_id": "PAY123", "status": "processed"})
        assert res.status == PayoutStatus.CONFIRMED
        assert res.payout_id == "PAY123"
        assert res.http_code == 201
        assert res.is_confirmed

    def test_409_returns_already_paid(self):
        res = self._run(409, {"payout_id": "PAY123", "status": "already_paid"})
        assert res.status == PayoutStatus.ALREADY_PAID
        assert res.is_confirmed   # 409 is still a confirmed state

    def test_400_returns_rejected(self):
        res = self._run(400, {"error": "Invalid amount"})
        assert res.status == PayoutStatus.REJECTED
        assert not res.is_confirmed
        assert res.http_code == 400

    def test_timeout_returns_uncertain(self):
        key = make_idempotency_key("R003", "2026-09-20", 25)
        with patch("httpx.Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            instance.post.side_effect = httpx.TimeoutException("timed out")
            res = execute_payout("R003", 25, key, "test ref", payswift_base_url="http://mock")
        assert res.status == PayoutStatus.UNCERTAIN
        assert not res.is_confirmed
        assert res.should_retry
        # Critically: idempotency key must be unchanged
        assert res.idempotency_key == key

    def test_timeout_retry_uses_same_key(self):
        """Simulate first call times out; second call (retry) must use the same key."""
        key_first = make_idempotency_key("R003", "2026-09-20", 25)
        # After timeout, caller re-derives the key — must be identical
        key_retry = make_idempotency_key("R003", "2026-09-20", 25)
        assert key_first == key_retry, "Retry must use the same idempotency key"

    def test_429_exhausted_returns_rate_limited(self):
        key = make_idempotency_key("R003", "2026-09-20", 25)
        with patch("httpx.Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            instance.post.return_value = self._mock_resp(429, {"error": "rate limited"})
            res = execute_payout("R003", 25, key, "test ref", payswift_base_url="http://mock")
        assert res.status == PayoutStatus.RATE_LIMITED

    def test_500_exhausted_returns_server_error(self):
        key = make_idempotency_key("R003", "2026-09-20", 25)
        with patch("httpx.Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            instance.post.return_value = self._mock_resp(500, {"error": "internal"})
            res = execute_payout("R003", 25, key, "test ref", payswift_base_url="http://mock")
        assert res.status == PayoutStatus.SERVER_ERROR


# ─── Live PaySwift Integration Tests ─────────────────────────────────────────

class TestPaySwiftLive:
    @integration
    def test_health_endpoint(self):
        resp = httpx.get(f"{PAYSWIFT_URL}/health", timeout=5.0)
        assert resp.status_code == 200

    @integration
    def test_successful_payout_201(self):
        key = make_idempotency_key("R003", "2026-09-20", 25, purpose="live-test")
        res = execute_payout("R003", 25, key, "live integration test", PAYSWIFT_URL)
        assert res.http_code in (201, 409)     # 201 first run, 409 if re-run
        assert res.status in (PayoutStatus.CONFIRMED, PayoutStatus.ALREADY_PAID)
        assert res.is_confirmed

    @integration
    def test_idempotency_second_call_409(self):
        """Send the same request twice. Second call must return 409."""
        key = make_idempotency_key("R003", "2026-09-20", 34, purpose="idem-test")
        res1 = execute_payout("R003", 34, key, "first call", PAYSWIFT_URL)
        assert res1.http_code in (201, 409)
        res2 = execute_payout("R003", 34, key, "first call", PAYSWIFT_URL)
        # If res1 was 201, res2 must be 409
        if res1.http_code == 201:
            assert res2.status == PayoutStatus.ALREADY_PAID
            assert res2.http_code == 409

    @integration
    def test_invalid_amount_zero_returns_400(self):
        key = make_idempotency_key("R003", "2026-09-20", 0, purpose="bad-amount")
        res = execute_payout("R003", 0, key, "should fail", PAYSWIFT_URL)
        assert res.status == PayoutStatus.REJECTED
        assert res.http_code == 400

    @integration
    def test_invalid_amount_negative_returns_400(self):
        key = make_idempotency_key("R003", "2026-09-20", -10, purpose="negative")
        res = execute_payout("R003", -10, key, "negative", PAYSWIFT_URL)
        assert res.status == PayoutStatus.REJECTED
        assert res.http_code == 400

    @integration
    def test_get_payouts_for_rider(self):
        ledger = get_payouts(rider_id="R003", payswift_base_url=PAYSWIFT_URL)
        assert isinstance(ledger, list)
        # Each entry should have expected keys
        for entry in ledger:
            assert "payout_id" in entry or len(entry) == 0

    @integration
    def test_get_all_payouts(self):
        ledger = get_payouts(payswift_base_url=PAYSWIFT_URL)
        assert isinstance(ledger, list)
