"""PaySwift payment client.

Strict boundary: this module is the ONLY code that calls PaySwift.
The LLM, frontend, and domain engine never call PaySwift directly.

Response code semantics (from PaySwift README + observed behaviour):
  201  Created     – payment succeeded, ledger updated
  400  Bad Request – malformed request (amount 0, unknown rider_id format, etc.)
  409  Conflict    – idempotency key already used with *different* body → do not retry with same key
  429  Too Many Requests – rate limit; back off and retry
  5xx  Server error – transient; retry with same idempotency key

Timeout handling:
  A timeout after a POST means the payment state is *uncertain*:
  PaySwift may or may not have committed the transaction.
  We MUST retry with the SAME idempotency key, never create a new one.
  Only a 201 or 409 response confirms the payment's final state.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_MAX_RETRIES = 3
_RETRYABLE_CODES = {429, 500, 502, 503, 504}


class PayoutStatus(str, Enum):
    CONFIRMED = "confirmed"     # PaySwift returned 201
    ALREADY_PAID = "already_paid"  # PaySwift returned 409 (idempotency: already done)
    REJECTED = "rejected"       # PaySwift returned 400 (permanent failure)
    RATE_LIMITED = "rate_limited"  # 429 exhausted retries
    SERVER_ERROR = "server_error"  # 5xx exhausted retries
    UNCERTAIN = "uncertain"     # Timeout after possible 201; must retry


@dataclass
class PayoutResult:
    status: PayoutStatus
    payout_id: Optional[str]
    raw_status: Optional[str]       # PaySwift status field from body
    http_code: Optional[int]
    idempotency_key: str
    error: Optional[str] = None

    @property
    def is_confirmed(self) -> bool:
        return self.status in (PayoutStatus.CONFIRMED, PayoutStatus.ALREADY_PAID)

    @property
    def should_retry(self) -> bool:
        return self.status == PayoutStatus.UNCERTAIN


# ── Idempotency Key Generation ────────────────────────────────────────────────

def make_idempotency_key(rider_id: str, dispute_date: str, amount: int, purpose: str = "dispute") -> str:
    """Generate a stable, deterministic idempotency key.

    The key is derived from immutable dispute identity: rider + date + amount + purpose.
    Retrying with the same inputs always produces the same key, which is the
    correct behaviour for uncertain (timeout) states.

    The key must NOT include timestamps, UUIDs, or randomness.
    """
    canonical = f"{purpose}:{rider_id}:{dispute_date}:{amount}"
    return "idem-" + hashlib.sha256(canonical.encode()).hexdigest()[:32]


# ── Core Payment Function ─────────────────────────────────────────────────────

def execute_payout(
    rider_id: str,
    amount: int,
    idempotency_key: str,
    reference: str,
    payswift_base_url: Optional[str] = None,
) -> PayoutResult:
    """Execute a single payout request against PaySwift.

    Args:
        rider_id:          QuickDrop rider identifier (e.g. 'R003').
        amount:            Whole rupees, 1–10000. Must already be verified by decision engine.
        idempotency_key:   Stable key from make_idempotency_key(). NEVER change on retry.
        reference:         Free-text ledger reference for audit trail.
        payswift_base_url: Override for tests; defaults to settings.payswift_base_url.

    Returns:
        PayoutResult describing the confirmed/uncertain/failed state.

    Raises:
        Nothing — all error conditions are captured in PayoutResult.
    """
    base_url = (payswift_base_url or settings.payswift_base_url).rstrip("/")
    url = f"{base_url}/v1/payouts"
    payload = {"rider_id": rider_id, "amount": amount, "reference": reference}
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
    }

    last_code: Optional[int] = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            logger.info(
                "PaySwift POST attempt %d/%d rider=%s amount=%d key=%s",
                attempt, _MAX_RETRIES, rider_id, amount, idempotency_key,
            )
            with httpx.Client(timeout=_TIMEOUT) as client:
                resp = client.post(url, json=payload, headers=headers)

            last_code = resp.status_code
            logger.info("PaySwift response %d: %s", resp.status_code, resp.text)

            # ── 201 Created: payment confirmed ───────────────────────────────
            if resp.status_code == 201:
                body = resp.json()
                return PayoutResult(
                    status=PayoutStatus.CONFIRMED,
                    payout_id=body.get("payout_id"),
                    raw_status=body.get("status"),
                    http_code=201,
                    idempotency_key=idempotency_key,
                )

            # ── 409 Conflict: already processed with this key ────────────────
            if resp.status_code == 409:
                body = resp.json()
                return PayoutResult(
                    status=PayoutStatus.ALREADY_PAID,
                    payout_id=body.get("payout_id"),
                    raw_status=body.get("status"),
                    http_code=409,
                    idempotency_key=idempotency_key,
                    error="Idempotency key already used; payment was previously processed.",
                )

            # ── 400 Bad Request: permanent failure, do not retry ─────────────
            if resp.status_code == 400:
                return PayoutResult(
                    status=PayoutStatus.REJECTED,
                    payout_id=None,
                    raw_status=None,
                    http_code=400,
                    idempotency_key=idempotency_key,
                    error=f"PaySwift rejected the request (400): {resp.text}",
                )

            # ── 429 / 5xx: retryable ─────────────────────────────────────────
            if resp.status_code in _RETRYABLE_CODES:
                logger.warning(
                    "PaySwift retryable error %d on attempt %d", resp.status_code, attempt
                )
                last_code = resp.status_code
                continue  # retry with SAME idempotency key

            # ── Unexpected code ───────────────────────────────────────────────
            return PayoutResult(
                status=PayoutStatus.SERVER_ERROR,
                payout_id=None,
                raw_status=None,
                http_code=resp.status_code,
                idempotency_key=idempotency_key,
                error=f"Unexpected status {resp.status_code}: {resp.text}",
            )

        except httpx.TimeoutException as exc:
            # ── CRITICAL: Timeout means state is uncertain ────────────────────
            # PaySwift may have committed the payment and the 201 was lost in transit.
            # We MUST NOT create a new payment. Caller must retry with the same key.
            logger.error(
                "PaySwift timeout on attempt %d rider=%s key=%s: %s",
                attempt, rider_id, idempotency_key, exc,
            )
            if attempt == _MAX_RETRIES:
                return PayoutResult(
                    status=PayoutStatus.UNCERTAIN,
                    payout_id=None,
                    raw_status=None,
                    http_code=None,
                    idempotency_key=idempotency_key,
                    error=f"Timeout after {_MAX_RETRIES} attempts. Payment state uncertain. Retry with same idempotency key.",
                )
            continue  # retry same key

        except httpx.ConnectError as exc:
            logger.error("PaySwift connect error attempt %d: %s", attempt, exc)
            if attempt == _MAX_RETRIES:
                return PayoutResult(
                    status=PayoutStatus.SERVER_ERROR,
                    payout_id=None,
                    raw_status=None,
                    http_code=None,
                    idempotency_key=idempotency_key,
                    error=f"Cannot connect to PaySwift: {exc}",
                )
            continue

    # Exhausted all retries on rate-limit or 5xx
    status = PayoutStatus.RATE_LIMITED if last_code == 429 else PayoutStatus.SERVER_ERROR
    return PayoutResult(
        status=status,
        payout_id=None,
        raw_status=None,
        http_code=last_code,
        idempotency_key=idempotency_key,
        error=f"Exhausted {_MAX_RETRIES} retries, last HTTP code: {last_code}",
    )


# ── Ledger Query ──────────────────────────────────────────────────────────────

def get_payouts(rider_id: Optional[str] = None, payswift_base_url: Optional[str] = None) -> list:
    """Query PaySwift ledger for all payouts (optionally filtered by rider_id)."""
    base_url = (payswift_base_url or settings.payswift_base_url).rstrip("/")
    params = {"rider_id": rider_id} if rider_id else {}
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.get(f"{base_url}/v1/payouts", params=params)
    resp.raise_for_status()
    return resp.json()
