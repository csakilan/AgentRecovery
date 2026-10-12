# Replay findings: what a resumed LangGraph run does about a killed charge

**Measured:** 2026-10-12, plan 2 Task 7. Everything below comes from measurement, by
killing a real process with SIGKILL and resuming it in a fresh one. None of it is taken
from LangGraph's documentation, which does not describe this case (D11).

## The answer

A tool node whose process dies after the charge has committed, but before the graph has
checkpointed the node's result, is run again from its start when the thread is resumed.
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
read taken while the child was parked, and in one run of 20 that read caught the background
write half landed (described under the contrast). The test was changed to judge each run by
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
| Charge results in the resumed graph | 1, fresh (`replayed: false`) | **1, the replay's** (`replayed: true`) | 1, fresh, the first attempt's |
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
response. The graph's latest checkpoint was still the model step's, so the resume ran the
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
child died (confirmed by reading it before each kill). The resume began at the model node.
The tool node did not run again and no second request was sent.

Under `"sync"`, then, a node is re-executed exactly when its process died before the
node's checkpoint was written, regardless of what the node had already done to the outside
world. A replayed tool call produced no second effect in any run, and the reason is the
backend's idempotency on a stable key, not anything in the graph.

## Durability contrast (secondary)

These runs change only the first process's durability; the resume is always `"sync"`. They
are separate from the primary result above.

At **K3** as defined (the orchestrator kills the child while it blocks at the start of its
second model call):

| First-process durability | Runs | Checkpoint at kill | Resume | Charge re-executed | Ledger |
|---|---|---|---|---|---|
| `"exit"` | 40 / 40 | **none** (0 `checkpoints` rows) | **refused**: `cannot resume thread ...: it has no checkpoint` | n/a, nothing to resume | 1 effect |
| `"async"` | 40 / 40 | tool step, holds the charge result (read after the kill; see below) | ran from the model node | no | 1 effect |

Under `"exit"` nothing is written until the graph exits, and a SIGKILL is not an exit, so
there is no checkpoint at all, although the recorder's rows (`model_calls` turn 0, the
charge's `tool_events` row) and the ledger effect exist. A runner could only start the
episode over, which would ask the model again with no memory of the charge. (That restart
was not measured here.)

Under `"async"` at K3 the resume matched `"sync"` in all 40 runs, but that does not show
`"async"` is safe. One read shows why: in one run, the checkpoint read taken while the child
was parked at the hook already showed the charge result but no next node (`next=()`), and
the read taken just after the kill showed `next=("model",)`. The background write was still
landing when the child had already started its next step. What saved the other runs is
the pause before the orchestrator's kill: it lands some milliseconds after the hook parks,
which is time enough for the background write to finish. So a supplementary kill point was
measured, **after_tool_step_immediate**, where the child SIGKILLs itself the instant its
second model call starts, with no row written and no pause. It is not one of the brief's
three kill points, and it is labelled as supplementary in the tests too.

| First-process durability | Runs | Checkpoint at kill | Tool node on resume | Charge requests | Ledger |
|---|---|---|---|---|---|
| `"sync"` | 40 / 40 | tool step, holds the charge result | not re-run | 1 (`committed`) | 1 effect |
| `"async"` | 40 / 40 | **model step only** (`next=("tool",)`) | **re-run** | **2**: `committed`, then `replayed` | 1 effect |

Under `"async"` the tool step had run to its end in the killed process (its `tool_events`
row is there, with `replayed: false`), but its checkpoint never reached the database, so
the resume re-ran the tool node and sent the charge again; `tool_events` then holds two
charge rows for turn 0. Under `"sync"` the same kill never lost the step. This is the
direct evidence that the durability setting changes crash behaviour, which D11's fairness
rule (every mode runs with `"sync"`) depends on: with LangGraph's default, `"async"`, a
completed tool step can be lost and replayed even after it finished.

## Limits of this measurement

- One kill point was not measured: a death during the model step, before its checkpoint.
  There the resume asks the model again (Task 6's in-process test shows the model node
  repeating). With a real model the repeated turn may choose a different idempotency key,
  which a replayed tool node never does. The scripted model is deterministic, so this
  experiment cannot say how often that happens.
- Only `agent_directed` mode, one policy, one healthy world, and the controlled profile.
- The `"async"` loss is a race by construction. It was the outcome in all 40 immediate-kill
  runs here, on this machine; the suite asserts only that it happens at least once in a
  batch and that each run is consistent with what its checkpoint held.
- `hold_after_commit` holds the response inside the simulator, so K2's kill is while the
  child waits for a response. A kill after the response arrives and before the
  `tool_events` row is written was not measured. By inference, not measurement, the
  checkpoint would be the same as at K2 (the tool node had not finished), and the first
  response would be in neither the graph nor the runtime's record.

## Where it is pinned

`tests/agent/test_replay.py` asserts all of the above, run by run, so an upgrade of
LangGraph or of the Postgres checkpointer that changes any of it fails the suite.
`tests/agent/replay_worker.py` is the child process and holds the kill-point hooks.
