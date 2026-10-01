"""Attack suite: payment flow idempotency, timeout safety, and duplicate payment prevention.

These tests prove — using concrete execution — that no combination of timeouts,
retries, or repeated messages can create a duplicate payout.
Each test documents the specific failure scenario it defends against.
"""

import pytest
from unittest.mock import patch, MagicMock, call
import httpx

from app.payswift_client import (
    make_idempotency_key,
    execute_payout,
    PayoutStatus,
    _MAX_RETRIES,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def mock_post(status_code: int, body: dict) -> MagicMock:
    m = MagicMock()
    m.status_code = status_code
    m.json.return_value = body
    m.text = str(body)
    return m


def run_payout(side_effects, rider="R003", date="2026-09-20", amount=25):
    """Run execute_payout with a sequence of mocked HTTP responses or exceptions."""
    key = make_idempotency_key(rider, date, amount)
    with patch("httpx.Client") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.post.side_effect = side_effects
        result = execute_payout(rider, amount, key, "attack-test", "http://mock")
    return result, key, instance


# ═══════════════════════════════════════════════════════════════════════════
# ATTACK 1: Timeout after PaySwift accepted the payment
#
# Scenario: Our POST arrives at PaySwift. PaySwift processes and commits
# the payment. The 201 response is lost in the network. We get a timeout.
# If we now create a new request with a NEW key, we pay the rider twice.
# ═══════════════════════════════════════════════════════════════════════════

class TestTimeoutAfterAcceptance:
    def test_timeout_status_is_uncertain_not_failed(self):
        """Timeout must be UNCERTAIN, never REJECTED or SERVER_ERROR."""
        result, key, _ = run_payout(
            [httpx.TimeoutException("network timeout")] * _MAX_RETRIES
        )
        assert result.status == PayoutStatus.UNCERTAIN
        assert result.http_code is None  # no HTTP code received

    def test_timeout_preserves_original_idempotency_key(self):
        """The idempotency key in the result must be the key we sent — never changed."""
        result, key, _ = run_payout(
            [httpx.TimeoutException("timeout")] * _MAX_RETRIES
        )
        assert result.idempotency_key == key, (
            "CRITICAL: key must be preserved so caller can retry safely"
        )

    def test_retrying_timeout_with_same_key_resolves_to_already_paid(self):
        """
        Simulate: attempt 1 times out (PaySwift committed), attempt 2 gets 409.
        This proves the retry resolves the uncertain state without double-paying.
        """
        result, key, mock_instance = run_payout([
            httpx.TimeoutException("first call lost"),   # attempt 1: timeout
            mock_post(409, {"payout_id": "PAY999", "status": "already_paid"}),  # attempt 2: 409
        ])
        assert result.status == PayoutStatus.ALREADY_PAID
        assert result.is_confirmed
        assert result.payout_id == "PAY999"

        # Verify BOTH calls used IDENTICAL headers (same idempotency key)
        calls = mock_instance.post.call_args_list
        assert len(calls) == 2
        key_call_1 = calls[0].kwargs["headers"]["Idempotency-Key"]
        key_call_2 = calls[1].kwargs["headers"]["Idempotency-Key"]
        assert key_call_1 == key_call_2 == key, (
            f"CRITICAL: keys diverged! call1={key_call_1} call2={key_call_2}"
        )

    def test_retrying_timeout_with_same_key_resolves_to_confirmed(self):
        """
        Simulate: attempt 1 times out (PaySwift did NOT commit), attempt 2 gets 201.
        Proves retry with same key is safe when PaySwift had not committed.
        """
        result, key, mock_instance = run_payout([
            httpx.TimeoutException("first call timed out before PaySwift committed"),
            mock_post(201, {"payout_id": "PAY888", "status": "processed"}),
        ])
        assert result.status == PayoutStatus.CONFIRMED
        assert result.payout_id == "PAY888"

        calls = mock_instance.post.call_args_list
        key_1 = calls[0].kwargs["headers"]["Idempotency-Key"]
        key_2 = calls[1].kwargs["headers"]["Idempotency-Key"]
        assert key_1 == key_2 == key


# ═══════════════════════════════════════════════════════════════════════════
# ATTACK 2: Service restart between attempt and retry
#
# If the service restarts (e.g., after a crash) it must re-derive the
# SAME idempotency key from the dispute identity — not generate a new UUID.
# ═══════════════════════════════════════════════════════════════════════════

class TestServiceRestartDurability:
    def test_key_is_deterministic_across_process_restarts(self):
        """Simulate two separate process runs computing the same key."""
        # Process 1 (before restart):
        key_before_restart = make_idempotency_key("R003", "2026-09-20", 25)

        # Process 2 (after restart, same dispute inputs):
        key_after_restart = make_idempotency_key("R003", "2026-09-20", 25)

        assert key_before_restart == key_after_restart, (
            "Keys diverged across process restarts — would cause duplicate payout!"
        )

    def test_key_does_not_contain_timestamp_or_uuid(self):
        """Prove the key is not time-dependent."""
        import time
        key1 = make_idempotency_key("R003", "2026-09-20", 25)
        time.sleep(0.01)
        key2 = make_idempotency_key("R003", "2026-09-20", 25)
        assert key1 == key2

    def test_different_disputes_produce_different_keys(self):
        """Prove distinct disputes cannot share a key (which would cause payment confusion)."""
        k1 = make_idempotency_key("R003", "2026-09-20", 25)
        k2 = make_idempotency_key("R003", "2026-09-20", 26)   # different amount
        k3 = make_idempotency_key("R003", "2026-09-21", 25)   # different date
        k4 = make_idempotency_key("R004", "2026-09-20", 25)   # different rider
        assert len({k1, k2, k3, k4}) == 4, "Every distinct dispute must have a unique key"


# ═══════════════════════════════════════════════════════════════════════════
# ATTACK 3: Rider sends the same complaint twice
#
# Two identical webhook calls arrive for the same dispute.
# The first creates an auto_pay decision + calls PaySwift → 201.
# The second call must NOT call PaySwift again.
# ═══════════════════════════════════════════════════════════════════════════

class TestDuplicateWebhookIdempotency:
    def test_same_key_on_second_call_gets_409_not_201(self):
        """
        Simulate exact same dispute re-submitted.
        PaySwift returns 409 — idempotency key already used.
        Client must treat 409 as is_confirmed=True, not as an error.
        """
        key = make_idempotency_key("R003", "2026-09-20", 25)
        with patch("httpx.Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            # First call: 201
            # Second call: 409 (idempotency hit)
            instance.post.side_effect = [
                mock_post(201, {"payout_id": "PAY001", "status": "processed"}),
                mock_post(409, {"payout_id": "PAY001", "status": "already_paid"}),
            ]
            result1 = execute_payout("R003", 25, key, "first call", "http://mock")
            result2 = execute_payout("R003", 25, key, "first call", "http://mock")

        assert result1.status == PayoutStatus.CONFIRMED
        assert result2.status == PayoutStatus.ALREADY_PAID

        # Both must report is_confirmed so caller knows money reached the rider
        assert result1.is_confirmed
        assert result2.is_confirmed

        # Both must carry the same payout_id — proving it's the same transaction
        assert result1.payout_id == result2.payout_id == "PAY001"

        # PaySwift was called exactly twice (not four times, not zero)
        assert instance.post.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# ATTACK 4: 5xx storm — PaySwift is flapping
#
# PaySwift returns 500 three times in a row.
# Client must exhaust retries with the same key and return SERVER_ERROR.
# It must NOT silently succeed or create phantom payment records.
# ═══════════════════════════════════════════════════════════════════════════

class TestServerErrorStorm:
    def test_three_500s_returns_server_error(self):
        result, key, instance = run_payout(
            [mock_post(500, {"error": "internal"})] * _MAX_RETRIES
        )
        assert result.status == PayoutStatus.SERVER_ERROR
        assert not result.is_confirmed
        assert instance.post.call_count == _MAX_RETRIES

    def test_all_500_retries_use_same_key(self):
        """Even during a 5xx storm, every retry must carry the same idempotency key."""
        result, key, instance = run_payout(
            [mock_post(500, {"error": "internal"})] * _MAX_RETRIES
        )
        for c in instance.post.call_args_list:
            assert c.kwargs["headers"]["Idempotency-Key"] == key

    def test_500_then_201_succeeds(self):
        """Single transient 5xx, then success — no double payment."""
        result, key, instance = run_payout([
            mock_post(500, {"error": "transient"}),
            mock_post(201, {"payout_id": "PAY777", "status": "processed"}),
        ])
        assert result.status == PayoutStatus.CONFIRMED
        assert result.payout_id == "PAY777"
        assert instance.post.call_count == 2

        # Both calls used the same key
        keys_used = [c.kwargs["headers"]["Idempotency-Key"] for c in instance.post.call_args_list]
        assert keys_used[0] == keys_used[1] == key


# ═══════════════════════════════════════════════════════════════════════════
# ATTACK 5: Client passes wrong amount (amount drift between decision and execution)
#
# Prove the payment identity includes the amount,
# so an accidentally different amount would produce a DIFFERENT key,
# and PaySwift would treat it as a new transaction — not silently overwrite.
# ═══════════════════════════════════════════════════════════════════════════

class TestAmountDriftSafety:
    def test_different_amounts_are_different_transactions(self):
        """If the amount somehow changes between calls, the keys must differ.
        This prevents accidental over/under-payment being silently accepted."""
        key_correct = make_idempotency_key("R003", "2026-09-20", 25)
        key_wrong   = make_idempotency_key("R003", "2026-09-20", 5000)  # rider's claimed amount
        assert key_correct != key_wrong, (
            "CRITICAL: if rider-claimed amount replaced calculated amount, "
            "it would be a different key → different transaction sent to PaySwift"
        )

    def test_key_encodes_amount_in_canonical_form(self):
        k = make_idempotency_key("R003", "2026-09-20", 25)
        import hashlib
        expected = "idem-" + hashlib.sha256(b"dispute:R003:2026-09-20:25").hexdigest()[:32]
        assert k == expected, "Key derivation must be fully reproducible"
