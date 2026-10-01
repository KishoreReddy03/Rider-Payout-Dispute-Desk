"""Data ingestion module for QuickDrop source data."""

import csv
import json
import os
from pathlib import Path
import psycopg2
from psycopg2.extras import execute_values
from app.config import settings

DATA_DIR = Path(__file__).parent.parent / "data"


def init_db(conn):
    """Execute schema.sql to initialize raw database tables and apply migrations."""
    schema_path = Path(__file__).parent / "schema.sql"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()
    with conn.cursor() as cur:
        cur.execute(schema_sql)
        cur.execute(
            """
            ALTER TABLE dispute_decisions ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(120);
            ALTER TABLE dispute_decisions ADD COLUMN IF NOT EXISTS payout_id VARCHAR(100);
            ALTER TABLE dispute_decisions DROP CONSTRAINT IF EXISTS chk_status;
            ALTER TABLE dispute_decisions ADD CONSTRAINT chk_status CHECK (status IN ('pending', 'executed', 'approved', 'rejected', 'uncertain', 'escalated'));
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'uq_conversation_traces_message_id'
                ) THEN
                    ALTER TABLE conversation_traces ADD CONSTRAINT uq_conversation_traces_message_id UNIQUE (message_id);
                END IF;
            END $$;
            CREATE TABLE IF NOT EXISTS trace_events (
                id SERIAL PRIMARY KEY,
                rider_id VARCHAR(50) NOT NULL,
                message_id VARCHAR(100),
                event_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                event_type VARCHAR(50) NOT NULL,
                name VARCHAR(100) NOT NULL,
                input JSONB NOT NULL DEFAULT '{}'::jsonb,
                output JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );

            """
        )
    conn.commit()



def ingest_riders(conn, riders_path: Path) -> int:
    """Ingest raw riders.csv dataset."""
    with open(riders_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        records = [(r["rider_id"], r["name"], r["city"], r["joined_on"]) for r in reader]

    query = """
    INSERT INTO raw_riders (rider_id, name, city, joined_on)
    VALUES %s
    ON CONFLICT (rider_id) DO UPDATE SET
        name = EXCLUDED.name,
        city = EXCLUDED.city,
        joined_on = EXCLUDED.joined_on;
    """
    with conn.cursor() as cur:
        execute_values(cur, query, records)
    conn.commit()
    return len(records)


def _normalize_rider_id(rider_id: str) -> str:
    """Normalize short rider IDs like 'R7' to canonical 'R007' format."""
    if rider_id.startswith("R") and rider_id[1:].isdigit():
        return f"R{int(rider_id[1:]):03d}"
    return rider_id


def ingest_trips(conn, trips_path: Path) -> int:
    """Ingest raw trips.csv dataset, preserving non-unique trip_id duplicate rows."""
    with open(trips_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        records = [
            (
                r["trip_id"],
                _normalize_rider_id(r["rider_id"]),
                r["started_at"],
                r["status"],
                float(r["distance_km"]),
                float(r["surge_multiplier"]),
            )
            for r in reader
        ]

    # Truncate raw_trips prior to re-ingestion to preserve exact duplicates without duplication on re-runs
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE raw_trips RESTART IDENTITY;")
        query = """
        INSERT INTO raw_trips (trip_id, rider_id, started_at, status, distance_km, surge_multiplier)
        VALUES %s;
        """
        execute_values(cur, query, records)
    conn.commit()
    return len(records)


def ingest_payout_lines(conn, payout_lines_path: Path) -> int:
    """Ingest raw payout_lines.csv dataset."""
    with open(payout_lines_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        records = [
            (
                r["line_id"],
                r["payout_date"],
                r["rider_id"],
                r["line_type"],
                r["trip_id"] if r["trip_id"] != "" else None,
                float(r["amount"]),
            )
            for r in reader
        ]

    query = """
    INSERT INTO raw_payout_lines (line_id, payout_date, rider_id, line_type, trip_id, amount)
    VALUES %s
    ON CONFLICT (line_id) DO UPDATE SET
        payout_date = EXCLUDED.payout_date,
        rider_id = EXCLUDED.rider_id,
        line_type = EXCLUDED.line_type,
        trip_id = EXCLUDED.trip_id,
        amount = EXCLUDED.amount;
    """
    with conn.cursor() as cur:
        execute_values(cur, query, records)
    conn.commit()
    return len(records)


def ingest_conversations(conn, conv_path: Path) -> int:
    """Ingest raw conversations.json dataset."""
    with open(conv_path, "r", encoding="utf-8") as f:
        scenarios = json.load(f)

    records = [
        (
            s.get("scenario"),
            s["rider_id"],
            json.dumps(s["turns"]),
            json.dumps(s["expected"]),
        )
        for s in scenarios
    ]

    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE raw_conversations RESTART IDENTITY;")
        query = """
        INSERT INTO raw_conversations (scenario, rider_id, turns, expected)
        VALUES %s;
        """
        execute_values(cur, query, records)
    conn.commit()
    return len(records)


def run_ingestion(db_url: str = None) -> dict:
    """Run ingestion for all datasets into PostgreSQL."""
    target_url = db_url or settings.database_url
    conn = psycopg2.connect(target_url)
    try:
        init_db(conn)
        riders_count = ingest_riders(conn, DATA_DIR / "riders.csv")
        trips_count = ingest_trips(conn, DATA_DIR / "trips.csv")
        payouts_count = ingest_payout_lines(conn, DATA_DIR / "payout_lines.csv")
        convs_count = ingest_conversations(conn, DATA_DIR / "conversations.json")

        summary = {
            "riders": riders_count,
            "trips": trips_count,
            "payout_lines": payouts_count,
            "conversations": convs_count,
        }
        return summary
    finally:
        conn.close()


if __name__ == "__main__":
    results = run_ingestion()
    print("Ingestion complete:", results)
