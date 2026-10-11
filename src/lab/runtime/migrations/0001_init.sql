CREATE TABLE experiments (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    manifest_sha256  TEXT NOT NULL,
    config_json      JSONB NOT NULL,
    frozen_at        TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE runs (
    id                TEXT PRIMARY KEY,
    experiment_id     TEXT NOT NULL REFERENCES experiments (id) ON DELETE CASCADE,
    episode_id        TEXT NOT NULL,
    namespace         TEXT NOT NULL,
    mode              TEXT NOT NULL CHECK (mode IN
                        ('agent_directed', 'deterministic_retry', 'durable_gateway')),
    thread_id         TEXT NOT NULL,
    state             TEXT NOT NULL CHECK (state IN
                        ('queued', 'running', 'completed', 'crashed', 'abandoned')),
    delivery_count    INTEGER NOT NULL DEFAULT 0,
    claimed_outcome   TEXT,
    deadline_at       TIMESTAMPTZ,
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    UNIQUE (experiment_id, episode_id)
);
CREATE INDEX runs_state_idx ON runs (state);

-- One row per model turn. Request and response are stored verbatim so an
-- episode can be reconstructed without re-running the model.
CREATE TABLE model_calls (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    turn               INTEGER NOT NULL,
    provider_model_id  TEXT NOT NULL,
    request_json       JSONB NOT NULL,
    response_json      JSONB,
    tool_calls_json    JSONB,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cost_usd           NUMERIC,
    latency_ms         DOUBLE PRECISION,
    error              TEXT,
    malformed          BOOLEAN NOT NULL DEFAULT false,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, turn)
);

-- What the model asked for versus what the runtime actually executed. The
-- distinction is the whole experiment: a stable key enforced by the runtime is
-- an infrastructure win, not evidence of better model reasoning.
CREATE TABLE tool_events (
    id                   BIGSERIAL PRIMARY KEY,
    run_id               TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    turn                 INTEGER NOT NULL,
    tool                 TEXT NOT NULL,
    requested_args_json  JSONB NOT NULL,
    requested_key        TEXT,
    effective_args_json  JSONB,
    effective_key        TEXT,
    rule                 TEXT NOT NULL,
    observation          TEXT NOT NULL,
    http_status          INTEGER,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX tool_events_run_idx ON tool_events (run_id, id);

-- A worker writes a row here and blocks, so a harness can kill it at an exact
-- point. Plan 1's barriers do the same job inside the simulator.
CREATE TABLE crash_points (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    worker_pid  INTEGER NOT NULL,
    reached_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at TIMESTAMPTZ,
    UNIQUE (run_id, name)
);

CREATE TABLE results (
    run_id            TEXT PRIMARY KEY REFERENCES runs (id) ON DELETE CASCADE,
    evaluator_version TEXT NOT NULL,
    verdict           TEXT NOT NULL,
    correct_recovery  BOOLEAN NOT NULL,
    score_json        JSONB NOT NULL,
    scored_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Shared across processes, so the daily ceiling cannot be defeated by running
-- two runners. One row per UTC day.
CREATE TABLE request_budget (
    day            DATE PRIMARY KEY,
    requests_made  INTEGER NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON experiments, runs, model_calls,
    tool_events, crash_points, results, request_budget TO runtime_svc;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO runtime_svc;
