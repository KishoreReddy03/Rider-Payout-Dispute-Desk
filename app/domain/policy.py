"""Deterministic policy and payout calculation engine.

Pure Python logic without external API or LLM dependencies.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Union, Sequence, Set

BASE_FARE = Decimal("25")
KM_RATE = Decimal("6")
INCENTIVE_THRESHOLD = 12
INCENTIVE_AMOUNT = 150
RIDER_CANCELLATION_PENALTY = -10


def normalize_distance(distance_km: Union[float, str, Decimal]) -> Decimal:
    """Normalize trip distance.

    Convert meter representations (> 100) to kilometers.
    """
    dist = Decimal(str(distance_km))
    if dist >= Decimal("100"):
        return dist / Decimal("1000")
    return dist


def round_half_up(amount: Decimal) -> int:
    """Round a decimal amount to the nearest integer, with 0.5 rounding up."""
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_trip_expected_payout(
    status: str,
    distance_km: Union[float, str, Decimal],
    surge_multiplier: Union[float, str, Decimal],
) -> Dict[str, int]:
    """Calculate the expected gross fare, cancellation penalty, and net expected payout for a single trip.

    Policy:
    - Completed trip: ₹25 base + ₹6 for each km after the first 2 km.
      Surge multiplier applies to the entire trip fare (base + distance).
      Round to nearest rupee, 0.5 rounds up.
    - Cancelled by rider: ₹10 penalty (-10 net).
    - Cancelled by customer: ₹0.
    """
    if status == "completed":
        dist = normalize_distance(distance_km)
        extra_km = max(Decimal("0"), dist - Decimal("2.0"))
        base_distance_fare = BASE_FARE + (extra_km * KM_RATE)
        surge = Decimal(str(surge_multiplier))
        gross_fare = round_half_up(base_distance_fare * surge)
        return {
            "gross_fare": gross_fare,
            "cancellation_penalty": 0,
            "net_expected": gross_fare,
        }
    elif status == "cancelled_by_rider":
        return {
            "gross_fare": 0,
            "cancellation_penalty": RIDER_CANCELLATION_PENALTY,
            "net_expected": RIDER_CANCELLATION_PENALTY,
        }
    elif status == "cancelled_by_customer":
        return {
            "gross_fare": 0,
            "cancellation_penalty": 0,
            "net_expected": 0,
        }
    else:
        return {
            "gross_fare": 0,
            "cancellation_penalty": 0,
            "net_expected": 0,
        }


def calculate_daily_incentive(completed_trip_ids: Sequence[str]) -> int:
    """Calculate daily incentive based on unique completed trip IDs for an IST calendar day.

    Policy: ₹150 daily incentive for 12 or more unique completed trips in a day.
    """
    unique_completed_count = len(set(completed_trip_ids))
    if unique_completed_count >= INCENTIVE_THRESHOLD:
        return INCENTIVE_AMOUNT
    return 0


def calculate_underpayment(expected: int, actual: int) -> int:
    """Calculate underpaid amount owed to rider. Negative values default to 0."""
    return max(0, expected - actual)
