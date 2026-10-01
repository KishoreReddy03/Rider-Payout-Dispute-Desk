#!/usr/bin/env python3
"""Check specific trip IDs and payout lines in raw DB tables."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import psycopg2
from app.config import settings

conn = psycopg2.connect(settings.database_url)
cur = conn.cursor()

# Check specific trip IDs referenced in test scenarios
trip_ids = ["T840677","T312538","T926334","T672899","T795007","T206956","T980582",
            "T252921","T482410","T502951","T604546","T849302","T990831"]
print("=== Trip ID Lookup ===")
for tid in trip_ids:
    cur.execute("SELECT trip_id, rider_id, started_at, status, surge_multiplier FROM raw_trips WHERE trip_id=%s", (tid,))
    row = cur.fetchone()
    if row:
        print(f"  FOUND {tid}: rider={row[1]}, date={row[2]}, status={row[3]}, surge={row[4]}")
    else:
        print(f"  MISSING {tid}")

# Check payout lines for key riders
print("\n=== Payout Lines for Key Riders ===")
for rider_id in ["R007", "R031", "R003", "R005", "R014", "R016", "R024", "R026", "R033", "R034", "R035"]:
    cur.execute("SELECT COUNT(*) FROM raw_payout_lines WHERE rider_id=%s", (rider_id,))
    count = cur.fetchone()[0]
    cur.execute("SELECT payout_date, amount FROM raw_payout_lines WHERE rider_id=%s ORDER BY payout_date LIMIT 3", (rider_id,))
    rows = cur.fetchall()
    print(f"  {rider_id}: {count} payout lines, samples={rows}")

# Check riders
print("\n=== Rider List ===")
cur.execute("SELECT rider_id FROM raw_riders ORDER BY rider_id")
riders = [r[0] for r in cur.fetchall()]
print("  Riders:", riders)

conn.close()
