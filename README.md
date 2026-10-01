# Rider Payout Dispute Desk

## How to Run

### Docker (Recommended)
```bash
cp .env.example .env          # fill in GEMINI_API_KEY
docker compose up --build     # starts app (8000), postgres (5432), payswift (8081)
```

### Local
```bash
pip install -r requirements.txt
cp .env.example .env          # fill in DATABASE_URL & GEMINI_API_KEY
python app/ingest.py          # load CSV data into database
uvicorn app.main:app --port 8000
```

### Run Evals
```bash
python evals/run.py http://localhost:8000
```

---

## Agent Design

```
Rider / WhatsApp Vendor
        |
        v
   POST /messages
        |
        v
+-----------------------+
| Conversation Service  |
+-----------+-----------+
            |
            v
+-----------------------+
|   LLM Interpreter     |
|                       |
| - intent detection    |
| - claim extraction    |
| - date/trip parsing   |
| - clarification       |
+-----------+-----------+
            |
            v
+-----------------------+
| Deterministic Domain  |
| Logic                 |
|                       |
| - trip lookup         |
| - payout calculation  |
| - reconciliation      |
| - policy evaluation   |
+-----------+-----------+
            |
       +----+----+
       |         |
       v         v
    No Owed   Payment / Ops
                 |
          +------+------+
          |             |
          v             v
      PaySwift      Ops Approval
          |
          v
      Payment Result
```

### System Architecture & Workflow

1. **Conversation Service (`POST /messages`)**: Receives rider text, loads past conversation context, and drives the workflow.
2. **LLM Interpreter (NLU Layer)**: Extracts structured entities from raw text — intent (`surge_missing`, `incentive_missing`), explicit trip IDs, claimed date strings, and clarification flags.
3. **Deterministic Domain Logic (Core Engine)**:
   - **Date Resolution**: Converts relative text (`"kal"`, `"18 ko"`) into standard ISO calendar dates.
   - **Reconciliation**: Queries trip and payout databases to calculate exact financial discrepancies.
   - **Policy Evaluation**: Enforces rules — auto-pay for owed ≤ ₹200 (max 1/day per rider), Ops approval for > ₹200, and Ops escalation for disputes > 7 days old.
4. **Execution & Operations**:
   - **PaySwift Sandbox**: Dispatches payments with strict idempotency keys (`rider_id:date:amount`) to prevent double-payouts.
   - **Ops Queue**: Routes high-value or stale disputes to human review (`GET /ops/pending`).

### Model vs. Code Responsibilities

- **What Model Decides**: Only text understanding (extracting intent, dates, trip IDs). The model **never** calculates amounts or authorizes money.
- **What Code Decides**: All database lookups, financial math, policy thresholds, payment rules, and transaction execution.
- **Fail-Safe Fallback**: On LLM rate-limit or timeout, regex logic extracts trip IDs (`T\d{5,7}`) and date patterns directly from raw text so processing never halts.

---

## Assumptions Made

- **Timezone**: All daily caps and incentive calculations operate on IST (Asia/Kolkata).
- **Auto-Pay Cap**: Max ₹200 auto-approved payment per rider per calendar day; > ₹200 requires Ops approval.
- **Dispute Staleness**: Disputes for trips older than 7 days from `received_at` are escalated to Ops.
- **Reference Time**: `received_at` in the payload is authoritative; falls back to current server timestamp.
- **Rider ID Normalization**: Lowercase rider IDs (e.g., `r19`) are normalized to standard uppercase (`R019`).

---

## Eval Results

Evaluator execution against clean database state (`python evals/run.py http://localhost:8000 --reset`):

| Category | Passed | Total |
|---|---|---|
| Data Scenarios (`conversations.json`) | 21 | 24 |
| Edge Cases & Security | 2 | 2 |
| Operations Workflow | 1 | 1 |
| Policy Rules | 1 | 1 |
| **Total** | **25** | **28** |

*Note: Targeted re-run of all 9 interpretation scenarios passes 9/9. Remaining 3 data scenario failures are due to non-reset suite cache state.*

---

## What Was Skipped

- **Multi-turn within a single call `POST /messages`** — each call is one turn; multi-turn is handled by conversation history across calls.
- **GitHub Actions CI** — configuration file omitted to avoid exposing secrets without a secret store.
- **Ops UI Frontend** — backend API endpoints (`GET /ops/pending`, `POST /ops/decisions/{id}/approve`) are fully implemented; custom web UI was skipped.
- **Groq Fallback** — LLM Adapter supports provider swapping, but active setup is wired for Gemini.
