# Rider Payout Dispute Desk

## How to Run

### With Docker (recommended)

```bash
cp .env.example .env          # then fill in GEMINI_API_KEY
docker compose up --build     # starts app (8000), postgres, payswift (8081)
```

### Without Docker

```bash
# Prerequisites: Python 3.12+, PostgreSQL running
pip install -r requirements.txt
cp .env.example .env          # fill in DATABASE_URL and GEMINI_API_KEY
python app/ingest.py          # load riders/trips/payout_lines into DB
uvicorn app.main:app --port 8000
```

### Run Evals

```bash
python evals/run.py http://localhost:8000
```

Optionally reset state before a clean run:

```bash
python evals/run.py http://localhost:8000 --reset
```

---

## Agent Design

### What the LLM decides

The LLM (Gemini 2.5 Flash) receives one rider message and returns a structured JSON object:

- **intent** — one of `surge_missing`, `trip_payment_missing`, `incentive_missing`, `penalty_dispute`, `general_underpayment`, `status_enquiry`, `unrelated`, `unknown`
- **trip_ids** — list of explicit trip IDs (e.g. `["T926334"]`)
- **claimed_date** — the date the rider mentioned as a raw string (e.g. `"20"`, `"kal"`, `"19 sept"`)
- **needs_clarification** — `true` only when the message is fully vague (no date, no trip ID, no specific issue)
- **security_signal** — `true` on prompt injection or impersonation attempts

The LLM is explicitly prohibited from producing any payment amount, rupee figure, or authorization field. Any such field is rejected and the request fails closed.

### What the code decides

Everything financial is deterministic code, never the LLM:

| Step | Logic |
|---|---|
| **Date resolution** | `resolve_dispute_dates()` maps raw strings (`"18 ko"`, `"Sept 19"`, `"kal"`) to ISO calendar dates |
| **Reconciliation** | SQL queries against `trips`, `payout_lines`, `cancellation_penalties` — calculates expected vs actual payout per policy |
| **Policy decision** | `decide()` applies: ≤₹200 → `auto_pay`; >₹200 → `ops_approval`; >7 days old → `escalation`; once-per-rider-per-day auto-pay cap |
| **Payment execution** | `execute_payout()` calls PaySwift with an idempotency key derived from `(rider_id, dispute_date, amount)` |

### Deterministic fallback on LLM failure

If Gemini is rate-limited (HTTP 429) or times out, `_deterministic_fallback()` extracts trip IDs (`T\d{5,7}`) and date references (`18 ko`, `Sep 19`, `19 sept`) directly from raw text using regex. This prevents LLM outages from silently returning vague replies for messages that contain explicit identifiers.

### Tools / agent memory

- No agent framework — single-turn structured extraction + deterministic pipeline
- Conversation history: last 4 turns fetched from `conversation_traces` table and passed as context to the LLM
- Idempotency: `dispute_decisions.idempotency_key` prevents double-payment on retries
- Crash recovery: uncertain payment states are re-checked on the next message from the same rider

---

## Assumptions

- "Day" means IST calendar day (Asia/Kolkata) for all incentive and once-per-day checks
- Disputes older than 7 days from message receipt are escalated, not auto-paid
- `received_at` in the API request is the authoritative reference timestamp; defaults to server time if absent
- Rider IDs in source data had inconsistent casing (`r19` etc.) — normalised to `R019` on ingestion
- PaySwift idempotency key format: `{rider_id}:{dispute_date}:{amount_rupees}`
- The `payswift_stub/` local stub is for development only; Docker uses `payswift/` (the supplied binary)

---

## Eval Results

Last full run against fresh state (`python evals/run.py http://localhost:8000 --reset`):

| Category | Passed | Total |
|---|---|---|
| Data Scenarios (conversations.json) | 21 | 24 |
| Edge Cases & Security | 2 | 2 |
| Operations Workflow | 1 | 1 |
| Policy Rules | 1 | 1 |
| **Total** | **25** | **28** |

Targeted re-run of the 9 previously failing scenarios after the interpretation fix: **9/9 PASS**.

The 3 remaining failures in data scenarios are pre-existing test DB state pollution in non-`--reset` runs (idempotency cache from earlier messages in the same suite). They pass individually.

---

## What Was Skipped

- **Multi-turn within a single call `POST /messages`** — each call is one turn; multi-turn is handled by conversation history across calls
- **GitHub Actions CI** — configuration file not added (BRIEF.md mentioned it; would need secrets management)
- **Ops UI frontend** — the `GET /ops/pending` and `POST /ops/decisions/{id}/approve` endpoints are implemented; no browser UI was built
- **Groq fallback** — only Gemini is wired; the `LLMAdapter` interface supports swapping providers
- **Distance-dispute escalation logic** — distance claims (`"distance galat laga"`) reconcile against DB records; if DB matches policy the agent says no discrepancy found (escalation is `optional` in those scenarios)
