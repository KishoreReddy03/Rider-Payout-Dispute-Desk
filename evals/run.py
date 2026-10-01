#!/usr/bin/env python3
"""Runner entrypoint for Black-Box Evaluator suite.

Usage:
    python evals/run.py http://localhost:8000 [http://localhost:8081] [--reset]

Flags:
    --reset   Flush conversation_traces and dispute_decisions before running
              so that idempotency cache from previous runs does not trap the suite.
              Also re-runs data ingestion to normalise raw source data.
"""

import sys
import os
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Add workspace directory to python path if needed
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from evals.client import PublicAPIClient
from evals.evaluator import BlackBoxEvaluator


def reset_state(base_url: str):
    """Call backend reset endpoint or flush DB tables directly."""
    import psycopg2
    from app.config import settings
    from app.ingest import run_ingestion

    print("  Re-ingesting source data (normalises rider IDs)...")
    summary = run_ingestion()
    print(f"  Ingestion done: {summary}")

    conn = psycopg2.connect(
        settings.database_url,
        connect_timeout=3,
        options="-c statement_timeout=5000 -c lock_timeout=3000",
    )
    cur = conn.cursor()
    print("  Flushing operational tables (traces, decisions, payments)...")
    cur.execute("TRUNCATE TABLE conversation_traces RESTART IDENTITY CASCADE;")
    cur.execute("TRUNCATE TABLE dispute_decisions RESTART IDENTITY CASCADE;")
    cur.execute("TRUNCATE TABLE payment_attempts RESTART IDENTITY CASCADE;")
    cur.execute("TRUNCATE TABLE trace_events RESTART IDENTITY CASCADE;")
    conn.commit()
    conn.close()
    print("  State reset complete.\n")


def main():
    args = sys.argv[1:]
    base_url = "http://localhost:8000"
    payswift_url = None
    do_reset = False
    http_args = []

    for arg in args:
        if arg == "--reset":
            do_reset = True
        elif arg.startswith("http"):
            http_args.append(arg)

    if len(http_args) >= 1:
        base_url = http_args[0]
    if len(http_args) >= 2:
        payswift_url = http_args[1]

    print(f"Connecting evaluator to base service: {base_url}")
    client = PublicAPIClient(base_url=base_url, payswift_url=payswift_url)

    # Health Check
    health = client.get_health()
    if health.get("status") != "ok":
        print(f"ERROR: Base URL {base_url} health check failed: {health}")
        sys.exit(1)

    print(f"Backend connected successfully. PaySwift URL: {client.payswift_url}")

    if do_reset:
        print("\nResetting state before evaluation run...")
        reset_state(base_url)

    scenarios_file = os.path.join(os.path.dirname(__file__), "..", "data", "conversations.json")
    # Generate a run-unique suffix so message_ids bypass idempotency cache on re-runs
    run_tag = str(int(time.time()))
    evaluator = BlackBoxEvaluator(client=client, run_tag=run_tag)

    results = evaluator.run_all(scenarios_file)

    print("\n" + "=" * 65)
    print("                      EVALUATION RESULTS                       ")
    print("=" * 65)

    passed_count = 0
    failed_count = 0

    for res in results:
        status_str = "[PASS]" if res.passed else "[FAIL]"
        print(f"\n{status_str} {res.name} ({res.category})")
        
        for log_line in res.logs:
            print(f"    -> {log_line}")

        if not res.passed:
            failed_count += 1
            for err in res.failures:
                print(f"    [!] Failure: {err}")
        else:
            passed_count += 1


    total = len(results)
    pass_rate = (passed_count / total * 100) if total > 0 else 0

    print("\n" + "=" * 65)
    print(f"SUMMARY: Total Scenarios: {total} | Passed: {passed_count} | Failed: {failed_count} | Pass Rate: {pass_rate:.1f}%")
    print("=" * 65 + "\n")

    if failed_count > 0:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
