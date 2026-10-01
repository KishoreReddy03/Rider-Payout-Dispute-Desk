#!/usr/bin/env python3
"""Check DB schema."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import psycopg2
from app.config import settings

conn = psycopg2.connect(settings.database_url)
cur = conn.cursor()

cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
tables = [r[0] for r in cur.fetchall()]
print("Tables:", tables)

for table in tables:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    count = cur.fetchone()[0]
    print(f"  {table}: {count} rows")

# Check specific trip IDs in the right table
trip_table = None
for t in tables:
    if "trip" in t.lower() or "order" in t.lower() or "delivery" in t.lower():
        trip_table = t
        break

if trip_table:
    cur.execute(f"SELECT * FROM {trip_table} LIMIT 3")
    cols = [desc[0] for desc in cur.description]
    rows = cur.fetchall()
    print(f"\n{trip_table} columns: {cols}")
    for row in rows:
        print(" ", dict(zip(cols, row)))

conn.close()
