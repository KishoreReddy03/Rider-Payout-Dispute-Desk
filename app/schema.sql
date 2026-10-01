-- Raw source data schema preserving raw structures and duplicate records

CREATE TABLE IF NOT EXISTS raw_riders (
    id SERIAL PRIMARY KEY,
    rider_id VARCHAR(50) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,
    city VARCHAR(50) NOT NULL,
    joined_on DATE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw_trips (
    id SERIAL PRIMARY KEY,
    trip_id VARCHAR(50) NOT NULL,
    rider_id VARCHAR(50) NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    status VARCHAR(50) NOT NULL,
    distance_km NUMERIC(10, 2) NOT NULL,
    surge_multiplier NUMERIC(5, 2) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw_payout_lines (
    id SERIAL PRIMARY KEY,
    line_id VARCHAR(50) NOT NULL UNIQUE,
    payout_date DATE NOT NULL,
    rider_id VARCHAR(50) NOT NULL,
    line_type VARCHAR(50) NOT NULL,
    trip_id VARCHAR(50),
    amount NUMERIC(10, 2) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw_conversations (
    id SERIAL PRIMARY KEY,
    scenario TEXT,
    rider_id VARCHAR(50) NOT NULL,
    turns JSONB NOT NULL,
    expected JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Policy decision records: one row per dispute decision issued.
-- Used to enforce the once-per-rider-per-day auto-pay rule deterministically.
CREATE TABLE IF NOT EXISTS dispute_decisions (
    id              SERIAL PRIMARY KEY,
    rider_id        VARCHAR(50)  NOT NULL,
    dispute_date    DATE         NOT NULL,
    decision_type   VARCHAR(30)  NOT NULL,
    amount          INTEGER      NOT NULL DEFAULT 0,
    reason          TEXT         DEFAULT NULL,
    status          VARCHAR(30)  NOT NULL DEFAULT 'pending',
    message_id      VARCHAR(100),
    idempotency_key VARCHAR(120) UNIQUE,
    payout_id       VARCHAR(100),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_decision_type CHECK (decision_type IN ('no_payment', 'auto_pay', 'ops_approval', 'escalation')),
    CONSTRAINT chk_amount_non_negative CHECK (amount >= 0),
    CONSTRAINT chk_status CHECK (status IN ('pending', 'executed', 'approved', 'rejected', 'uncertain', 'escalated'))
);

-- Conversation audit traces: log of all turn processing steps for GET /trace/{rider_id}
CREATE TABLE IF NOT EXISTS conversation_traces (
    id SERIAL PRIMARY KEY,
    rider_id VARCHAR(50) NOT NULL,
    message_id VARCHAR(100) UNIQUE,
    turn_input JSONB,
    llm_output JSONB,
    reconciliation_evidence JSONB,
    policy_decision JSONB,
    payment_result JSONB,
    agent_reply TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Payment attempts audit log: tracks all PaySwift API attempts & idempotency retry identities
CREATE TABLE IF NOT EXISTS payment_attempts (
    id SERIAL PRIMARY KEY,
    decision_id INTEGER REFERENCES dispute_decisions(id),
    rider_id VARCHAR(50) NOT NULL,
    amount INTEGER NOT NULL,
    idempotency_key VARCHAR(120) NOT NULL,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(30) NOT NULL,
    http_code INTEGER,
    payout_id VARCHAR(100),
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Persistent audit trace events for GET /trace/{rider_id}
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

