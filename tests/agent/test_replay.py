"""The replay experiment (plan 2, Task 7; D11): what a resumed LangGraph run does about a
charge whose process was SIGKILLed, measured at three kill points.

Layout. This pytest process hosts the payment simulator (its public app on a uvicorn thread,
against the throwaway payments database) and orchestrates. The agent runs in a real child
process, tests/agent/replay_worker.py, started with `sys.executable`. The child is killed
with SIGKILL, never with an exception, and the run is resumed on the same `thread_id` in a
second, fresh child with no hooks, `messages=None` and durability="sync". The simulator lives
here, outside the process that dies, so a request in flight when the agent dies is still
completed and recorded.

Kill points:

    before_send                     the executor wrapper blocks before the first charge is
                                    delegated; no request has been sent.
    after_commit_before_checkpoint  no harness hook. The namespace holds charge call 1 after
                                    its commit (hold_after_commit=(1,)); the child is killed
                                    while it waits for the response, then the barrier is
                                    released.
    after_checkpoint                the model wrapper blocks at the start of the second model
                                    call. Before killing, the orchestrator reads the thread's
                                    latest checkpoint and records whether it holds the tool
                                    result.

The durability contrast runs the first process under a weaker durability (the resume is
always "sync"). At after_checkpoint, "exit" and "async" are measured as above. Because an
orchestrator kill always lands some milliseconds after the hook parks, which is time enough
for a background "async" checkpoint write to land, a supplementary kill point is measured
too: after_tool_step_immediate, where the worker SIGKILLs itself the instant its second model
call starts, under "sync" and "async". It is labelled as supplementary everywhere and is not
one of the three kill points of the brief.

The policy is `false_success` in a healthy world (no fault besides the hold), which there
just means "charge once with key k1, then report success": any second charge request that
reaches the simulator is a replay by the loop, not a retry by the model.

The assertions pin the measured behaviour, so an upgrade of LangGraph or of the checkpointer
that changes it fails here loudly. docs/replay-findings.md records the measurement.

Environment (all optional):
    LAB_REPLAY_REPS            repetitions per kill point (default 3)
    LAB_REPLAY_CONTRAST_REPS   repetitions per durability-contrast test (default 2)
    LAB_REPLAY_PARALLEL        repetitions run at once, each in its own namespace (3)
    LAB_REPLAY_EVIDENCE        a file to append one JSON line of observations per run to
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import psycopg
import pytest
import uvicorn
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from lab.agent.executor import (
    RULE_AGENT_DIRECTED,
    ToolOutcome,
    render_observation,
    requested_key_of,
)
from lab.agent.graph import build_graph, run_episode
from lab.agent.prompts import system_prompt, user_prompt
from lab.agent.scripted import ScriptedModel
from lab.agent.tools import tool_schemas
from lab.protocol.faults import FaultPlan
from lab.protocol.task import DEFAULT_TASK, Profile
from lab.runner.tracing import TRACING_VARIABLES
from lab.simulator import service
from lab.simulator.app import create_public_app
from tests.network_guard import LOG_ENV, PROVIDER_HOSTS, read_log

REPS = int(os.environ.get("LAB_REPLAY_REPS", "3"))
CONTRAST_REPS = int(os.environ.get("LAB_REPLAY_CONTRAST_REPS", "2"))
PARALLEL = max(1, int(os.environ.get("LAB_REPLAY_PARALLEL", "3")))
EVIDENCE = os.environ.get("LAB_REPLAY_EVIDENCE")

WORKER = Path(__file__).with_name("replay_worker.py")
REPO = Path(__file__).resolve().parents[2]
TOKEN = "client-replay"
POLICY = "false_success"
BARRIER = "after_commit:1"
TOOLS = tool_schemas(Profile.CONTROLLED, status_available=True)
START = [
    {"role": "system", "content": system_prompt("neutral")},
    {"role": "user", "content": user_prompt(DEFAULT_TASK, Profile.CONTROLLED)},
]

BEFORE_SEND = "before_send"
AFTER_COMMIT = "after_commit_before_checkpoint"
AFTER_CHECKPOINT = "after_checkpoint"
# Supplementary, for the durability contrast: the worker SIGKILLs itself the instant its
# second model call starts (see replay_worker.py for why an orchestrator kill cannot do this).
AFTER_TOOL_STEP_IMMEDIATE = "after_tool_step_immediate"
# The worker's hook for each kill point. The commit kill point has none: the simulator holds.
WORKER_HOOK = {
    BEFORE_SEND: "before_send",
    AFTER_COMMIT: "none",
    AFTER_CHECKPOINT: "after_checkpoint",
    AFTER_TOOL_STEP_IMMEDIATE: "after_tool_step_immediate",
}

SPAWN_TIMEOUT_S = 60.0  # a cold child imports langgraph in about 2.5 s; parallel ones slower
_evidence_lock = threading.Lock()


# The simulator, hosted in this process


@pytest.fixture(scope="module")
def sim_public_url(payments_db):
    """The public app on a real uvicorn server on an ephemeral port, for the whole module."""
    pool = ConnectionPool(
        payments_db["svc"],
        min_size=1,
        max_size=4 * PARALLEL + 4,
        open=True,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(
        uvicorn.Config(
            create_public_app(pool, TOKEN), log_level="warning", lifespan="off", loop="asyncio"
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "the simulator did not start"
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(15)
        listener.close()
        pool.close()
    assert not thread.is_alive(), "the simulator did not stop"


class Lab:
    """Connection details plus every child process and namespace this module started, so
    a failing test can still kill the one and release the other."""

    def __init__(self, sim_url: str, payments: dict[str, str], runtime: dict[str, str]):
        self.sim_url = sim_url
        self.sim_svc = payments["svc"]
        self.ledger = payments["reader"]
        self.runtime = runtime["svc"]
        self.children: list[subprocess.Popen[str]] = []
        self.namespaces: list[str] = []
        self._lock = threading.Lock()
        # Each child's network guard appends the connections it refused to its own file here.
        self._guard_dir = tempfile.mkdtemp(prefix="replay-guard-")
        self._guard_logs: dict[int, str] = {}

    def blocked_attempts(self, *children: subprocess.Popen[str]) -> list[str]:
        """What the guards of `children` (default: every child started) refused."""
        with self._lock:
            logs = [self._guard_logs[c.pid] for c in children] or list(self._guard_logs.values())
        return [line for log in logs for line in read_log(log)]

    def forget_blocked_attempts(self, *children: subprocess.Popen[str]) -> None:
        """Empty the guard logs of `children`. Only for a test that provoked a refusal on
        purpose and has already asserted on it."""
        with self._lock:
            logs = [self._guard_logs[c.pid] for c in children]
        for log in logs:
            Path(log).unlink(missing_ok=True)

    def assert_no_blocked_attempts(self, *children: subprocess.Popen[str]) -> None:
        """Fail if a worker tried to leave the machine. The workers run the whole graph with
        a model chosen inside the worker, so this is where a real provider client added to a
        worker would be caught. It reads the log, not the exit code: a worker killed by the
        orchestrator has no exit code to read, and one that swallowed the error has a good one."""
        attempts = self.blocked_attempts(*children)
        assert not attempts, f"a replay worker tried to leave the machine: {attempts}"

    def spawn(
        self,
        ids: dict[str, str],
        *,
        hook: str,
        durability: str,
        resume: bool,
        probe_host: str | None = None,
    ):
        command = [
            sys.executable,
            str(WORKER),
            "--namespace",
            ids["namespace"],
            "--run-id",
            ids["run"],
            "--thread-id",
            ids["thread"],
            "--policy",
            POLICY,
            "--kill-point",
            hook,
            "--durability",
            durability,
        ]
        if resume:
            command.append("--resume")
        if probe_host is not None:
            command += ["--probe-host", probe_host]
        guard_log = os.path.join(self._guard_dir, f"{uuid.uuid4().hex}.log")
        env = {
            **os.environ,
            "LAB_REPLAY_RUNTIME_URL": self.runtime,
            "LAB_REPLAY_SIM_URL": self.sim_url,
            "LAB_REPLAY_CLIENT_TOKEN": TOKEN,
            LOG_ENV: guard_log,
            **dict.fromkeys(TRACING_VARIABLES, "false"),
        }
        child = subprocess.Popen(
            command, cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        with self._lock:
            self.children.append(child)
            self._guard_logs[child.pid] = guard_log
        return child

    def add_namespace(self, namespace: str) -> None:
        with self._lock:
            self.namespaces.append(namespace)

    def cleanup(self) -> None:
        """Kill any child still alive and release every barrier this module could hold."""
        try:
            for child in self.children:
                _reap(child)
            with psycopg.connect(self.sim_svc, autocommit=True) as conn:
                for namespace in self.namespaces:
                    service.release_barrier(conn, namespace, BARRIER)
            # A last look at every worker of the module, in case a test did not check its own.
            self.assert_no_blocked_attempts()
        finally:
            shutil.rmtree(self._guard_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def lab(sim_public_url, payments_db, runtime_db):
    lab = Lab(sim_public_url, payments_db, runtime_db)
    try:
        yield lab
    finally:
        lab.cleanup()


def _reap(child: subprocess.Popen[str] | None) -> None:
    if child is None:
        return
    if child.poll() is None:
        child.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, ValueError, OSError):
        child.communicate(timeout=10)


# Observing a run


class _NoExecutor:
    def execute(self, call):
        raise AssertionError("the probe graph only reads checkpoints")


class _NoRecorder:
    def record_model_call(self, **kwargs):
        raise AssertionError("the probe graph only reads checkpoints")

    def record_tool_event(self, **kwargs):
        raise AssertionError("the probe graph only reads checkpoints")


def _charge_results(messages: list[dict[str, Any]]) -> list[str]:
    """What the graph holds about the charge: the text the model was shown, one entry per
    `charge` tool message. A fresh commit and a replay read differently (the simulator's
    SUCCESS_MESSAGE and REPLAY_MESSAGE, which RetryLedger also tells apart), so the text alone
    says whether the loop sent the charge twice."""
    return [m["content"] for m in messages if m.get("role") == "tool" and m.get("name") == "charge"]


def _checkpoint(probe, saver_conn, thread: str) -> dict[str, Any]:
    snapshot = probe.get_state({"configurable": {"thread_id": thread}})
    rows = saver_conn.execute(
        "SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s", (thread,)
    ).fetchone()["n"]
    if snapshot.created_at is None:
        return {"exists": False, "rows": rows, "next": None, "charge_results": None}
    checkpoint = {
        "exists": True,
        "rows": rows,
        "next": list(snapshot.next),
        "charge_results": _charge_results(snapshot.values["messages"]),
        # The step of the checkpoint get_state read, and the tasks whose pending writes it
        # applied. Read in the same call as `next`; `rows` is a separate, later query.
        "step": snapshot.metadata.get("step"),
        "tasks_with_writes": sorted(t.name for t in snapshot.tasks if t.result is not None),
    }
    checkpoint["boundary"] = _boundary(checkpoint)
    return checkpoint


# Where a thread stands after the tool step, as a resume sees it. LangGraph keeps two things
# per step: the task's pending writes (`put_writes`, saved as each task finishes) and the
# step checkpoint (`put`, saved after the step). `get_state` applies pending writes to the
# values and leaves a task with writes out of `next` (langgraph/pregel/main.py,
# _prepare_state_snapshot), and a resume hands a finished task its saved writes instead of
# running it again (langgraph/pregel/_loop.py, _reapply_writes_to_succeeded_nodes).
TOOL_STEP_NOT_SAVED = "tool_step_not_saved"  # neither writes nor checkpoint saved: re-run
STEP_CHECKPOINTED = "step_checkpointed"  # the tool step's checkpoint landed
PENDING_WRITES_ONLY = "pending_writes_only"  # the tool task's writes landed, the checkpoint not


def _boundary(checkpoint: dict[str, Any]) -> str | None:
    """Classify a checkpoint read as one of the three states above, or None if it is none
    of them (no checkpoint, or a shape no test expects)."""
    if not checkpoint["exists"]:
        return None
    shape = (tuple(checkpoint["next"]), len(checkpoint["charge_results"]))
    shape += (tuple(checkpoint["tasks_with_writes"]),)
    return {
        (("tool",), 0, ()): TOOL_STEP_NOT_SAVED,
        (("model",), 1, ()): STEP_CHECKPOINTED,
        ((), 1, ("tool",)): PENDING_WRITES_ONLY,
    }.get(shape)


def _call_log(sim, namespace: str) -> list[dict[str, Any]]:
    rows = sim.execute(
        "SELECT call_index, outcome, http_status, response FROM call_log"
        " WHERE namespace = %s AND endpoint = 'charge' ORDER BY id",
        (namespace,),
    ).fetchall()
    return [
        {
            "call_index": r["call_index"],
            "outcome": r["outcome"],
            "http_status": r["http_status"],
            "replayed": (r["response"] or {}).get("replayed"),
        }
        for r in rows
    ]


def _effects(ledger, namespace: str) -> int:
    return ledger.execute(
        "SELECT count(*) AS n FROM charges WHERE namespace = %s", (namespace,)
    ).fetchone()["n"]


def _tool_events(rt, run: str) -> list[dict[str, Any]]:
    rows = rt.execute(
        "SELECT turn, tool, http_status, observation FROM tool_events WHERE run_id = %s"
        " ORDER BY id",
        (run,),
    ).fetchall()
    out = []
    for r in rows:
        event = {"turn": r["turn"], "tool": r["tool"], "http_status": r["http_status"]}
        if r["tool"] == "charge":
            event["replayed"] = r["observation"] == service.REPLAY_MESSAGE
        out.append(event)
    return out


def _model_turns(rt, run: str) -> list[int]:
    rows = rt.execute(
        "SELECT turn FROM model_calls WHERE run_id = %s ORDER BY turn", (run,)
    ).fetchall()
    return [r["turn"] for r in rows]


def _wait(
    predicate: Callable[[], Any], what: str, child: subprocess.Popen[str] | None = None
) -> Any:
    deadline = time.monotonic() + SPAWN_TIMEOUT_S
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if child is not None and child.poll() is not None:
            _, err = child.communicate()
            raise AssertionError(
                f"the worker exited ({child.returncode}) before {what}:\n{err[-3000:]}"
            )
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def run_replay(lab: Lab, kill_point: str, rep: int, durability: str = "sync") -> dict[str, Any]:
    """One repetition: start, reach the kill point, SIGKILL, resume, observe."""
    suffix = uuid.uuid4().hex[:10]
    ids = {
        "experiment": f"e-replay-{suffix}",
        "run": f"r-replay-{suffix}",
        "thread": f"t-replay-{suffix}",
        "namespace": f"replay-{suffix}",
    }
    obs: dict[str, Any] = {
        "kill_point": kill_point,
        "durability": durability,
        "rep": rep,
        "policy": POLICY,
        "ids": ids,
    }
    first = resumed = None
    with (
        psycopg.connect(lab.runtime, autocommit=True, row_factory=dict_row) as rt,
        psycopg.connect(lab.sim_svc, autocommit=True, row_factory=dict_row) as sim,
        psycopg.connect(lab.ledger, autocommit=True, row_factory=dict_row) as ledger,
        PostgresSaver.from_conn_string(lab.runtime) as saver,
    ):
        probe = build_graph(
            ScriptedModel(POLICY),
            _NoExecutor(),
            TOOLS,
            6,
            recorder=_NoRecorder(),
            checkpointer=saver,
        )
        rt.execute(
            "INSERT INTO experiments (id, name, manifest_sha256, config_json)"
            " VALUES (%s, 'replay', 'replay', '{}')",
            (ids["experiment"],),
        )
        rt.execute(
            "INSERT INTO runs (id, experiment_id, episode_id, namespace, mode, thread_id, state)"
            " VALUES (%s, %s, 'replay-episode', %s, 'agent_directed', %s, 'running')",
            (ids["run"], ids["experiment"], ids["namespace"], ids["thread"]),
        )
        plan = FaultPlan(hold_after_commit=(1,)) if kill_point == AFTER_COMMIT else FaultPlan()
        service.configure_namespace(sim, ids["namespace"], Profile.CONTROLLED, plan)
        lab.add_namespace(ids["namespace"])
        try:
            first = lab.spawn(
                ids, hook=WORKER_HOOK[kill_point], durability=durability, resume=False
            )

            # 1. Wait until the child is parked at the kill point (or, for the immediate
            # kill point, until it has SIGKILLed itself there).
            if kill_point == AFTER_TOOL_STEP_IMMEDIATE:
                first.wait(SPAWN_TIMEOUT_S)
            elif kill_point == AFTER_COMMIT:
                _wait(
                    lambda: service.barrier_state(sim, ids["namespace"], BARRIER)["reached"],
                    "the simulator to hold the committed charge",
                    first,
                )
            else:
                row = _wait(
                    lambda: rt.execute(
                        "SELECT worker_pid FROM crash_points WHERE run_id = %s AND name = %s",
                        (ids["run"], kill_point),
                    ).fetchone(),
                    f"the worker to reach {kill_point}",
                    first,
                )
                obs["crash_point_pid_is_child"] = row["worker_pid"] == first.pid

            # 2. What is durable at the moment of the kill.
            obs["at_kill"] = {
                "checkpoint": _checkpoint(probe, rt, ids["thread"]),
                "call_log": _call_log(sim, ids["namespace"]),
                "ledger_effects": _effects(ledger, ids["namespace"]),
                "tool_events": _tool_events(rt, ids["run"]),
                "model_turns": _model_turns(rt, ids["run"]),
            }

            # 3. SIGKILL, never an exception.
            if kill_point != AFTER_TOOL_STEP_IMMEDIATE:
                os.kill(first.pid, signal.SIGKILL)
                first.wait(10)
            obs["killed_returncode"] = first.returncode
            if kill_point == AFTER_COMMIT:
                service.release_barrier(sim, ids["namespace"], BARRIER)
                _wait(
                    lambda: all(
                        c["outcome"] != service.IN_FLIGHT for c in _call_log(sim, ids["namespace"])
                    ),
                    "the abandoned charge to finish after the release",
                )
            obs["after_kill"] = {
                "checkpoint": _checkpoint(probe, rt, ids["thread"]),
                "call_log": _call_log(sim, ids["namespace"]),
            }

            # 4. Resume in a fresh process: same thread and run, no hooks, messages=None.
            resumed = lab.spawn(ids, hook="none", durability="sync", resume=True)
            out, err = resumed.communicate(timeout=SPAWN_TIMEOUT_S)
            obs["resume_returncode"] = resumed.returncode
            lines = out.strip().splitlines()
            assert lines, f"the resumed worker printed nothing (exit {resumed.returncode}):\n{err}"
            result = json.loads(lines[-1])
            if result["ok"]:
                state = result["state"]
                obs["resume"] = {
                    "ok": True,
                    "done": state["done"],
                    "claimed_outcome": state["claimed_outcome"],
                    "ended_by": state["ended_by"],
                    "turn": state["turn"],
                    "charge_results": _charge_results(state["messages"]),
                }
            else:
                obs["resume"] = {"ok": False, "error": result["error"]}

            # 5. The record after the resume.
            obs["final"] = {
                "call_log": _call_log(sim, ids["namespace"]),
                "ledger_effects": _effects(ledger, ids["namespace"]),
                "tool_events": _tool_events(rt, ids["run"]),
                "model_turns": _model_turns(rt, ids["run"]),
            }
            final = obs["final"]
            obs["charge_requests_unrecorded_by_tool_events"] = len(final["call_log"]) - sum(
                1 for e in final["tool_events"] if e["tool"] == "charge"
            )
            lab.assert_no_blocked_attempts(first, resumed)
        finally:
            _reap(first)
            _reap(resumed)
            service.release_barrier(sim, ids["namespace"], BARRIER)
            saver.delete_thread(ids["thread"])
            rt.execute("DELETE FROM experiments WHERE id = %s", (ids["experiment"],))
    if EVIDENCE:
        with _evidence_lock, open(EVIDENCE, "a") as fh:
            fh.write(json.dumps(obs, sort_keys=True) + "\n")
    return obs


def run_reps(lab: Lab, kill_point: str, reps: int, durability: str = "sync"):
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        return list(pool.map(lambda rep: run_replay(lab, kill_point, rep, durability), range(reps)))


def first_charge_answered_fresh() -> str:
    return service.SUCCESS_MESSAGE


def charge_answered_as_replay() -> str:
    return service.REPLAY_MESSAGE


# The measurements. Each asserts every repetition, with the whole observation on failure.


def test_killed_before_send_the_resume_sends_the_charge_for_the_first_time(lab):
    for obs in run_reps(lab, BEFORE_SEND, REPS):
        at_kill, after, final, resume = (
            obs["at_kill"],
            obs["after_kill"],
            obs["final"],
            obs["resume"],
        )
        assert obs["crash_point_pid_is_child"], obs
        assert obs["killed_returncode"] == -signal.SIGKILL, obs
        # At the kill: the model step is checkpointed, the tool step has not run, nothing sent.
        assert at_kill["checkpoint"]["exists"] and at_kill["checkpoint"]["next"] == ["tool"], obs
        assert at_kill["checkpoint"]["charge_results"] == [], obs
        assert at_kill["call_log"] == [] and at_kill["ledger_effects"] == 0, obs
        assert at_kill["tool_events"] == [] and at_kill["model_turns"] == [0], obs
        assert after["checkpoint"] == at_kill["checkpoint"], obs
        # The resume runs the tool node from its start; this is the first request.
        assert resume["ok"] and resume["done"], obs
        assert (resume["claimed_outcome"], resume["ended_by"]) == ("succeeded", "report_done")
        assert resume["charge_results"] == [first_charge_answered_fresh()], obs
        assert [(c["call_index"], c["outcome"]) for c in final["call_log"]] == [(1, "committed")]
        assert final["ledger_effects"] == 1, obs
        assert [(e["turn"], e["tool"]) for e in final["tool_events"]] == [
            (0, "charge"),
            (1, "report_done"),
        ], obs
        assert final["model_turns"] == [0, 1], obs
        assert obs["charge_requests_unrecorded_by_tool_events"] == 0, obs


def test_killed_after_commit_before_checkpoint_the_charge_is_replayed_and_absorbed(lab):
    for obs in run_reps(lab, AFTER_COMMIT, REPS):
        at_kill, after, final, resume = (
            obs["at_kill"],
            obs["after_kill"],
            obs["final"],
            obs["resume"],
        )
        assert obs["killed_returncode"] == -signal.SIGKILL, obs
        # At the kill: the charge has committed (one effect, its call in flight), but the
        # graph's latest checkpoint is still the model step's, with no tool result in it.
        assert at_kill["ledger_effects"] == 1, obs
        assert [(c["call_index"], c["outcome"]) for c in at_kill["call_log"]] == [
            (1, service.IN_FLIGHT)
        ], obs
        assert at_kill["checkpoint"]["exists"] and at_kill["checkpoint"]["next"] == ["tool"], obs
        assert at_kill["checkpoint"]["charge_results"] == [], obs
        assert at_kill["tool_events"] == [] and at_kill["model_turns"] == [0], obs
        # Released, the abandoned request finishes as a fresh commit nobody receives.
        assert [(c["call_index"], c["outcome"], c["replayed"]) for c in after["call_log"]] == [
            (1, "committed", False)
        ], obs
        assert after["checkpoint"] == at_kill["checkpoint"], obs
        # The resume re-executes the tool node: the same charge goes out a second time and
        # the simulator answers it from the idempotency record. One effect, not two.
        assert resume["ok"] and resume["done"], obs
        assert (resume["claimed_outcome"], resume["ended_by"]) == ("succeeded", "report_done")
        assert [(c["call_index"], c["outcome"], c["replayed"]) for c in final["call_log"]] == [
            (1, "committed", False),
            (2, "replayed", True),
        ], obs
        assert final["ledger_effects"] == 1, obs
        # The graph holds one charge result, the replay's. The first attempt left nothing in
        # it; only the replay text ("Idempotent hit: ...") says there was one.
        assert resume["charge_results"] == [charge_answered_as_replay()], obs
        assert [(e["turn"], e["tool"]) for e in final["tool_events"]] == [
            (0, "charge"),
            (1, "report_done"),
        ], obs
        assert final["tool_events"][0]["replayed"] is True, obs
        assert final["model_turns"] == [0, 1], obs
        # The killed attempt's request is in the simulator's call log and nowhere in the
        # runtime's record: tool_events alone would say one request was made.
        assert obs["charge_requests_unrecorded_by_tool_events"] == 1, obs


def test_killed_after_checkpoint_the_tool_node_does_not_run_again(lab):
    for obs in run_reps(lab, AFTER_CHECKPOINT, REPS):
        at_kill, after, final, resume = (
            obs["at_kill"],
            obs["after_kill"],
            obs["final"],
            obs["resume"],
        )
        assert obs["crash_point_pid_is_child"], obs
        assert obs["killed_returncode"] == -signal.SIGKILL, obs
        # Evidence that the kill is after the checkpoint: the thread's latest checkpoint
        # already holds the charge's result and waits at the model node.
        assert at_kill["checkpoint"]["exists"] and at_kill["checkpoint"]["next"] == ["model"]
        assert at_kill["checkpoint"]["charge_results"] == [first_charge_answered_fresh()], obs
        assert [(c["call_index"], c["outcome"]) for c in at_kill["call_log"]] == [
            (1, "committed")
        ], obs
        assert at_kill["ledger_effects"] == 1 and at_kill["model_turns"] == [0], obs
        assert [e["tool"] for e in at_kill["tool_events"]] == ["charge"], obs
        assert after["checkpoint"] == at_kill["checkpoint"], obs
        # The resume starts at the model node: no second request, no second tool event.
        assert resume["ok"] and resume["done"], obs
        assert (resume["claimed_outcome"], resume["ended_by"]) == ("succeeded", "report_done")
        assert resume["charge_results"] == [first_charge_answered_fresh()], obs
        assert [(c["call_index"], c["outcome"]) for c in final["call_log"]] == [(1, "committed")]
        assert final["ledger_effects"] == 1, obs
        assert [(e["turn"], e["tool"]) for e in final["tool_events"]] == [
            (0, "charge"),
            (1, "report_done"),
        ], obs
        assert final["model_turns"] == [0, 1], obs
        assert obs["charge_requests_unrecorded_by_tool_events"] == 0, obs


# The durability contrast: the same after_checkpoint kill with the first process running
# under a weaker durability. The resume always runs with "sync".


def test_under_exit_durability_a_kill_after_the_tool_step_leaves_nothing_to_resume(lab):
    for obs in run_reps(lab, AFTER_CHECKPOINT, CONTRAST_REPS, durability="exit"):
        at_kill, final, resume = obs["at_kill"], obs["final"], obs["resume"]
        assert obs["killed_returncode"] == -signal.SIGKILL, obs
        # The charge committed and was recorded, but "exit" writes no checkpoint before the
        # graph exits, and a SIGKILL is not an exit.
        assert at_kill["checkpoint"] == {
            "exists": False,
            "rows": 0,
            "next": None,
            "charge_results": None,
        }, obs
        assert at_kill["ledger_effects"] == 1, obs
        assert [e["tool"] for e in at_kill["tool_events"]] == ["charge"], obs
        assert resume == {
            "ok": False,
            "error": f"cannot resume thread {obs['ids']['thread']!r}: it has no checkpoint",
        }, obs
        assert obs["resume_returncode"] == 3, obs
        assert [(c["call_index"], c["outcome"]) for c in final["call_log"]] == [(1, "committed")]
        assert final["ledger_effects"] == 1, obs


# The helpers judge a run by the checkpoint read after the kill, which is the state the
# resume starts from. Under "async" the read taken while the child is parked can race the
# background writes (once in 40 runs it caught the tool task's writes landed and the step
# checkpoint not yet), so that read is recorded as evidence and not asserted on there.


def _resumed_without_re_execution(obs: dict[str, Any]) -> None:
    """The tool step survived the kill, as its checkpoint or as its task's pending writes
    alone. Either way the resume must not run the tool node again."""
    checkpoint, final, resume = obs["after_kill"]["checkpoint"], obs["final"], obs["resume"]
    assert checkpoint["boundary"] in (STEP_CHECKPOINTED, PENDING_WRITES_ONLY), obs
    assert checkpoint["charge_results"] == [first_charge_answered_fresh()], obs
    assert resume["ok"] and resume["charge_results"] == [first_charge_answered_fresh()], obs
    assert [(c["call_index"], c["outcome"]) for c in final["call_log"]] == [(1, "committed")]
    assert [(e["turn"], e["tool"]) for e in final["tool_events"]] == [
        (0, "charge"),
        (1, "report_done"),
    ], obs


def _resumed_by_re_executing_the_charge(obs: dict[str, Any]) -> None:
    checkpoint, final, resume = obs["after_kill"]["checkpoint"], obs["final"], obs["resume"]
    # The tool step ran to its end in the killed process (its event row is there) but
    # neither its task's writes nor its checkpoint reached the database, so the thread
    # still waits at the tool node.
    assert checkpoint["boundary"] == TOOL_STEP_NOT_SAVED, obs
    assert [e["tool"] for e in obs["at_kill"]["tool_events"]] == ["charge"], obs
    # The resume runs the tool node again: a second request, answered as a replay.
    assert resume["ok"] and resume["charge_results"] == [charge_answered_as_replay()], obs
    assert [(c["call_index"], c["outcome"]) for c in final["call_log"]] == [
        (1, "committed"),
        (2, "replayed"),
    ], obs
    assert [(e["turn"], e["tool"], e.get("replayed")) for e in final["tool_events"]] == [
        (0, "charge", False),
        (0, "charge", True),
        (1, "report_done", None),
    ], obs


def _resumed_consistently_with_its_checkpoint(obs: dict[str, Any]) -> None:
    assert obs["killed_returncode"] == -signal.SIGKILL, obs
    if obs["after_kill"]["checkpoint"]["boundary"] == TOOL_STEP_NOT_SAVED:
        _resumed_by_re_executing_the_charge(obs)
    else:
        _resumed_without_re_execution(obs)
    assert obs["final"]["ledger_effects"] == 1, obs


def test_under_async_durability_a_kill_after_the_tool_step_resumes_from_what_landed(lab):
    """Measured, every run's tool-step checkpoint had landed by the time the orchestrator's
    kill arrived, so the resume did not re-run the tool node (40 of 40). That is a race won
    by the pause before the kill, not a guarantee, so this test accepts any of the three
    states and checks only that each run's resume is consistent with the state it found.
    The 40 of 40 is a recorded measurement, not something this test pins."""
    for obs in run_reps(lab, AFTER_CHECKPOINT, CONTRAST_REPS, durability="async"):
        _resumed_consistently_with_its_checkpoint(obs)


# The decisive contrast. The worker SIGKILLs itself the instant its second model call starts,
# so there is no pause in which a background checkpoint write could land.


def test_an_immediate_kill_after_the_tool_step_under_sync_never_re_executes_it(lab):
    for obs in run_reps(lab, AFTER_TOOL_STEP_IMMEDIATE, CONTRAST_REPS, durability="sync"):
        assert obs["killed_returncode"] == -signal.SIGKILL, obs
        # "sync" writes the step checkpoint before the next step starts, so it is there.
        assert obs["after_kill"]["checkpoint"]["boundary"] == STEP_CHECKPOINTED, obs
        _resumed_without_re_execution(obs)
        assert obs["final"]["ledger_effects"] == 1, obs


def test_an_immediate_kill_after_the_tool_step_under_async_resumes_from_what_landed(
    lab, record_property
):
    """Under "async" the next step starts without waiting for the tool step's saves, so
    whether a kill at that instant finds them saved is a race whose odds depend on the
    host. Measured on one Mac, nothing was saved in 40 of 40 runs (docs/replay-findings.md).
    That rate is recorded here, in the test output and the evidence, and not asserted: a
    correct LangGraph may save before the kill on another host. What is pinned is that each
    run's resume matches what landed. The property the rate depends on, that "async" does
    not wait for the step checkpoint, is pinned deterministically by
    test_what_the_next_step_waits_for below."""
    observed = run_reps(lab, AFTER_TOOL_STEP_IMMEDIATE, CONTRAST_REPS, durability="async")
    for obs in observed:
        _resumed_consistently_with_its_checkpoint(obs)
    boundaries = [o["after_kill"]["checkpoint"]["boundary"] for o in observed]
    summary = {
        "summary": "immediate_async_loss_rate",
        "runs": len(boundaries),
        "tool_step_not_saved": boundaries.count(TOOL_STEP_NOT_SAVED),
        "pending_writes_only": boundaries.count(PENDING_WRITES_ONLY),
        "step_checkpointed": boundaries.count(STEP_CHECKPOINTED),
    }
    record_property("immediate_async_loss_rate", json.dumps(summary))
    print(f"immediate kill under async: {json.dumps(summary)}")
    if EVIDENCE:
        with _evidence_lock, open(EVIDENCE, "a") as fh:
            fh.write(json.dumps(summary, sort_keys=True) + "\n")


# The third state, constructed in-process. A saver that drops every checkpoint after the
# model step leaves the tool task's pending writes as the only record of the tool step.
# This checks the classification above and the resume behaviour it relies on, without
# needing a SIGKILL to land in the window between `put_writes` and `put`.


class _DropCheckpointsAfterModelStep(InMemorySaver):
    def put(self, config, checkpoint, metadata, new_versions):
        if metadata.get("step", -1) <= 1:
            return super().put(config, checkpoint, metadata, new_versions)
        conf = config["configurable"]
        return {
            "configurable": {
                "thread_id": conf["thread_id"],
                "checkpoint_ns": conf.get("checkpoint_ns", ""),
                "checkpoint_id": checkpoint["id"],
            }
        }


class _CountingExecutor:
    def __init__(self):
        self.calls = 0

    def execute(self, call):
        self.calls += 1
        body = {"message": "Charge succeeded.", "replayed": False, "charge": {}}
        return ToolOutcome(
            requested_args=dict(call.arguments),
            requested_key=requested_key_of(call.arguments),
            effective_args=dict(call.arguments),
            effective_key=requested_key_of(call.arguments),
            rule=RULE_AGENT_DIRECTED,
            observation=render_observation(
                RULE_AGENT_DIRECTED, tool="charge", http_status=200, body=body
            ),
            http_status=200,
        )


class _MemoryRecorder:
    def __init__(self):
        self.tool_events = []

    def record_model_call(self, **kwargs):
        pass

    def record_tool_event(self, **kwargs):
        self.tool_events.append(kwargs["tool"])


class _Died(Exception):
    pass


class _DieAtSecondCall:
    def __init__(self, inner):
        self._inner = inner
        self._calls = 0
        self.model_id = inner.model_id

    def complete(self, messages, tools):
        self._calls += 1
        if self._calls == 2:
            raise _Died("stands in for the process dying as the next step starts")
        return self._inner.complete(messages, tools)


def test_pending_writes_alone_keep_a_finished_tool_step_from_running_again():
    saver = _DropCheckpointsAfterModelStep()
    thread = f"t-pending-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread}}
    first = _CountingExecutor()
    graph = build_graph(
        _DieAtSecondCall(ScriptedModel(POLICY)),
        first,
        TOOLS,
        6,
        recorder=_MemoryRecorder(),
        checkpointer=saver,
    )
    with pytest.raises(_Died):
        run_episode(graph, thread, START)
    assert first.calls == 1

    # Fresh graph, fresh executor, the same saver: what a new process would find.
    again = _CountingExecutor()
    recorder = _MemoryRecorder()
    resumed = build_graph(
        ScriptedModel(POLICY), again, TOOLS, 6, recorder=recorder, checkpointer=saver
    )
    snapshot = resumed.get_state(config)
    assert snapshot.metadata["step"] == 1  # the model step's checkpoint is the latest
    found = {
        "exists": True,
        "next": list(snapshot.next),
        "charge_results": _charge_results(snapshot.values["messages"]),
        "tasks_with_writes": sorted(t.name for t in snapshot.tasks if t.result is not None),
    }
    assert found["next"] == [] and found["tasks_with_writes"] == ["tool"]
    assert _boundary(found) == PENDING_WRITES_ONLY

    state = run_episode(resumed, thread, None)
    assert again.calls == 0  # the tool node was handed its saved writes, not run again
    assert recorder.tool_events == ["report_done"]
    assert state["claimed_outcome"] == "succeeded"
    assert _charge_results(state["messages"]) == [first_charge_answered_fresh()]


# What the next step waits for, pinned deterministically in-process. A saver holds back one
# kind of save for the tool step until it is released: either the tool task's pending writes
# (`put_writes`) or the tool step's checkpoint (`put`). The model notes, at the start of its
# second call, whether the held save had completed, then releases it. A timer releases it
# after HOLD_S in case the second call never comes first (the step waits for the save), and
# every wait has a timeout, so a regression fails instead of hanging.

HOLD_S = 1.0
GUARD_S = 10.0


def _is_tool_task_writes(writes) -> bool:
    return any(
        channel == "messages" and value and value[-1].get("role") == "tool"
        for channel, value in writes
    )


class _HoldOneSave(InMemorySaver):
    def __init__(self, hold: str):
        super().__init__()
        self.hold = hold  # "tool_writes" or "step_checkpoint"
        self.release = threading.Event()
        self.saved = threading.Event()
        self.held_too_long = False

    def _wait(self):
        if not self.release.wait(GUARD_S):
            self.held_too_long = True

    def put_writes(self, config, writes, task_id, task_path=""):
        is_tool = _is_tool_task_writes(writes)
        if is_tool and self.hold == "tool_writes":
            self._wait()
        result = super().put_writes(config, writes, task_id, task_path)
        if is_tool and self.hold == "tool_writes":
            self.saved.set()
        return result

    def put(self, config, checkpoint, metadata, new_versions):
        is_tool_step = metadata.get("step") == 2  # input -1, start 0, model 1, tool 2
        if is_tool_step and self.hold == "step_checkpoint":
            self._wait()
        result = super().put(config, checkpoint, metadata, new_versions)
        if is_tool_step and self.hold == "step_checkpoint":
            self.saved.set()
        return result


class _NoteAtSecondCall:
    def __init__(self, saver: _HoldOneSave):
        self._inner = ScriptedModel(POLICY)
        self.model_id = self._inner.model_id
        self._saver = saver
        self._calls = 0
        self.saved_at_second_call: bool | None = None

    def complete(self, messages, tools):
        self._calls += 1
        if self._calls == 2:
            self.saved_at_second_call = self._saver.saved.is_set()
            self._saver.release.set()
        return self._inner.complete(messages, tools)


@pytest.mark.parametrize(
    ("hold", "durability", "saved_before_next_step"),
    [
        # "sync" holds the next step until the step checkpoint is saved; "async" does not.
        ("step_checkpoint", "sync", True),
        ("step_checkpoint", "async", False),
        # Neither waits for the tool task's pending writes. Under "sync" the step checkpoint,
        # which carries the same values, is what is guaranteed before the next step.
        ("tool_writes", "sync", False),
        ("tool_writes", "async", False),
    ],
)
def test_what_the_next_step_waits_for(hold, durability, saved_before_next_step):
    saver = _HoldOneSave(hold)
    model = _NoteAtSecondCall(saver)
    graph = build_graph(
        model, _CountingExecutor(), TOOLS, 6, recorder=_MemoryRecorder(), checkpointer=saver
    )
    timer = threading.Timer(HOLD_S, saver.release.set)
    timer.start()
    outcome: dict[str, Any] = {}

    def run():
        try:
            outcome["state"] = run_episode(
                graph,
                f"t-hold-{uuid.uuid4().hex[:8]}",
                START,
                durability=durability,
                allow_non_sync=True,
            )
        except BaseException as exc:  # reported below, never swallowed
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(GUARD_S)
    timer.cancel()
    saver.release.set()
    assert not worker.is_alive(), "the episode hung"
    assert "error" not in outcome, outcome.get("error")
    assert not saver.held_too_long
    assert outcome["state"]["claimed_outcome"] == "succeeded"
    assert saver.saved.wait(GUARD_S)  # the held save did complete in the end
    assert model.saved_at_second_call is saved_before_next_step


# ---------------------------------------------------------------------------
# The workers are held to the same no-network rule as the suite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", [PROVIDER_HOSTS[0], PROVIDER_HOSTS[2], PROVIDER_HOSTS[3]])
def test_a_worker_that_tries_to_leave_the_machine_fails_the_parent(lab, host):
    """The worker runs the whole graph with a model of its own, so a provider client added
    there would reach the network from a process the suite's socket patch does not cover.
    Here a worker's first model call connects to a provider host and swallows the error, as a
    retrying client would; the episode still finishes and prints its result. The worker exits
    non-zero and its guard log names the host, and the orchestrator's check fails on it."""
    suffix = uuid.uuid4().hex[:10]
    ids = {
        "experiment": f"e-probe-{suffix}",
        "run": f"r-probe-{suffix}",
        "thread": f"t-probe-{suffix}",
        "namespace": f"probe-{suffix}",
    }
    with (
        psycopg.connect(lab.runtime, autocommit=True, row_factory=dict_row) as rt,
        psycopg.connect(lab.sim_svc, autocommit=True, row_factory=dict_row) as sim,
        PostgresSaver.from_conn_string(lab.runtime) as saver,
    ):
        rt.execute(
            "INSERT INTO experiments (id, name, manifest_sha256, config_json)"
            " VALUES (%s, 'probe', 'probe', '{}')",
            (ids["experiment"],),
        )
        rt.execute(
            "INSERT INTO runs (id, experiment_id, episode_id, namespace, mode, thread_id, state)"
            " VALUES (%s, %s, 'probe-episode', %s, 'agent_directed', %s, 'running')",
            (ids["run"], ids["experiment"], ids["namespace"], ids["thread"]),
        )
        service.configure_namespace(sim, ids["namespace"], Profile.CONTROLLED, FaultPlan())
        lab.add_namespace(ids["namespace"])
        child = None
        try:
            child = lab.spawn(ids, hook="none", durability="sync", resume=False, probe_host=host)
            out, err = child.communicate(timeout=SPAWN_TIMEOUT_S)
            # The error was swallowed, so the episode itself ran to the end.
            assert json.loads(out.strip().splitlines()[-1])["ok"] is True, (out, err)
            assert child.returncode == 6, (child.returncode, err)
            assert host in err
            assert lab.blocked_attempts(child) == [f"dns {host}"]
            with pytest.raises(AssertionError, match=host):
                lab.assert_no_blocked_attempts(child)
        finally:
            _reap(child)
            saver.delete_thread(ids["thread"])
            rt.execute("DELETE FROM experiments WHERE id = %s", (ids["experiment"],))
            # The attempt above was provoked on purpose: forget it, or the module's final
            # check in Lab.cleanup would (rightly) fail on it.
            if child is not None:
                lab.forget_blocked_attempts(child)
