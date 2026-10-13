"""The child process of the replay experiment (tests/agent/test_replay.py).

It runs one episode of the real agent loop (`build_graph` + `run_episode`, a `PostgresSaver`
and a `PostgresRecorder` on `runtime_svc`, a `DirectExecutor` over HTTP to the simulator)
and either blocks at a kill point until the orchestrator SIGKILLs it, or runs to the end and
prints the final state as one JSON line on stdout.

Kill-point hooks live here, in the harness, and only here: they wrap the model or the
executor handed to `build_graph`, so nothing in src/ knows about crashes. A hook writes a
`crash_points` row with this process's pid and then sleeps. It never raises: a raise is an
orderly exit through LangGraph, which flushes checkpoints on the way out, and that is
exactly the behaviour a real crash does not get.

    before_send       the executor blocks before the first charge is delegated, so no
                      request has left the process.
    after_checkpoint  the model blocks at the start of its second call. LangGraph starts
                      that call only after the tool node's step is finished, and under
                      durability="sync" only after that step's checkpoint is written.
    after_tool_step_immediate
                      the model SIGKILLs its own process at the start of its second call,
                      with no row written and no pause. Supplementary, for the durability
                      contrast only: a kill the orchestrator sends always lands some
                      milliseconds after the hook parks, which is time enough for a
                      background (durability="async") checkpoint write to land, so only a
                      kill at the very instant the next step starts can show the difference.
    none              no hook. Used for the kill between the commit and the checkpoint,
                      where the simulator itself holds the charge (hold_after_commit) and
                      the orchestrator kills this process while it waits for the response.

Configuration: the connection details come from the environment, so no password is on a
command line (LAB_REPLAY_RUNTIME_URL is a runtime_svc URL, LAB_REPLAY_SIM_URL the public
simulator URL, LAB_REPLAY_CLIENT_TOKEN its bearer token). Everything else is argv.

Network guard. The worker installs tests/network_guard.py before anything else. Every refused
connection is appended to the file named by LAB_NETWORK_GUARD_LOG at once (so a worker that is
SIGKILLed still leaves it), and a worker that finishes with any refusal exits 6 and lists them
on stderr. The orchestrator fails the test on a non-empty log, whatever the exit code.
`--probe-host HOST` is the test of that: the first model call tries to connect to HOST and
swallows the error, as a retrying HTTP client would.

Exit codes: 0 finished (stdout has {"ok": true, "state": ...}); 3 the resume was refused
(stdout has {"ok": false, "error": ...}); 4 a hook was never killed; 5 the orchestrator
went away while a hook was blocking; 6 the guard refused a network attempt. 4 and 5 leave
through os._exit, so no LangGraph exit path runs and the database is left as a crash would
leave it.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

# The suite's own guard, installed before anything else runs: this process builds and runs
# the whole graph with a model chosen here, so it must not be able to leave the machine
# either. `tests` is importable as a package from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.network_guard import LOG_ENV, NetworkGuard  # noqa: E402

GUARD = NetworkGuard(log_path=os.environ.get(LOG_ENV))
GUARD.install()

# Before anything imports langchain/langsmith: no trace of an episode may leave the machine.
# The same function the runner uses, so the variable list lives in one place.
from lab.runner.tracing import force_tracing_off  # noqa: E402

force_tracing_off()

import argparse  # noqa: E402
import json  # noqa: E402
import socket  # noqa: E402
import time  # noqa: E402
from typing import Any  # noqa: E402

import psycopg  # noqa: E402
from langgraph.checkpoint.postgres import PostgresSaver  # noqa: E402

from lab.agent.client import SimulatorClient, TrustedContext  # noqa: E402
from lab.agent.executor import DirectExecutor, Executor, ToolOutcome  # noqa: E402
from lab.agent.graph import PostgresRecorder, build_graph, run_episode  # noqa: E402
from lab.agent.model import ModelTurn, ToolCall  # noqa: E402
from lab.agent.prompts import system_prompt, user_prompt  # noqa: E402
from lab.agent.scripted import ScriptedModel  # noqa: E402
from lab.agent.tools import tool_schemas  # noqa: E402
from lab.protocol.task import DEFAULT_TASK, Profile  # noqa: E402

KILL_POINTS = ("before_send", "after_checkpoint", "after_tool_step_immediate", "none")
MAX_TURNS = 6
TOOLS = tool_schemas(Profile.CONTROLLED, status_available=True)
START = [
    {"role": "system", "content": system_prompt("neutral")},
    {"role": "user", "content": user_prompt(DEFAULT_TASK, Profile.CONTROLLED)},
]

# A hook that is never killed gives up after this long, so a broken orchestrator cannot
# leave a worker behind for ever.
BLOCK_LIMIT_S = 120.0
EXIT_RESUME_REFUSED = 3
EXIT_NOT_KILLED = 4
EXIT_ORPHANED = 5
EXIT_NETWORK_ATTEMPT = 6


class KillPoint:
    """Announce that this process reached `name`, then wait to be killed."""

    def __init__(self, conn: psycopg.Connection, run_id: str, name: str) -> None:
        self._conn = conn
        self._run_id = run_id
        self.name = name

    def block(self) -> None:
        parent = os.getppid()
        self._conn.execute(
            "INSERT INTO crash_points (run_id, name, worker_pid) VALUES (%s, %s, %s)",
            (self._run_id, self.name, os.getpid()),
        )
        deadline = time.monotonic() + BLOCK_LIMIT_S
        while time.monotonic() < deadline:
            if os.getppid() != parent:
                os._exit(EXIT_ORPHANED)
            time.sleep(0.02)
        os._exit(EXIT_NOT_KILLED)


class BlockBeforeFirstCharge:
    """Executor wrapper: the first charge stops here, before any request is sent."""

    def __init__(self, inner: Executor, kill_point: KillPoint) -> None:
        self._inner = inner
        self._kill_point = kill_point

    def execute(self, call: ToolCall) -> ToolOutcome:
        if call.name == "charge":
            self._kill_point.block()
        return self._inner.execute(call)


class BlockAtSecondModelCall:
    """Model wrapper: the second model call of this process stops here, before it starts."""

    def __init__(self, inner: ScriptedModel, kill_point: KillPoint) -> None:
        self._inner = inner
        self._kill_point = kill_point
        self._calls = 0
        self.model_id = inner.model_id

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        self._calls += 1
        if self._calls == 2:
            self._kill_point.block()
        return self._inner.complete(messages, tools)


class KillAtSecondModelCall:
    """Model wrapper: the second model call of this process SIGKILLs the process at once."""

    def __init__(self, inner: ScriptedModel) -> None:
        self._inner = inner
        self._calls = 0
        self.model_id = inner.model_id

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        self._calls += 1
        if self._calls == 2:
            os.kill(os.getpid(), signal.SIGKILL)
        return self._inner.complete(messages, tools)


class ProbeNetworkOnFirstCall:
    """Model wrapper: the first model call tries to connect to `host` and ignores the failure.
    Only the guard's log can tell that it happened."""

    def __init__(self, inner: ScriptedModel, host: str) -> None:
        self._inner = inner
        self._host = host
        self._probed = False
        self.model_id = inner.model_id

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        if not self._probed:
            self._probed = True
            try:
                socket.create_connection((self._host, 443), timeout=1)
            except OSError:
                pass
        return self._inner.complete(messages, tools)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--policy", default="false_success")
    parser.add_argument("--kill-point", choices=KILL_POINTS, default="none")
    parser.add_argument("--durability", default="sync")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--probe-host", help="test hook: try to connect to this host once")
    return parser.parse_args(argv)


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    runtime_url = os.environ["LAB_REPLAY_RUNTIME_URL"]
    ctx = TrustedContext(
        tenant="replay",
        namespace=args.namespace,
        run_id=args.run_id,
        episode_id="replay-episode",
        task_id="charge-order-1234",
    )
    client = SimulatorClient(
        os.environ["LAB_REPLAY_SIM_URL"], os.environ["LAB_REPLAY_CLIENT_TOKEN"], ctx
    )
    executor: Executor = DirectExecutor(client)
    model: Any = ScriptedModel(args.policy)
    try:
        with (
            PostgresSaver.from_conn_string(runtime_url) as saver,
            psycopg.connect(runtime_url, autocommit=True) as record_conn,
            psycopg.connect(runtime_url, autocommit=True) as hook_conn,
        ):
            if args.kill_point == "before_send":
                executor = BlockBeforeFirstCharge(
                    executor, KillPoint(hook_conn, args.run_id, args.kill_point)
                )
            elif args.kill_point == "after_checkpoint":
                model = BlockAtSecondModelCall(
                    model, KillPoint(hook_conn, args.run_id, args.kill_point)
                )
            elif args.kill_point == "after_tool_step_immediate":
                model = KillAtSecondModelCall(model)
            if args.probe_host:
                model = ProbeNetworkOnFirstCall(model, args.probe_host)
            graph = build_graph(
                model,
                executor,
                TOOLS,
                MAX_TURNS,
                recorder=PostgresRecorder(record_conn, args.run_id),
                checkpointer=saver,
            )
            try:
                state = run_episode(
                    graph,
                    args.thread_id,
                    None if args.resume else START,
                    durability=args.durability,
                    allow_non_sync=args.durability != "sync",
                )
            except ValueError as exc:
                # run_episode refuses to resume a thread with no checkpoint. That refusal is
                # a measured result (durability="exit"), so it is reported, not raised.
                _emit({"ok": False, "error": str(exc)})
                return EXIT_RESUME_REFUSED
    finally:
        client.close()
    _emit({"ok": True, "state": state})
    return 0


def _run() -> int:
    code = main()
    if GUARD.blocked:
        sys.stderr.write(
            "network guard refused: " + "; ".join(str(a) for a in GUARD.blocked) + "\n"
        )
        return code or EXIT_NETWORK_ATTEMPT
    return code


if __name__ == "__main__":
    sys.exit(_run())
