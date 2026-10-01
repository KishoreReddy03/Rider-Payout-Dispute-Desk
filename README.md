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

### Model vs Code Responsibility
- **What the Model Decides (LLM Interpreter)**: Structured extraction only — intent (`surge_missing`, `incentive_missing`, etc.), explicit trip IDs, claimed date strings, and clarification flags. The LLM is strictly prohibited from deciding money or payment authorization.
- **What Code Decides (Deterministic Domain)**: Date normalization (e.g. `"18 ko"`, `"Sept 19"` → ISO dates), trip & payout database lookups, exact financial reconciliation, policy threshold evaluation (auto-pay ≤ ₹200/day, Ops approval > ₹200, escalation for > 7 days old), and PaySwift transaction execution with idempotency key locking.
- **Fallback**: On LLM rate-limit/timeout, regex fallback extracts trip IDs (`T\d{5,7}`) and date patterns directly from raw text.

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
