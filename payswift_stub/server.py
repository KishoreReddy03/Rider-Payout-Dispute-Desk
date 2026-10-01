"""Lightweight, standalone PaySwift Sandbox Server.

Runs on port 8081 to emulate the PaySwift Payout API specification for local evaluation.
"""

import uuid
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Header, Response, status
from pydantic import BaseModel, Field
import uvicorn

app = FastAPI(title="PaySwift Sandbox Server")

# In-memory ledger and idempotency store
payouts_ledger: List[Dict[str, Any]] = []
idempotency_store: Dict[str, Dict[str, Any]] = {}


class PayoutRequest(BaseModel):
    rider_id: str
    amount: int
    reference: Optional[str] = ""


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/payouts", status_code=201)
def create_payout(
    payload: PayoutRequest,
    response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    if payload.amount <= 0 or payload.amount > 10000:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"error": "Invalid amount (must be between 1 and 10000)"}

    # Check idempotency
    if idempotency_key and idempotency_key in idempotency_store:
        existing = idempotency_store[idempotency_key]
        response.status_code = status.HTTP_409_CONFLICT
        return {
            "payout_id": existing["payout_id"],
            "rider_id": existing["rider_id"],
            "amount": existing["amount"],
            "status": "already_paid",
        }

    p_id = f"po_{uuid.uuid4().hex[:12]}"
    record = {
        "payout_id": p_id,
        "rider_id": payload.rider_id,
        "amount": payload.amount,
        "reference": payload.reference,
        "status": "SUCCESS",
        "idempotency_key": idempotency_key,
    }

    payouts_ledger.append(record)
    if idempotency_key:
        idempotency_store[idempotency_key] = record

    return {
        "payout_id": p_id,
        "rider_id": payload.rider_id,
        "amount": payload.amount,
        "status": "SUCCESS",
    }


@app.get("/v1/payouts")
def list_payouts(rider_id: Optional[str] = None):
    if rider_id:
        return [p for p in payouts_ledger if p["rider_id"] == rider_id]
    return payouts_ledger


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8081)
