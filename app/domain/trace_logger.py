"""Structured Event Trace Logger for Rider Payout Dispute Desk.

Provides persistent, secret-sanitized audit event logging.
Each trace event records:
  - at    : ISO 8601 timestamp string
  - type  : Event category (message_in, tool_call, decision, reply, error)
  - name  : Human-readable step name (e.g. receive_whatsapp_message, llm_interpret, reconcile_dispute, policy_decision, execute_payswift_payout)
  - input : Structured input parameters (sanitized against secrets)
  - output: Structured output results (sanitized against secrets)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# Sensitive keys to redact automatically
_SENSITIVE_KEYS = {
    "api_key", "apikey", "secret", "password", "token", "auth",
    "authorization", "bearer", "credentials", "private_key",
}


def sanitize_data(data: Any) -> Any:
    """Recursively redact secrets and sensitive values from dictionaries and lists."""
    if isinstance(data, dict):
        sanitized = {}
        for key, val in data.items():
            key_lower = str(key).lower()
            if any(s in key_lower for s in _SENSITIVE_KEYS):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = sanitize_data(val)
        return sanitized
    elif isinstance(data, list):
        return [sanitize_data(item) for item in data]
    return data


def log_trace_event(
    conn,
    rider_id: str,
    message_id: Optional[str],
    event_type: str,
    name: str,
    input_data: Optional[Dict[str, Any]] = None,
    output_data: Optional[Dict[str, Any]] = None,
    timestamp: Optional[str] = None,
) -> int:
    """Persist a single audit trace event to Postgres trace_events table.

    Args:
        conn:        Active DB connection.
        rider_id:    Authoritative rider identifier.
        message_id:  WhatsApp wamid identifier (or correlation ID).
        event_type:  Category: 'message_in', 'tool_call', 'decision', 'reply', 'error'.
        name:        Step name: e.g. 'receive_whatsapp_message', 'llm_interpret'.
        input_data:  Step input dict.
        output_data: Step output dict.
        timestamp:   ISO format timestamp string. Defaults to current timestamp.

    Returns:
        Inserted event row ID.
    """
    event_at_str = timestamp or datetime.now().isoformat()
    clean_input = sanitize_data(input_data or {})
    clean_output = sanitize_data(output_data or {})

    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO trace_events
                (rider_id, message_id, event_at, event_type, name, input, output)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
            """,
            (
                rider_id,
                message_id,
                event_at_str,
                event_type,
                name,
                json.dumps(clean_input),
                json.dumps(clean_output),
            ),
        )
        event_id = cur.fetchone()[0]
        conn.commit()
        return event_id
    except Exception as exc:
        conn.rollback()
        logger.error("Failed to log trace event %s (%s): %s", name, event_type, exc)
        return 0


def get_trace_events_for_rider(conn, rider_id: str) -> List[Dict[str, Any]]:
    """Retrieve full chronological sequence of trace events for a rider.

    Returns list of formatted dicts with keys: at, type, name, input, output.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT event_at, event_type, name, input, output, message_id
        FROM trace_events
        WHERE rider_id = %s
        ORDER BY id ASC;
        """,
        (rider_id,),
    )
    rows = cur.fetchall()

    events = []
    for r in rows:
        events.append(
            {
                "at": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]),
                "type": r[1],
                "name": r[2],
                "message_id": r[5],
                "input": r[3],
                "output": r[4],
            }
        )
    return events
