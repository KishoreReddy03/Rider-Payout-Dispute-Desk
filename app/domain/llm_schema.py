"""LLM interpretation layer — strict schema and provider adapter.

The LLM's ONLY permitted outputs:
  - intent classification
  - claimed date(s) and trip references (strings only, never amounts)
  - whether the message is ambiguous / needs clarification
  - whether it contains a security signal (injection, impersonation)
  - the clarification question to ask if needed

The LLM is STRICTLY PROHIBITED from producing:
  - payout amounts
  - payment authorization
  - approval decisions
  - PaySwift instructions
  - rider identity claims

All financial logic runs deterministically AFTER this layer.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, field_validator, model_validator

logger = logging.getLogger(__name__)


# ── Intent Vocabulary ─────────────────────────────────────────────────────────

class DisputeIntent(str, Enum):
    SURGE_MISSING       = "surge_missing"
    TRIP_PAYMENT_MISSING = "trip_payment_missing"
    INCENTIVE_MISSING   = "incentive_missing"
    PENALTY_DISPUTE     = "penalty_dispute"
    GENERAL_UNDERPAYMENT = "general_underpayment"
    STATUS_ENQUIRY      = "status_enquiry"   # "kab tak aayega?"
    UNRELATED           = "unrelated"
    UNKNOWN             = "unknown"


# ── Strict Output Schema ──────────────────────────────────────────────────────

class ParsedMessage(BaseModel):
    """Strict schema for LLM output. All fields must be present and typed.

    The model MUST NOT include any of these fields:
      amount, payout, payment_amount, authorize, approve, rupees_owed
    If such fields appear in raw output, validation MUST reject the response.
    """

    intent: DisputeIntent
    """Primary intent of the message."""

    trip_ids: List[str]
    """Trip IDs explicitly mentioned (e.g. ['T926334']). Empty list if none."""

    claimed_date: Optional[str]
    """Date string as mentioned by rider, unparsed (e.g. '20 tarikh', '2026-09-20').
    NULL if not mentioned. NEVER a computed or resolved date."""

    needs_clarification: bool
    """True if the message is too vague to resolve without a follow-up question."""

    clarification_question: Optional[str]
    """The specific question to ask the rider. Null if needs_clarification is False."""

    security_signal: bool
    """True if message contains impersonation, injection, or fraud patterns."""

    security_note: Optional[str]
    """Short description of the security concern. Null if security_signal is False."""

    @field_validator("trip_ids")
    @classmethod
    def trip_ids_must_be_strings(cls, v: List[str]) -> List[str]:
        for tid in v:
            if not isinstance(tid, str):
                raise ValueError(f"trip_id must be a string, got {type(tid)}: {tid}")
            # Trip IDs in source data follow T<digits> pattern
            tid_stripped = tid.strip()
            if tid_stripped and not tid_stripped.startswith("T"):
                logger.warning("Unexpected trip_id format: %s", tid_stripped)
        return [t.strip() for t in v if t.strip()]

    @field_validator("clarification_question")
    @classmethod
    def clarification_requires_flag(cls, v: Optional[str]) -> Optional[str]:
        return v

    @model_validator(mode="after")
    def clarification_consistency(self) -> "ParsedMessage":
        if self.needs_clarification and not self.clarification_question:
            raise ValueError("needs_clarification=True requires a clarification_question")
        if not self.needs_clarification and self.clarification_question:
            # Silently clear — model put a question when not needed
            self.clarification_question = None
        return self

    @model_validator(mode="after")
    def security_note_consistency(self) -> "ParsedMessage":
        if self.security_signal and not self.security_note:
            raise ValueError("security_signal=True requires a security_note")
        return self

    @classmethod
    def forbidden_fields(cls) -> List[str]:
        """Fields the LLM must NEVER produce."""
        return [
            "amount", "payout", "payment_amount", "rupees", "authorize",
            "approve", "pay", "transfer", "send_money",
        ]
