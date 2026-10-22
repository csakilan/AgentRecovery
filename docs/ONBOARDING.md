# Agent Recovery Lab: Complete Onboarding and Handoff

This is the single document a new engineer or AI agent reads to take over this
project cold. It is self-contained: you should be able to start Plan 4 after
reading it, without the chat history that produced the first three plans. Where
it says "see X", X is an authoritative file in the repo with more depth.

Repository: https://github.com/csakilan/AgentRecovery.git
Working tree on the original machine: `/Users/ak/Documents/Pro/VSWorkspace/Projects/AgentRecovery`
Default branch: `main`. Plans 1–3 and the Plan 4 Tasks 1–7 checkpoint are on `main`.
Last milestone commit: `6eeb977` (Merge Plan 3).

Read these alongside this document, in this order, when you need detail:
- `docs/superpowers/plans/2026-10-04-roadmap.md`: the master plan, all 8 sub-plans, contracts, test matrix, deployment.
- `docs/decisions.md`: decisions D1 through D13, each dated, with the reasoning.
- `docs/protocol.md`: the payment simulator's exact wire contract.
- `docs/replay-findings.md`: the measured crash/replay result (the project's core finding).
- `docs/handoff.md`: the running, append-only state log across Plans 1 to 3 (older and more granular than this file; this file supersedes it as the onboarding entry point).
- `docs/behaviour-findings.md` (+ `.csv`, `.jsonl`): the Plan 3 baseline numbers.
- [Plan 4 execution and recovery design](superpowers/specs/2026-10-19-plan-4-execution-and-recovery-design.md): Run 1 specification approved on 2026-10-20. The [implementation plan](superpowers/plans/2026-10-20-plan-4-execution-and-recovery.md) was approved for direct execution on 2026-10-20. Run 3 Tasks 1–7 are implemented; production runner integration remains mode A only.

---

## 0. Golden rules (do not violate these; they are enforced by tests and reviews)

1. **`:free` models only.** No paid model ID is ever sent to a provider. The
   OpenRouter client refuses any model id that does not end in `:free` at
   construction (`assert_model_allowed`). A paid call would draw real money; the
   whole design forbids it (D1).
2. **Zero real model requests from the test suite.** Every provider code path is
   unreachable from tests, and a socket-level guard proves it. Never add a test
   that hits the network. See `tests/network_guard.py` and
   `tests/runner/test_no_live_calls.py`.
3. **Every graph run uses `durability="sync"`.** In every mode, without
   exception. A mode comparison where one mode checkpoints less often than
   another is rigged (D11). The setting is stored in `experiments.config_json`.
4. **Model output is untrusted input.** Tenant and namespace always come from
   trusted context. Mode A forwards the model's requested idempotency key; the
   B/C executors derive their effective key from trusted run/task
   identity. Observation text may carry adversarial instructions; it can change
   what the model asks for, never what the runtime is allowed to do.
5. **The evaluator never reads the runtime database to decide a verdict.** It
   reads the payment ledger through the read-only `ledger_reader` role only. A
   report that aggregates already-decided verdicts may read the runtime DB; the
   scorer may not. This separation is structural (separate DB, separate role,
   `CONNECT` revoked from `PUBLIC`).
6. **Never edit `tests/reference/retryledger_original.py`.** It is the vendored
   original benchmark, pinned by a header-stripped SHA-256
   (`b1961cd8daac7606f252dce7dc78e5e5a61adaacce64ab770eadf17c6cd2a077`). It is the
   oracle of truth for the compatibility profile's byte-for-byte parity.
7. **Never commit secrets.** The real OpenRouter key lives only in the gitignored
   `.env` (`.gitignore` line 6). Dev-only credentials live in `.env.example` and
   `compose.yaml`. Never create a `.env` with a real key in a commit.
8. **Do not destroy the local Postgres.** Do not run `docker compose down` on the
   shared stack or touch the `postgres` container/volume; it holds dev data across
   sessions. `docker compose up` to bring services up is fine.
9. **Confirm before the one thing that spends quota.** A real-model run (the
   behaviour study, the comparison) spends the daily request allowance. It is
   gated on the operator's explicit go-ahead; everything else is built and tested
   offline with a scripted model and mocked HTTP.
10. **Commit message trailer.** Commits so far end with
    `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. If a different
    agent or human continues, use your own correct attribution; do not keep
    claiming Claude authorship for work you did.

---

## 1. What the project is

### Plain language

An AI agent is told to do one simple job: charge a synthetic customer $49.99 for
order 1234, exactly once. Not zero times, not twice. The catch is that the
payment system sometimes lies about whether the charge went through (the
response is lost, or it says "error" when it actually succeeded), and the
process running the agent can be killed in the middle. The project measures how
reliably the "charge exactly once" job is done, and it separates how much of that
reliability comes from the model being clever versus from boring infrastructure
around it (a stable retry key, a durable journal, a recovery sweeper).

Think of it as a cashier who must ring up one sale, where the card machine
sometimes freezes without saying if it charged, and the cashier occasionally
faints mid-transaction. We measure how often the customer ends up charged exactly
once, under the cashier alone, then under the cashier plus a written procedure
and a supervisor who checks the till after a faint.

### The precise thesis

Reliability of a single external side effect under ambiguous failures and real
process crashes decomposes into three contributions that can be measured
separately: (a) the model's own decisions, (b) a stable idempotency key enforced
by infrastructure rather than chosen by the model, and (c) durable recovery
infrastructure (an intent journal, leases with fencing, reconciliation through
authoritative status, an outbox, and a sweeper triggered independently of the
crashed process). The experiment holds everything else equal and varies only the
execution mode.

### The resume framing (D10, the most important decision)

The evaluation is **baseline and treatment**, not a three-way survey:
- **Baseline** = the model alone (mode A, "agent-directed").
- **Treatment** = the adapter (modes B and C).
- **Primary metric** = duplicate charges. **Secondary** = false success (the
  agent claims success when the ledger shows no effect, or vice versa).
- **The headline number is written only after it is measured.** Never invent or
  predict a result. Plan 3 produced the baseline; Plan 6 produces the
  head-to-head comparison and the percentage difference that is the resume line.

---

## 2. Current status (as built, 2026-10-21)

- **Plans 1, 2, 3 are complete and merged to `main`.** Plans 4 through 8 remain.
- **Test suite: 1,172 passing** on 2026-10-21 (85.32s, no warnings); ruff lint and
  formatting clean (124 files). Run 3 adds 49 tests to Run 2's 1,123. Baseline
  before Run 2: 1,016 tests. No real model requests.
- **Plan 4 Run 3 checkpoint:** Tasks 1–7 implemented and verified on
  `main`, with `plan-4-durable-gateway` retained for the remaining Plan 4 work.
  B and C's normal path work through
  scripted graphs and the real simulator. Saved preparation, run ownership,
  atomic checkpoint fencing and invocation evidence are available as foundations.
  Production `run_spec` still enables only A until Task 10; the sweeper, durable
  wake-ups, common redelivery and final SIGKILL acceptance remain pending.
- **Code:** 6,438 Python lines under `src/lab`, 13,826 under `tests`.
- **The baseline number exists** (`docs/behaviour-findings.md`): the model alone
  (mode A, controlled profile), 48 episodes, 45 of 48 CORRECT and 3 WRONG_EFFECT,
  correct recovery in 44 of 48 eligible trials. It ran on
  `nvidia/nemotron-3-super-120b-a12b:free` (see the deviation note in §13 and D12).
- **The core crash finding exists** (`docs/replay-findings.md`): under
  `durability="sync"`, a tool node killed after its charge commits but before the
  step checkpoints re-executes on resume and re-sends the same idempotency key;
  the backend absorbs it as a replay, so the ledger holds one effect while the
  resumed graph has no memory of the first attempt. This is the argument for the
  journal Plan 4 builds.

Roadmap progress (see §20): 3 of 8 sub-plans done, roughly a third of the build.
The parts that produce the resume headline (Plan 4 durable gateway, Plan 6
comparison) are still ahead.

---

## 3. Toolchain and environment

- **Python 3.12+**, managed with **uv** (`uv.lock` committed). Run everything
  through `uv run ...`.
- **PostgreSQL 16**, in Docker, published on host port **54329**. No local `psql`
  binary is assumed; use psycopg from Python.
- **Key libraries:** FastAPI + Starlette (the simulator), pydantic v2, psycopg 3
  + psycopg_pool, httpx, LangGraph `1.2.14` and `langgraph-checkpoint-postgres`
  `3.1.2` (both pinned), OpenTelemetry api/sdk/otlp-proto-http `1.45.0`.
- **ruff** for lint and format, line length 100. Use `datetime.UTC` (not
  `timezone.utc`; UP017).
- **pytest** in importlib mode.

### Environment variables (dev values in `.env.example`)

```
LAB_PG_ADMIN_URL=postgresql://postgres:postgres@localhost:54329/postgres
LAB_PAYMENTS_DB=payments
LAB_PAYMENT_SVC_PASSWORD=dev-payment-svc
LAB_LEDGER_READER_PASSWORD=dev-ledger-reader
LAB_PAYMENTS_URL=postgresql://payment_svc:dev-payment-svc@localhost:54329/payments
LAB_LEDGER_READER_URL=postgresql://ledger_reader:dev-ledger-reader@localhost:54329/payments
SIM_CLIENT_TOKEN=dev-client-token
SIM_CONTROL_TOKEN=dev-control-token
SIM_PUBLIC_URL=http://127.0.0.1:8100
SIM_CONTROL_URL=http://127.0.0.1:8101
LAB_RUNTIME_DB=runtime
LAB_RUNTIME_SVC_PASSWORD=dev-runtime-svc
LAB_RUNTIME_URL=postgresql://runtime_svc:dev-runtime-svc@localhost:54329/runtime
```

The real model key is NOT in `.env.example`. It lives in the gitignored `.env`:
```
OPENROUTER_API_KEY=sk-or-...   # real key; never committed
```
Source both for a real run: `set -a; . ./.env.example; . ./.env; set +a`.

---

## 4. Repository layout (file by file)

### `src/lab/`: the one package (one entry point per process, D7)

**Shared / domain**
- `db.py`: Postgres helpers: `ensure_role`, `ensure_database`, `with_database`,
  `drop_database`, `apply_migrations` (records applied files in a
  `schema_migrations` table, idempotent, one file + its bookkeeping per
  transaction).
- `obs.py`: OpenTelemetry tracing. No-op unless `OTEL_EXPORTER_OTLP_ENDPOINT` is
  set. Module-level provider/tracer (no process-global `set_tracer_provider`), so
  tests can install an in-memory exporter and reset. Spans wrap the episode, each
  model call, each tool call, with `lab.run_id`/`lab.episode_id`. A tracing
  failure never fails an episode. Attribute names are GenAI-style but the code
  never claims conformance to that convention (D6).

**`protocol/`: the hidden domain model (never shown to the model)**
- `task.py`: `Profile` (`COMPAT`, `CONTROLLED`), `ChargeTask(order_id="1234",
  amount=Decimal("49.99"), currency="USD")`, `DEFAULT_TASK`, `parse_amount`,
  `is_minor_unit_amount`, `is_storable_amount`, `args_hash(order_id, amount,
  currency)` (SHA-256 of canonical JSON; this is the identity hash Plan 4 uses).
  Also the NUMERIC column limits (`NUMERIC_MAX_INTEGER_DIGITS=131072`,
  `NUMERIC_MAX_SCALE=16383`), measured against a real Postgres 16.
- `faults.py`: `World` (`HEALTHY`, `LOST_ACK`, `TRUE_FAIL`, `PERMANENT_FAIL`),
  `FaultPlan` (frozen: `charge: dict[int, ChargeFault]`, `charge_default`,
  `status_unavailable_calls`, `hold_before_commit`, `hold_after_commit`),
  `world_fault_plan(world)`. Call indexes are 1-based over validated calls.
- `render.py`: `render_charge(http_status, body)`, `render_status(...)`: the
  EXACT model-visible text for a simulator response, byte-for-byte with
  RetryLedger (note the em dash in status lines is deliberate: that is the
  original's text, not our prose). `reason_phrase(status)` helper.
- `manifest.py`: `EpisodeSpec` (frozen: episode_id, profile, world, mode,
  status_available, prompt_variant, repetition, scenario, fault_plan, task,
  expect_effect, eligible_recoverable), `Manifest` (version, name, seed, episodes,
  `.sha256()`, `.write`/`.load`), `Mode` (`AGENT_DIRECTED`,
  `DETERMINISTIC_RETRY`, `DURABLE_GATEWAY`), `make_spec(...)`,
  `behavior_matrix(reps=12, prompts=("neutral",), seed=20261004)` (2 worlds ×
  2 status × reps, mode A, controlled), `infrastructure_matrix(reps=24)` (2 worlds
  × 3 modes, for Plan 6).

**`simulator/`: the payment simulator (FastAPI, runs as `payment_svc`)**
- `app.py`: `create_public_app(pool, client_token)` (port 8100: `POST
  /v1/charges`, `GET /v1/payments/{key}`), `create_internal_app(pool,
  control_token)` (port 8101: `PUT`/`DELETE /internal/namespaces/{ns}`, barrier
  get/release). Bearer auth; `X-Lab-Namespace` header required on the public API.
  Handlers are **synchronous** on purpose (see `test_client_disconnect.py`): a
  sync handler runs to completion in a worker thread, so a client disconnect
  cannot leave a half-finished effect.
- `service.py`: the logic: atomic charge (the effect and the idempotency claim
  are one `INSERT ... ON CONFLICT`), the hidden faults, the barriers, the call
  log. Outcomes written to `call_log.outcome` are a fixed CHECK'd set.
- `__main__.py`: `python -m lab.simulator migrate|public|internal`.
- `migrations/`: `0001_init` (tables + grants), `0002_narrow_payment_svc_grants`
  (payment_svc loses UPDATE/DELETE on committed evidence; see §13),
  `0003_call_log_outcome_check`, `0004_restrict_connect` (CONNECT revoked from
  PUBLIC; only payment_svc + ledger_reader may connect).

**`oracle/`: the independent evaluator**
- `ledger.py`: `read_ledger(conn, namespace) -> (list[LedgerCharge],
  CallSummary)`. Reads through `ledger_reader`.
- `score.py`: `Claim` (`SUCCEEDED/FAILED/UNRESOLVED/NONE`), `Verdict` (9 values,
  below), `Expectation.from_spec`, `LegacyScore` (reproduces RetryLedger's key
  count), `EpisodeScore`, `score_episode(charges, calls, expectation, raw_claim,
  completed_before_deadline)`, `normalize_claim`. `EVALUATOR_VERSION="oracle-v1"`
  in `oracle/__init__.py`.

**`agent/`: the LLM loop**
- `model.py`: `ToolCall(id, name, arguments)`, `ModelTurn(text, tool_calls, raw,
  input_tokens, output_tokens, malformed)`, `ModelClient` Protocol
  (`model_id: str`, `complete(messages, tools) -> ModelTurn`).
- `scripted.py`: `ScriptedModel(policy)`, `SCRIPTED_POLICIES`, policy plans.
  Deterministic; used by every test. Chooses its step by counting `role=="tool"`
  messages, so tool results MUST be appended as `role: "tool"` messages.
- `prompts.py`: `PROMPT_VERSION="v1"`, `system_prompt(variant)`,
  `user_prompt(task, profile)`.
- `tools.py`: `TOOL_SCHEMA_VERSION="v1"`, `tool_schemas(profile,
  status_available)` in the original `{name, description, input_schema}` shape.
- `client.py`: `TrustedContext(tenant, namespace, run_id, episode_id, task_id)`
  (frozen), `SimulatorClient(public_url, client_token, ctx)` with `charge(args)`
  and `status(key)`. The namespace header comes ONLY from `ctx.namespace`; a
  namespace-like key in the args is denied (recorded, not raised); no method takes
  a namespace parameter (enforced by a signature test). `DENIED_STATUS = 0`.
- `executor.py`: `ToolOutcome`, `Executor` Protocol, `DirectExecutor(client)`
  (mode A: effective == requested), `render_observation(...)` (the single place
  model-facing tool text is produced; delegates to `protocol/render.py` for sent
  requests so compat parity holds).
- `graph.py`: `EpisodeState` (TypedDict: messages, turn, claimed_outcome, done,
  ended_by, optional model_invocation_id), `build_graph(model, executor, tools,
  max_turns, *, recorder, checkpointer, invocations=None, guard=None)`, `run_episode(graph, thread_id, messages=None, *,
  durability="sync", allow_non_sync=False)`. Two nodes (model, tool), conditional
  edge, ends on `report_done` or `max_turns`. The recorder writes `model_calls`
  and `tool_events` rows immediately on an autocommit connection so a SIGKILL
  leaves the evidence written. New-version callers opt into ASCII-encoded
  recorder evidence and retain each model invocation separately; legacy
  first-response snapshots are unchanged.
- `openrouter.py`: `to_openai_tools`, `to_openai_messages`, `parse_completion`
  (pure conversions), `OpenRouterClient` (the real `ModelClient`: retry/backoff on
  429/5xx/transport honoring Retry-After, rate-limit pacing via `min_interval_s`,
  `:free` refusal, once-per-request `RequestBudget.claim`, never raises on a bad
  response body), `OpenRouterError`. `STUDY_MODELS` lives in `runner/batch.py`.

**`execution/`: retry and durable C foundations (Runs 2–3)**
- `identity.py`: UUID5 identity from trusted `(tenant, run_id, task_id)` and B/C's
  effective key; changing a model-requested key does not change this identity.
- `config.py`, `clock.py`: frozen versioned limits, injectable time/deadline checks
  and guard protocols. These settings and the HTTP timeout are saved in experiment
  configuration; each operation retains its first settings and bounded deadline.
- `retry.py`: `RetryExecutor`, at most three charge sends per tool invocation,
  waits of 1/2 seconds, stable key and original arguments, no journal. Valid status
  calls map to that key. Invalid source arguments retain the original refusal path.
- `outcomes.py`: fixed uncertainty/recovery text; `ToolOutcome` has optional
  execution evidence, now persisted in `tool_events.execution_json`.
- `protocol/validation.py` now owns the unchanged simulator validation contract;
  `protocol/evidence.py` provides lossless versioned ASCII storage envelopes,
  connected through opt-in versioned recording; old rows retain their old shape.

- `types.py`, `journal.py`: immutable intent snapshots; first valid arguments win,
  conflicting arguments append a denial, and intended attempts commit before HTTP.
  Operation leases fence changes; terminal state and one outbox event commit together.
- `leases.py`: independently renewed heartbeat with failure detection.
- `reconcile.py`, `gateway.py`, `factory.py`: normal C success/refusal, cached
  outcomes and conservative UNKNOWN/UNRESOLVED handling. Status-based recovery
  belongs to Run 4; production runner integration belongs to Run 5. A foreground
  timeout leaves another live operation owner intact and reports uncertainty.
- `agent/invocations.py`: one row per actual logical model invocation, including
  repeated turns. Its full normalized response includes raw data and text. A late
  response can finish its own audit row but cannot save stale graph state.

**`runtime/`: the runtime database and the budget guard (runs as `runtime_svc`)**
- `db.py`: `runtime_url()` from `LAB_RUNTIME_URL`.
- `budget.py`: `assert_model_allowed(model_id)` (`:free` allowlist, raises
  `ModelNotAllowed`), `RequestBudget(conn, daily_limit=900)` with `claim(n=1)`
  (raises `BudgetExceeded`), keyed on the UTC calendar day. The counter is in the
  database so a second runner process cannot defeat it.
- `checkpoints.py`: creates the LangGraph `PostgresSaver` tables in the RUNTIME
  database (as admin, during migrate) and grants DML on them to `runtime_svc`.
- `__main__.py`: `python -m lab.runtime migrate`.
- `migrations/`: `0001_init` (experiments, runs, model_calls, tool_events,
  crash_points, results, request_budget + grants), `0002_restrict_connect`,
  `0003_run_score_failed` (adds the `score_failed` run state),
  `0004_execution_recovery` (saved resume inputs, run claims, operations, attempts,
  recovery events, outbox, model invocations and run deliveries). This migration
  is additive; it does not invent resume metadata for historical records.
- `fenced_checkpoints.py`: a dedicated saver connection checks and locks the run
  owner in the same transaction as checkpoint/blob/pending-write changes, then
  rechecks expiry before commit. It is restricted to the owned graph thread.
- `serde.py`: preserves unpaired surrogates in JSON-native graph values using
  the existing ASCII codec and a saver-controlled blob tag. Other values use
  the pinned library serializer unchanged. This prevents silent replacement
  with `?` when new-version graphs resume.

**`runner/`: preparation, ownership and existing A orchestration**
- `types.py`, `prepared.py`: reconstructible nonsecret model descriptions, safe
  worker inputs and immutable run preparation. Save pending metadata before
  create-only setup; retry without deleting payments or fault-call position.
  Worker-facing loads explicitly exclude hidden setup/scoring input.
- `ownership.py`: shared A/B/C claims, renewal, guards and fenced finalization.
  Only new-version ready runs can be claimed. The production runner switches
  to this shared path in Task 10.
- `episode.py`: `RunnerConfig(.from_env)`, `run_spec(spec, model, config, *,
  manifest_sha256, experiment_name, experiment_id=None)` → `EpisodeResult`.
  Configures the namespace via the control API, derives `TrustedContext`, builds
  the graph, runs it, reads the ledger through `ledger_reader`, scores with the
  oracle, writes a `results` row. State machine: queued → running →
  completed/crashed, plus `score_failed` if scoring fails after a completed run.
  `open_ledger` checks the connection is `ledger_reader` on the payments DB (not
  the runtime DB). `derive_experiment_id`, `experiment_config`, `new_namespace`,
  `run_is_complete`, `clear_incomplete_run` (deletes a run row only in
  queued/running/crashed: never a scored result).
- `batch.py`: `run_manifest(manifest, model, config, *, experiment_name)` →
  `BatchSummary`; one model per experiment; resumable (skip completed+scored,
  clear+retry incomplete). `build_study_models(config)` (the one seam that builds
  the real client), `STUDY_MODELS`.
- `__main__.py`: `python -m lab.runner run --policy ...` (scripted, offline) and
  `python -m lab.runner study [--reps N] [--seed S] [--experiment-name NAME]` (the
  real-model path; the only path that builds the OpenRouter client).
- `tracing.py`: `force_tracing_off()` (sets LangSmith/LangChain tracing env vars
  off and clears langsmith's memoized lookups; coupled to langsmith 0.14.7).

**`report/`: the behaviour study report**
- `behaviour.py`: `wilson_interval`, `ResultRow`, `CellSummary`, `summarise`,
  `render_markdown`, `write_report(experiment_id, runtime_url, out_dir)`. Reads
  decided verdicts from `runs`/`results` and writes `.md`/`.csv`/`.jsonl`.

### `tests/`: mirrors `src`, plus:
- `conftest.py`: `payments_db` (with `svc`/`reader` URLs), `runtime_db`, `svc_url`
  (truncates payment tables), the autouse session network guard.
- `network_guard.py`: `PROVIDER_HOSTS`, the socket-level guard, `expecting()`.
- `helpers.py`: `CHARGE`, `charges`.
- `reference/retryledger_original.py`: the vendored benchmark. NEVER edit.
- `runner/conftest.py`: `runner_config` (in-process simulator + runtime DB),
  `clean_experiments` (deletes experiments named `runner-test*`). Test experiment
  names MUST start with `runner-test` or rows leak.

---

## 5. The payment simulator contract (see `docs/protocol.md`)

Two hidden axes the model never sees: the **profile** and the **world**.

**Profiles.** `compat` reproduces the original RetryLedger benchmark byte for
byte (model-visible text, legacy score). `controlled` is the one used for the
real study: it validates order/currency and gives identical first errors across
worlds so the only difference between `lost_ack` and `true_fail` is whether the
effect committed, not the error text.

**Worlds** (fault plans, `world_fault_plan`):
- `healthy`: no faults.
- `lost_ack`: the first charge commits, then returns an ambiguous error
  (`commit_then_error`): the effect happened but the caller saw a 504/500. The
  classic "did my charge go through?" case.
- `true_fail`: the first charge genuinely does not commit and returns an error
  (`error_no_commit`); a retry under the same key then commits.
- `permanent_fail`: every charge fails to commit.
Recoverable worlds are `lost_ack` and `true_fail` (`eligible_recoverable`).

**Barriers** (`hold_before_commit`, `hold_after_commit`): pause a charge call at a
named point until released through the control API. This is how the replay
experiment (Plan 2 Task 7) kills a process at an exact instant: configure
`hold_after_commit=(1,)`, wait until the simulator reports the barrier reached
(charge committed, one ledger effect), SIGKILL the runner, release, resume.

**Public API** (port 8100, `Authorization: Bearer <client token>`,
`X-Lab-Namespace` on every request):
- `POST /v1/charges`: JSON body; success is 200 with `message`, `replayed`, and a
  `charge` object (`charge_id`, `idempotency_key`, `order_id`, `amount` as a
  string, `currency`).
- `GET /v1/payments/{idempotency_key}`: 200 with `status` `SUCCEEDED` + charge,
  or `status` `NOT_FOUND` + `charge: null`.
Errors use `{"error": {"code", "message"}}`; FastAPI's own errors (bad token 401,
unparseable request 422) use `{"detail": ...}`. A key containing NUL or an
unpaired surrogate is rejected 400 (Postgres cannot store it). A key containing
`/` is accepted on a charge but cannot be looked up afterward (Starlette decodes
`%2F` before routing): this is a known open item (§14).

**Control API** (port 8101, separate token, bound to 127.0.0.1): `PUT
/internal/namespaces/{ns}` with `profile` and `fault_plan` creates/resets a
namespace; `DELETE` removes it; barrier get/release. The current trusted
orchestrator holds the control token for setup; model-facing `SimulatorClient`
instances receive only the public token. The Plan 4 design proposes separate
worker/sweeper processes without control or ledger credentials; that process
boundary is not implemented yet.

---

## 6. The oracle verdicts (see `src/lab/oracle/score.py`)

Two scores are always reported side by side. `LegacyScore` reproduces
RetryLedger's count of committed keys (ignores the amount). The extended
`Verdict` checks the intended action and whether the agent told the truth:

| Verdict | Meaning |
|---|---|
| `CORRECT` | exactly one intended effect, agent claimed success |
| `CORRECT_NO_EFFECT` | no effect expected, none made, claimed accordingly |
| `MISSED` | an effect was expected, none was made |
| `DUPLICATE` | more than one effect |
| `WRONG_EFFECT` | an effect with the wrong arguments (e.g. wrong amount) |
| `UNEXPECTED_EFFECT` | an effect where none was expected |
| `FALSE_SUCCESS` | claimed success, ledger shows no (intended) effect |
| `FALSE_FAILURE` | claimed failure, ledger shows the effect |
| `UNRESOLVED` | the agent truthfully reported it could not tell |

`correct_recovery = verdict in {CORRECT, CORRECT_NO_EFFECT} AND
completed_before_deadline`, counted only over `eligible_recoverable` trials.
`Claim` is the agent's own `report_done` outcome, normalized from untrusted text.

The scripted policies map to these verdicts predictably; `tests/agent/
test_scripted_verdicts.py` scores each policy against the real oracle. For the
record (controlled profile), in `lost_ack`/`true_fail`: `naive_new_key` →
DUPLICATE/CORRECT, `stable_key` → CORRECT/CORRECT, `check_status` →
CORRECT/CORRECT, `give_up` → FALSE_FAILURE/MISSED, `false_success` →
CORRECT/FALSE_SUCCESS, `wrong_amount` → WRONG_EFFECT/FALSE_SUCCESS,
`malformed_call` → MISSED/MISSED.

---

## 7. The core crash/replay finding (see `docs/replay-findings.md`)

Measured with a real SIGKILL of a subprocess at three kill points, 40 runs per
point, under `durability="sync"`:
- **Before the charge request is sent:** the tool node re-runs on resume and
  sends the charge for the first time. 1 request, 1 effect.
- **After the charge commits, before the step is checkpointed:** the tool node
  re-runs and sends the SAME idempotency key again. The backend answers
  `replayed: true`, so the ledger holds 1 effect. The resumed graph keeps only the
  replay's result and has no memory of the first attempt. The killed first request
  appears in the simulator's `call_log` but not in `tool_events`.
- **After the step is checkpointed:** the tool node does not re-run. 1 request.

The exact boundary (verified in LangGraph's source, not assumed): a node re-runs
when the process died before the task's own writes were saved; the step
checkpoint is sufficient but not necessary. Durability contrast: under the default
`"async"`, an immediate kill after the tool step loses it every time and re-sends
the charge (absorbed as a replay); under `"sync"` it is never lost. This is the
whole justification for `durability="sync"` and for a journal outside the graph
(Plan 4).

---

## 8. How to run everything

Always source the dev env first for anything that touches the DB or simulator:
```bash
cd /path/to/AgentRecovery && set -a && . ./.env.example && . ./.env && set +a
```

- **Tests:** `uv run pytest -q` (whole suite, ~75s, needs Postgres on 54329).
  `uv run ruff check .` and `uv run ruff format --check src tests scripts`.
- **Bring up the stack (Postgres + simulator):** `docker compose up -d`. The
  stack publishes Postgres on 54329, sim-public on 8100, sim-internal on
  127.0.0.1:8101. `docker compose ps` to check health. Do NOT `docker compose
  down` the shared stack.
- **Migrate the runtime DB (needed before any runner run):**
  `uv run python -m lab.runtime migrate` (idempotent; creates the runtime DB,
  `runtime_svc` role, the runtime tables, and the LangGraph checkpoint tables).
- **One scripted episode (offline, no quota):**
  `uv run python -m lab.runner run --policy stable_key --world lost_ack`.
- **The real behaviour study (spends quota: gate on the operator):**
  `uv run python -m lab.runner study --reps 12 --experiment-name <name>`.
  It paces under ~20 req/min; run it to completion in one pass (a partial resume
  can split a study across the fallback models). Resumable: re-running skips
  completed+scored episodes.
- **Generate the report:** call `lab.report.behaviour.write_report(experiment_id,
  os.environ["LAB_RUNTIME_URL"], Path("docs"))`.
- **Tracing (optional):** set `OTEL_EXPORTER_OTLP_ENDPOINT` and bring up Jaeger
  via the `tracing` Compose profile (`docker compose --profile tracing up -d`).
  Off and silent by default.

---

## 9. CI (`.github/workflows/ci.yml`)

Runs on every push and PR. Two jobs: `test` (uv sync --frozen, ruff check, ruff
format --check on `src tests scripts`, pytest, against a Postgres 16 service on
54329) and `stack` (builds the image, brings up the Compose stack, waits for
healthchecks, asserts the control API is published on loopback only, runs
`scripts/smoke_simulator.py`, tears down with `--volumes`). CI uses no real model
key; the no-live-calls guard proves nothing reaches a provider. `uv` is pinned to
0.12.22 and setup-uv to v10.2.0.

---

## 10. How development has been run (continue this way)

Work is executed with the **superpowers subagent-driven-development** workflow:
write a design spec (`docs/superpowers/specs/`), then a bite-sized implementation
plan (`docs/superpowers/plans/`), then execute task by task. Each task: a fresh
implementer, then a task review (spec compliance + code quality), then a fix loop
(resume the implementer, scoped re-review) until clean, then a ledger entry. After
all tasks, a whole-branch review on the most capable model, then merge. Every task
is TDD (write the failing test, see it red, implement, green, commit) and proves
its key tests can fail via mutation. Progress is tracked in a git-ignored ledger
under `.superpowers/sdd/<plan>/progress.md` (deleted after merge; git history is
the record). If you (ChatGPT/Codex) do not have that harness, keep the spirit: a
spec and a plan before code, TDD, a review pass before merge, and small commits.

Branch per plan (`plan-N-...`), off `main`. Never implement on `main` without
consent. The plans landed via local merge + push to `main` (no PRs), each with a
`--no-ff` merge commit, after re-running the full suite on the merged tree.

---

## 11. Databases, roles, migrations (as built)

One Postgres instance on 54329 holds two databases:

**`payments`** (the simulator's ledger). Roles: `payment_svc` (the agent-facing
service; narrowed grants: INSERT/SELECT on charges, no UPDATE/DELETE on committed
evidence; see `simulator/migrations/0002`), `ledger_reader` (read-only, the
evaluator). `CONNECT` is revoked from `PUBLIC`; only those two may connect
(`0004`). Tables: `sim_namespaces`, `charges` (UNIQUE(namespace,
idempotency_key) is the idempotency record), `call_log` (every request with a
CHECK'd `outcome`), `barriers`.

**`runtime`** (the lab's own records). Role: `runtime_svc`. `CONNECT` revoked from
`PUBLIC` (`0002`). Tables: `experiments` (id, name, manifest_sha256, config_json,
frozen_at), `runs` (state in queued/running/completed/crashed/abandoned/
score_failed, UNIQUE(experiment_id, episode_id)), `model_calls`
(UNIQUE(run_id,turn)), `tool_events` (requested vs effective args and key, rule,
observation, http_status), `crash_points`, `results` (run_id PRIMARY KEY,
verdict, correct_recovery, score_json), `request_budget` (one row per UTC day).
The LangGraph `PostgresSaver` checkpoint tables also live here (see §13).

---

## 12. Decisions (see `docs/decisions.md` for full text)

- **D1** Spending is requests/day, not dollars; `:free` only; the $10 credit is
  never drawn down.
- **D2** Model selection was to be gated on a tool-calling conformance pilot
  (superseded by D12).
- **D3** Deployment is private (IAM only), torn down; the showcase is a recorded
  local walkthrough, not a public URL.
- **D4** Real-model matrix sizes: behaviour study 48 episodes (dropped the
  prompt-wording arm); comparison 144 episodes (24 reps/cell).
- **D5** Security test scope: cross-namespace, argument changes under an intent,
  malicious tool-result text, no model access to the control plane/ledger, secrets
  never in observations/logs: all kept at full weight. TLS/dep-scanning/rate-limit
  tuning cut (private + torn down).
- **D6** Extras filtered by resume value; tracing uses GenAI attribute names
  without claiming conformance; Postgres is the scoring source of record.
- **D7** One `src/lab` package, one entry point per process.
- **D8** Plan 1 task-4 coverage and review checkpoint notes.
- **D9** The simulator's call-counter ordering.
- **D10** Baseline-and-treatment framing (see §1).
- **D11** Verified LangGraph/checkpoint-postgres facts for Plan 2;
  `durability="sync"` fairness; the replay boundary is undocumented and was
  measured.
- **D12** No conformance pilot (pick a free model directly); the Plan 3 baseline
  ran on the reachable fallback `nemotron-3-super-120b-a12b:free` because the
  pinned Ultra did not respond.
- **D13** All three modes resume the same saved run after a worker crash, with
  the original namespace, thread, configuration and deadline. This is an accepted
  design rule; the common redelivery path is not implemented yet.

---

## 13. As-built divergences from the roadmap (read this before trusting the roadmap's tables)

The roadmap (`2026-10-04-roadmap.md`) was written up front; the implementation
refined some things. Where they differ, the code wins:

1. **Checkpoints live in the `runtime` database, not a separate `checkpoints`
   database, and there is no `checkpoint_svc` role.** The roadmap's architecture
   diagram shows a separate checkpoints DB; Plan 2 put the LangGraph
   `PostgresSaver` tables in `runtime`, created as admin at migrate time and
   granted to `runtime_svc` (`src/lab/runtime/checkpoints.py`). If Plan 4/8 want a
   separate checkpoints DB, that is a migration not yet done.
2. **Plan 4's additive runtime schema is implemented.** Migration 0004 adds
   the durable journal, invocations, outbox and delivery table. Outbox dispatch
   and the delivery APIs remain pending; a table alone is not a completed queue.
3. **Shared run ownership and checkpoint fencing are implemented as foundations.**
   Production graph redelivery is still pending Task 10. Historical runs have
   null resume metadata and refuse same-run execution without guessed inputs.
4. **`TrustedContext.task_id` is a free `str`** in the code
   (`src/lab/agent/client.py`), not the `Literal["charge"]` the roadmap sketch
   shows. The runner sets it to `f"charge-order-{order_id}"`.
5. **Only mode A (`agent_directed`) is wired into `run_spec`.** Run 2 implemented
   the standalone B executor and verified it through scripted graphs and the real
   simulator. `run_spec` still refuses B/C until Task 10 adds safe setup/resume.
   C's normal path and durable tables are now tested standalone; background
   reconciliation and production integration remain pending.
6. **The conformance pilot was dropped (D12).** The behaviour baseline is on the
   120B fallback model, not Nemotron Ultra (which was unreachable). If you want an
   Ultra baseline, re-run `study` when its free endpoint responds; it becomes its
   own experiment.

---

## 14. Open decisions awaiting the operator

1. **Keys containing `/`.** The Plan 1 simulator accepts a payment key containing
   `/` on a charge but cannot look it up afterward (Starlette decodes `%2F` before
   routing). Decide: document it, or reject `/` in keys. Not yet done.
2. **Leftover test database `runtime_test_cdbb66c7`** (≈8 MB, 0 sessions) on the
   dev Postgres. Drop it or keep it. The controller declined to drop it on a
   subagent's request earlier; awaiting the operator.
**Resolved:** roadmap open decision 10 is now D13: all three modes receive
same-run redelivery. The user selected this during Plan 4 Run 1. A shared restart
policy keeps the comparison focused on the execution treatments; measured ties
remain valid results.

---

## 15. Known deferred items / tech debt (none block merge; fix opportunistically)

- `model_calls` uses `ON CONFLICT (run_id, turn) DO NOTHING` on replay. With the
  deterministic scripted model this loses nothing, but with a non-deterministic
  real model a replayed turn's stored row can disagree with the checkpointed
  transcript and the second provider request goes unrecorded (budget accounting).
  Revisit before any analysis that relies on exact recorded request counts.
- The `study` CLI prints a traceback (not a clean exit) if `BudgetExceeded` or a
  non-`RunnerError` escapes; the in-flight episode is left resumable.
- The `study` stop rule ("completed > 0 or failed == 0") can split a study across
  the fallback models on a partial resume; run to completion in one pass.
- No regression test pins "a retry does not re-claim the budget" (the code is
  correct now; a refactor could silently break it).
- `tests/report/behaviour.py` has a dead `reps` parameter; grammar nit "holds 1
  scored trials".
- A pre-existing Starlette `TestClient` deprecation warning in the suite (from
  importing `fastapi.testclient`).
- Jaeger (`jaegertracing/jaeger:2.4.0`) has never been pulled or run; a real OTLP
  export is untested. Tracing is silent by default.
- The dev runtime DB needs `python -m lab.runtime migrate` to have `0003` applied;
  without it a scoring-failure path raises a CheckViolation.

---

## 16. The road ahead

See the roadmap's Backlog table (§5 there) for all eight sub-plans and their
completion gates. In order:

- **Plan 4: Execution modes A, B, C; the durable gateway.** The big one, and the
  actual fix the project argues for. Build modes B (deterministic retry: effective
  key from the trusted intent, up to 3 charge attempts on ambiguous errors with
  backoff, no journal) and C (durable gateway: intent journal, attempts, leases
  with fencing, reconciliation through authoritative status, outbox, recovery
  sweeper triggered independently of the crashed process). The
  [Run 1 written spec](superpowers/specs/2026-10-19-plan-4-execution-and-recovery-design.md)
  was approved on 2026-10-20. The
  [implementation plan](superpowers/plans/2026-10-20-plan-4-execution-and-recovery.md)
  was approved for direct execution and divides the work into tested session checkpoints.
  Run 2 added trusted identity, shared validation/settings and B. Run 3 adds
  durable intent, normal C execution, safe preparation, ownership/checkpoint
  fencing and model replay evidence. Runs 4–6 add the sweeper, delivery/worker/CLI
  integration and actual process-kill acceptance.
  The design refines the
  older roadmap §§3.3–3.6 and §2: preserve `Executor.execute(call)`, derive B/C
  identity from `(tenant, run_id, task_id)` because episode IDs repeat across
  experiments, and treat status `NOT_FOUND` as provisional evidence. These
  identity is implemented; status reconciliation remains planned for Run 4. D13 separately settles common
  same-run redelivery. Completion gate: every mode
  runs the same scenario showing requested vs effective actions; a SIGKILLed
  runner's operation completes under the same intent with a truthful outcome;
  operation/recovery spans join the run's trace.
- **Plan 5: Crash, concurrency, and security regressions in CI** with real
  process termination, independently verified on the ledger. (Depends on Plan 4.)
- **Plan 6: The three-mode comparison** (144 real-model episodes + held-out
  scripted crash trials), Wilson/Newcombe intervals, the report where every number
  links to raw JSONL. This produces the resume headline. (Depends on Plans 3, 5.)
- **Plan 7: The server-rendered console and the recorded walkthrough** showing a
  worker killed mid-payment and recovered with the ledger visible. (Depends on
  Plan 4.)
- **Plan 8: Private GCP deploy by Terraform, CI with Workload Identity
  Federation, budget controls, verified teardown, recruiter-facing README.**
  (Depends on Plans 5, 7.) Deployment/budget/teardown detail is in the roadmap §7;
  verify current GCP prices on the day.

Modes B and C expected behaviour, stated before measuring (do not assume it): on
the simple worlds and most crash boundaries, B and C tie on effect correctness,
because a stable key plus backend idempotency already prevents duplicates; C
should separate on truthful completion when status evidence is needed, on argument
changes under redelivery, and on backend call count after crashes. Publish
whatever actually happens, including a full tie.

---

## 17. Operational traps worth knowing

- `docker compose build sim-public` does nothing; `sim-migrate` owns the build,
  so build via `docker compose build` (plain) or the migrate service.
- A merge commit message piped with `git merge -F -` fails ("could not read file
  '-'"); write the message to a file and `git merge --no-ff -F <file>`.
- macOS has no `timeout` binary and foreground `sleep` is blocked in this harness;
  wait on conditions by polling the DB or backgrounding the command.
- The simulator handlers must stay synchronous (the client-disconnect guarantee
  depends on it; `tests/simulator/test_client_disconnect.py` pins why).
- Editing a safety-guard test file (e.g. `tests/runner/test_no_live_calls.py`) may
  trip a "CI Bypass" permission hook; it is protecting exactly that, so get
  explicit human approval rather than working around it.
- The real OpenRouter key was pasted into a chat once; treat it as potentially
  exposed and rotate it at OpenRouter when convenient.
