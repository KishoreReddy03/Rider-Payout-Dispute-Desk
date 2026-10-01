"""FastAPI Application Entrypoint for Rider Payout Dispute Desk.

Exposes required endpoints:
  - POST /messages           : Intake incoming WhatsApp message, process via orchestrator, return reply
  - GET  /trace/{rider_id}   : Chronological audit trace of all agent actions for a rider
  - GET  /ops/pending        : List of decisions pending ops approval or escalation
  - GET  /health             : Service health check
"""

from typing import Optional, Dict, Any, List
import psycopg2
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field, field_validator

import os
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from app.config import settings
from app.db import check_db_connection
from app.domain.orchestrator import (
    process_rider_message,
    get_pending_ops,
    approve_ops_decision,
    reject_ops_decision,
    escalate_ops_decision,
    OpsWorkflowError,
)
from app.domain.trace_logger import get_trace_events_for_rider

from app.ingest import run_ingestion

app = FastAPI(
    title="Rider Payout Dispute Desk",
    description="Assistant for resolving rider payout disputes",
    version="0.1.0",
)


@app.on_event("startup")
def on_startup():
    """Ensure database schema is initialized and dataset is seeded on startup."""
    try:
        run_ingestion()
    except Exception as exc:
        print(f"Warning: startup db ingestion failed or skipped: {exc}")


static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")



@app.get("/", response_class=HTMLResponse)
@app.get("/ops", response_class=HTMLResponse)
def get_ops_portal():
    """Serve the minimal operations interface for reviewing pending approvals & escalations."""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>Ops Dashboard Interface</h1>")



class MessagePayload(BaseModel):
    message_id: str = Field(..., description="WhatsApp wamid identifier")
    rider_id: str = Field(..., description="Authoritative QuickDrop rider identifier (e.g. R003)")
    text: str = Field(..., description="Message text from rider")
    received_at: Optional[str] = Field(None, description="Message timestamp in ISO format")

    @field_validator("message_id", "rider_id", "text")
    @classmethod
    def validate_non_empty(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"Field '{info.field_name}' must not be empty")
        return v.strip()


class MessageResponse(BaseModel):
    reply: str


class OpsActionPayload(BaseModel):
    reason: Optional[str] = Field(None, description="Optional ops reason or notes")


def get_db_connection():
    """Create a new PostgreSQL database connection with strict timeouts."""
    try:
        return psycopg2.connect(
            settings.database_url,
            connect_timeout=3,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database connection error: {exc}",
        )


@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    """Health check endpoint required for service readiness verification."""
    db_ok = check_db_connection()
    if not db_ok:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unhealthy", "database": "disconnected"},
        )
    return {
        "status": "ok",
        "database": "connected",
        "payswift_url": settings.payswift_base_url,
    }


@app.post("/messages", response_model=MessageResponse, status_code=status.HTTP_200_OK)
def handle_incoming_message(payload: MessagePayload):
    """Process an incoming rider message through the deterministic orchestration layer."""
    conn = get_db_connection()
    try:
        result = process_rider_message(
            conn=conn,
            rider_id=payload.rider_id,
            text=payload.text,
            message_id=payload.message_id,
            received_at=payload.received_at,
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing message: {exc}",
        )
    finally:
        conn.close()


@app.get("/trace/{rider_id}")
def get_rider_trace(rider_id: str):
    """Retrieve full chronological JSON list of trace steps for a rider."""
    if not rider_id or not rider_id.strip():
        raise HTTPException(status_code=400, detail="rider_id cannot be empty")
    conn = get_db_connection()
    try:
        events = get_trace_events_for_rider(conn, rider_id.strip())
        return events
    finally:
        conn.close()


@app.get("/ops/pending")
def list_pending_ops_actions():
    """List decisions waiting for ops team approval or escalation."""
    conn = get_db_connection()
    try:
        pending = get_pending_ops(conn)
        return pending
    finally:
        conn.close()


@app.post("/ops/approve/{decision_id}")
@app.post("/ops/decisions/{decision_id}/approve")
def approve_pending_decision(decision_id: int):
    """Approve a pending ops decision and execute payout via backend revalidation."""
    conn = get_db_connection()
    try:
        res = approve_ops_decision(conn, decision_id)
        return res
    except OpsWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error approving decision: {exc}")
    finally:
        conn.close()


@app.post("/ops/reject/{decision_id}")
@app.post("/ops/decisions/{decision_id}/reject")
def reject_pending_decision(decision_id: int, payload: Optional[OpsActionPayload] = None):
    """Reject a pending ops decision without moving money."""
    conn = get_db_connection()
    try:
        reason = payload.reason if payload else None
        res = reject_ops_decision(conn, decision_id, reason=reason)
        return res
    except OpsWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error rejecting decision: {exc}")
    finally:
        conn.close()


@app.post("/ops/escalate/{decision_id}")
@app.post("/ops/decisions/{decision_id}/escalate")
def escalate_pending_decision(decision_id: int, payload: Optional[OpsActionPayload] = None):
    """Escalate a pending ops decision without moving money automatically."""
    conn = get_db_connection()
    try:
        reason = payload.reason if payload else None
        res = escalate_ops_decision(conn, decision_id, reason=reason)
        return res
    except OpsWorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error escalating decision: {exc}")
    finally:
        conn.close()

