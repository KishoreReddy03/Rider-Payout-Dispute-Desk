"""Deterministic reconciliation layer for Rider Payout Dispute Desk.

Compares actual database payout ledger against policy-calculated expected earnings.
Independently verifiable, purely deterministic, and non-authoritative (does not execute payments).
"""

from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import List, Optional, Dict, Any
from app.domain.policy import (
    calculate_trip_expected_payout,
    calculate_daily_incentive,
    calculate_underpayment,
)


@dataclass
class TripEvaluation:
    trip_id: str
    status: str
    started_at: str
    distance_km: float
    surge_multiplier: float
    expected_gross_fare: int
    expected_penalty: int
    expected_net: int
    actual_paid: int
    underpayment: int
    discrepancy_reason: Optional[str]


@dataclass
class IncentiveEvaluation:
    date: str
    unique_completed_trips_count: int
    incentive_expected: int
    incentive_actual_paid: int
    underpayment: int
    discrepancy_reason: Optional[str]


@dataclass
class ReconciliationResult:
    rider_id: str
    target_date: Optional[str]
    target_trip_id: Optional[str]
    trips_evaluated: List[TripEvaluation]
    incentive_evaluated: Optional[IncentiveEvaluation]
    total_expected_payout: int
    total_actual_paid: int
    calculated_amount_owed: int
    discrepancy_reasons: List[str]
    structured_evidence: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        """Convert result to dictionary representation."""
        return asdict(self)


def reconcile_rider_dispute(
    conn,
    rider_id: str,
    target_date: Optional[str] = None,
    target_trip_id: Optional[str] = None,
) -> ReconciliationResult:
    """Reconcile rider payout records against expected policy calculations.

    Does NOT authorize payment or execute external API calls.
    """
    cur = conn.cursor()
    trips_eval: List[TripEvaluation] = []
    incentive_eval: Optional[IncentiveEvaluation] = None
    discrepancy_reasons: List[str] = []

    # Case 1: Specific Trip ID Dispute
    if target_trip_id:
        cur.execute(
            """
            SELECT trip_id, rider_id, started_at, status, distance_km, surge_multiplier
            FROM raw_trips
            WHERE rider_id = %s AND trip_id = %s;
            """,
            (rider_id, target_trip_id),
        )
        trip_rows = cur.fetchall()

        if not trip_rows:
            # Unknown trip ID case
            reason = f"Trip ID '{target_trip_id}' not found in database records for rider '{rider_id}'."
            discrepancy_reasons.append(reason)
            return ReconciliationResult(
                rider_id=rider_id,
                target_date=target_date,
                target_trip_id=target_trip_id,
                trips_evaluated=[],
                incentive_evaluated=None,
                total_expected_payout=0,
                total_actual_paid=0,
                calculated_amount_owed=0,
                discrepancy_reasons=discrepancy_reasons,
                structured_evidence={"error": reason},
            )

        # Process matching trip
        t = trip_rows[0]
        trip_id, r_id, started_at, status, dist_km, surge = (
            t[0],
            t[1],
            t[2].isoformat(),
            t[3],
            float(t[4]),
            float(t[5]),
        )

        policy_res = calculate_trip_expected_payout(status, dist_km, surge)
        exp_net = policy_res["net_expected"]

        cur.execute(
            """
            SELECT amount FROM raw_payout_lines
            WHERE rider_id = %s AND trip_id = %s;
            """,
            (rider_id, target_trip_id),
        )
        payout_rows = cur.fetchall()
        actual_paid = int(sum(float(p[0]) for p in payout_rows))

        owed = calculate_underpayment(exp_net, actual_paid)
        reason = None

        if len(payout_rows) > 1 and status == "cancelled_by_rider":
            reason = f"Duplicate cancellation penalty deducted ({len(payout_rows)} times, actual: {actual_paid})."
            discrepancy_reasons.append(reason)
        elif not payout_rows and status == "completed":
            reason = f"Missing payout line for completed trip {trip_id} (expected ₹{exp_net})."
            discrepancy_reasons.append(reason)
        elif owed > 0:
            reason = f"Underpaid trip {trip_id}: expected ₹{exp_net}, actual paid ₹{actual_paid} (owed ₹{owed})."
            discrepancy_reasons.append(reason)

        trips_eval.append(
            TripEvaluation(
                trip_id=trip_id,
                status=status,
                started_at=started_at,
                distance_km=dist_km,
                surge_multiplier=surge,
                expected_gross_fare=policy_res["gross_fare"],
                expected_penalty=policy_res["cancellation_penalty"],
                expected_net=exp_net,
                actual_paid=actual_paid,
                underpayment=owed,
                discrepancy_reason=reason,
            )
        )

        total_exp = exp_net
        total_act = actual_paid
        total_owed = max(0, total_exp - total_act)

        return ReconciliationResult(
            rider_id=rider_id,
            target_date=target_date,
            target_trip_id=target_trip_id,
            trips_evaluated=trips_eval,
            incentive_evaluated=None,
            total_expected_payout=total_exp,
            total_actual_paid=total_act,
            calculated_amount_owed=total_owed,
            discrepancy_reasons=discrepancy_reasons,
            structured_evidence={
                "trip_id": target_trip_id,
                "trips": [asdict(te) for te in trips_eval],
            },
        )

    # Case 2: Target Date Dispute (or Date Search)
    if target_date:
        dates_to_check = [target_date]
    else:
        # Get all dates for this rider
        cur.execute(
            """
            SELECT DISTINCT (started_at AT TIME ZONE 'Asia/Kolkata')::date::text
            FROM raw_trips WHERE rider_id = %s
            UNION
            SELECT DISTINCT payout_date::text FROM raw_payout_lines WHERE rider_id = %s;
            """,
            (rider_id, rider_id),
        )
        dates_to_check = [r[0] for r in cur.fetchall() if r[0]]

    total_exp_accum = 0
    total_act_accum = 0

    for dt in dates_to_check:
        cur.execute(
            """
            SELECT trip_id, rider_id, started_at, status, distance_km, surge_multiplier
            FROM raw_trips
            WHERE rider_id = %s AND (started_at AT TIME ZONE 'Asia/Kolkata')::date = %s;
            """,
            (rider_id, dt),
        )
        trips = cur.fetchall()

        completed_trip_ids = [t[0] for t in trips if t[3] == "completed"]

        # Evaluate each trip
        for t in trips:
            t_id, r_id, started_at, status, dist_km, surge = (
                t[0],
                t[1],
                t[2].isoformat(),
                t[3],
                float(t[4]),
                float(t[5]),
            )
            policy_res = calculate_trip_expected_payout(status, dist_km, surge)
            exp_net = policy_res["net_expected"]

            cur.execute(
                """
                SELECT amount FROM raw_payout_lines
                WHERE rider_id = %s AND trip_id = %s;
                """,
                (rider_id, t_id),
            )
            payout_rows = cur.fetchall()
            actual_paid = int(sum(float(p[0]) for p in payout_rows))
            owed = calculate_underpayment(exp_net, actual_paid)
            reason = None

            if len(payout_rows) > 1 and status == "cancelled_by_rider":
                reason = f"Duplicate cancellation penalty on trip {t_id} (paid ₹{actual_paid})."
                discrepancy_reasons.append(reason)
            elif not payout_rows and status == "completed":
                reason = f"Missing payout line for trip {t_id} (expected ₹{exp_net})."
                discrepancy_reasons.append(reason)
            elif owed > 0:
                reason = f"Underpaid trip {t_id}: expected ₹{exp_net}, actual ₹{actual_paid}."
                discrepancy_reasons.append(reason)

            trips_eval.append(
                TripEvaluation(
                    trip_id=t_id,
                    status=status,
                    started_at=started_at,
                    distance_km=dist_km,
                    surge_multiplier=surge,
                    expected_gross_fare=policy_res["gross_fare"],
                    expected_penalty=policy_res["cancellation_penalty"],
                    expected_net=exp_net,
                    actual_paid=actual_paid,
                    underpayment=owed,
                    discrepancy_reason=reason,
                )
            )

            total_exp_accum += exp_net
            total_act_accum += actual_paid

        # Evaluate daily incentive
        inc_expected = calculate_daily_incentive(completed_trip_ids)
        cur.execute(
            """
            SELECT amount FROM raw_payout_lines
            WHERE rider_id = %s AND payout_date = %s AND line_type = 'daily_incentive';
            """,
            (rider_id, dt),
        )
        inc_rows = cur.fetchall()
        inc_actual = int(sum(float(p[0]) for p in inc_rows))
        inc_owed = calculate_underpayment(inc_expected, inc_actual)
        inc_reason = None

        if inc_owed > 0:
            inc_reason = f"Missing daily incentive of ₹{inc_expected} for {len(set(completed_trip_ids))} completed trips on {dt}."
            discrepancy_reasons.append(inc_reason)

        if inc_expected > 0 or inc_actual > 0:
            incentive_eval = IncentiveEvaluation(
                date=dt,
                unique_completed_trips_count=len(set(completed_trip_ids)),
                incentive_expected=inc_expected,
                incentive_actual_paid=inc_actual,
                underpayment=inc_owed,
                discrepancy_reason=inc_reason,
            )

        total_exp_accum += inc_expected
        total_act_accum += inc_actual

    # Strictly enforce that calculated amount owed CANNOT exceed total_exp_accum - total_act_accum
    final_amount_owed = max(0, total_exp_accum - total_act_accum)

    return ReconciliationResult(
        rider_id=rider_id,
        target_date=target_date,
        target_trip_id=target_trip_id,
        trips_evaluated=trips_eval,
        incentive_evaluated=incentive_eval,
        total_expected_payout=total_exp_accum,
        total_actual_paid=total_act_accum,
        calculated_amount_owed=final_amount_owed,
        discrepancy_reasons=discrepancy_reasons,
        structured_evidence={
            "trips_count": len(trips_eval),
            "trips": [asdict(te) for te in trips_eval],
            "incentive": asdict(incentive_eval) if incentive_eval else None,
        },
    )
