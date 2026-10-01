#!/usr/bin/env python3
"""DB inspection script for evaluator debugging."""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import psycopg2
from app.config import settings

conn = psycopg2.connect(settings.database_url)
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM trips")
print("Total trips:", cur.fetchone()[0])

cur.execute("SELECT COUNT(*) FROM riders")
print("Total riders:", cur.fetchone()[0])

cur.execute("SELECT DISTINCT rider_id FROM trips ORDER BY rider_id")
rider_ids = [r[0] for r in cur.fetchall()]
print("Rider IDs with trips:", rider_ids)

trip_ids = ["T840677", "T312538", "T926334", "T672899", "T795007", "T206956", "T980582", "T252921", "T482410", "T502951", "T604546", "T849302"]
for tid in trip_ids:
    cur.execute("SELECT trip_id, rider_id, started_at, status, surge_multiplier, base_fare, actual_payout FROM trips WHERE trip_id=%s", (tid,))
    row = cur.fetchone()
    if row:
        print(f"  FOUND trip {tid}: rider={row[1]}, date={row[2]}, status={row[3]}, surge={row[4]}, base={row[5]}, paid={row[6]}")
    else:
        print(f"  MISSING trip {tid}")

# Check all trips in DB
cur.execute("SELECT trip_id, rider_id, started_at FROM trips ORDER BY rider_id, started_at LIMIT 30")
print("\nAll trips (first 30):")
for row in cur.fetchall():
    print(f"  {row[0]} | {row[1]} | {row[2]}")

conn.close()
