# Plan 4: Execution Modes and Durable Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add stable-key retry and a durable payment gateway, with the same safe restart policy in A/B/C and independently verified crash recovery.

**Architecture:** Keep the synchronous `Executor.execute(call)` graph seam. Put payment intent, attempts, reconciliation and terminal wake-ups in the runtime database; use a separate sweeper for abandoned operations. Give every mode the same leased run ownership, atomically fenced checkpoints, immutable saved inputs and durable graph redelivery.

**Tech Stack:** Python 3.12+, uv, psycopg 3 and hand-written PostgreSQL SQL; existing FastAPI/httpx, LangGraph 1.2.14, checkpoint-postgres 3.1.2 and OpenTelemetry 1.45.0; pytest and ruff. No new dependencies.

**Spec:** [Execution and recovery design](../specs/2026-10-19-plan-4-execution-and-recovery-design.md), approved by the user's “Continue working” instruction on 2026-10-20. D13 settles shared same-run redelivery. The user approved direct execution with “lets go” on 2026-10-20. Run 2 implements Tasks 1–2; no product code was included in Run 1.

## Global Constraints

- Keep Python 3.12+, uv, psycopg 3, hand-written SQL, the existing FastAPI simulator and pinned LangGraph/checkpointer versions. Add no queue service or workflow framework. Keep the single `src/lab` package.
- Keep `durability="sync"` in every mode and store it in experiment configuration.
- Use scripted models and mocked provider HTTP throughout this build and its tests. Preserve the socket-level provider guard. A real-model run requires a separate explicit instruction and a model ID ending in `:free`.
- Preserve Plan 3's measured artifacts and existing results. New execution and resume semantics are versioned in new experiment configurations; historical runs and findings are not rewritten or silently reclassified.
- Keep the grader's payment connection read-only and separate from runtime storage. Recovery uses the public status API, never the evaluator's ledger credentials, fault plan or simulator control API.
- Preserve mode A's requested arguments and compatibility-profile observation parity. New B/C executors support the controlled profile; an attempted B/C compat run is refused before setup or dispatch.
- Keep the shared local Postgres, existing databases and volumes. Migrations are additive and forward-only; tests use their isolated databases. Do not delete the deferred test database or reset old experiment namespaces.
- No console, GCP deployment, load study, real-model comparison, full Plan 5 regression campaign or second workflow in this plan.
- Preserve the user's review-before-commit/push workflow. Each task's last step prepares a reviewable checkpoint; it does not authorize a commit or push. Do not copy earlier plans' Claude attribution.
- Run `uv run ruff check .` and `uv run ruff format --check src tests scripts` at each coding checkpoint, plus that task's tests. The full suite is required at the final gate.

## Review Focus

1. A setup response is lost after namespace creation: repeating preparation must preserve the ledger and fault-call position. Task 4 tests this.
2. An invalid requested key would become valid if rewritten: reject or forward the original invalid request through its existing refusal path; do not repair it. Tasks 1–2 test this.
3. A late original charge commits after status said `NOT_FOUND`: preserve uncertainty, use one key, and settle backend work before final acceptance grading. Tasks 8 and 12 test this.
4. A wake-up arrives while another wake-up is being acknowledged: retain the newer generation and eventually resume the run. Task 9 tests this.
5. A model result returns after ownership was lost: retain invocation evidence without allowing a stale checkpoint or final result. Tasks 5 and 7 test this.

## Repository map and sequencing

Base is `ae76be5`; current branch is `plan-4-durable-gateway`. The untracked `.DS_Store` is unrelated. Inspect status before editing; preserve other work. Source ranges below refer to this base and will move.

| Files | Responsibility / task |
| --- | --- |
| `src/lab/protocol/{validation,evidence}.py`; existing `simulator/service.py` | Shared validation without changing simulator responses (1) |
| `src/lab/execution/{__init__,identity,config,clock,outcomes,retry}.py` | Identity, settings, bounded waits, new observations and B (1–2) |
| `src/lab/runtime/migrations/0004_execution_recovery.sql` | Additive run, journal, invocation and delivery schema (3) |
| `src/lab/execution/{types,journal}.py` | Operation records and fenced transactions (3, 6) |
| `src/lab/runner/{types,prepared,ownership}.py`; `runtime/{fenced_checkpoints,serde}.py` | Immutable prepared runs and common ownership (4–5) |
| `src/lab/execution/{gateway,reconcile,leases,factory}.py` | C executor, evidence classification and recovery work (6, 8) |
| `src/lab/agent/invocations.py`; existing `agent/graph.py` | Preserve every logical model invocation (7) |
| `src/lab/execution/outbox.py`; `runner/deliveries.py` | Terminal events and durable run wake-ups (9) |
| `src/lab/sweeper/{__init__,__main__}.py` | Independent bounded reconciliation process (8) |
| `src/lab/runner/{worker,process,coordinator}.py` | Credential-limited worker and common redelivery (10) |
| Existing `runner/{episode,batch,__main__}.py` | Orchestration, scoring and CLI integration (10–11) |
| Existing `obs.py`; new tests and `docs/recovery-findings.md` | Cross-process traces and acceptance evidence (11–12) |

One plan is appropriate because these parts share the same operation/run ownership and recovery contract. The dependency order deliberately brings common setup and checkpoint fencing forward; C must not be integrated on top of unsafe run ownership.

| Session | Tasks | Natural stop |
| --- | --- | --- |
| Run 1 | Written spec and this plan | Review this plan and choose execution method before code |
| Run 2 | 1–2 | B works in a scripted graph; fixed identity, validation and retry tests pass |
| Run 3 | 3–7 | Durable C normal path works; shared setup, fencing and replay evidence foundations pass |
| Run 4 | 8 | Separate sweeper resolves abandoned operations under bounded leases |
| Run 5 | 9–11 | Durable wake-ups and common A/B/C graph resumption work end to end |
| Run 6 | 12 | Actual SIGKILL acceptance, complete suite and independent review |

Run 3 is the largest checkpoint because safe C depends on shared ownership. Stop at a task boundary within it if needed; retain these dependencies. No incomplete mode is exposed as a completed Plan 4 treatment.

## Exact shared contracts

**Settings.** `ExecutionConfig` is frozen and rejects extra fields. Its JSON contains `execution_protocol_version=1`, `resume_protocol_version=1`, `b_max_attempts=3`, `b_backoff_s=(1.0, 2.0)`, `c_max_attempts=3`, `operation_timeout_s=120.0`, `lease_s=30.0`, `renew_every_s=10.0`, `status_backoff_s=(1.0, 2.0, 4.0, 8.0)`, `tick_s=2.0`. Keep existing runner defaults `max_turns=10`, `episode_deadline_s=300.0`, `http_timeout_s=30.0`. Record the HTTP timeout and all these settings in experiment configuration. Validate finite positive durations, positive attempt counts, sufficient B backoff entries, and `0 < renew_every_s < lease_s`; episode deadline zero remains allowed for existing deadline tests. Reject unsupported protocol versions before setup.

**Identity.** `operation_id(ctx: TrustedContext) -> UUID` uses namespace `e0a30ae5-f624-4f2f-805c-ee7805ef5fa9` and `json.dumps([ctx.tenant, ctx.run_id, ctx.task_id], ensure_ascii=True, separators=(",", ":"))`. `effective_key(ctx: TrustedContext) -> str` returns `arl-<UUID>`. Literal vector: `lab`, `run-001`, `charge-order-42` → `112acdfc-c3cc-539a-9db0-5cc42dfa5e27`. Do not use `episode_id` in place of `run_id`.

**Clock and guards.** `Clock` exposes `now() -> datetime`, `monotonic() -> float`, `sleep(seconds: float) -> None`; `SystemClock` implements it. `DeadlineBudget(deadline_at: datetime, clock: Clock)` exposes `remaining_s() -> float` and `check() -> None`, raising `DeadlineExceeded` at or after expiry. `BoundaryGuard` is a protocol with `check() -> None`; `NoopGuard` is only for existing unversioned callers/tests. Database claims use PostgreSQL `clock_timestamp()` and controlled DB expiries in tests; application backoff uses the injected clock. Never hold a transaction over HTTP or sleeping.

**Tool records.** Keep `ToolOutcome`'s existing fields; append optional `evidence: ExecutionEvidence | None = None`. Define frozen `ExecutionEvidence` in `agent/executor.py`: `operation_id`, `operation_state`, `attempt_no`, `fence_version` (all nullable), `source_kind` (`charge_response`, `status_response`, `transport_uncertainty`, `argument_conflict`, `deadline`), `source_http_status: int | None`, `last_http_tool: str | None`, `cached: bool = False`, `request_count: int = 0`. Store it in nullable `tool_events.execution_json`. For B/C, `effective_args`, `effective_key` and `http_status` describe the last HTTP request/response attempted by that executor invocation; a transport failure has no response status. If C only consulted status, the effective tool is status and the effective arguments contain its key; if it used a cache, these three fields are null. Original charge intent and all earlier requests remain in operation/attempt/recovery records. Cached evidence retains its original source and status in `evidence`, without fabricating a fresh HTTP response. Existing A output/parity stays unchanged.

**New fixed observation text.** Put these literal constants in `execution/outcomes.py`; never interpolate model text into them:

| Constant | Exact value |
| --- | --- |
| `TRANSPORT_UNCERTAIN` | `Payment outcome is unknown because no response was received.` |
| `INTENT_CONFLICT` | `Not executed: this run already has a payment operation with different arguments.` |
| `STATUS_CONFIRMED` | `Payment status confirmed that the recorded payment succeeded.` |
| `STATUS_CONFLICT` | `Payment status conflicts with the recorded payment arguments; success is not confirmed.` |
| `RECOVERY_UNRESOLVED` | `Payment outcome remains unresolved at the recovery deadline.` |

Use existing renderers for actual charge responses and ordinary model-requested status responses. A cached C result repeats the stored terminal observation; `evidence.cached=True` distinguishes it. Do not echo a lost charge response when status was the source of confirmation.

**Evidence storage.** Add `pack_evidence(value: Any) -> dict[str, str]` and `unpack_evidence(envelope: dict[str, str]) -> Any` in `protocol/evidence.py`. The exact envelope is `{"encoding": "json-ascii-v1", "payload": json.dumps(value, ensure_ascii=True, separators=(",", ":"))}`. This preserves NUL/surrogates without placing those characters directly in JSONB. `pack_text(value: str) -> str` uses the same ASCII JSON serialization for untrusted TEXT columns; `unpack_text` reverses it. New-version journal and invocation APIs encode untrusted arguments/responses/outcomes on write and decode on read. New-version `PostgresRecorder` does the same for request/response/tool-call/argument JSON and model-controlled tool names, requested/effective keys and observations; null stays null. Existing unversioned records retain their original shape. Decode only according to the trusted saved protocol version, never by inspecting a model-controlled marker. Graph/model messages remain their original strings; only storage is encoded.

**Test conventions.** Add `tests/execution/conftest.py` using the root isolated `runtime_db` and runtime truncation pattern, scoped only to that subtree. New runner tests retain the existing `runner-test` experiment-name prefix. Pure executor tests use `httpx.MockTransport` and a local fake clock recording sleeps. Database tests use real service-role connections; ownership race tests use two connections and events, not mocks. Subprocess tests install `tests.network_guard.NetworkGuard` before importing a model/worker, and the parent asserts its refusal log is empty. No task runs `study` or reads `.env`.

---

### Task 1: Trusted identity, settings and unchanged source validation

**Files:** Create `src/lab/execution/{__init__,identity,config,clock}.py`, `src/lab/protocol/{validation,evidence}.py`, `tests/execution/{__init__,test_identity,test_config,test_validation,test_evidence}.py`; modify `src/lab/simulator/service.py:71,102,274` and `tests/simulator/test_service.py`.

**Interfaces:** Consume `TrustedContext`, `Profile`, `parse_amount`, `args_hash` and the existing validation responses. Produce the identity/config/clock contracts above. Produce frozen `ValidCharge(key: str, order_id: str | None, amount: Decimal, currency: str | None)`, `ChargeRefusal(http_status: int, body: dict[str, Any])`, `validate_charge(profile: Profile, payload: dict[str, Any]) -> ValidCharge | ChargeRefusal`, and `text_is_storable(value: str) -> bool` in `protocol/validation.py`; produce the lossless evidence codec above in `protocol/evidence.py`.

- [x] Write `test_identity_literal_vector_and_boundaries`; its assertions include:

```python
assert str(operation_id(ctx)) == "112acdfc-c3cc-539a-9db0-5cc42dfa5e27"
assert effective_key(ctx) == "arl-112acdfc-c3cc-539a-9db0-5cc42dfa5e27"
assert operation_id(ctx.model_copy(update={"episode_id": "other"})) == operation_id(ctx)
for field in ("tenant", "run_id", "task_id"):
    assert operation_id(ctx.model_copy(update={field: "different"})) != operation_id(ctx)
```

- [x] Write `test_defaults_are_serializable_and_finite` with the exact settings above, rejected NaN/infinity/invalid renewal intervals, unsupported versions and unchanged zero episode-deadline behavior. Write `test_source_validation_matches_simulator` parameterized over missing/numeric/empty/256-character/NUL/surrogate keys, boolean/nonfinite/sub-cent amounts, extra fields, invalid currencies and equivalent decimal representations; compare status and body with the old service validator before moving it.
- [x] Write `test_evidence_round_trip_keeps_unstorable_and_envelope_shaped_input`: round-trip NUL, lone surrogate, literal backslash-u text, nested arguments and a model dictionary shaped like the trusted envelope; assert restored values equal originals, with no actual NUL/surrogate in packed database values.
- [x] Run `uv run pytest tests/execution/test_identity.py tests/execution/test_config.py tests/execution/test_validation.py tests/execution/test_evidence.py -q`; expect missing new interfaces, then implement those interfaces and move the validator without changing error order, wording or amount rules. Keep `service._validate_charge` as a small adapter to `SimResponse` so existing service callers remain stable. Do not change the slash-key contract.
- [x] Run the new tests plus `uv run pytest tests/unit tests/simulator/test_service.py tests/oracle/test_parity.py -q`; expect no failures. Check lint/format. Prepare the task diff for review before commit.

### Task 2: Mode B and truthful execution records

**Files:** Create `src/lab/execution/{outcomes,retry}.py`, `tests/execution/{test_retry,test_outcomes}.py`; modify `src/lab/agent/executor.py:110` and `src/lab/agent/client.py:118,146,165`; add focused tests in `tests/agent/test_client_binding.py` and `tests/agent/test_observation_parity.py`.

**Interfaces:** Consume Task 1 and existing `DirectExecutor`/`render_observation`. Produce `RetryExecutor(client: SimulatorClient, config: ExecutionConfig, budget: DeadlineBudget, *, clock: Clock, guard: BoundaryGuard)` with `execute(call: ToolCall) -> ToolOutcome`. Extend the client constructor with optional `guard: BoundaryGuard | None = None`, `remaining_s: Callable[[], float] | None = None`; retain `charge(args)` and `status(key)` signatures. Produce `ExecutionEvidence` and exact observation constants above.

- [x] Write `test_retry_uses_one_key_and_two_waits`: give the fake transport three ambiguous responses and a fake clock; assert three charge sends, sleeps `[1.0, 2.0]`, unchanged payment arguments, and only the trusted effective key. Repeat with transport exceptions and assert `TRANSPORT_UNCERTAIN`, `http_status is None`, `request_count == 3` and attempted effective arguments retained.

```python
assert len(sent) == 3
assert {request["idempotency_key"] for request in sent} == {effective_key(ctx)}
assert clock.sleeps == [1.0, 2.0]
assert all(request["amount"] == requested["amount"] for request in sent)
assert outcome.observation == TRANSPORT_UNCERTAIN
assert outcome.http_status is None
```
- [x] Write `test_invalid_source_is_never_repaired`: invalid charge arguments take the original direct refusal path once, without key rewriting or retries; namespace-like arguments remain denied without HTTP. Write `test_status_maps_valid_requested_key_only`: valid model keys map to the trusted key; malformed status arguments retain the current invalid/refusal behavior. No journal rows exist in B.
- [x] Write `test_definitive_statuses_do_not_retry` for 400/401/404/409/422, `test_success_keeps_existing_renderer`, and `test_deadline_stops_wait_and_next_send`. Use the fake clock to exhaust the remaining budget during backoff and a client hook to observe each timeout bounded by remaining time.
- [x] Run `uv run pytest tests/execution/test_retry.py tests/execution/test_outcomes.py -q`; expect missing executor/evidence behavior. Implement source validation before rewriting; valid charge copies keep order/amount/currency, retry only 5xx/transport, at most three sends per invocation. Invalid calls use the existing direct refusal path, with no journal and no repaired key. Status is not automatically retried by B. A final transport failure uses the fixed uncertainty text; do not invent an HTTP status.
- [x] Apply the optional client guard and timeout cap immediately before each actual HTTP request. Do not change a supplied HTTP client's old timeout behavior when the new hooks are absent. A guard failure propagates to the worker; it is not a payment response. Build a scripted graph directly around B with `InMemorySaver` to prove the executor seam works; production `run_spec` remains A-only until Task 10 supplies safe preparation/resume.
- [x] Run `uv run pytest tests/execution tests/agent/test_executor.py tests/agent/test_client_binding.py tests/agent/test_observation_parity.py tests/agent/test_scripted_verdicts.py -q`; expect no failures. Check lint/format and stop for Run 2 review before commit.

### Task 3: Additive persistence and operation registration

**Files:** Create `src/lab/runtime/migrations/0004_execution_recovery.sql`, `src/lab/execution/{types,journal}.py`, `tests/execution/{conftest,test_journal}.py`; modify `tests/runtime/test_runtime_db.py`, `tests/runtime/test_connect_isolation.py`.

**Interfaces:** Consume `ValidCharge`, trusted identity and configuration. Produce `OperationState` with the seven spec states; `Operation` frozen snapshot containing trusted context, original requested arguments/hash, effective key, state, attempt count, ownership/version, due/deadline timestamps, immutable `config: ExecutionConfig`, `last_evidence: dict[str, Any] | None` and `terminal_outcome: ToolOutcome | None`; `OperationClaim(operation_id: str, owner: str, version: int)`; `IntentConflict` and `LeaseLost` exceptions. Produce `Journal(conn: Connection[Any])` with `register(ctx: TrustedContext, charge: ValidCharge, requested_args: dict[str, Any], deadline_at: datetime, *, config: ExecutionConfig) -> Operation` and `get(operation_id: str) -> Operation`. Connections are service-role autocommit connections; each mutation owns an explicit short transaction.

- [x] Write `test_migration_preserves_old_rows_and_grants`: insert legacy run/model/tool/result records using migrations through 0003, apply 0004 twice through the migration runner, and assert old values unchanged, `resume_version is None`, runtime DML grants present, and ledger reader still unable to connect to runtime. Do not backfill invented run specifications or model invocations.
- [x] Write `test_register_is_unique_under_race` with two connections; assert one operation ID/key, state `READY`, zero attempts and the first canonical argument hash. Write `test_conflict_does_not_mutate_existing_intent`, including terminal rows and equivalent decimal strings. A first valid wrong amount remains stored wrong.

```python
assert first.id == second.id == str(operation_id(ctx))
assert first.state == OperationState.READY
assert first.attempt_count == 0
assert journal.get(first.id).args_hash == args_hash("1234", Decimal("49.99"), "USD")
```
- [x] Run `uv run pytest tests/execution/test_journal.py tests/runtime/test_runtime_db.py -q`; expect missing schema/journal. Implement migration and registration with uniqueness plus compare/read, not last-writer-wins argument replacement. Use the Task 1 codec at journal storage boundaries. Save the operation settings on first registration and verify them against the parent saved run. Commit a conflict audit event before raising `IntentConflict`, so transaction rollback does not erase the denial evidence.

Migration shape, decided here so later tasks share column names:

| Table | Additions / required constraints |
| --- | --- |
| `runs` | Nullable `resume_version`, `episode_spec_json`, `worker_input_json`, `execution_config_json`, `initial_messages_json`, `model_descriptor_json`, `trace_carrier_json`, `setup_state`; nullable `claim_owner`, `lease_expires_at`; `version BIGINT NOT NULL DEFAULT 0`. `setup_state` is `pending` or `ready`; new resumable rows require all saved input fields except optional trace carrier. Legacy null metadata remains permitted. Existing state values and unique experiment/episode constraint remain. |
| `operations` | `id TEXT PK`, `run_id FK`, `tenant`, `namespace`, `task_id`, `effective_key`, `requested_args_json`, `args_hash`, `config_json`, `state`, `attempt_count DEFAULT 0`, `version DEFAULT 0`, `claim_owner`, `lease_expires_at`, `next_check_at`, `status_check_count DEFAULT 0`, `deadline_at`, `last_evidence_json`, `terminal_outcome_json`, `created_at`, `finished_at`; unique `(tenant, run_id, task_id)`, unique `(namespace, effective_key)`, state check, nonnegative counters and paired owner/lease. |
| `attempts` | `id TEXT PK`, `operation_id FK`, `attempt_no`, `fence_version`, `owner`, `started_at`, nullable `finished_at`, `http_status`, `response_json`, `error_type`; unique `(operation_id, attempt_no)`. A recorded intended send is not proof the simulator received it. |
| `recovery_events` | `id BIGSERIAL`, `operation_id FK`, `event_type`, `fence_version`, `details_json`, `created_at`; append-only through the journal API. |
| `outbox` | `id TEXT PK` equal to `terminal-<operation_id>`, unique `operation_id FK`, `run_id FK`, `available_at`, `delivered_at`, `delivery_attempts DEFAULT 0`, `claim_owner`, `lease_expires_at`, `version DEFAULT 0`. One terminal event per operation. |
| `model_invocations` | `id TEXT PK`, `run_id FK`, `delivery_version`, `turn`, `provider_model_id`, `request_json`, nullable response/tool-calls/token/latency/error fields, `started_at`, `finished_at`; index `(run_id, turn, started_at)`. No unique run/turn constraint here. |
| `tool_events` | Nullable `execution_json JSONB`, `model_invocation_id TEXT` referencing `model_invocations`. Existing rows unchanged. |
| `run_deliveries` | `run_id TEXT PK/FK`, `available_at`, `generation BIGINT DEFAULT 1`, `acknowledged_generation BIGINT DEFAULT 0`, `claim_owner`, `lease_expires_at`, `version DEFAULT 0`, `delivery_attempts DEFAULT 0`; index due/unacknowledged work; `0 <= acknowledged_generation <= generation`. |

Use TIMESTAMPTZ for timestamps, JSONB for structured evidence and cascading cleanup only through the existing test/experiment deletion lifecycle. Grant runtime DML and sequences explicitly. Add partial due-work indexes for nonterminal operations, undelivered outbox and runnable deliveries. Do not modify checkpoint library tables or payments grants.

- [x] Run `uv run pytest tests/execution/test_journal.py tests/runtime -q`; expect no failures. Check lint/format and prepare review before commit.

### Task 4: Immutable preparation without destructive setup retries

**Files:** Create `src/lab/runner/{types,prepared}.py`, `tests/runner/test_prepared.py`; modify `src/lab/runner/episode.py:129,196,328`, `src/lab/simulator/service.py:137`, `src/lab/simulator/app.py:109`, `docs/protocol.md`, `tests/simulator/test_http.py`, `tests/simulator/test_service.py`.

**Interfaces:** Consume Task 3 fields and `RunnerConfig`. Define the following data models in `runner/types.py` and preparation functions in `runner/prepared.py`, using type-only/deferred imports of the existing orchestration helpers to avoid an episode/prepared import cycle. Produce frozen `ModelDescriptor(kind: Literal["scripted", "openrouter", "injected"], model_id: str, policy: str | None = None, parameters: dict[str, Any] = {})`, rejecting secrets and unknown parameters. `WorkerInput` contains only trusted context, profile, task, prompt variant, status availability, model descriptor and saved limits; no hidden fault plan or expected verdict. `PreparedRun` contains saved identifiers, safe `WorkerInput`, initial messages and timestamps; `load_prepared` selects these explicit fields and never loads hidden setup/scoring input. Produce `prepare_run(spec: EpisodeSpec, model: ModelDescriptor, config: RunnerConfig, *, manifest_sha256: str, experiment_name: str = "adhoc", experiment_id: str | None = None, clock: Clock) -> PreparedRun`, and `load_prepared(conn: Connection[Any], run_id: str) -> PreparedRun`; absent metadata raises `LegacyResumeUnsupported`.

- [x] Write `test_prepare_is_immutable_and_idempotent`: repeat preparation for the same experiment/episode, assert same run/namespace/thread/start/deadline, unchanged saved inputs and one logical namespace creation. Changed model/config/manifest is refused before any reset. Secret values never enter either saved input JSON or repr.
- [x] Write `test_lost_setup_ack_preserves_existing_payment`: create namespace, commit a payment, simulate lost setup acknowledgement before `setup_state='ready'`, repeat preparation, and assert the effect and call-log entries still exist. Two concurrent preparations must share the same row. A pending setup cannot be delivered to a worker.

```python
assert resumed.run_id == original.run_id
assert resumed.deadline_at == original.deadline_at
assert ledger_after == ledger_before
assert call_log_after == call_log_before
```
- [x] Run `uv run pytest tests/runner/test_prepared.py -q`; expect missing preparation. Add `service.configure_namespace_once(conn, namespace: str, profile: Profile, plan: FaultPlan) -> None`, using insert-if-absent and verifying existing profile/plan without deletion. A mismatch raises `NamespaceConfigConflict`.
- [x] Expose create-only behavior on the existing internal PUT when `If-None-Match: *` is supplied: 200 for new or identical existing configuration, 409 for conflicting configuration. Existing PUT without that header retains its documented reset behavior for old callers. Update protocol and HTTP tests; no new model tool or privilege is introduced.
- [x] Save the immutable run row in `pending` before setup; finish create-only setup and mark it `ready`. Retrying an interrupted pending preparation uses the same IDs and create-only request. Stamp start/deadline once, using the existing deadline convention, and preserve them on retry. `worker_input_json` is an explicit safe projection; worker code must not deserialize `episode_spec_json` or inspect the hidden fault plan. Full `EpisodeSpec` remains available only to trusted orchestration for setup/scoring.
- [x] Add `execution: ExecutionConfig = field(default_factory=ExecutionConfig)` to frozen `RunnerConfig`; extend its finite timeout/deadline validation while retaining zero episode deadline. Extend `experiment_config(model_id, config, manifest_sha256, *, model_descriptor: ModelDescriptor | None = None)` with version/settings and the descriptor; absent descriptor explicitly means injected model. Add `describe_model(model: ModelClient) -> ModelDescriptor` in `runner/prepared.py` and pass its result consistently from `run_spec`/`run_manifest` so their experiment hashes agree. Test stable hashes and legacy/config mismatch refusal. Injected model descriptors permit the existing in-process dependency-injection API but require a supplied model with matching ID on resume; they cannot be reconstructed by the process CLI. OpenRouter descriptors allow only the current constructor's nonsecret parameters: `base_url="https://openrouter.ai/api/v1"`, `min_interval_s=3.0`, `max_retries=5`, `timeout_s=60.0`. Save these effective values with the experiment/run; do not invent a temperature setting the client does not currently send. Provider keys remain external. Scripted policy must belong to `SCRIPTED_POLICIES`; never deserialize arbitrary callables or pickle.
- [x] Export `DEFAULT_BASE_URL` from the existing allowed `agent/openrouter.py` and consume it when building descriptors; do not copy the provider-host literal into another source/test file or widen the host-scan allowlist. Include this small constant change in Task 4's diff. Expose `ScriptedModel.policy: str` and `OpenRouterClient.parameters: dict[str, Any]` as fresh nonsecret copies for `describe_model`; store the requested timeout explicitly. Built-in default clients are reconstructible; arbitrary/injected model implementations or custom HTTP transports use `kind="injected"` and require in-process injection. Add these small changes to `agent/scripted.py` and `agent/openrouter.py` in Task 4. Never read private keys to construct descriptors.
- [x] Run `uv run pytest tests/runner/test_prepared.py tests/simulator/test_http.py tests/simulator/test_service.py tests/oracle/test_parity.py -q`; expect no failures. Check lint/format; prepare review before commit.

### Task 5: Common run claims and atomic checkpoint fencing

**Files:** Create `src/lab/runner/ownership.py`, `src/lab/runtime/{fenced_checkpoints,serde}.py`, `src/lab/execution/leases.py`, `tests/runner/test_ownership.py`, `tests/runtime/test_fenced_checkpoints.py`; modify `src/lab/agent/client.py` only if Task 2's hook needs composition.

**Interfaces:** Produce `RunClaim(run_id: str, thread_id: str, owner: str, version: int, deadline_at: datetime)`, `claim_run(conn, run_id: str, owner: str, lease_s: float = 30.0) -> RunClaim | None`, `renew_run(conn, claim: RunClaim, lease_s: float = 30.0) -> bool`, `assert_run_owner(conn, claim: RunClaim) -> None`, and `finish_run(conn, claim: RunClaim, *, claimed_outcome: str | None, finished_at: datetime, state: str) -> None`. Expired or superseded claims raise `LeaseLost`; a live duplicate gets no claim. `RunGuard(conn, claim: RunClaim, budget: DeadlineBudget).check() -> None` checks both ownership and deadline. `LeaseHeartbeat(renew: Callable[[], bool], *, interval_s: float = 10.0)` is a context manager with `tick_once() -> bool` and `check() -> None`; use a separate connection from checkpoint/model/tool I/O.

- [x] Write `test_only_one_live_delivery_and_monotonic_takeover`: two connections compete; assert one owner, version increments after controlled expiry, and old renew/finalize fail. Repeat for every mode. Write `test_heartbeat_survives_blocked_model_call` using an event-controlled model and manual `tick_once`, not a 30-second sleep.
- [x] Write `test_stale_checkpoint_and_pending_writes_are_rejected`: first worker saves a checkpoint, expires, replacement claims; old saver `put` and `put_writes` both raise `LeaseLost`, and checkpoint/blobs/writes are unchanged. Also exercise lease expiry during a checkpoint transaction and rollback of every write. Reads needed for resumed invocation use the new claim.

```python
assert replacement.version > original.version
with pytest.raises(LeaseLost):
    old_saver.put(config, checkpoint, metadata, new_versions)
with pytest.raises(LeaseLost):
    old_saver.put_writes(config, [("messages", ["stale"])], "task-1")
assert checkpoint_rows_after == checkpoint_rows_before
```
- [x] Run `uv run pytest tests/runner/test_ownership.py tests/runtime/test_fenced_checkpoints.py -q`; expect missing ownership/saver. Implement `claim_run` with conditional UPDATE/row locking and PostgreSQL time, allowing only ready new-version queued/crashed/expired-running runs. Do not claim completed, abandoned or `score_failed` runs. A takeover never changes original inputs or deadline; increment `delivery_count` on each successful execution claim.
- [x] Implement `FencedPostgresSaver(conn: Connection[Any], claim: RunClaim)` as a small subclass of pinned `PostgresSaver`. Override `_cursor(*, pipeline: bool = False)` with a serialized, explicit transaction: acquire the saver lock, lock/check the run row, yield a binary dict-row cursor, recheck lease before commit, rollback on loss. Do not call the parent `_cursor` while holding its non-reentrant lock, and do not use a persistent pipeline. Keep inherited serialization/SQL and `get_next_version`. Minimal public-method wrappers must reject a thread ID other than `claim.thread_id` before delegating `put`, `put_writes` and reads; unfiltered cross-thread listing is disabled. Reject pool/pipeline inputs and disable `setup`/`delete_thread` on the worker adapter. The connection is dedicated to this saver, autocommit outside its short transactions. No HTTP runs while it holds the run row lock.
- [x] Add an installed-version contract test that `put` and `put_writes` still go through this boundary. Source inspected while writing this plan: checkpoint-postgres 3.1.2 calls `_cursor(pipeline=True)` for both and otherwise uses the same SQL serialization; this is a narrow private hook, so dependency changes must fail loudly. Run a real graph/checkpoint round trip, not only direct saver calls. Heartbeat locks must not deadlock checkpoint writes.
- [x] Run `uv run pytest tests/runner/test_ownership.py tests/runtime/test_fenced_checkpoints.py tests/runtime/test_checkpoints.py tests/agent/test_replay.py -q`; expect no failures. Check lint/format and prepare review before commit.

### Task 6: Durable C normal path and replayed outcomes

**Files:** Modify `src/lab/execution/{types,journal}.py`; create `src/lab/execution/{gateway,reconcile,factory}.py`, `tests/execution/{test_gateway,test_claims}.py`.

**Interfaces:** Extend `Journal` with `claim(operation_id: str, owner: str, lease_s: float) -> OperationClaim | None`, `renew(claim: OperationClaim, lease_s: float) -> bool`, `release(claim: OperationClaim) -> None`, `begin_attempt(claim: OperationClaim, max_attempts: int) -> Attempt`, and `apply(claim: OperationClaim, update: OperationUpdate) -> Operation`. `Attempt` exposes `id`, `number`, `fence_version`, `effective_args`; `OperationUpdate` is frozen with fields `state: OperationState`, `evidence: dict[str, Any]`, `next_check_at: datetime | None = None`, `status_check_count: int | None = None`, `attempt_id: str | None = None`, `http_status: int | None = None`, `response_body: dict[str, Any] | None = None`, `error_type: str | None = None`, `terminal_outcome: ToolOutcome | None = None`. `apply` owns attempt completion, event append and terminal outbox insertion atomically. Produce `Reconciler(journal: Journal, client: SimulatorClient, config: ExecutionConfig, *, clock: Clock, guard: BoundaryGuard)` with `step(operation_id: str, owner: str) -> Operation`. Produce `GatewayExecutor(client, journal, reconciler, config, budget, *, clock, guard)` with `execute(call) -> ToolOutcome`. `make_executor(mode: Mode, client, config, budget, *, clock, guard, journal: Journal | None = None) -> Executor` refuses C without a journal.

- [x] Write `test_intent_and_attempt_precede_send`: inspect runtime from inside a transport callback; assert saved original args/hash, state `IN_FLIGHT`, attempt number 1 and the trusted key before accepting HTTP. Valid success/replay with matching key/order/amount/currency yields `CONFIRMED_SUCCESS` and exactly one terminal outbox row.
- [x] Write `test_cached_success_makes_no_request`: repeat identical C call and assert unchanged attempt count, null effective request/status, original observation, `evidence.cached is True`. Changed canonical arguments produce `INTENT_CONFLICT`, no send, and an appended conflict event without changing state/intent, including a terminal operation. Equivalent amounts reuse the operation; a valid first wrong amount is never corrected.

```python
assert cached.observation == first.observation
assert cached.evidence.cached is True
assert cached.effective_args is cached.effective_key is cached.http_status is None
assert request_count_after == request_count_before
assert conflict.observation == INTENT_CONFLICT
assert journal.get(op_id).args_hash == original_hash
```
- [x] Write `test_operation_claim_fences_attempts_and_terminal_commit`: two owners, expiry/takeover, stale response and terminal-write rollback. Assert attempt count increments atomically before sending and never exceeds configured three, including after restart. A crash between registration and first send leaves recoverable `READY` or recorded intended attempt evidence.
- [x] Run `uv run pytest tests/execution/test_gateway.py tests/execution/test_claims.py -q`; expect missing journal transitions/gateway. Implement short transactional operation claims and fenced mutations. `claim` increments version; expired `IN_FLIGHT` becomes `UNKNOWN` for status reconciliation. `begin_attempt` only accepts `READY`/provisional `CONFIRMED_NO_EFFECT`, remaining budget and unexpired deadline. Terminal writes clear ownership and insert `terminal-<operation_id>` in the same transaction.
- [x] Implement the foreground step initially for valid success/replay, definitive refusal and ambiguous classification; ambiguous work remains durably `UNKNOWN`. Matching structured 2xx evidence is required; malformed 2xx is uncertain. A foreground gateway drives bounded steps or waits on existing ownership until terminal/deadline, renewing its operation lease via Task 5's heartbeat. Source validation and status key mapping follow B; status-only lookup never registers an operation. Gate all foreground work through the common run guard.
- [x] Run `uv run pytest tests/execution tests/agent/test_observation_parity.py -q`; expect no failures. An ambiguous operation with no available recovery result must end truthfully `UNRESOLVED` at its deadline; Task 8 adds its recovery transitions. Check lint/format; production runner integration remains gated until Task 10.

### Task 7: Record every logical model invocation and its checkpoint identity

**Files:** Create `src/lab/agent/invocations.py`, `tests/agent/test_invocations.py`; modify `src/lab/agent/graph.py:51,61,127,154`, `src/lab/agent/openrouter.py:43,68`, relevant graph/recorder test doubles in `tests/agent/test_graph.py` and `tests/runner/test_no_live_calls.py`.

**Interfaces:** Produce `InvocationJournal(conn, claim: RunClaim)` with `start(*, turn: int, provider_model_id: str, messages: list[dict], tools: list[dict]) -> str`, `finish(invocation_id: str, result: ModelTurn, latency_ms: float) -> None`, `fail(invocation_id: str, error_type: str, latency_ms: float) -> None`. Extend `build_graph(..., invocations: InvocationJournal | None = None, guard: BoundaryGuard | None = None)`; add optional `model_invocation_id` to state. Extend `record_tool_event` with optional `model_invocation_id: str | None = None`; retain existing `record_model_call` as first-response compatibility snapshot. Add optional `guard` and `remaining_s` constructor hooks to `OpenRouterClient`, leaving legacy behavior unchanged when absent.

- [x] Write `test_replayed_turn_retains_both_invocations`: run the same logical model turn twice before checkpointing; assert two invocation IDs/response rows, distinct delivery versions where applicable, original legacy `model_calls` row unchanged, checkpoint state referencing the second actual response and tool-event reference matching it.

```python
assert len(rows) == 2
assert rows[0]["id"] != rows[1]["id"]
assert legacy_first_response_after == legacy_first_response_before
assert state["model_invocation_id"] == rows[1]["id"]
assert tool_event["model_invocation_id"] == rows[1]["id"]
```
- [x] Write `test_kill_before_model_response_leaves_started_unknown`: park the mock model after `start`; assert a durable invocation exists with null response/finish. Write `test_late_model_response_is_evidence_only`: after takeover, the original invocation may record its own response, but cannot change saved graph state, run result or start more work. The journal's finishing method can only complete its own matching invocation, never another delivery's row; it is observational evidence, not permission to execute.
- [x] Write `test_deadline_and_ownership_checked_before_provider_retry`: mock HTTP, pacing and retry waits; expire the budget/lease between attempts and assert no later send or extra budget claim. Provider retries still use the existing daily budget contract. One logical invocation must not be misreported as an exact HTTP-request count.
- [x] Run `uv run pytest tests/agent/test_invocations.py -q`; expect missing invocation lifecycle. Begin recording before `model.complete`, finish/fail after it, and place its ID in the same graph update as the actual assistant response. Pass that ID when recording tool events. Keep the provider payload and tool schemas unchanged; internal IDs do not enter model text. Check the common guard before model work and every executable tool call.
- [x] Apply provider hooks before each actual HTTP attempt, after pacing/backoff and before quota claim; cap each request timeout by remaining time. If a call already in progress finishes late, save its evidence and let the guard/checkpointer prevent further stale execution. Never rewrite the returned model claim or fill in a guessed response after a crash.
- [x] Use the Task 1 evidence codec and add optional `encoded_evidence: bool = False` to `PostgresRecorder`; new-version workers set it true. Write `test_unstorable_arguments_are_recorded_losslessly` against real Postgres: restore original request/response/tool-call JSON and untrusted TEXT exactly, including NUL/surrogate keys, without changing model-visible refusal text. Historical rows and unversioned caller shapes remain unchanged; version-aware audit readers decode new rows before comparisons. Do not weaken validation to avoid testing this path.
- [x] Run `uv run pytest tests/agent tests/runner/test_no_live_calls.py -q`; expect no failures. Check lint/format and stop for Run 3 review before commit; do not claim end-to-end redelivery yet.

### Task 8: Reconciliation and a separate sweeper

**Files:** Modify `src/lab/execution/{reconcile,journal,leases}.py`; create `src/lab/sweeper/{__init__,__main__}.py`, `tests/execution/test_reconcile.py`, `tests/runner/test_sweeper_process.py`.

**Interfaces:** Complete `Reconciler.step` from Task 6, using each operation's saved `ExecutionConfig`, never a sweeper default that replaces its recorded limits. Extend `Journal` with `due_operation_ids(limit: int) -> list[str]`. Produce frozen `SweeperConfig(runtime_url, sim_public_url, client_token)` with secret repr exclusions and a strict environment loader requiring only runtime/public credentials. `sweep_once(config: SweeperConfig, *, owner: str, limit: int = 100, clock: Clock | None = None) -> int` returns work items processed, not confirmed effects. CLI: `python -m lab.sweeper --once [--limit 100]` or the default 2-second loop; no model/ledger/control imports.

- [ ] Write `test_status_confirmation_names_its_source`: ambiguous charge → matching `SUCCEEDED` status → success, `STATUS_CONFIRMED`, status evidence and terminal event. Malformed status/2xx stays unknown; mismatched successful status produces `STATUS_CONFLICT` and terminal conflict evidence. Never read the payments database directly.
- [ ] Write `test_not_found_is_provisional_and_retries_keep_original_args`: `NOT_FOUND` permits another same-key/original-args send while attempts/time remain. After three intended sends, continue status only; ultimately `UNRESOLVED`, never proven no-effect failure. Test a delayed original charge committing after `NOT_FOUND`, then settle it and assert one effect. Do not equate the conservative intended-attempt count with received HTTP calls.
- [ ] Write `test_status_backoff_survives_restart`: assert persisted schedules of 1, 2, 4, 8, 8 seconds and an operation deadline `min(registration + 120 seconds, run_deadline)`. Temporary outage later resolves; permanent outage expires truthfully. No new send at or after deadline. Test expired lease/takeover while HTTP is held, with stale response rejected and operation heartbeat renewing on a separate connection.

```python
assert observed_delays == [1.0, 2.0, 4.0, 8.0, 8.0]
assert exhausted.attempt_count == 3
assert exhausted.state == OperationState.UNKNOWN
assert expired.state == OperationState.UNRESOLVED
assert expired.terminal_outcome.observation == RECOVERY_UNRESOLVED
```
- [ ] Run `uv run pytest tests/execution/test_reconcile.py tests/runner/test_sweeper_process.py -q`; expect missing transitions/process. Implement the spec state table: due ready work dispatches; unknown work queries status first; valid no-effect observation may authorize retry; malformed/unavailable evidence schedules another check. Charge refusals retain their real evidence without claiming earlier requests had no effect. Keep transaction scopes separate from all HTTP.
- [ ] Launch a real sweeper child with only runtime/public credentials and the network guard, after terminating a foreground worker. Assert recovery proceeds without model, control or ledger credentials, no lease/backoff reset, and a nonzero processed count. Failure of one item must leave other due items recoverable and log only IDs/error types, not credentials/bodies. A one-tick invocation is bounded by limit and per-request remaining deadlines.
- [ ] Run `uv run pytest tests/execution tests/runner/test_sweeper_process.py tests/runtime/test_fenced_checkpoints.py -q`; expect no failures. Check lint/format; stop for Run 4 review before commit.

### Task 9: Atomic outbox handoff and durable run deliveries

**Files:** Create `src/lab/execution/outbox.py`, `src/lab/runner/deliveries.py`, `tests/execution/test_outbox.py`, `tests/runner/test_deliveries.py`; modify `execution/journal.py` only to share the terminal event contract.

**Interfaces:** Produce `DeliveryClaim(run_id: str, owner: str, version: int, generation: int)`. `enqueue_run(conn, run_id: str, *, available_at: datetime) -> None` increments a durable generation; `claim_delivery(conn, owner: str, lease_s: float = 30.0) -> DeliveryClaim | None`; `ack_delivery(conn, claim: DeliveryClaim) -> None` acknowledges only its captured generation; `retry_delivery(conn, claim: DeliveryClaim, available_at: datetime) -> None`. `dispatch_outbox_once(conn, *, owner: str, limit: int = 100) -> int` durably transfers terminal events to `run_deliveries` and marks each delivered in the same runtime transaction. All claim/ack mutations compare owner/version and expiry.

- [ ] Write `test_terminal_state_and_event_commit_together`: force failure between those statements; assert neither terminal state nor event commits. Then commit, crash before dispatch and assert a later dispatcher produces a durable wake-up.
- [ ] Write `test_handoff_and_ack_are_atomic_under_replay`: crash after enqueue but before outbox delivered marking; rollback leaves event pending and no partial handoff. Repeat the same event and assert one durable run queue row, no new payment operation, and one eventual final run result. An event is never marked delivered merely because an in-memory callback returned.
- [ ] Write `test_new_generation_survives_old_ack`: claim generation 1, enqueue another wake-up, acknowledge generation 1; assert `generation == 2`, `acknowledged_generation == 1`, and another claim remains possible. Expired dispatcher acknowledgements fail; rows for completed/score_failed runs are durably satisfied without invoking the graph.

```python
assert row["generation"] == 2
assert row["acknowledged_generation"] == 1
assert next_claim.generation == 2
assert operation_count_after == operation_count_before
```
- [ ] Run `uv run pytest tests/execution/test_outbox.py tests/runner/test_deliveries.py -q`; expect missing delivery APIs. Implement per-run upsert/generation tracking and short `FOR UPDATE SKIP LOCKED` claims. If the run has a live owner, postpone processing rather than launch another worker or lose the wake-up. `score_failed` wakes trusted scoring only. A crashed dispatcher/worker leaves an expiring claim and durable pending work.
- [ ] Run both test files plus `tests/execution/test_gateway.py`; expect no failures. Check lint/format and prepare review before commit.

### Task 10: Credential-limited worker and shared A/B/C resume

**Files:** Create `src/lab/runner/{worker,process,coordinator}.py`, `tests/runner/{test_worker,test_resume,test_process,test_coordinator}.py`; modify `src/lab/runner/episode.py:442,603`, `src/lab/runner/batch.py:55` and `tests/runner/{test_episode,test_batch}.py`.

**Interfaces:** Produce `WorkerConfig(runtime_url, sim_public_url, client_token, *, provider_key: str | None = None)` with no control/ledger/admin fields and secret repr exclusions. `ExecutionResult(run_id: str, claimed_outcome: str | None, ended_by: str | None, turns: int, expired: bool)` is an unscored execution summary. `execute_delivery(run_id: str, model: ModelClient, config: WorkerConfig, *, owner: str, clock: Clock | None = None) -> ExecutionResult | None` claims ready work or returns no work; it never configures namespaces or scores. `resume_run(run_id: str, model: ModelClient, config: RunnerConfig, *, clock: Clock | None = None) -> EpisodeResult` is trusted orchestration that executes if needed and scores. Existing `run_spec` signature remains and composes preparation, execution and scoring.

`worker_env(config: WorkerConfig, *, model: ModelDescriptor, guard_log: str | None = None) -> dict[str, str]` builds a fresh allowlist: `LAB_RUNTIME_URL`, `SIM_PUBLIC_URL`, `SIM_CLIENT_TOKEN`, `PYTHONUNBUFFERED=1`, tracing switches forced off for LangSmith, optional `OTEL_EXPORTER_OTLP_ENDPOINT`/`OTEL_SDK_DISABLED`, and test-only `LAB_NETWORK_GUARD_LOG`. Only OpenRouter workers additionally receive `OPENROUTER_API_KEY`. Use the absolute interpreter and installed package/repository cwd, not inherited PATH/PYTHONPATH or arbitrary parent environment; `launch_worker(run_id: str, config: WorkerConfig, *, model: ModelDescriptor) -> subprocess.Popen[str]` starts `sys.executable -m lab.runner.worker --run-id <id>`. `launch_sweeper(config: SweeperConfig, *, once: bool = False) -> subprocess.Popen[str]` uses the same runtime/public allowlist, without a provider key. `coordinator_once(config: RunnerConfig, *, owner: str, limit: int = 100) -> int` discovers expired ready runs in every mode, dispatches outbox, launches/collects pending workers with an explicit local limit of one live graph worker by default and performs trusted finalization/scoring. Expose `worker.main(argv: Sequence[str] | None = None) -> int` for direct module launch.

- [ ] Write parameterized `test_resume_keeps_saved_run_in_every_mode`: crash before dispatch, after external commit and after saved graph state. Assert identical run ID, namespace, graph thread, model/config, original deadline and retained evidence. Resume with `messages=None` when any checkpoint exists, or stored initial messages if none exists. Never call setup or delete the run from a delivery.
- [ ] Write `test_deadline_and_terminal_states_do_not_reexecute`: expired run starts no model/charge; saved terminal result can finalize late; otherwise mark abandoned with no fabricated claim and score existing ledger. Completed/scored returns existing result. `score_failed` only re-scores. Legacy missing metadata refuses same-run execution without deleting data. Concurrent finalizers cannot overwrite a result.

```python
for field in ("id", "namespace", "thread_id", "started_at", "deadline_at"):
    assert after[field] == before[field]
assert expired_model_calls == expired_charge_calls == 0
assert len(result_rows) == 1
assert replayed_claim == actual_model_claim
```
- [ ] Write `test_worker_environment_is_an_allowlist`: start from a parent environment containing sentinel control/admin/ledger/arbitrary secrets. The child receives only necessary runtime/public variables, explicitly selected provider key, deterministic import/runtime variables and allowed tracing/test-guard variables. Assert forbidden names/values absent from child environment, argv, output and saved input JSON. OpenRouter is always mocked in tests. Injected descriptors require explicit in-process model injection; process launch refuses them before dispatch.
- [ ] Run `uv run pytest tests/runner/test_worker.py tests/runner/test_resume.py tests/runner/test_process.py tests/runner/test_coordinator.py -q`; expect missing APIs. Integrate `make_executor` for all modes, a dedicated fenced saver, invocation journal and independently renewed run claim. Build models from saved scripted/OpenRouter descriptors for child execution, with `RequestBudget` on a dedicated runtime connection for OpenRouter; verify supplied injected model IDs match. Persist completed execution through fenced `finish_run`; on loss, stop without a stale run transition. A returned summary is not an oracle verdict.
- [ ] Keep `run_spec` as the existing dependency-injection convenience for tests/custom models, using the same worker logic with a restricted config object; do not claim that an in-process caller lacks credentials. The actual process path and its child-environment test establish process isolation. Trusted supervisor finalization takes the saved terminal checkpoint/run claim, then scores through the unchanged explicit oracle inputs and reader role. Retain `score_failed` retry behavior and use insert-on-conflict-do-nothing/read-existing for immutable results.
- [ ] Replace new-version batch clearing with load-and-resume; never delete an unfinished new-version run. Prevent `clear_incomplete_run` from deleting resumable rows even if called accidentally. Existing legacy completed/score_failed results stay readable; incomplete legacy runs require an explicitly new experiment, not guessed inputs. A partially resumed study must stay on its recorded model; disable fallback switching when that named study has resumable work. Do not launch a real study while testing this.
- [ ] Test coordinator restart, duplicate deliveries, failure before child spawn, child crash before claim and crash after terminal checkpoint before supervisor finalization. A live owner is skipped, an expired owner is eventually replaced in A/B/C, and C can independently finish its operation while graph delivery waits. Queue acknowledgement follows durable execution/finalization state; no unbounded child pool or busy loop.
- [ ] Run `uv run pytest tests/runner tests/execution tests/runtime -q`; expect no failures. Check lint/format and prepare review before commit.

### Task 11: Saved traces, CLI and audit compatibility

**Files:** Modify `src/lab/obs.py:236,293`, `src/lab/runner/__main__.py:56,127`, `src/lab/runner/tracing.py`, `src/lab/runner/prepared.py`; add `tests/obs/test_recovery_tracing.py`, `tests/runner/test_resume_cli.py`; update `tests/runner/test_main.py`, `tests/report/test_behaviour.py`, `docs/ONBOARDING.md` and `docs/decisions.md` with implementation status, not invented measurements.

**Interfaces:** Produce `EpisodeTrace.carrier() -> dict[str, str]`, optional `obs.episode(run_id, episode_id, *, carrier: Mapping[str, str] | None = None)`, and `EpisodeTrace.recovery(*, operation_id: str, state: str, fence_version: int) -> ContextManager[SpanHandle]`. Use W3C traceparent/tracestate only, with explicit context extraction/injection; drop baggage and arbitrary incoming keys. Persist the trusted carrier during preparation while its root is active, before launching a worker. Add `runner run --mode {agent_directed,deterministic_retry,durable_gateway}`, `runner resume --run-id ID`, and `runner recover --once [--limit 100]`; retain existing required scripted `--policy`, manifest and status options. Resume loads saved model selection, not a new command-line model.

- [ ] Write `test_recovery_spans_keep_trace_across_serialization`: independent in-memory exporters on either side of persisted carrier serialization show the same trace ID for episode, operation/recovery and resumed model/tool spans. Also launch a child with an in-memory exporter that writes only span IDs/metadata to a temporary test file; merge evidence in the parent and assert common trace identity. Off/malformed-carrier/exporter-failure paths remain nonfatal.
- [ ] Write `test_trace_and_cli_never_expose_payloads_or_credentials`: tokens, raw prompts/tool bodies and injected baggage never become trace attributes or CLI summaries. Run recovery spans with only IDs/rules/state/timing. Missing tracing produces an empty carrier and no network request.

```python
assert {span["trace_id"] for span in parent_and_child_spans} == {original_trace_id}
assert secret_token not in json.dumps(recorded_attributes)
assert raw_tool_body not in json.dumps(recorded_attributes)
assert "baggage" not in carrier
```
- [ ] Run `uv run pytest tests/obs/test_recovery_tracing.py tests/runner/test_resume_cli.py -q`; expect absent trace/CLI behavior. Implement the interfaces and attach operation/recovery spans explicitly across worker/sweeper threads and processes. Cached/status-confirmed tools retain their real evidence in runtime rows; traces never feed scoring.
- [ ] Exercise scripted CLI modes through the real credential-limited process path; refuse B/C compat before preparation; support resume/recover without resetting namespaces. The supervisor uses `launch_sweeper` for background recovery rather than leaking its inherited credentials to a sweeper child. Preserve the existing JSON summary fields and existing `run`/`study` error behavior, adding only nonsecret execution IDs where useful. Reports continue to aggregate legacy scored snapshots, not join against invocation logs to invent new denominators. Use the Task 1 codec in version-aware audit reads; raw SQL strings for new-version tool observations/keys are encoded storage, not model-visible messages. Preserve the old measured files byte-for-byte.
- [ ] Run `uv run pytest tests/obs tests/runner tests/report -q`; expect no failures. Check lint/format and stop for Run 5 review before commit.

### Task 12: Actual process-kill acceptance and final review

**Files:** Create `tests/runner/recovery_worker.py`, `tests/runner/test_recovery_acceptance.py`, `docs/recovery-findings.md`; modify test helpers only where sharing is necessary. Leave `tests/agent/test_replay.py` and `docs/replay-findings.md` as the existing measurement; do not rewrite Plan 2/3 findings.

**Interfaces:** Reuse production `prepare_run`, worker module, sweeper, coordinator, delivery and scoring paths. A test-only worker wrapper installs the socket guard before production imports and exposes named crash hooks `before_send`, `after_journal_before_checkpoint`, `after_terminal_before_delivery`, `after_handoff_before_ack`, `after_model_response_before_checkpoint`. After-commit killing uses the existing simulator barrier `hold_after_commit`, not an exception pretending to be SIGKILL. Hooks record run/delivery identity and wait on bounded events; resumed production workers have hooks disabled.

- [ ] Write `test_sigkill_after_commit_recovers_same_run` parameterized over A/B/C with a scripted charge-then-report policy and real loopback simulator outside the killed process. Wait until the independent ledger sees the effect and barrier, send actual SIGKILL, expire only that test run's lease in its isolated runtime DB, release held backend work, then launch a fresh worker/recovery path. Assert same run/namespace/thread/deadline/config, one intended effect, supported scripted observation, one immutable final result and retained trace identity. Inspect all call-log outcomes, including in-flight/server-error evidence.
- [ ] Write `test_other_crash_boundaries_preserve_evidence`: before send, after C journal terminal commit before checkpoint, after terminal outbox commit before dispatch, and dispatcher after durable handoff before ack. Cached C replay makes no charge request; duplicate wake-ups do not start another active graph. A/B get the same common restart treatment; measure their calls rather than assert a manufactured advantage for C.

```python
assert killed.returncode == -signal.SIGKILL
assert resumed.run_id == prepared.run_id
assert score.n_effects == score.n_intended == 1
assert score.n_wrong == 0
assert final_result_count == 1
assert child_guard_refusals == []
```
- [ ] Write `test_model_step_replay_preserves_both_actual_calls`: kill after response evidence but before graph checkpoint, resume with a scripted replay response using different arguments/key, and assert both logical invocations retained. A forwards the requested key, B retains the trusted key, C denies argument conflict while preserving its first valid intent. Score actual ledger effects and leave the model claim untouched.
- [ ] Run `uv run pytest tests/runner/test_recovery_acceptance.py -q`; first establish that each assertion fails when its required mechanism is absent, then complete test hooks/harness without alternate production recovery logic. Keep waits bounded (child import allowance 60 seconds; per-event assertions 10 seconds), always terminate owned child processes and release owned test barriers in `finally`. Do not delete or restart shared databases/containers.
- [ ] Settle every held/in-flight simulator request before the final ledger read; retain an additional check for late effects after an unresolved outcome. Record trial IDs, modes, kill points, requested/effective keys, intended/received request counts, recovery outcome, ledger verdict, timings and limitations in `docs/recovery-findings.md`. Distinguish observed results from protocol guarantees and historical baseline results. No unrun real-model headline or comparison percentage.
- [ ] Prove core tests detect breakage, one temporary change at a time: remove key enforcement, allow a stale checkpoint write, omit terminal outbox insert, reset deadline on resume and skip pre-send attempt recording. Run the targeted assertion, require failure, restore the change, then require it to pass. Keep these checks local and record the exact detected failure; do not commit mutants or add a general mutation framework.
- [ ] Run `uv run ruff check .`, `uv run ruff format --check src tests scripts`, `uv run pytest`, and `git diff --check`; require zero failures and report the actual new test count. Confirm old finding artifacts have no diff. If local Postgres is unavailable, report that blocking verification honestly and do not reset the shared stack or call the suite passed.
- [ ] Obtain an independent whole-branch review under the user's chosen execution workflow, fix actionable findings and rerun affected checks. Leave all work ready for user review before commit/push. Plan 4 is complete only after the real crash gate, full verification and review are satisfied; Plan 5's broader crash/concurrency/security campaign remains separate.

## Spec coverage and handoff

| Spec requirement | Owning tasks |
| --- | --- |
| Equal settings, identity, source validation, wrong-amount preservation | 1–2, 6, 10 |
| Same-run metadata, no reset, credential boundary, legacy compatibility | 3–5, 10–11 |
| Durable intent/attempts, conflict denial, fencing, terminal atomicity | 3, 5–6, 9 |
| Status evidence, NOT_FOUND uncertainty, bounded independent recovery | 6, 8, 12 |
| Wake-ups, duplicate/crashed delivery and stale acknowledgements | 9–10, 12 |
| Model replay evidence, immutable claim, independent scoring | 7, 10, 12 |
| Cross-process traces and nonfatal observability | 11–12 |
| Actual SIGKILL, regression/mutation evidence, honest claims | 12 |

**Review status:** direct execution approved on 2026-10-20. Tasks 1–7 are implemented for the Run 3 review checkpoint; full-suite verification is recorded in the onboarding checkpoint note. Tasks 8–12 remain pending. The user approved committing and merging the Tasks 1–7 checkpoint into main on 2026-10-21.

**Execution choice:** native execution, accepted on 2026-10-20: the journal, run ownership and checkpoint adapter share several evolving interfaces, and keeping one implementer through each session reduces handoff overhead. A fresh independent whole-branch reviewer remains scheduled for the Plan 4 final gate; no per-task implementer/reviewer agents are used. Review-before-commit checkpoints remain in place.


## Run 2 implementation notes

`SimulatorClient.with_bounds` supplies a non-owning view that combines the original
client's guards/time limits with the retry budget. This prevents accidental use of
an unbounded supplied client and preserves shorter per-phase timeouts. Mode B's
simulator integration uses real loopback HTTP because Starlette's in-memory
TestClient does not support request timeouts. The tests also compare received
backend charge counts with execution evidence and score through `ledger_reader`.
Production `run_spec` remains mode A only until Task 10; this checkpoint does not
claim durable redelivery, a journal, or complete Plan 4 integration.

Verification: full offline suite **1,123 passed in 79.45s**, no warnings;
ruff lint/format and diff whitespace checks passed. The original baseline was
1,016 tests; Run 2 adds 107. No model quota was used. Changes were left uncommitted at the Run 2 review checkpoint.

## Run 3 implementation notes

Tasks 3–7 add migration 0004, the immutable operation journal, create-only saved
preparation, common run leases, atomic checkpoint fencing, C's normal graph path
and model invocation evidence. The production runner remains A-only until Task 10.
Task 8 still owns status reconciliation and the separate sweeper; ambiguous C
work currently waits conservatively and becomes UNRESOLVED at its deadline.
Terminal operation and outbox insertion are atomic; dispatch is Task 9.

New `model_invocations.response_json` stores the full normalized `ModelTurn`,
including its nested raw response, text and malformed flag. Separate tool-call
fields remain available. The legacy `model_calls` snapshot retains its original
first-response behavior and raw-response shape. New-version JSON/TEXT evidence
uses the explicit ASCII codec; the saved run version determines decoding.
`initial_messages_json` uses the same envelope; trusted worker/config/spec JSON
remains structured. Pending preparation is never claimable.

Provider hooks preserve one quota claim per logical complete, with no re-claim
on HTTP retry. In bounded workers, initial pacing precedes that claim so an
expired wait spends no new quota. A separate runtime connection renews operation
leases; its password comes from psycopg's connection info because `info.dsn`
intentionally excludes passwords. No credentials are saved in descriptors.
Descriptors are revalidated before preparation writes, including nested mutable
parameters. Charge JSON uses ASCII escapes so an unpaired surrogate reaches the
simulator's existing refusal and can be recorded; decoded arguments, validation
and model-visible responses are unchanged.

Verification details and the review checkpoint are recorded in ONBOARDING.md and
the local SDD ledger. The user approved committing and merging this checkpoint into main.

A live operation owner is never stolen at foreground expiry. The foreground
returns a deadline-sourced unresolved observation with the actual current
operation state; the owner may still finish its already-sent request. This avoids
a zero-wait loop while preserving fencing and late-effect uncertainty.

Checkpoint encoding ruling: the pinned JsonPlusSerializer uses
`OPT_REPLACE_SURROGATES`, which silently changes an unpaired surrogate to `?`.
The new fenced saver composes `LosslessJsonPlusSerializer` for JSON-native graph
values containing surrogates, using a saver-controlled `arl-json-ascii-v1` blob
type and the existing ASCII codec. All other values retain the inherited
serializer byte-for-byte; SQL, atomic cursor boundary and next-version behavior
remain inherited. Non-JSON custom containers with surrogates fail clearly rather
than silently changing data. The real HTTP/refusal test reloads its checkpoint
and compares the original invalid key exactly. Legacy savers remain unchanged.

Run 3 final verification (2026-10-21): **1,172 passed in 85.32s**, no warnings.
Run 3 adds 49 tests to Run 2's 1,123. Ruff check and formatting (124 files),
whitespace checks and measured-artifact preservation checks passed. Tasks 3–7
are complete; Tasks 8–12 remain pending. The review checkpoint was left
uncommitted at fa0e132 until the user approved integration into main.

Publication authorization (2026-10-21): the user requested committing the reviewed
Run 2–3 changes and pushing them into main. This publishes the Tasks 1–7
checkpoint; it does not mark Plan 4 complete or change the review checkpoints
for future implementation runs. The feature branch is retained for Tasks 8–12.
