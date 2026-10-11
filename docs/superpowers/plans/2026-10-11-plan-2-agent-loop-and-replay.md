# Plan 2: Agent Loop, Budget Guard and the Replay Experiment

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the bounded agent loop that drives the simulator, prove empirically what LangGraph replays when a worker is killed inside a tool call, and make every bit of it testable without spending a single model request.

**Architecture:** A LangGraph graph with two nodes, a model node and a tool node, persisted by `PostgresSaver` with `durability="sync"`. The model behind it is an interface with two implementations: a scripted policy used by every test, and a real OpenRouter client used only in Plan 3. A runtime database records every model call, every tool event and every crash point, so an episode can be reconstructed and scored without trusting the agent's own account of itself.

**Tech Stack:** Python 3.12, uv, LangGraph 1.2.14, langgraph-checkpoint-postgres 3.1.2, psycopg 3, Pydantic v2, pytest, OpenTelemetry Python SDK.

## Scope boundary, and why it sits here

Plan 2 spends **zero model requests**. Everything in it runs against a scripted
model, which makes the whole suite free, deterministic and CI-safe.

That is not a limitation, it is the point. The project's central question is what
happens at the commit-then-crash boundary, and that question is about the
runtime, not about the model. Answering it with a scripted model gives an exact,
repeatable answer, and leaves the paid quota for the one thing that genuinely
needs a real model: measuring what a real model decides.

**Plan 3** takes the quota work: the conformance pilot across the 14 free
tool-capable models, the behaviour study, and the frozen comparison.

## Global Constraints

- Python 3.12 or newer; uv for environments; `uv.lock` committed.
- PostgreSQL 16 on host port 54329. No local `psql` binary exists.
- `langgraph==1.2.14` and `langgraph-checkpoint-postgres==3.1.2`, pinned. Both
  require Python 3.10+. Re-verify on the day and record any change.
- **Every graph run uses `durability="sync"`, in every mode, without exception.**
  Per D11: a mode comparison where the baselines checkpoint less often than the
  treatment is a rigged comparison. The setting is recorded in the experiment
  config beside the model ID and prompt version.
- The runtime database is a separate database from `payments`, owned by a
  separate role. The evaluator reads the payment ledger through `ledger_reader`
  and must never read the runtime database to decide a verdict.
- Zero model requests in this plan. Any code path that would call a real
  provider must be unreachable from the test suite, and a test must prove it.
- Model output is untrusted input. Tenant, namespace and the effective
  idempotency key are never taken from it.
- Dev-only credentials stay in `.env.example` and `compose.yaml`.
- Commit message bodies end with:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

## What Plan 1 left for this plan to honour

Read these before Task 1; each is a trap already paid for once.

- **The attempt must be recorded before anything can fail.** Four Plan 1 bugs
  shared one shape: a request failed with a 500 and lost the attempt before it
  was recorded. When this plan adds a new failure path, the question is not only
  what status it returns but whether the attempt survived it.
- **Namespace isolation rests on a header.** `X-Lab-Namespace` must be derived
  from trusted episode context in the runner and never from model output.
  Nothing structurally enforces that yet, and this plan builds the runner.
- **`docker compose build sim-public` exits 0 and does nothing.** The build is
  owned by `sim-migrate`.
- **The client-disconnect guarantee holds only while the simulator's handlers are
  synchronous.** A structural test pins it. Do not convert them.
- **The compat parity claim is scoped to well-formed sequences.** Any legacy
  figure must exclude episodes where `invalid_charge_calls > 0`, and a scripted
  policy that deliberately malforms a call will produce exactly those.

## File Structure

```text
src/lab/runtime/__init__.py
src/lab/runtime/migrations/0001_init.sql   experiments, runs, model_calls, tool_events, crash_points, results
src/lab/runtime/db.py                      runtime connection helpers and migration entry point
src/lab/runtime/budget.py                  request ceiling, :free allowlist, daily counter
src/lab/agent/__init__.py
src/lab/agent/model.py                     ModelClient protocol, ModelTurn, ToolCall
src/lab/agent/scripted.py                  deterministic policies used by every test
src/lab/agent/prompts.py                   versioned system and user prompts
src/lab/agent/tools.py                     versioned tool schemas, per profile
src/lab/agent/graph.py                     the two-node LangGraph loop
src/lab/agent/client.py                    simulator HTTP client with trusted-context binding
src/lab/runner/__init__.py
src/lab/runner/episode.py                  manifest spec -> namespace -> graph -> recorded run
src/lab/runner/__main__.py                 run one episode or a manifest
src/lab/obs.py                             tracing setup, no-op unless configured
tests/runtime/test_budget.py
tests/runtime/test_runtime_db.py
tests/agent/test_scripted.py
tests/agent/test_tools_prompts.py
tests/agent/test_client_binding.py
tests/agent/test_graph.py
tests/agent/test_replay.py                 THE replay experiment
tests/runner/test_episode.py
tests/runner/test_no_live_calls.py
tests/obs/test_tracing.py
docs/replay-findings.md                    what LangGraph actually replays, with evidence
```

---

### Task 1: Runtime database schema and helpers

**Files:**
- Create: `src/lab/runtime/__init__.py`, `src/lab/runtime/db.py`, `src/lab/runtime/migrations/0001_init.sql`
- Test: `tests/runtime/test_runtime_db.py`

**Interfaces:**
- Consumes: `lab.db` helpers from Plan 1 (`ensure_role`, `ensure_database`, `apply_migrations`, `with_database`).
- Produces:
  - `RUNTIME_MIGRATIONS: Path`
  - `runtime_url() -> str` reading `LAB_RUNTIME_URL`
  - A `runtime_svc` role and `runtime` database, created by the same migration runner Plan 1 uses.

**Why a separate database:** the evaluator must not be able to reach the agent's
own records when deciding a verdict. Plan 1 put the payment ledger behind a
read-only role for the same reason. Keeping the runtime tables in a different
database with a different owner makes the separation structural rather than a
convention someone remembers.

- [ ] **Step 1: Write the failing test**

```python
import psycopg
import pytest

from lab import db
from lab.runtime import RUNTIME_MIGRATIONS


def test_migrations_are_idempotent(runtime_db):
    assert db.apply_migrations(runtime_db["admin"], RUNTIME_MIGRATIONS) == []


def test_runtime_role_can_write_its_own_tables(runtime_db):
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO experiments (id, name, manifest_sha256, config_json)"
            " VALUES ('e1', 'test', 'abc', '{}')"
        )
        assert conn.execute("SELECT count(*) FROM experiments").fetchone()[0] == 1


def test_runtime_role_cannot_reach_the_payment_ledger(runtime_db, payments_db):
    """The evaluator's independence is structural, not conventional."""
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UndefinedTable):
            conn.execute("SELECT count(*) FROM charges")


def test_runs_require_a_known_experiment(runtime_db):
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO runs (id, experiment_id, episode_id, namespace, mode,"
                " thread_id, state) VALUES ('r1', 'nope', 'ep', 'ns', 'agent_directed',"
                " 't1', 'queued')"
            )


def test_run_state_is_constrained(runtime_db):
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO experiments (id, name, manifest_sha256, config_json)"
            " VALUES ('e1', 'test', 'abc', '{}')"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO runs (id, experiment_id, episode_id, namespace, mode,"
                " thread_id, state) VALUES ('r1', 'e1', 'ep', 'ns', 'agent_directed',"
                " 't1', 'banana')"
            )


def test_durability_is_recorded_on_the_experiment(runtime_db):
    """D11: the comparison is only fair if every mode shares one setting, so the
    setting must be stored where a reader can check it."""
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO experiments (id, name, manifest_sha256, config_json)"
            " VALUES ('e1', 'test', 'abc', '{\"durability\": \"sync\"}')"
        )
        row = conn.execute(
            "SELECT config_json->>'durability' FROM experiments WHERE id = 'e1'"
        ).fetchone()
        assert row[0] == "sync"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/runtime/test_runtime_db.py -v`
Expected: a collection error, `ModuleNotFoundError: No module named 'lab.runtime'`.

- [ ] **Step 3: Write the migration**

`src/lab/runtime/migrations/0001_init.sql`:

```sql
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
```

- [ ] **Step 4: Write the module**

`src/lab/runtime/__init__.py`:

```python
"""Runtime records: experiments, runs, model calls, tool events, crash points."""

from pathlib import Path

RUNTIME_MIGRATIONS = Path(__file__).parent / "migrations"
```

`src/lab/runtime/db.py`:

```python
"""Runtime database access.

Deliberately a different database from `payments`, owned by a different role.
The evaluator reads the payment ledger to decide a verdict and must not be able
to reach the agent's own records, so the separation is structural.
"""

from __future__ import annotations

import os


def runtime_url() -> str:
    return os.environ["LAB_RUNTIME_URL"]
```

- [ ] **Step 5: Add the fixture**

Append to `tests/conftest.py`:

```python
RUNTIME_SVC_PASSWORD = os.environ.get("LAB_RUNTIME_SVC_PASSWORD", "dev-runtime-svc")


@pytest.fixture(scope="session")
def runtime_db():
    from lab.runtime import RUNTIME_MIGRATIONS

    name = f"runtime_test_{uuid.uuid4().hex[:8]}"
    db.ensure_role(ADMIN_URL, "runtime_svc", RUNTIME_SVC_PASSWORD)
    db.ensure_database(ADMIN_URL, name)
    admin_url = db.with_database(ADMIN_URL, name)
    db.apply_migrations(admin_url, RUNTIME_MIGRATIONS)
    yield {
        "admin": admin_url,
        "svc": db.with_database(ADMIN_URL, name, "runtime_svc", RUNTIME_SVC_PASSWORD),
    }
    db.drop_database(ADMIN_URL, name)
```

- [ ] **Step 6: Run to verify it passes**

Run: `uv run pytest tests/runtime/test_runtime_db.py -v`
Expected: all PASS. Then `uv run pytest -q` and confirm the Plan 1 count is unchanged plus your additions.

- [ ] **Step 7: Commit**

```bash
git add src/lab/runtime tests/runtime tests/conftest.py
git commit -m "feat(runtime): runtime schema, role and separate database"
```

---

### Task 2: The request budget guard

**Files:**
- Create: `src/lab/runtime/budget.py`
- Test: `tests/runtime/test_budget.py`

**Interfaces:**
- Consumes: Task 1's `request_budget` table.
- Produces:
  - `class BudgetExceeded(Exception)`
  - `class ModelNotAllowed(Exception)`
  - `FREE_SUFFIX = ":free"`
  - `assert_model_allowed(model_id: str) -> None`
  - `class RequestBudget` with `__init__(conn, daily_limit: int = 900)`, `claim(n: int = 1) -> int`, `used_today() -> int`

**Why 900 and not 1000:** D11 confirms the provider's ceiling is 1,000 per day.
Stopping at 900 leaves room for the retries and reruns that always happen, and
means hitting our limit is a clean local error rather than a provider rejection
mid-episode.

- [ ] **Step 1: Write the failing test**

```python
import psycopg
import pytest

from lab.runtime.budget import (
    BudgetExceeded,
    ModelNotAllowed,
    RequestBudget,
    assert_model_allowed,
)


@pytest.mark.parametrize(
    "model_id",
    [
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "thinkingmachines/inkling:free",
    ],
)
def test_free_models_are_allowed(model_id):
    assert_model_allowed(model_id)


@pytest.mark.parametrize(
    "model_id",
    [
        "openai/gpt-5",
        "anthropic/claude-opus-5",
        "google/gemma-4-31b-it",
        "google/gemma-4-31b-it:nitro",
        "free:google/gemma-4-31b-it",
        ":free",
        "",
    ],
)
def test_everything_else_is_refused(model_id):
    """D1 allows no paid usage, so the allowlist is a code-level guard rather
    than a configuration value someone can mistype."""
    with pytest.raises(ModelNotAllowed):
        assert_model_allowed(model_id)


def test_claim_counts_up_and_reports(runtime_conn):
    budget = RequestBudget(runtime_conn, daily_limit=5)
    assert budget.used_today() == 0
    assert budget.claim() == 1
    assert budget.claim(3) == 4
    assert budget.used_today() == 4


def test_claim_refuses_to_exceed_the_limit(runtime_conn):
    budget = RequestBudget(runtime_conn, daily_limit=3)
    budget.claim(3)
    with pytest.raises(BudgetExceeded):
        budget.claim()
    assert budget.used_today() == 3, "a refused claim must not consume budget"


def test_a_partial_claim_is_all_or_nothing(runtime_conn):
    budget = RequestBudget(runtime_conn, daily_limit=5)
    budget.claim(4)
    with pytest.raises(BudgetExceeded):
        budget.claim(2)
    assert budget.used_today() == 4


def test_two_processes_share_one_ceiling(runtime_db):
    """A per-process counter would be defeated by running two runners, so the
    counter lives in the database."""
    import threading

    from psycopg.rows import dict_row

    results = []
    barrier = threading.Barrier(8)

    def worker():
        with psycopg.connect(
            runtime_db["svc"], autocommit=True, row_factory=dict_row
        ) as conn:
            budget = RequestBudget(conn, daily_limit=5)
            barrier.wait()
            try:
                budget.claim()
                results.append("ok")
            except BudgetExceeded:
                results.append("refused")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert results.count("ok") == 5
    assert results.count("refused") == 3
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/runtime/test_budget.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lab.runtime.budget'`.

- [ ] **Step 3: Implement**

`src/lab/runtime/budget.py`:

```python
"""Hard ceilings on paid resources.

Two independent guards. The allowlist refuses any model whose ID does not end
in ":free", because D1 permits no paid usage at all. The counter lives in the
database rather than in a process, so running a second runner cannot defeat it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg

FREE_SUFFIX = ":free"


class BudgetExceeded(Exception):
    """The daily request ceiling would be exceeded. Nothing was consumed."""


class ModelNotAllowed(Exception):
    """The model is not a free variant, which D1 forbids."""


def assert_model_allowed(model_id: str) -> None:
    if not model_id.endswith(FREE_SUFFIX):
        raise ModelNotAllowed(f"{model_id!r} is not a :free model")
    if len(model_id) <= len(FREE_SUFFIX):
        raise ModelNotAllowed(f"{model_id!r} has no model name before {FREE_SUFFIX}")


class RequestBudget:
    """A shared daily counter.

    `claim` is all or nothing: a claim that would exceed the limit consumes
    none of it, so a refused claim leaves the budget exactly as it was.
    """

    def __init__(self, conn: psycopg.Connection, daily_limit: int = 900) -> None:
        if not conn.autocommit:
            raise ValueError("budget connections must use autocommit=True")
        self._conn = conn
        self._limit = daily_limit

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def used_today(self) -> int:
        row = self._conn.execute(
            "SELECT requests_made FROM request_budget WHERE day = %s", (self._today(),)
        ).fetchone()
        return 0 if row is None else int(row[0])

    def claim(self, n: int = 1) -> int:
        if n < 1:
            raise ValueError("n must be at least 1")
        row = self._conn.execute(
            "INSERT INTO request_budget (day, requests_made) VALUES (%s, %s)"
            " ON CONFLICT (day) DO UPDATE SET requests_made ="
            "   request_budget.requests_made + EXCLUDED.requests_made,"
            "   updated_at = now()"
            " WHERE request_budget.requests_made + EXCLUDED.requests_made <= %s"
            " RETURNING requests_made",
            (self._today(), n, self._limit),
        ).fetchone()
        if row is None:
            raise BudgetExceeded(
                f"claiming {n} would exceed the daily limit of {self._limit}"
            )
        return int(row[0])
```

Note the single statement: the increment and the limit check happen together, which
is what makes the concurrency test pass. A read-then-write would let two runners
both see 4 of 5 used and both proceed. This is the same lesson as Plan 1's atomic
charge insert.

- [ ] **Step 4: Add the function-scoped connection fixture**

Append to `tests/conftest.py`:

```python
@pytest.fixture
def runtime_conn(runtime_db):
    with psycopg.connect(runtime_db["admin"], autocommit=True) as conn:
        conn.execute("TRUNCATE experiments, request_budget CASCADE")
    with psycopg.connect(
        runtime_db["svc"], autocommit=True, row_factory=dict_row
    ) as conn:
        yield conn
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/runtime/test_budget.py -v`
Expected: all PASS, including the eight-thread test showing exactly 5 successes.

- [ ] **Step 6: Prove the concurrency test can fail**

Replace `claim`'s single statement with a read followed by a write, run the
concurrency test, and confirm more than 5 claims succeed. Restore with
`git checkout -- src/lab/runtime/budget.py` and confirm it passes again. Do not
commit the broken variant.

- [ ] **Step 7: Commit**

```bash
git add src/lab/runtime/budget.py tests/runtime/test_budget.py tests/conftest.py
git commit -m "feat(runtime): free-model allowlist and a shared daily request ceiling"
```

---

### Task 3: The model interface and the scripted model

**Files:**
- Create: `src/lab/agent/__init__.py`, `src/lab/agent/model.py`, `src/lab/agent/scripted.py`
- Test: `tests/agent/test_scripted.py`

**Interfaces:**
- Produces:
  - `class ToolCall(BaseModel)`: `id: str`, `name: str`, `arguments: dict[str, Any]` (frozen)
  - `class ModelTurn(BaseModel)`: `text: str | None`, `tool_calls: tuple[ToolCall, ...]`, `raw: dict`, `input_tokens: int | None`, `output_tokens: int | None`, `malformed: bool = False` (frozen)
  - `class ModelClient(Protocol)`: `model_id: str`; `complete(messages: list[dict], tools: list[dict]) -> ModelTurn`
  - `Policy = Literal["naive_new_key", "stable_key", "check_status", "give_up", "false_success", "malformed_call", "wrong_amount"]`
  - `class ScriptedModel`: `__init__(policy: Policy, model_id: str = "scripted/" + policy)`, implementing `ModelClient`
  - `SCRIPTED_POLICIES: tuple[Policy, ...]`

**Why the scripted model carries seven policies:** each one produces a distinct
verdict from Plan 1's oracle, which is how the whole pipeline gets tested for
free. `naive_new_key` yields DUPLICATE in the lost-acknowledgement world,
`give_up` yields MISSED, `false_success` yields FALSE_SUCCESS, `wrong_amount`
yields WRONG_EFFECT, `stable_key` and `check_status` yield CORRECT, and
`malformed_call` exercises the `invalid_charge_calls` path that scopes the compat
parity claim.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from lab.agent.model import ModelTurn, ToolCall
from lab.agent.scripted import SCRIPTED_POLICIES, ScriptedModel

CHARGE_TOOLS = [{"name": "charge"}, {"name": "get_payment_status"}, {"name": "report_done"}]


def drive(policy, observations):
    """Run a scripted model through a conversation, returning its tool calls."""
    model = ScriptedModel(policy)
    messages = [{"role": "user", "content": "charge it"}]
    calls = []
    for observation in [None, *observations]:
        if observation is not None:
            messages.append({"role": "tool", "content": observation})
        turn = model.complete(messages, CHARGE_TOOLS)
        calls.append(turn.tool_calls)
        messages.append({"role": "assistant", "content": turn.text or ""})
    return calls


def test_every_policy_is_deterministic():
    for policy in SCRIPTED_POLICIES:
        first = drive(policy, ["HTTP 504 Gateway Timeout: ..."])
        second = drive(policy, ["HTTP 504 Gateway Timeout: ..."])
        assert first == second, policy


def test_naive_new_key_changes_the_key_on_retry():
    calls = drive("naive_new_key", ["HTTP 504 Gateway Timeout: ..."])
    first_key = calls[0][0].arguments["idempotency_key"]
    second_key = calls[1][0].arguments["idempotency_key"]
    assert calls[0][0].name == "charge"
    assert calls[1][0].name == "charge"
    assert first_key != second_key


def test_stable_key_reuses_the_key_on_retry():
    calls = drive("stable_key", ["HTTP 504 Gateway Timeout: ..."])
    assert calls[0][0].arguments["idempotency_key"] == calls[1][0].arguments["idempotency_key"]


def test_check_status_looks_before_retrying():
    calls = drive("check_status", ["HTTP 504 Gateway Timeout: ...", "Status for 'k1': NOT_FOUND"])
    assert [c[0].name for c in calls[:3]] == ["charge", "get_payment_status", "charge"]


def test_give_up_reports_failed_without_retrying():
    calls = drive("give_up", ["HTTP 504 Gateway Timeout: ..."])
    assert calls[1][0].name == "report_done"
    assert calls[1][0].arguments["outcome"] == "failed"


def test_false_success_claims_success_after_an_error():
    calls = drive("false_success", ["HTTP 504 Gateway Timeout: ..."])
    assert calls[1][0].name == "report_done"
    assert calls[1][0].arguments["outcome"] == "succeeded"


def test_wrong_amount_charges_the_wrong_value():
    calls = drive("wrong_amount", [])
    assert calls[0][0].arguments["amount"] != 49.99


def test_malformed_call_is_flagged_and_omits_a_required_field():
    """Free models are expected to emit malformed calls (D2), and the compat
    parity claim is scoped to episodes without them, so the pipeline must be
    able to produce one on purpose."""
    model = ScriptedModel("malformed_call")
    turn = model.complete([{"role": "user", "content": "charge it"}], CHARGE_TOOLS)
    assert turn.malformed is True
    assert "currency" not in turn.tool_calls[0].arguments


def test_a_turn_reports_no_token_usage():
    """Scripted turns must not fabricate usage numbers that would pollute a
    cost report."""
    model = ScriptedModel("stable_key")
    turn = model.complete([{"role": "user", "content": "go"}], CHARGE_TOOLS)
    assert turn.input_tokens is None
    assert turn.output_tokens is None


def test_model_id_marks_it_as_scripted():
    """A scripted run must never be mistaken for a real one in the records."""
    for policy in SCRIPTED_POLICIES:
        assert ScriptedModel(policy).model_id.startswith("scripted/")


def test_tool_call_and_turn_are_frozen():
    call = ToolCall(id="1", name="charge", arguments={})
    with pytest.raises(Exception):
        call.name = "other"
    turn = ModelTurn(text=None, tool_calls=(), raw={})
    with pytest.raises(Exception):
        turn.text = "x"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/agent/test_scripted.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lab.agent'`.

- [ ] **Step 3: Implement the interface**

`src/lab/agent/__init__.py`:

```python
"""The agent loop and the models that drive it."""
```

`src/lab/agent/model.py`:

```python
"""What the runtime needs from a model, and nothing more.

Two implementations exist: `ScriptedModel`, used by every test, and the
OpenRouter client added in Plan 3. Keeping the surface this small is what lets
the entire pipeline be tested without spending a request.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any]


class ModelTurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str | None
    tool_calls: tuple[ToolCall, ...]
    raw: dict[str, Any]
    input_tokens: int | None = None
    output_tokens: int | None = None
    malformed: bool = False


class ModelClient(Protocol):
    model_id: str

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn: ...
```

- [ ] **Step 4: Implement the scripted model**

`src/lab/agent/scripted.py`: implement each policy as a function of the
conversation so far, keyed on how many tool observations have been seen. Derive
the turn index by counting messages with `role == "tool"`. Every policy issues
`charge` on turn 0 with key `"k1"`, amount `49.99`, order `"1234"`, currency
`"USD"`, except `wrong_amount` (amount `4999.00`) and `malformed_call` (omits
`currency`, sets `malformed=True`). On the turn after the first observation:
`naive_new_key` charges again with key `"k2"`; `stable_key` charges again with
`"k1"`; `check_status` calls `get_payment_status` for `"k1"` and then charges
again with `"k1"`; `give_up` reports `failed`; `false_success` reports
`succeeded`. Any policy that has issued its planned sequence reports `done` with
the outcome implied by its name. `model_id` is `f"scripted/{policy}"` and token
counts stay `None`.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/agent/test_scripted.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lab/agent tests/agent/test_scripted.py
git commit -m "feat(agent): model interface and seven deterministic scripted policies"
```

---

### Task 4: Versioned prompts and tool schemas

**Files:**
- Create: `src/lab/agent/prompts.py`, `src/lab/agent/tools.py`
- Test: `tests/agent/test_tools_prompts.py`

**Interfaces:**
- Produces:
  - `PROMPT_VERSION: str` and `TOOL_SCHEMA_VERSION: str`, both of the form `"v1"`
  - `system_prompt(variant: PromptVariant) -> str`
  - `user_prompt(task: ChargeTask, profile: Profile) -> str`
  - `tool_schemas(profile: Profile, status_available: bool) -> list[dict]`

The prompt and schema text must come from Plan 1's protocol, which already fixed
them byte for byte, and the compat variants must match the vendored original
exactly. Tests assert the compat schemas equal the vendored
`retryledger_original.TOOLS` and that the controlled schemas add `order_id` and
`currency` with `additionalProperties: false`. Both versions are recorded in the
experiment config, so a frozen run can be reproduced.

Key test: removing `get_payment_status` when `status_available` is false, since
that is the behaviour study's experimental variable.

---

### Task 5: The simulator client with trusted-context binding

**Files:**
- Create: `src/lab/agent/client.py`
- Test: `tests/agent/test_client_binding.py`

**Interfaces:**
- Produces:
  - `class TrustedContext(BaseModel)` (frozen): `tenant`, `namespace`, `run_id`, `episode_id`, `task_id`
  - `class SimulatorClient`: `__init__(public_url, client_token, ctx: TrustedContext)`, `charge(args: dict) -> tuple[int, dict]`, `status(key: str) -> tuple[int, dict]`

**This task carries a Plan 1 debt.** Namespace isolation rests entirely on the
`X-Lab-Namespace` header, and nothing structurally stops a runner deriving it
from model output. Fix that here: the client takes its namespace from
`TrustedContext` at construction, and the per-call argument dictionaries have no
namespace field at all. The required tests:

- The namespace header always equals `ctx.namespace`, whatever the arguments contain.
- A `namespace` key inside the model's arguments is rejected rather than forwarded,
  and the rejection is recorded as a denial rather than raised as an error.
- No method accepts a namespace parameter. Assert this structurally with
  `inspect.signature`, so a later refactor cannot quietly add one.

---

### Task 6: The LangGraph loop

**Files:**
- Create: `src/lab/agent/graph.py`
- Test: `tests/agent/test_graph.py`

**Interfaces:**
- Produces:
  - `class EpisodeState(TypedDict)`: messages, turn, claimed_outcome, done
  - `build_graph(model: ModelClient, executor, tools, max_turns: int) -> CompiledGraph`
  - `run_episode(graph, thread_id: str, *, durability: str = "sync") -> EpisodeState`

Two nodes, model and tool, with a conditional edge that ends on `report_done` or
on `max_turns`. Checkpointed with `PostgresSaver` against the checkpoints
database. **Every call passes `durability="sync"`**, and a test asserts the
default is `"sync"` and that `run_episode` refuses any other value unless
explicitly overridden by an experiment config, so the fairness constraint in D11
cannot drift.

Tests: a scripted episode completes; the turn cap is enforced and produces a
truthful unresolved outcome rather than a silent stop; every model call and tool
event is recorded with requested and effective arguments; a `report_done` ends
the run.

---

### Task 7: The replay experiment

**Files:**
- Create: `tests/agent/test_replay.py`, `docs/replay-findings.md`

**This is the task the plan exists for.** Per D11, LangGraph documents what
happens to successful sibling nodes when one fails, but says nothing about a node
that completed while its write was uncommitted. That is the commit-then-crash
boundary. Determine it empirically and write down what you find.

Method, using machinery that already exists:

1. Configure the simulator namespace with `hold_after_commit=(1,)`, so the charge
   commits and then blocks inside the simulator.
2. Run the episode in a real subprocess, not a thread.
3. Wait until the simulator reports the barrier reached, which means the charge
   has committed and the ledger shows one effect.
4. `SIGKILL` the runner subprocess. Do not raise an exception; a raise is not a
   crash, and Plan 1 learned that distinction the hard way.
5. Release the barrier.
6. Resume the graph on the same `thread_id` in a fresh process.
7. Observe and record: did the tool node re-execute? Did a second charge request
   reach the simulator? How many effects does the ledger hold? What did the
   resumed graph believe about the tool result?

Run the same procedure at three kill points: before the charge request is sent,
after it commits but before the tool result is checkpointed, and after the tool
result is checkpointed. Repeat each enough times to be confident the answer is
stable rather than a race, and say how many.

`docs/replay-findings.md` records the finding with evidence: the LangGraph
version, the durability mode, each kill point, what re-ran, and the resulting
ledger state. State plainly whether a replayed tool call produced a second
effect, and that the answer comes from measurement rather than documentation.

**Expected, but do not assume it:** with a stable key the replay is absorbed by
the backend's idempotency record, so the ledger still holds one effect, while the
graph has no memory of the first attempt. If that is what happens, it is the
clearest possible argument for a journal outside the graph, and it is the result
the project's thesis rests on. If it is not what happens, report what did, and
revise the roadmap's failure-path table before Plan 4 builds on it.

---

### Task 8: The episode runner

**Files:**
- Create: `src/lab/runner/__init__.py`, `src/lab/runner/episode.py`, `src/lab/runner/__main__.py`
- Test: `tests/runner/test_episode.py`, `tests/runner/test_no_live_calls.py`

Wires one `EpisodeSpec` end to end: configure the namespace through the control
API, derive `TrustedContext`, build the graph, run it, read the ledger through
`ledger_reader`, score it with Plan 1's oracle, and write a `results` row.

Required tests, one per scripted policy, asserting the verdict Plan 1's oracle
gives: `naive_new_key` in `lost_ack` is DUPLICATE; `give_up` in `true_fail` is
MISSED; `false_success` is FALSE_SUCCESS; `wrong_amount` is WRONG_EFFECT;
`stable_key` and `check_status` are CORRECT. That set proves the whole pipeline
produces distinguishable, correct verdicts for free.

`tests/runner/test_no_live_calls.py` enforces the plan's own scope: assert that
no test can reach a real provider. Patch the network at the socket layer for the
whole suite and assert nothing attempts an outbound connection to a provider
host, so a stray real client in a later task fails loudly here.

---

### Task 9: Tracing

**Files:**
- Create: `src/lab/obs.py`
- Test: `tests/obs/test_tracing.py`
- Modify: `compose.yaml` (add Jaeger)

Per D6 and D11: use the GenAI attribute names where convenient, do not claim
conformance to a convention whose releases page is empty, and keep structured
events in Postgres as the source of record for scoring. Tracing is for debugging
and the demo.

`obs.py` configures an exporter only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set,
and is a no-op otherwise, so tests and CI stay unaffected. Spans wrap the
episode, each model call and each tool call, with the run ID and episode ID as
attributes. A test asserts that with no endpoint configured nothing is exported
and no error is raised, and that with an in-memory exporter one episode produces
one trace whose spans nest as expected.

---

## Phase gates for this plan

| Gate | Evidence |
|---|---|
| Scripted episodes complete end to end and score distinctly | One runner test per policy, each asserting its expected verdict |
| A killed tool node's replay behaviour is known, not assumed | `tests/agent/test_replay.py` plus `docs/replay-findings.md` with evidence at three kill points |
| The comparison cannot be rigged by durability | `run_episode` defaults to `"sync"`, asserted, and the setting is stored in the experiment config |
| No paid request is reachable | The socket-level test in `tests/runner/test_no_live_calls.py`, plus the `:free` allowlist |
| The daily ceiling cannot be defeated by a second process | The eight-thread budget test, proven able to fail |
| The agent cannot choose its own namespace | Structural signature test plus the denial test in Task 5 |
| Tracing is optional and silent by default | `tests/obs/test_tracing.py` |

## Open questions for Plan 3

- Which of the 14 free tool-capable models conforms well enough to use, and at
  what malformed-call rate. D2's pilot answers this; it is the first thing in
  Plan 3 and the first thing that spends quota.
- Whether run redelivery is given to all three modes. The roadmap's open
  decision 10 is still open, and Task 7's finding should settle it: if a replayed
  tool call is absorbed by the backend's idempotency record, then redelivery
  alone makes modes B and C tie on effect correctness, and the interesting
  difference moves to truthful reporting. Decide it with Task 7's evidence in
  hand, not before.
