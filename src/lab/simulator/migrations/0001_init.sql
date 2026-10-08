CREATE TABLE sim_namespaces (
    namespace     TEXT PRIMARY KEY,
    profile       TEXT NOT NULL CHECK (profile IN ('compat', 'controlled')),
    fault_plan    JSONB NOT NULL,
    charge_calls  INTEGER NOT NULL DEFAULT 0,
    status_calls  INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per committed effect. The unique constraint is the idempotency record:
-- committing the effect and claiming the key are the same atomic insert.
CREATE TABLE charges (
    id               BIGSERIAL PRIMARY KEY,
    namespace        TEXT NOT NULL REFERENCES sim_namespaces (namespace) ON DELETE CASCADE,
    idempotency_key  TEXT NOT NULL,
    order_id         TEXT,
    amount           NUMERIC NOT NULL,
    currency         TEXT,
    args_hash        TEXT NOT NULL,
    call_index       INTEGER NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (namespace, idempotency_key)
);

CREATE TABLE call_log (
    id               BIGSERIAL PRIMARY KEY,
    namespace        TEXT NOT NULL REFERENCES sim_namespaces (namespace) ON DELETE CASCADE,
    endpoint         TEXT NOT NULL CHECK (endpoint IN ('charge', 'status')),
    call_index       INTEGER,
    idempotency_key  TEXT,
    request          JSONB NOT NULL,
    http_status      INTEGER NOT NULL,
    response         JSONB NOT NULL,
    outcome          TEXT NOT NULL,
    server_ms        DOUBLE PRECISION NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX call_log_namespace_idx ON call_log (namespace, id);

CREATE TABLE barriers (
    namespace    TEXT NOT NULL REFERENCES sim_namespaces (namespace) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    reached_at   TIMESTAMPTZ,
    released_at  TIMESTAMPTZ,
    PRIMARY KEY (namespace, name)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON sim_namespaces, charges, call_log, barriers
    TO payment_svc;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO payment_svc;
GRANT SELECT ON sim_namespaces, charges, call_log TO ledger_reader;
