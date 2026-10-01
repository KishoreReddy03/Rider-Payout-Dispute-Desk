"""Unit tests for deterministic payout policy calculations."""

import pytest
from app.domain.policy import (
    normalize_distance,
    round_half_up,
    calculate_trip_expected_payout,
    calculate_daily_incentive,
    calculate_underpayment,
)
from decimal import Decimal


def test_normalize_distance():
    # Normal km
    assert normalize_distance(0.8) == Decimal("0.8")
    assert normalize_distance(7.4) == Decimal("7.4")
    # Meter representation normalization (> 100)
    assert normalize_distance(5000) == Decimal("5")
    assert normalize_distance("11200") == Decimal("11.2")


def test_round_half_up():
    assert round_half_up(Decimal("73.4")) == 73
    # 0.5 rounding up
    assert round_half_up(Decimal("73.5")) == 74
    assert round_half_up(Decimal("100.5")) == 101
    assert round_half_up(Decimal("64.5")) == 65


def test_short_distance_trip():
    # Trips <= 2km pay base fare ₹25
    res = calculate_trip_expected_payout("completed", distance_km=0.8, surge_multiplier=1.0)
    assert res["gross_fare"] == 25
    assert res["net_expected"] == 25

    res = calculate_trip_expected_payout("completed", distance_km=1.5, surge_multiplier=1.0)
    assert res["gross_fare"] == 25
    assert res["net_expected"] == 25


def test_normal_distance_with_surge_and_half_up_rounding():
    # Trip T926334: 6.0km, surge 1.5x -> (25 + 4*6) * 1.5 = 49 * 1.5 = 73.5 -> 74
    res = calculate_trip_expected_payout("completed", distance_km=6.0, surge_multiplier=1.5)
    assert res["gross_fare"] == 74
    assert res["net_expected"] == 74

    # Trip T840677: 9.0km, surge 1.5x -> (25 + 7*6) * 1.5 = 67 * 1.5 = 100.5 -> 101
    res = calculate_trip_expected_payout("completed", distance_km=9.0, surge_multiplier=1.5)
    assert res["gross_fare"] == 101

    # Trip T672899: 7.4km, surge 1.0x -> (25 + 5.4*6) = 25 + 32.4 = 57.4 -> 57
    res = calculate_trip_expected_payout("completed", distance_km=7.4, surge_multiplier=1.0)
    assert res["gross_fare"] == 57


def test_meter_distance_trip():
    # Trip T312538: 5000m (5.0km), surge 1.5x -> (25 + 3*6) * 1.5 = 43 * 1.5 = 64.5 -> 65
    res = calculate_trip_expected_payout("completed", distance_km=5000, surge_multiplier=1.5)
    assert res["gross_fare"] == 65


def test_cancellation_behavior():
    # Rider cancellation -> ₹10 penalty (-10 net)
    res = calculate_trip_expected_payout("cancelled_by_rider", distance_km=5.0, surge_multiplier=1.0)
    assert res["gross_fare"] == 0
    assert res["cancellation_penalty"] == -10
    assert res["net_expected"] == -10

    # Customer cancellation -> ₹0
    res = calculate_trip_expected_payout("cancelled_by_customer", distance_km=5.0, surge_multiplier=1.0)
    assert res["gross_fare"] == 0
    assert res["cancellation_penalty"] == 0
    assert res["net_expected"] == 0


def test_daily_incentive_and_duplicate_trips():
    # 11 trips -> ₹0 incentive
    trips_11 = [f"T{i}" for i in range(11)]
    assert calculate_daily_incentive(trips_11) == 0

    # 12 unique trips -> ₹150 incentive
    trips_12 = [f"T{i}" for i in range(12)]
    assert calculate_daily_incentive(trips_12) == 150

    # 12 trip entries containing duplicate trip IDs (only 11 unique) -> ₹0 incentive
    trips_12_with_dup = [f"T{i}" for i in range(11)] + ["T0"]
    assert calculate_daily_incentive(trips_12_with_dup) == 0


def test_underpayment_calculation():
    # Expected 74, paid 49 -> underpayment 25
    assert calculate_underpayment(expected=74, actual=49) == 25
    # Overpaid or exact -> underpayment 0
    assert calculate_underpayment(expected=50, actual=60) == 0


def test_rider_claimed_amount_isolation():
    """Verify rider text claims (e.g. 5000, 999, 120) never dictate payout amounts."""
    # Even if rider claims 5000 in text message, expected payout is strictly computed from DB trip data
    res = calculate_trip_expected_payout("completed", distance_km=6.0, surge_multiplier=1.5)
    rider_claimed_amount = 5000  # From text "mera payout 5000 kam hai"
    actual_paid = 49  # From database payout_lines

    # Owed amount MUST use computed expected (74), NOT rider_claimed_amount (5000)
    owed_from_policy = calculate_underpayment(res["net_expected"], actual_paid)
    owed_from_claim = calculate_underpayment(rider_claimed_amount, actual_paid)

    assert owed_from_policy == 25
    assert owed_from_policy != owed_from_claim

