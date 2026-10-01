"""Focused unit tests for the deterministic policy decision engine.

Tests all boundary values, financial invariants, and decision branches.
Uses a real DB connection for once-per-day constraint checks.
"""

import pytest
import psycopg2
from datetime import date
from unittest.mock import patch, MagicMock
from app.config import settings
from app.domain.decision import (
    decide,
    record_decision,
    PolicyDecision,
    DecisionType,
    AUTO_PAY_LIMIT,
)
from app.domain.reconciler import ReconciliationResult


# ─── Helpers ────────────────────────────────────────────────────────────────

def make_result(rider_id: str, owed: int, date_str: str = "2026-09-20") -> ReconciliationResult:
    """Build a minimal ReconciliationResult stub for decision engine testing."""
    return ReconciliationResult(
        rider_id=rider_id,
        target_date=date_str,
        target_trip_id=None,
        trips_evaluated=[],
        incentive_evaluated=None,
        total_expected_payout=owed,
        total_actual_paid=0,
        calculated_amount_owed=owed,
        discrepancy_reasons=[f"Underpaid ₹{owed}"] if owed > 0 else [],
        structured_evidence={},
    )


@pytest.fixture
def db_conn():
    conn = psycopg2.connect(settings.database_url)
    yield conn
    conn.rollback()   # never persist test rows to shared DB
    conn.close()


# ─── Boundary Values ────────────────────────────────────────────────────────

def test_owed_zero_is_no_payment(db_conn):
    decision = decide(db_conn, make_result("R001", 0), date(2026, 9, 20))
    assert decision.decision_type == DecisionType.NO_PAYMENT
    assert decision.amount == 0


def test_owed_one_rupee_is_auto_pay(db_conn):
    decision = decide(db_conn, make_result("R001", 1), date(2026, 9, 20))
    assert decision.decision_type == DecisionType.AUTO_PAY
    assert decision.amount == 1


def test_owed_199_is_auto_pay(db_conn):
    decision = decide(db_conn, make_result("R001", 199), date(2026, 9, 20))
    assert decision.decision_type == DecisionType.AUTO_PAY
    assert decision.amount == 199


def test_owed_200_exactly_is_auto_pay(db_conn):
    decision = decide(db_conn, make_result("R001", 200), date(2026, 9, 20))
    assert decision.decision_type == DecisionType.AUTO_PAY
    assert decision.amount == 200


def test_owed_201_requires_ops_approval(db_conn):
    decision = decide(db_conn, make_result("R001", 201), date(2026, 9, 20))
    assert decision.decision_type == DecisionType.OPS_APPROVAL
    assert decision.amount == 201


def test_large_amount_ops_approval(db_conn):
    decision = decide(db_conn, make_result("R001", 425), date(2026, 9, 19))
    assert decision.decision_type == DecisionType.OPS_APPROVAL
    assert decision.amount == 425


# ─── Financial Invariants ────────────────────────────────────────────────────

def test_auto_pay_amount_never_exceeds_owed(db_conn):
    result = make_result("R001", 150)
    decision = decide(db_conn, result, date(2026, 9, 20))
    assert decision.amount <= result.calculated_amount_owed


def test_auto_pay_amount_never_exceeds_ceiling(db_conn):
    result = make_result("R001", 200)
    decision = decide(db_conn, result, date(2026, 9, 20))
    assert decision.amount <= AUTO_PAY_LIMIT


def test_ops_approval_amount_matches_owed_exactly(db_conn):
    result = make_result("R001", 350)
    decision = decide(db_conn, result, date(2026, 9, 20))
    assert decision.decision_type == DecisionType.OPS_APPROVAL
    assert decision.amount == 350   # full verified owed, not capped or rounded


def test_no_payment_amount_is_always_zero(db_conn):
    decision = decide(db_conn, make_result("R001", 0), date(2026, 9, 20))
    assert decision.amount == 0


def test_escalation_amount_is_always_zero(db_conn):
    result = make_result("R001", 999)
    decision = decide(db_conn, result, date(2026, 9, 20), escalate_reason="Prompt injection detected.")
    assert decision.decision_type == DecisionType.ESCALATION
    assert decision.amount == 0


# ─── Once-Per-Day Auto-Pay Rule ──────────────────────────────────────────────

def test_second_dispute_same_day_routes_to_ops(db_conn):
    """Simulate first auto-pay committed, then second dispute arrives same day."""
    # Write a committed auto_pay row into DB for rider R099 on 2099-01-01
    cur = db_conn.cursor()
    cur.execute(
        """
        INSERT INTO dispute_decisions
            (rider_id, dispute_date, decision_type, amount, status, reason)
        VALUES ('R099', '2099-01-01', 'auto_pay', 25, 'executed', 'test setup row');
        """
    )
    # Do NOT commit — relies on rollback in fixture to clean up

    result = make_result("R099", 10)
    decision = decide(db_conn, result, date(2099, 1, 1))
    assert decision.decision_type == DecisionType.OPS_APPROVAL
    assert "auto-pay has already been issued" in decision.reason


def test_first_dispute_day_gets_auto_pay(db_conn):
    """Rider with no auto_pay today gets auto_pay decision."""
    result = make_result("R098", 50)
    decision = decide(db_conn, result, date(2099, 1, 2))  # date with no prior rows
    assert decision.decision_type == DecisionType.AUTO_PAY
    assert decision.amount == 50


# ─── Escalation ─────────────────────────────────────────────────────────────

def test_forced_escalation_overrides_owed_amount(db_conn):
    result = make_result("R001", 50)
    decision = decide(db_conn, result, date(2026, 9, 20), escalate_reason="Rider claims different identity.")
    assert decision.decision_type == DecisionType.ESCALATION
    assert decision.amount == 0   # never pay anything on escalation


# ─── Decision Persistence ────────────────────────────────────────────────────

def test_record_decision_inserts_row(db_conn):
    result = make_result("R001", 150)
    decision = decide(db_conn, result, date(2099, 6, 1))
    # record_decision commits, so rollback will remove it
    row_id = record_decision(db_conn, decision, message_id="wamid.TEST123")
    assert isinstance(row_id, int)
    assert row_id > 0
