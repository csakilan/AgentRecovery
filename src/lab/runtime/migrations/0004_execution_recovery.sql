-- Additive protocol-v1 metadata. Legacy records deliberately remain unversioned.
ALTER TABLE runs
    ADD COLUMN resume_version INTEGER CHECK (resume_version = 1),
    ADD COLUMN episode_spec_json JSONB,
    ADD COLUMN worker_input_json JSONB,
    ADD COLUMN execution_config_json JSONB,
    ADD COLUMN initial_messages_json JSONB,
    ADD COLUMN model_descriptor_json JSONB,
    ADD COLUMN trace_carrier_json JSONB,
    ADD COLUMN setup_state TEXT CHECK (setup_state IN ('pending', 'ready')),
    ADD COLUMN claim_owner TEXT,
    ADD COLUMN lease_expires_at TIMESTAMPTZ,
    ADD COLUMN version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    ADD CHECK ((claim_owner IS NULL) = (lease_expires_at IS NULL)),
    ADD CHECK (resume_version IS NULL OR
        (episode_spec_json IS NOT NULL AND worker_input_json IS NOT NULL
         AND execution_config_json IS NOT NULL AND initial_messages_json IS NOT NULL
         AND model_descriptor_json IS NOT NULL AND setup_state IS NOT NULL
         AND started_at IS NOT NULL AND deadline_at IS NOT NULL));

CREATE TABLE operations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    tenant TEXT NOT NULL, namespace TEXT NOT NULL, task_id TEXT NOT NULL,
    effective_key TEXT NOT NULL,
    requested_args_json JSONB NOT NULL, args_hash TEXT NOT NULL, config_json JSONB NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('READY','IN_FLIGHT','UNKNOWN',
        'CONFIRMED_NO_EFFECT','CONFIRMED_SUCCESS','TERMINAL_FAILURE','UNRESOLVED')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    claim_owner TEXT, lease_expires_at TIMESTAMPTZ,
    next_check_at TIMESTAMPTZ,
    status_check_count INTEGER NOT NULL DEFAULT 0 CHECK (status_check_count >= 0),
    deadline_at TIMESTAMPTZ NOT NULL,
    last_evidence_json JSONB, terminal_outcome_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), finished_at TIMESTAMPTZ,
    UNIQUE (tenant,run_id,task_id), UNIQUE (namespace,effective_key),
    CHECK ((claim_owner IS NULL) = (lease_expires_at IS NULL))
);
CREATE INDEX operations_due_idx ON operations(next_check_at,deadline_at)
    WHERE state IN ('READY','IN_FLIGHT','UNKNOWN','CONFIRMED_NO_EFFECT');
CREATE TABLE attempts (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    fence_version BIGINT NOT NULL, owner TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), finished_at TIMESTAMPTZ,
    http_status INTEGER, response_json JSONB, error_type TEXT,
    UNIQUE (operation_id,attempt_no)
);
CREATE TABLE recovery_events (
    id BIGSERIAL PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL, fence_version BIGINT NOT NULL,
    details_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE outbox (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), delivered_at TIMESTAMPTZ,
    delivery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
    claim_owner TEXT, lease_expires_at TIMESTAMPTZ,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    CHECK ((claim_owner IS NULL) = (lease_expires_at IS NULL))
);
CREATE INDEX outbox_due_idx ON outbox(available_at) WHERE delivered_at IS NULL;
CREATE TABLE model_invocations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    delivery_version BIGINT NOT NULL, turn INTEGER NOT NULL,
    provider_model_id TEXT NOT NULL, request_json JSONB NOT NULL,
    response_json JSONB, tool_calls_json JSONB, input_tokens INTEGER, output_tokens INTEGER,
    latency_ms DOUBLE PRECISION, error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), finished_at TIMESTAMPTZ
);
CREATE INDEX model_invocations_turn_idx ON model_invocations(run_id,turn,started_at);
ALTER TABLE tool_events ADD COLUMN execution_json JSONB,
    ADD COLUMN model_invocation_id TEXT REFERENCES model_invocations(id);
CREATE TABLE run_deliveries (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    generation BIGINT NOT NULL DEFAULT 1,
    acknowledged_generation BIGINT NOT NULL DEFAULT 0,
    claim_owner TEXT, lease_expires_at TIMESTAMPTZ,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    delivery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
    CHECK (0 <= acknowledged_generation AND acknowledged_generation <= generation),
    CHECK ((claim_owner IS NULL) = (lease_expires_at IS NULL))
);
CREATE INDEX run_deliveries_due_idx ON run_deliveries(available_at)
    WHERE acknowledged_generation < generation;
GRANT SELECT,INSERT,UPDATE,DELETE ON operations,attempts,recovery_events,outbox,
    model_invocations,run_deliveries TO runtime_svc;
GRANT USAGE,SELECT ON SEQUENCE recovery_events_id_seq TO runtime_svc;
