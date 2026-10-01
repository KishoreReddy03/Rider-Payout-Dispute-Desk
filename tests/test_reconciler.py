"""Unit and integration tests for the deterministic reconciliation layer."""

import pytest
import psycopg2
from app.config import settings
from app.domain.reconciler import reconcile_rider_dispute


@pytest.fixture
def db_conn():
    conn = psycopg2.connect(settings.database_url)
    yield conn
    conn.close()


def test_missing_payout_lines_multiple_trips(db_conn):
    # R016 on 2026-09-19: 9 completed trips, only 4 paid lines (5 missing payouts)
    res = reconcile_rider_dispute(db_conn, rider_id="R016", target_date="2026-09-19")
    assert res.rider_id == "R016"
    assert res.total_expected_payout == 599
    assert res.total_actual_paid == 174
    assert res.calculated_amount_owed == 425
    assert len(res.discrepancy_reasons) >= 5


def test_duplicate_cancellation_penalty(db_conn):
    # R014 on 2026-09-18: Trip T637155 penalized twice (-10 twice = -20)
    res = reconcile_rider_dispute(db_conn, rider_id="R014", target_date="2026-09-18")
    assert res.rider_id == "R014"
    assert res.calculated_amount_owed == 10
    assert any("Duplicate cancellation penalty" in r for r in res.discrepancy_reasons)


def test_missing_daily_incentive(db_conn):
    # R005 on 2026-09-21: 13 completed trips, trip payouts paid, missing ₹150 incentive
    res = reconcile_rider_dispute(db_conn, rider_id="R005", target_date="2026-09-21")
    assert res.rider_id == "R005"
    assert res.calculated_amount_owed == 150
    assert res.incentive_evaluated is not None
    assert res.incentive_evaluated.unique_completed_trips_count == 13
    assert res.incentive_evaluated.underpayment == 150


def test_correct_payout_no_discrepancy(db_conn):
    # R001 on 2026-09-12: all trip payouts paid correctly
    res = reconcile_rider_dispute(db_conn, rider_id="R001", target_date="2026-09-12")
    assert res.rider_id == "R001"
    assert res.calculated_amount_owed == 0
    assert len(res.discrepancy_reasons) == 0


def test_unknown_trip_id(db_conn):
    # Non-existent trip ID requested
    res = reconcile_rider_dispute(db_conn, rider_id="R001", target_trip_id="TINVALID999")
    assert res.rider_id == "R001"
    assert res.calculated_amount_owed == 0
    assert len(res.discrepancy_reasons) == 1
    assert "not found in database" in res.discrepancy_reasons[0]


def test_amount_owed_cannot_exceed_calculated_deficit(db_conn):
    # Verify calculated_amount_owed is strictly max(0, total_expected - total_actual)
    res = reconcile_rider_dispute(db_conn, rider_id="R003", target_trip_id="T926334")
    assert res.calculated_amount_owed == res.total_expected_payout - res.total_actual_paid
    assert res.calculated_amount_owed <= max(0, res.total_expected_payout - res.total_actual_paid)
