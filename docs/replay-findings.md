# Replay findings: what a resumed LangGraph run does about a killed charge

**Measured:** 2026-10-12, plan 2 Task 7. Everything below comes from measurement, by
killing a real process with SIGKILL and resuming it in a fresh one. None of it is taken
from LangGraph's documentation, which does not describe this case (D11).

## The answer

A tool node whose process dies after the charge has committed, but before the graph has
saved anything of the node's result, is run again from its start when the thread is
resumed.
The same charge request reaches the payment backend a second time. Because it carries the
same idempotency key, the backend answers it from its idempotency record
(`replayed: true`) and the ledger still holds **one effect, not two**. The resumed graph
holds exactly one result for the charge, the replay's. Nothing in the graph's state records
the first attempt; the only trace of it the graph ever sees is the backend saying that a
payment already succeeded under this key.

That held in every repetition: 40 of 40 at that kill point, over two separate runs of 20.
It is the result the brief expected, and it is the case for a journal outside the graph:
the graph cannot tell "I already charged" from "I am charging", and only the backend's
idempotency record stopped a second effect.

## Setup

| | |
|---|---|
| langgraph | 1.2.14 |
| langgraph-checkpoint | 4.2.0 |
| langgraph-checkpoint-postgres | 3.1.2 (`PostgresSaver`, runtime database, role `runtime_svc`) |
| psycopg / Python / PostgreSQL | 3.3.6 / 3.12.8 / 16.14 |
| Durability | `"sync"` for every primary run, first process and resume alike |
| Profile, mode | controlled, `agent_directed` (`DirectExecutor`) |
| Model | `ScriptedModel("false_success")`, zero real model requests |
| Host | macOS (Darwin 25.6.0), 8 cores |

The versions are read from the installed packages (`importlib.metadata`) and the server
(`SHOW server_version`).

**The policy name.** `false_success` is the plan `[charge k1, report_done succeeded]`. Its
name refers to the verdict it earns in the `true_fail` world. Here the world is healthy (no
fault, apart from the hold that creates one kill point), so the policy simply charges once
with key `k1` and reports success. That isolates the replay question: the model never asks
for a second charge, so any second charge request that reaches the simulator was sent by
the loop replaying a step, not by the model retrying.

**Layout.** The pytest process hosts the simulator (its public app on a uvicorn thread,
against a throwaway payments database) and orchestrates. The agent runs in a child process
(`tests/agent/replay_worker.py`, started with `sys.executable`) with the real loop:
`build_graph` and `run_episode`, a `PostgresSaver`, a `PostgresRecorder`, and a
`DirectExecutor` sending HTTP to the simulator. The orchestrator kills the child with
`os.kill(pid, SIGKILL)`, never by raising inside it, and resumes the thread in a second,
fresh child with no hooks: same `thread_id` and `run_id`, `messages=None`,
`durability="sync"`. The simulator lives outside the process that dies, so a request in
flight at the kill still completes and is recorded.

**How each kill point is reached.** The hooks are in the test harness only; they wrap the
model or the executor handed to `build_graph`, write a `crash_points` row with the child's
pid, and sleep until killed.

| Kill point | How it is reached | Evidence the child was where we said |
|---|---|---|
| K1 `before_send` | executor wrapper blocks before delegating the first charge | `crash_points` row with the child's pid; simulator `call_log` empty |
| K2 `after_commit_before_checkpoint` | no hook; the namespace has `hold_after_commit=(1,)`, so the simulator commits charge 1 and holds its response at barrier `after_commit:1`. The child is killed once the barrier reports reached, then the barrier is released | barrier reached; ledger holds one effect; `call_log` row `in_flight`; latest checkpoint is the model step's (`next = ("tool",)`) with no tool result |
| K3 `after_checkpoint` | model wrapper blocks at the start of the second model call | `crash_points` row with the child's pid; read before the kill, the thread's latest checkpoint has `next = ("model",)` and already holds the charge's tool result |

**What is observed in each run.** At the kill: the latest checkpoint (whether one exists,
its `next`, the charge results in its messages, the number of `checkpoints` rows), the
simulator's charge `call_log`, the ledger (read as `ledger_reader`), and the runtime's
`tool_events` and `model_calls`. After the resume: the final state's claimed outcome,
`ended_by` and charge results; the `call_log` with each response's `replayed` flag; the
ledger; `tool_events`; `model_calls`. `tool_events` is cross-checked against `call_log`,
because a kill after a request is sent and before its event row is written leaves a request
the runtime never recorded.

A "charge result" in the graph is the text of the `charge` tool message, which is what the
model reads. That text is RetryLedger's (`lab.protocol.render`), so it does not carry the
simulator's `replayed` field: a fresh commit reads `Charge succeeded.` and a replay reads
`Idempotent hit: a payment already succeeded under this key. No additional charge was made.`
The tables below say "fresh" and "replay" for those two texts. The `replayed` flag itself
is read from the simulator's `call_log`, which is where it is recorded.

## Repetitions and the command

The suite runs each test with 3 repetitions per kill point and 2 per contrast case, three
at a time in separate namespaces (about 36 s for the file). The numbers in this document
come from two larger runs of the same command, each sequential (one repetition at a time)
with 20 repetitions of every case, so 140 killed-and-resumed runs each and 40 repetitions
of every case in all:

```
LAB_REPLAY_REPS=20 LAB_REPLAY_CONTRAST_REPS=20 LAB_REPLAY_PARALLEL=1 \
LAB_REPLAY_EVIDENCE=<file> uv run pytest tests/agent/test_replay.py -q
```

Run 1 measured everything below, and one of its seven tests failed on an assertion of
mine, not on the behaviour under test: the `"async"` test at K3 asserted on the checkpoint
read taken while the child was parked, and in one run of 20 that read caught the tool
task's pending writes saved and the step checkpoint not yet saved (described under the
contrast). The test was changed to judge each run by
the checkpoint read after the kill, which is the state the resume starts from, and run 2
used the final tests: `7 passed`. Run 2's observations had the same shapes as run 1's, case
by case, except that the half-landed read did not recur. Run 2 overlapped a period of heavy
unrelated load on the host (load average up to 49).

`LAB_REPLAY_EVIDENCE` appends one JSON line of observations per run. The tables below
summarise those lines. Within each case every run had the same observations, apart from the
one checkpoint read noted under the contrast.

## Primary result (durability "sync")

| | K1 before send | K2 after commit, before checkpoint | K3 after checkpoint |
|---|---|---|---|
| Runs, all with the same outcome | 40 / 40 | 40 / 40 | 40 / 40 |
| Child exit | SIGKILL (-9) | SIGKILL (-9) | SIGKILL (-9) |
| Latest checkpoint at kill | model step, `next=("tool",)`, no tool result | model step, `next=("tool",)`, no tool result | tool step, `next=("model",)`, holds the charge result |
| Ledger at kill | 0 effects | 1 effect | 1 effect |
| Did the tool node run again on resume | yes, from its start (it had not sent anything) | **yes, from its start** | no, the resume began at the model node |
| Charge requests that reached the simulator | 1 (`committed`) | **2**: `committed`, then `replayed` | 1 (`committed`) |
| Second request answered as replay | n/a | **yes, `replayed: true`** | n/a |
| Ledger after resume | **1 effect** | **1 effect** | **1 effect** |
| Charge results in the resumed graph | 1, fresh (`Charge succeeded.`) | **1, the replay's** (`Idempotent hit: ...`) | 1, fresh, the first attempt's |
| Did the graph see the first attempt's result | there was none | **no** | yes, from the checkpoint |
| `tool_events` charge rows | 1 | 1 | 1 |
| `call_log` requests absent from `tool_events` | 0 | **1** | 0 |
| `model_calls` turns | 0, 1 | 0, 1 | 0, 1 |
| Claimed outcome, ended by | succeeded, report_done | succeeded, report_done | succeeded, report_done |

What re-ran at each point, in words.

At **K1** the child died inside the tool node before any request left it. The resume found
the model step's checkpoint and ran the tool node again from its start, which sent the
charge for the first time. One request, one effect.

At **K2** the charge had committed in the backend, and the child died while waiting for the
response. The tool node had not finished, so nothing of it was saved: the latest checkpoint
was still the model step's and the tool task had no pending writes. The resume ran the
tool node again from its start and sent the identical request: the arguments come from the
checkpointed assistant message, so the replay necessarily carries the same idempotency key
`k1`. The simulator answered it from the idempotency record. The first request's response
(`committed`, `replayed: false`) was finished by the simulator after the barrier was
released and went to a socket nobody was reading. The resumed graph's state holds one
charge result, the replay's; the first attempt left no message, no state and no
`tool_events` row. `tool_events` alone would say one charge request was made, while the
simulator's `call_log` shows two: the runtime's own record of tool calls misses exactly the
request that matters, which is why the cross-check is needed.

At **K3** the tool step's checkpoint, holding the charge's result, was written before the
child died (confirmed by reading it before each kill, and classified as
`step_checkpointed` in the evidence). The resume began at the model node.
The tool node did not run again and no second request was sent.

Under `"sync"`, then, a tool node that died before finishing was re-executed from its
start, regardless of what it had already done to the outside world, and a tool node whose
step was saved was not. A replayed tool call produced no second effect in any run, and the
reason is the backend's idempotency on a stable key, not anything in the graph.

## Where exactly the boundary is

The boundary is the tool task's saved writes, not only the step checkpoint. LangGraph saves
two things for a step: each task's pending writes, saved as the task finishes
(`put_writes`, called from `langgraph/pregel/_loop.py:481` unless durability is `"exit"`),
and the step checkpoint, saved after the step (`put`). On resume, a task whose writes are
saved is handed those writes instead of being run again
(`_reapply_writes_to_succeeded_nodes`, `langgraph/pregel/_loop.py:772-783`, called at
`:678`). `get_state` applies the same pending writes to the values it returns and leaves a
task with writes out of `next` (`_prepare_state_snapshot`,
`langgraph/pregel/main.py:1307-1325`; line numbers are for langgraph 1.2.14 as installed).
So a tool node is re-executed exactly when its process died before either of the two was
saved: its task's writes or its step's checkpoint. Either one is enough on its own.

What the next step waits for is a separate question, and it is measured deterministically
by `test_what_the_next_step_waits_for`. An in-process saver holds back one save of the
tool step until released, and the model notes at the start of its second call whether
that save had completed:

| Save held back | `"sync"`: saved before the next step starts? | `"async"`: saved before the next step starts? |
|---|---|---|
| the step checkpoint (`put`) | **yes**: the next step waited for it | no |
| the tool task's writes (`put_writes`) | **no** | no |

So `"sync"` guarantees the step checkpoint before the next step, not the task's writes.
The checkpoint carries the same values, which is why a kill after that point never re-runs
the node under `"sync"`. Neither durability waits for the task's writes, because
`_checkpointer_put_after_previous` (`langgraph/pregel/_loop.py:1596-1613`) waits only for
earlier writes to delta channels, and this graph has none. That source reading explains
the table; the table itself is measured, and its expectations were checked to fail when
flipped.

What is measured and what is read from source, separately:

- **Measured with SIGKILL.** Under `"sync"`, K1 and K2 (tool node unfinished, so neither
  writes nor checkpoint saved) re-ran the node; K3 did not. These runs cannot tell the
  writes from the checkpoint as the boundary: at K1 and K2 neither existed, and at K3 the
  step checkpoint was saved, which `"sync"` guarantees before the next step starts.
- **Measured with SIGKILL, under `"async"`.** The immediate kill left neither saved and
  the node re-ran in 40 of 40 runs on this host. That rate is a host-dependent
  measurement, not a pinned property (see the last section). One parked read in run 1
  caught the in-between state: the charge result present and `next=()`, which is the tool
  task's writes applied on top of the model step's checkpoint. By the time of the kill the
  step checkpoint had landed too, so that run's resume does not test the in-between state.
  It shows only that the state exists and that a reader can see it.
- **Measured in-process, by construction.**
  `test_pending_writes_alone_keep_a_finished_tool_step_from_running_again` uses a saver that
  drops every checkpoint after the model step, so the tool task's writes are the only
  record of the tool step. A fresh graph reads the model step's checkpoint (step 1), with
  `next=()` and the tool task's writes applied, and its resume does not call the executor.
  The process is stopped by an exception there, not a SIGKILL. That does not matter for
  this question, since what is being tested is what a fresh graph does with the saved
  state.
- **From reading the source.** The two functions cited above are why the in-between state
  resumes without re-execution. That explanation is not measured; the behaviour it explains
  is.

For a journal outside the graph (Plan 4) this means the dangerous window for a tool call is
from the moment its request can reach the backend until the first of its task's writes or
its step checkpoint is saved. Under `"sync"` the next step cannot start before the
checkpoint is saved. Under `"async"` it can, so the window can stay open while the graph
has already moved on.

## Durability contrast (secondary)

These runs change only the first process's durability; the resume is always `"sync"`. They
are separate from the primary result above.

At **K3** as defined (the orchestrator kills the child while it blocks at the start of its
second model call):

| First-process durability | Runs | Checkpoint at kill | Resume | Charge re-executed | Ledger |
|---|---|---|---|---|---|
| `"exit"` | 40 / 40 | **none** (0 `checkpoints` rows) | **refused**: `cannot resume thread ...: it has no checkpoint` | n/a, nothing to resume | 1 effect |
| `"async"` | 40 / 40 | tool step, holds the charge result (read after the kill; one read before it caught writes without checkpoint, see below) | ran from the model node | no | 1 effect |

Under `"exit"` nothing is written until the graph exits, and a SIGKILL is not an exit, so
there is no checkpoint at all, although the recorder's rows (`model_calls` turn 0, the
charge's `tool_events` row) and the ledger effect exist. A runner could only start the
episode over, which would ask the model again with no memory of the charge. (That restart
was not measured here.)

Under `"async"` at K3 the resume matched `"sync"` in all 40 runs, but that does not show
`"async"` is safe. One read shows why. In one run, the checkpoint read taken while the
child was parked at the hook showed the charge result but no next node (`next=()`): the
tool task's pending writes had been saved and the step checkpoint had not yet. The read
taken just after the kill showed the step checkpoint saved (`next=("model",)`). (That
parked read also counted 4 `checkpoints` rows, because the row count is a separate query
taken a moment after `get_state`, by which time the checkpoint had landed.) The child had
already started its next step while its saves were still landing. What saved the other
runs is the pause before the orchestrator's kill: it lands some milliseconds after the
hook parks, which is time enough for the background saves to finish. So a supplementary
kill point was measured, **after_tool_step_immediate**, where the child SIGKILLs itself
the instant its second model call starts, with no row written and no pause. It is not one
of the brief's three kill points, and it is labelled as supplementary in the tests too.

| First-process durability | Runs | Checkpoint at kill | Tool node on resume | Charge requests | Ledger |
|---|---|---|---|---|---|
| `"sync"` | 40 / 40 | tool step, holds the charge result | not re-run | 1 (`committed`) | 1 effect |
| `"async"` | 40 / 40 on this host (rate recorded, not pinned) | **model step only, no tool writes** (`next=("tool",)`) | **re-run** | **2**: `committed`, then `replayed` | 1 effect |

Under `"async"` the tool step had run to its end in the killed process (its `tool_events`
row is there, holding the fresh result), but neither the tool task's writes nor the step
checkpoint reached the database, so the resume re-ran the tool node and sent the charge
again; `tool_events` then holds two charge rows for turn 0. Under `"sync"` the same kill
never lost the step. This is the direct evidence that the durability setting changes crash
behaviour, which D11's fairness rule (every mode runs with `"sync"`) depends on: with
LangGraph's default, `"async"`, a completed tool step can be lost and replayed even after
it finished. How often it is lost depends on how fast the host saves in the background,
so the 40 of 40 here is a measurement on one Mac, not a rate to expect elsewhere. What
does not depend on the host is the mechanism, pinned by `test_what_the_next_step_waits_for`:
under `"async"` the next step starts without waiting for either save.

## Limits of this measurement

- One kill point was not measured: a death during the model step, before its checkpoint.
  There the resume asks the model again (Task 6's in-process test shows the model node
  repeating). With a real model the repeated turn may choose a different idempotency key,
  which a replayed tool node never does. The scripted model is deterministic, so this
  experiment cannot say how often that happens.
- Only `agent_directed` mode, one policy, one healthy world, and the controlled profile.
- The `"async"` loss is a race by construction. It was the outcome in all 40 immediate-kill
  runs here, on this machine. The suite records the rate (test output and evidence) and
  asserts only that each run's resume is consistent with what its checkpoint held.
- `hold_after_commit` holds the response inside the simulator, so K2's kill is while the
  child waits for a response. A kill after the response arrives and before the
  `tool_events` row is written was not measured. By inference, not measurement, nothing of
  the tool step would be saved, as at K2 (the tool node had not finished, so it has no
  writes and no checkpoint), and the first response would be in neither the graph nor the
  runtime's record.

## What the tests pin and what is only recorded

`tests/agent/test_replay.py` asserts on every run it makes, and `tests/agent/replay_worker.py`
is the child process with the kill-point hooks. Not every row above is pinned, though.

Pinned by assertions, run by run, so a change in LangGraph or the Postgres checkpointer
fails the suite:

- every cell of the primary table (K1, K2, K3 under `"sync"`);
- the `"exit"` row: no checkpoint, the resume refused, one effect;
- the immediate kill under `"sync"`: the step checkpoint saved, no re-execution;
- the in-process construction: tool task writes alone, `next=()`, no re-execution;
- what the next step waits for (the table in "Where exactly the boundary is"):
  `"sync"` waits for the step checkpoint and neither durability waits for the task's
  writes. This is deterministic, with timeouts so a regression fails instead of hanging.

Recorded measurements only, not pinned:

- The `"async"` row at K3 ("tool step, not re-run", 40 of 40). The test accepts any of the
  three states (step checkpointed, writes only, neither saved) and checks only that each
  run's resume is consistent with the state it found, because which state a run lands in
  is a race.
- The immediate kill under `"async"` ("neither saved, re-run", 40 of 40). The test checks
  each run for consistency in the same way and reports the loss rate in its output (as a
  printed line and a `record_property`) and in the evidence file. It asserts no rate,
  because a correct LangGraph can save before the kill on a faster host.

Since this document's first version, the evidence for each run also records which of the
three states its checkpoint reads were in (`boundary`: `tool_step_not_saved`,
`step_checkpointed`, `pending_writes_only`), plus the step read and the tasks with pending
writes. The two 20-repetition runs behind the tables predate that field. Two later runs at
the suite defaults (17 runs each) classified every read as expected: K1, K2 and the
immediate `"async"` kill as `tool_step_not_saved`; K3 under both durabilities and the
immediate `"sync"` kill as `step_checkpointed`.
