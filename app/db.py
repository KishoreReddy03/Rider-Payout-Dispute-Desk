"""Database connection and readiness management."""

import psycopg2
from app.config import settings


def check_db_connection() -> bool:
    """Verify database connectivity by running a simple query."""
    try:
        conn = psycopg2.connect(settings.database_url, connect_timeout=3)
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1;")
            result = cursor.fetchone()
        conn.close()
        return result is not None and result[0] == 1
    except Exception as e:
        print(f"Database connection error: {e}")
        return False
