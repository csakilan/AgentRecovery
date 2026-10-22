# Plan 4 design: execution modes and durable payment recovery

**Date:** 2026-10-19
**Status:** written specification approved by the user’s “Continue working” instruction on 2026-10-20; direct implementation approved on 2026-10-20; Run 2 Tasks 1–2 implemented, with later checkpoints pending.
**Implementation plan:** [Plan 4 tasks and checkpoints](../plans/2026-10-20-plan-4-execution-and-recovery.md).
**Base:** `main` at `ae76be5`, with Plans 1, 2 and 3 merged.
**Scope:** the payment workflow in the controlled profile. No product code is
implemented by this document.

## 1. Purpose and success

The lab can run and score an agent-directed payment, and has measured what its
LangGraph workflow replays after a real process kill. Plan 4 adds the two
infrastructure treatments that will later be compared with that baseline:

- **A, agent-directed:** the model chooses its payment key and subsequent actions.
- **B, deterministic retry:** infrastructure enforces a stable key and performs
  bounded retries, without a durable operation journal.
- **C, durable gateway:** infrastructure also records intent before dispatch,
  reconciles uncertain outcomes, and finishes abandoned operations independently
  of the original worker.

All three resume the same saved run after a crash. The user selected this rule
explicitly during Run 1. A restart preserves the run, namespace, graph thread,
configuration, original deadline and evidence. It does not erase the unfinished
run and create a fresh namespace, which is what the existing batch retry does.

Plan 4 succeeds when scripted episodes run through all three modes, requested
and effective actions are distinguishable in the records, and a real SIGKILL
after a payment commits is followed by recovery under the same intent. The
independent ledger must show one intended effect in that acceptance case, and
the scripted agent must receive a supported outcome. Recovery spans must retain
the run's trace identity. This is not a claim that every model reports truthfully.

## 2. Constraints and non-goals

- Keep Python 3.12+, uv, psycopg 3, hand-written SQL, the existing FastAPI
  simulator and pinned LangGraph/checkpointer versions. Add no queue service or
  workflow framework. Keep the single `src/lab` package.
- Keep `durability="sync"` in every mode and store it in experiment configuration.
- Use scripted models and mocked provider HTTP throughout this build and its
  tests. Preserve the socket-level provider guard. A real-model run requires a
  separate explicit instruction and a model ID ending in `:free`.
- Preserve Plan 3's measured artifacts and existing results. New execution and
  resume semantics are versioned in new experiment configurations; historical
  runs and findings are not rewritten or silently reclassified.
- Keep the grader's payment connection read-only and separate from runtime
  storage. Recovery uses the public status API, never the evaluator's ledger
  credentials, fault plan or simulator control API.
- Preserve mode A's requested arguments and compatibility-profile observation
  parity. New B/C executors support the controlled profile; an attempted B/C
  compat run is refused before setup or dispatch.
- Keep the shared local Postgres, existing databases and volumes. Migrations are
  additive and forward-only; tests use their isolated databases. Do not delete
  the deferred test database or reset old experiment namespaces.
- No console, GCP deployment, load study, real-model comparison, full Plan 5
  regression campaign or second workflow in this plan. The optional second
  workflow remains a later, separately scoped extension under D6; make no
  general-purpose or reusable-workflow claim from payments alone.

## 3. Architecture and the chosen approach

Keep the existing graph's model/tool loop and its `Executor.execute(call)` seam.
Add B/C behind that seam. The normal tool invocation waits for a bounded result;
mode C's operation continues durably if that waiting process dies. A separate
sweeper reconciles operations and a delivery coordinator resumes abandoned runs.
An outbox connects terminal operations to reliable wake-up handling.

The alternative is an entirely asynchronous graph that interrupts on every
payment and resumes from an external event. That reduces waiting workers but
changes the graph's control flow substantially. The selected approach preserves
the measured replay behavior and existing tool-loop interface. Its deliberate
tradeoff is that a live C worker can wait while reconciliation runs.

Responsibilities, all within the existing Python package:

| Unit | Responsibility |
| --- | --- |
| `lab.execution.identity` | Derive a trusted operation identity and B/C effective key |
| `lab.execution.retry` | Mode B's bounded, same-key retry executor |
| `lab.execution.journal` | Operation/attempt persistence and atomic fenced transitions |
| `lab.execution.gateway` | Mode C executor, cached outcomes and argument-conflict denials |
| `lab.execution.reconcile` | One bounded unit of authoritative status/retry work |
| `lab.execution.outbox` | Durable wake-up records and idempotent delivery |
| `lab.sweeper` | Separate process that scans due operations and drives reconciliation |
| `lab.runner` | Trusted setup/scoring, common run claims and same-run resumption |
| `lab.agent` | Existing graph, model interface and requested/effective tool records |

The written implementation plan will assign exact files, interfaces and tests
within these responsibilities after this spec is approved. It must adapt the
existing executor interface rather than copy the roadmap's obsolete
`charge(ctx, args)`/`status(ctx, key)` sketch.

## 4. Identity, arguments and validation

The trusted job identity is `(tenant, run_id, task_id)`. One run has one payment
task. Derive the operation ID with UUID5 using the fixed project namespace
`e0a30ae5-f624-4f2f-805c-ee7805ef5fa9`. Serialize the three strings, in that order,
as a JSON array with `ensure_ascii=True` and `separators=(",", ":")`; preserve
case and whitespace inside each string. The B/C effective payment key is
`arl-<operation UUID>`.

The literal identity vector for implementation and tests is:

- Tenant `lab`, run `run-001`, task `charge-order-42`.
- Serialized UUID5 name: `["lab","run-001","charge-order-42"]`.
- Operation ID: `112acdfc-c3cc-539a-9db0-5cc42dfa5e27`.
- Effective key: `arl-112acdfc-c3cc-539a-9db0-5cc42dfa5e27`.

This revises the roadmap's `(tenant, episode_id, task_id)` formula: episode IDs
are reused across experiments, so they cannot be the unique identity of a
particular payment run. A resumed run keeps its run ID and key; a genuinely new
run gets new IDs. Existing uniqueness of `(experiment_id, episode_id)` remains:
requesting a fresh trial requires a new experiment or episode, not overwriting
an existing completed trial.

Mode A still forwards the model's requested key. If a model step replays and the
model chooses a new key, mode A can send that new key. Shared run identity does
not promise a stable model-selected payment key.

For every mode, preserve the current controlled request contract. B/C must not
repair malformed arguments by replacing an invalid key or filling missing
fields. Namespace-like arguments retain the existing denial behavior. An
invalid call follows the existing validation/refusal path, unchanged, and
creates no durable operation or automatic retry. Share protocol validation as
needed, with regression coverage proving existing simulator responses and mode
A observations remain unchanged.

For the first valid C charge, persist the order, amount and currency actually
requested by the model and their existing canonical `args_hash`. Do not replace
them with the task's desired values. A valid first request for the wrong amount
can therefore produce `WRONG_EFFECT`; recovery is not a covert amount-correction
intervention.

Later C requests with the same intent and a different canonical argument hash
are recorded denials. They neither dispatch a charge nor alter or cancel the
original operation, including when it is already terminal. Equivalent decimal
representations have the same hash. A denial is not an operation failure.

B has no persisted argument binding. The backend's existing same-key conflict
behavior remains its protection against changed arguments after an effect has
committed. Do not give B a hidden journal to reproduce C's stronger binding.

Model-requested status lookups in B/C use the effective key, even if the model
supplies another valid key. Record both the requested and effective values.
A lookup before the first charge must not register or dispatch an operation.

## 5. Fairness and configuration

Hold task, model/provider configuration, prompts, tool schemas, sampling, model
turn/request budgets, fault schedule, model-visible status availability,
checkpoint setting and run redelivery policy equal across modes. Only the
execution intervention differs. The agent's final claim remains its own;
neither gateway nor scorer silently rewrites a false report as success.

Defaults follow the current runner and the roadmap's recovery defaults:

| Setting | Default and boundary |
| --- | --- |
| Model turn cap | Existing `RunnerConfig.max_turns`, currently 10; same configured value in every mode |
| Episode deadline | Existing 300 seconds from first run start; never reset on redelivery |
| Public HTTP timeout | Existing 30 seconds, capped by remaining operation/run time |
| B charge attempts | At most 3 per executor invocation, including the first send; waits of 1 and 2 seconds |
| C charge attempts | At most 3 across the durable operation's lifetime |
| C operation deadline | Earlier of registration + 120 seconds and the run's original deadline |
| Run and operation leases | 30 seconds, renewed by the owner every 10 seconds during live work |
| C status backoff | 1, 2, 4, then 8 seconds capped at 8, bounded by the operation deadline |
| Local coordinator/sweeper tick | 2 seconds; bounded one-tick entry points for tests |

These values are part of the new configuration/version, not unrecorded globals.
Tests inject time or expire controlled leases rather than sleeping for full
production deadlines. All retry limits include the initial send.

B's local attempt counter can reset after a crash or a later model tool call;
C's durable counter cannot. Both respect the original episode deadline. This is
an explicit consequence of the journal intervention, and extra backend calls
are measured rather than hidden. Model-call budgets and configured comparison
settings stay equal; identical backend call counts are not assumed.

`status_available` controls the tool exposed to the model. C's internal use of
the public status endpoint is part of its treatment and is recorded. A status
service outage is a separate fault-plan condition. Never substitute ledger
reads when status is unavailable.

## 6. Common setup and same-run resumption

Separate trusted episode preparation, worker execution and evaluation. The
supervisor creates a run and its namespace exactly once, stores the immutable
episode specification and execution configuration, and prepares its initial
messages. A worker then starts or resumes the stored graph thread.

The model-facing worker and sweeper receive the runtime connection and public
simulator credentials they need. They receive no control token, ledger-reader
URL or database-admin URL. Trusted preparation/evaluation may hold those
credentials; the current all-in-one `run_spec` is orchestration, not proof of
process-level credential isolation. The new process launch path must use a
credential allowlist rather than inherit all sourced environment variables.

Every delivery claims the run with an owner, lease and monotonically increasing
fence version. A live owner renews its lease independently of slow model or tool
calls. Duplicate deliveries do not start a second active graph invocation.
After expiry, a new delivery may take over; stale owners cannot finalize the
run or publish later checkpoint writes. Checkpoint fencing must be enforced
atomically with ownership validation in the runtime database, not just checked
once before `graph.invoke`. Apply this same protection in all modes.

A resume reloads the stored episode/configuration, same namespace and thread,
original deadline and current delivery count. It never calls namespace setup
again. With a checkpoint it calls the existing resume path (`messages=None`).
If a worker died before the first checkpoint, initialization uses the stored
initial messages in the same prepared run, not a new run or reset namespace.

Completed/scored runs return their existing result without execution.
`score_failed` runs use the existing re-score path without re-running the
agent. Concurrent finalizers cannot overwrite an existing result. A supervisor
that dies after the terminal graph checkpoint but before updating `runs` can
finalize that saved result on its next delivery.

After the original run deadline, start no new model or charge requests. A saved
terminal graph result can still be finalized and scored as late. If no terminal
graph result exists, mark the run abandoned, retain its evidence and score it
with no fabricated final claim. An unfinished C operation reaches `UNRESOLVED`
when its own deadline expires. Expiry does not erase an in-flight request or
turn an unknown payment into a confirmed failure.

New runs use versioned, resumable metadata. Older runs whose new fields are
absent remain readable and scoreable; a same-run resume requiring absent
metadata fails clearly without deleting their records. Migrations do not
invent specifications or configurations for historical runs.

Replace the new-version batch path's delete-and-start-over retry with this
resumption policy. A completed model's identity/configuration cannot change to
a fallback model midway through a resumed run. Keep the older measured study's
data intact; new configuration hashes separate the new execution semantics.

## 7. Mode B

For a valid charge, derive the effective key from trusted identity, preserve all
other requested arguments and send the charge through the public client.

- A valid success or replay is returned through the existing renderer.
- Retry ambiguous 5xx and transport failures, with the same effective key and
  identical arguments, within the three-send limit and remaining deadline.
- Do not retry 400, 401, 404, 409 or 422 as an ambiguous payment failure.
- A denial or invalid source call creates no retry sequence.
- When retries end, return the final supported observation; if no response
  arrived, use a fixed transport-uncertainty observation and no fabricated HTTP
  status. Never claim that exhaustion proves the payment did not happen.

B does not reconcile an ambiguous charge internally or create operation,
attempt, recovery-event or outbox rows. The model can use its status tool when
available. Common run records/checkpoints are not B's operation journal.

## 8. Mode C persistence and state transitions

Add `operations`, `attempts`, `recovery_events` and `outbox` to the runtime
database with explicit `runtime_svc` grants. Keep checkpoint tables there.
The payments database remains the independent source of effects.

- `operations`: unique trusted intent; run/tenant/namespace/task binding;
  requested payment arguments and hash; effective key; state; fence version;
  attempt count; lease owner/expiry; next check time; fixed deadline; last
  observation and evidence type; terminal outcome and timestamps.
- `attempts`: each intended charge dispatch, with operation, attempt number,
  worker/fence version, start/finish time, response status/body or transport
  uncertainty. Write the attempt before sending; a crash must leave it visible.
- `recovery_events`: append-only explanations of claims, status evidence,
  conflicts, expiry and fencing. These explain decisions, not model reasoning.
- `outbox`: terminal-operation wake-ups, including run and operation identities,
  a stable event identity, available time, delivery attempts and claim metadata.

Use a uniqueness constraint for intent registration and compare-and-set
transitions for ownership. A failed ownership check means the old worker stops;
it does not retry a stale write. Fencing protects journal/run/checkpoint writes.
It cannot cancel an HTTP request already sent: the backend's same-key atomic
idempotency remains what prevents a second payment effect.

| From | Evidence/event | Result |
| --- | --- | --- |
| None | First valid charge | `READY`, persist intent before dispatch |
| `READY` | Successful claim before deadline | `IN_FLIGHT`, increment version and insert attempt atomically |
| `IN_FLIGHT` | Valid charge success/replay with matching arguments/key | `CONFIRMED_SUCCESS` |
| `IN_FLIGHT` | Ambiguous error, transport error or expired lease | `UNKNOWN`; expired ownership is fenced |
| `IN_FLIGHT` | Definitive refusal | `TERMINAL_FAILURE`, preserving the actual refusal evidence |
| `UNKNOWN` | Status `SUCCEEDED`, key and canonical payment arguments match | `CONFIRMED_SUCCESS` |
| `UNKNOWN` | Status `SUCCEEDED` but arguments mismatch | `TERMINAL_FAILURE` with conflict evidence; never mark success |
| `UNKNOWN` | Status `NOT_FOUND` | `CONFIRMED_NO_EFFECT`, a provisional observation only |
| `CONFIRMED_NO_EFFECT` | Attempts remain and deadline has not passed | `IN_FLIGHT`, same key and original arguments |
| `CONFIRMED_NO_EFFECT` | Attempt budget exhausted | No more sends; continue bounded status checks as `UNKNOWN` |
| `UNKNOWN` | Status unavailable or malformed, time remains | Stay `UNKNOWN`, schedule next check |
| Nonterminal | Deadline reached without supported completion | `UNRESOLVED`; no further charge sends |

The three terminal states are `CONFIRMED_SUCCESS`, `TERMINAL_FAILURE` and
`UNRESOLVED`. Finalizing one and inserting its outbox event is one database
transaction. A terminal operation is never silently reopened by a replay.

`NOT_FOUND` means no committed effect is visible at the time of the read. It
does not prove an earlier delayed request can never commit. Therefore exhausted
retries plus `NOT_FOUND` do not establish definitive failure; the gateway
continues bounded checks and ultimately reports uncertainty. This deliberately
refines the roadmap's exhaustion-to-`TERMINAL_FAILURE` sketch.

Only classify a 2xx as success when its structured evidence identifies the
effective key and matching payment arguments. A malformed success body is
uncertain, not proof of success. A definitive refusal is reported as a refusal,
not as an unsupported claim that no previous request had any effect.

For the same argument hash, the executor returns a stored terminal outcome
without another charge request. While live and nonterminal, it can drive a
bounded reconciliation step or wait on the journal. It does not start an
independent second attempt while another valid operation lease is held.

## 9. Background recovery and reliable wake-ups

The sweeper is a separate process, able to work when the original runner is
gone. Each tick claims due `READY` operations or fenced/expired uncertain
operations, then performs bounded work through the public API. No transaction
or row lock remains open during network I/O or waiting. Leases are renewable;
backoff and deadlines are persisted, so restarts do not reset them.

The common delivery coordinator also discovers abandoned runs in every mode.
C operation recovery and common graph redelivery are distinct responsibilities:
the former establishes payment evidence, the latter resumes saved agent work.

The outbox dispatcher is at-least-once. It marks an event delivered only after
durably handing off or satisfying its wake-up. Replaying an event cannot create
a second active graph execution or another payment intent. If the run already
has a live owner, it can observe the result itself; if that owner later dies,
common abandoned-run detection remains responsible for redelivery.

Do not use a model call to choose a recovery action. The sweeper's status
queries, retries and terminal decisions are deterministic infrastructure.

## 10. Model observations, evidence and scoring

Preserve `requested_args`/`requested_key` as the model wrote them and
`effective_args`/`effective_key` as actually sent. Retain the existing observation
renderer for actual payment responses. Store C's operation ID/state, rule,
evidence type and relevant attempt/fence identity alongside tool outcomes.

A result established through status must say it was established through
status; it must not masquerade as the lost original charge response. A cached
terminal result must remain identifiable as cached in the execution records.
Every new fixed model-visible message is pinned literally in the implementation
plan and tests before running a real comparison. It must not echo secrets or
instructions embedded in untrusted tool text.

Use the unchanged oracle and read-only payment connection for verdicts. The
gateway never supplies a verdict as payment evidence. Keep recovery eligibility,
effect correctness, truthful reporting and before-deadline completion separate.
The completion timestamp is when the supervisor finalizes the run's returned
claim; a resumed finalization can therefore be late even if the payment was
earlier. Preserve original start/deadline timestamps on every delivery.

`results` is a scored snapshot, not a claim that an external request can never
finish later. The scripted acceptance harness releases held requests and waits
for their backend work to settle before its final independent ledger check.
Late observations are retained as additional evidence and do not overwrite a
published result. The broader held-out campaign must declare its observation
cutoff and settle/cleanup procedure in Plan 5/6.

Backend request counts come from simulator call-log evidence, including
`in_flight` and `server_error` attempts. Add no success-only filter. An attempted
send that never reaches the simulator is recorded as transport uncertainty in
runtime evidence, not invented as a simulator log entry.

The known replayed-model-turn logging gap must be addressed for new resumable
runs: preserve the first stored model response and append each later model
invocation rather than discard it on `(run_id, turn)` conflict. Label delivery
and invocation identity so the checkpointed transcript can be matched to the
right response. Existing rows/results remain readable. An invocation that dies
before its response is recorded must remain identifiable as started/unknown;
it cannot be counted as zero requests. This is logical model invocation
evidence; per-HTTP retry accounting remains the provider client's separate
contract and is not inferred from graph turns.

Persist a trusted trace carrier with the run and its recovery work so spans in
another process attach to the same trace. Keep run/operation/attempt IDs, rules,
states and timing; keep credentials and raw model/tool payloads out of trace
attributes. Tracing failures remain non-fatal, and traces never determine scores.

## 11. Versioning, privileges and compatibility

Add an execution/resume protocol version and all effective recovery settings to
experiment configuration so comparison artifacts cannot mix old and new
semantics. Save a serializable immutable run specification/configuration for
new runs, excluding secrets. Credential rotation does not change the stored
task; secrets are supplied externally at execution time.

Migrations keep old complete and `score_failed` runs and their evidence intact.
Do not widen payments permissions or grant ledger-reader access to runtime
tables. The worker/sweeper's new process path is tested without control,
ledger-reader or admin credentials. The trusted supervisor may prepare and
score; the model gets only its existing registered tools.

Correct onboarding wording when recording the design: effective keys are
model-selected in A and trusted in B/C; the current trusted orchestrator holds
the control token, while model-facing clients do not. Do not describe the
current all-in-one runner as already isolated at the process-credential level.

## 12. Changes from the older roadmap and scope refinements

1. Shared same-run redelivery is now an explicit user decision for A/B/C.
2. Intent identity includes `run_id`, because episode IDs repeat across
   experiments. Checkpoints remain in the runtime database as currently built.
3. Retain `Executor.execute(call)`, rather than introducing the roadmap's older
   two-method execution interface.
4. Status `NOT_FOUND` is provisional. Budget exhaustion cannot turn an ambiguous
   in-flight request into proven no-effect failure.
5. Common fenced delivery and credential-limited worker launch are necessary
   parts of reliable same-run resumption, not benefits reserved for C.
6. Preserve every logical model invocation on replay so new crash evidence is
   not silently discarded. Historical baseline artifacts stay unchanged.
7. The payment-only scope omits the optional second workflow; it remains a
   separately scoped follow-up rather than a basis for a reusability claim.

These approved design refinements are not claims that the new behavior is
already implemented or verified. D13 records the shared-restart rule. The user
approved this written specification on 2026-10-20; the detailed implementation
plan was approved for direct execution on 2026-10-20. Run 2 covers its first two
tasks; common run redelivery and durable C recovery remain future checkpoints.

## 13. Acceptance and review gates

Tests use the scripted model, real isolated Postgres databases and local or
mocked simulator HTTP. Keep the provider network guard in every process,
including crash-test subprocesses. Test timing and failure hooks must be
deterministic, bounded and independent of live model/provider behavior.

The implementation must demonstrate:

1. **Identity:** same run/task has the same B/C key before and after restart;
   different runs, tenants and tasks cannot collide; a default UUID/key vector
   is pinned as a literal.
2. **Shared resume:** A/B/C keep namespace, graph thread, deadline, config and
   existing evidence. Resuming never resets the payment namespace. Duplicate
   deliveries have one active owner and stale checkpoint/final-result writes
   are rejected.
3. **B:** lost acknowledgement and true failure recover under one key within
   the local three-send limit; invalid and definitive-failure paths do not
   retry; all status requests map to the effective key.
4. **C registration:** intent and attempt evidence exist before dispatch;
   duplicate registration and concurrent same-intent requests share an
   operation; changed arguments are denied without altering that operation.
5. **C evidence:** valid success/replay, matching status evidence, malformed
   2xx, mismatching status arguments, temporary/permanent status outages,
   delayed original requests and exhausted retries follow the specified state
   rules. Uncertainty never becomes fabricated success or failure.
6. **Fencing:** an expired worker cannot publish a journal, run or checkpoint
   result after takeover. A late external request still uses the same key and
   cannot add another effect for that key.
7. **Wake-ups:** kill after terminal operation commit but before delivery;
   deliver an event twice; kill a dispatcher after handoff before acknowledgement.
   Each leaves recoverable work and one final result, without a new intent.
8. **Real crash:** kill a runner after the backend charge commits but before
   journal confirmation; recover in a fresh process under the same intent and
   graph thread. The final independent ledger has one intended payment.
9. **Additional crash points:** before dispatch and after journal confirmation
   but before graph checkpoint; the latter C replay returns the recorded result
   without another charge request. Apply common redelivery to A/B too and
   report observed differences, including ties.
10. **Evidence:** a replayed model invocation is retained; requested/effective
    keys and arguments remain distinct; all attempts and limits can be audited;
    recovery spans carry the same trace identity with an in-memory exporter.
11. **Boundaries:** model-facing worker and sweeper lack privileged credentials;
    injected tool-result instructions cannot choose namespace, effective key,
    recovery rules or journal state. The oracle still uses the payments reader.
12. **Regression:** existing parity, scoring, namespace, no-live-call and budget
    protections pass. Core ownership, identity, deadline and crash assertions
    are shown to detect deliberate breakages. No measured headline is invented.

At each coding checkpoint, run its focused tests, relevant regression tests,
lint and formatting checks. At the Plan 4 gate run the complete suite and an
independent branch review. Record failures accurately, including environment
failures, rather than calling them expected or passing.

## 14. Development-session checkpoints

| Session | Deliverable | Natural stop |
| --- | --- | --- |
| Run 1 | This design, the shared-redelivery decision and then an implementation plan | User reviews the written spec before the implementation plan is authored; user reviews that plan before code |
| Run 2 | Identity plus mode B and the common execution configuration | Scripted retry and effective-key checks pass |
| Run 3 | C journal, attempt records and normal execution | Intent conflict, idempotency and stored-outcome checks pass |
| Run 4 | Independent reconciliation, leases and fencing | Abandoned operations reach supported terminal outcomes |
| Run 5 | Common graph redelivery, reliable outbox and complete replay evidence | Resumed runs deliver and score their results without resetting evidence |
| Run 6 | Real SIGKILL acceptance, trace propagation and whole-branch review | Plan 4's ledger-backed acceptance evidence is ready |

The implementation plan may order common run-lifecycle/schema primitives earlier
where another task depends on them; the session labels are review checkpoints,
not permission to build dependent code before its foundations. Each checkpoint
leaves changes ready for the user's review before commit/push. No product code
starts in Run 1, and no real-model experiment is included in these sessions.
