"""The agent loop: flow, the turn cap, durability, recording and resume.

The first groups run the loop with an in-memory checkpointer and recorder and a fake
executor, so they pin the flow without a database. The later groups use the real pieces: a
`PostgresSaver` and `PostgresRecorder` on a `runtime_svc` connection, and the simulator's
public app on a throwaway payments database. The resume tests interrupt a run in-process;
the real SIGKILL version is Task 7.
"""

import inspect
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from lab.agent.client import SimulatorClient, TrustedContext
from lab.agent.executor import DirectExecutor, ToolOutcome, render_observation
from lab.agent.graph import PostgresRecorder, build_graph, run_episode
from lab.agent.model import ModelTurn, ToolCall
from lab.agent.scripted import ScriptedModel
from lab.agent.tools import tool_schemas
from lab.protocol.faults import World, world_fault_plan
from lab.protocol.task import Profile
from lab.simulator.app import create_internal_app, create_public_app
from tests.helpers import charges

TOOLS = tool_schemas(Profile.CONTROLLED, status_available=True)
START = [
    {"role": "system", "content": "You are a payments agent."},
    {"role": "user", "content": "Charge order 1234 for 49.99 USD."},
]
TOKEN = "client-t"
CONTROL = {"Authorization": "Bearer control-t"}
NAMESPACE = "ns-graph"
CTX = TrustedContext(
    tenant="tenant-1", namespace=NAMESPACE, run_id="run-1", episode_id="ep-1", task_id="task-1"
)


# Fakes for the logic tests


class MemoryRecorder:
    def __init__(self):
        self.model_calls = []
        self.tool_events = []

    def record_model_call(self, **kwargs):
        self.model_calls.append(kwargs)

    def record_tool_event(self, **kwargs):
        self.tool_events.append(kwargs)


class FakeExecutor:
    """Answers every call with a 200 and remembers what it was asked."""

    def __init__(self):
        self.calls: list[ToolCall] = []

    def execute(self, call):
        self.calls.append(call)
        key = call.arguments.get("idempotency_key")
        return ToolOutcome(
            requested_args=call.arguments,
            requested_key=key,
            effective_args=call.arguments,
            effective_key=key,
            rule="agent_directed",
            observation=render_observation("agent_directed", http_status=200, body={"ok": True}),
            http_status=200,
        )


class SequenceModel:
    """Plays a fixed list of turns, one per call, and repeats the last. Counts its calls."""

    model_id = "test/sequence"

    def __init__(self, turns):
        self._turns = turns
        self.calls = 0

    def complete(self, messages, tools):
        turn = self._turns[min(self.calls, len(self._turns) - 1)]
        self.calls += 1
        return turn


def turn_of(*calls, text=None):
    return ModelTurn(
        text=text,
        tool_calls=tuple(
            ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)
        ),
        raw={"fake": True},
    )


def charge(key="k1"):
    return ("charge", {"order_id": "1234", "amount": 49.99, "currency": "USD",
                       "idempotency_key": key})  # fmt: skip


def done(outcome="succeeded"):
    return ("report_done", {"outcome": outcome})


def memory_graph(model, executor=None, max_turns=10):
    recorder = MemoryRecorder()
    executor = executor or FakeExecutor()
    graph = build_graph(
        model, executor, TOOLS, max_turns, recorder=recorder, checkpointer=InMemorySaver()
    )
    return graph, recorder, executor


def thread():
    return f"t-{uuid.uuid4().hex[:8]}"


# Flow


def test_a_scripted_episode_runs_to_report_done_and_claims_its_outcome():
    graph, recorder, executor = memory_graph(ScriptedModel("check_status"))
    state = run_episode(graph, thread(), START)
    assert state["done"] is True
    assert state["ended_by"] == "report_done"
    assert state["claimed_outcome"] == "succeeded"
    assert state["turn"] == 4  # charge, status, charge, report_done
    assert [c.name for c in executor.calls] == ["charge", "get_payment_status", "charge"]
    assert [m["role"] for m in state["messages"]] == ["system", "user"] + [
        "assistant",
        "tool",
    ] * 4


def test_messages_have_the_shapes_the_scripted_model_and_a_provider_expect():
    graph, _, _ = memory_graph(ScriptedModel("give_up"))
    state = run_episode(graph, thread(), START)
    assistant, tool_result = state["messages"][2:4]
    assert assistant == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "scripted_give_up_0",
                "name": "charge",
                "arguments": charge("k1")[1],
            }
        ],
    }
    assert tool_result == {
        "role": "tool",
        "tool_call_id": "scripted_give_up_0",
        "name": "charge",
        "content": '{"http_status":200,"body":{"ok":true}}',
    }


def test_report_done_ends_the_run_and_is_not_sent_to_the_executor():
    model = ScriptedModel("give_up")
    graph, recorder, executor = memory_graph(model)
    state = run_episode(graph, thread(), START)
    assert (state["claimed_outcome"], state["ended_by"], state["turn"]) == (
        "failed",
        "report_done",
        2,
    )
    assert [c.name for c in executor.calls] == ["charge"]
    last = recorder.tool_events[-1]
    assert last["tool"] == "report_done"
    assert last["outcome"].rule == "report_done"
    assert last["outcome"].http_status is None
    assert last["outcome"].effective_args is None


def test_the_model_is_not_called_again_after_report_done():
    model = SequenceModel([turn_of(done("succeeded"))])
    graph, _, _ = memory_graph(model)
    run_episode(graph, thread(), START)
    assert model.calls == 1


def test_a_report_without_an_outcome_claims_nothing_and_other_types_are_stringified():
    graph, _, _ = memory_graph(SequenceModel([turn_of(("report_done", {}))]))
    state = run_episode(graph, thread(), START)
    assert state["claimed_outcome"] is None and state["ended_by"] == "report_done"

    graph, _, _ = memory_graph(SequenceModel([turn_of(("report_done", {"outcome": 7}))]))
    assert run_episode(graph, thread(), START)["claimed_outcome"] == "7"


def test_a_model_may_report_unresolved_and_that_is_not_the_turn_cap():
    graph, _, _ = memory_graph(SequenceModel([turn_of(done("unresolved"))]))
    state = run_episode(graph, thread(), START)
    assert (state["claimed_outcome"], state["ended_by"]) == ("unresolved", "report_done")


def test_a_turn_with_no_tool_calls_appends_the_message_and_asks_again():
    model = SequenceModel(
        [turn_of(text="thinking"), turn_of(text="still thinking"), turn_of(done())]
    )
    graph, recorder, executor = memory_graph(model)
    state = run_episode(graph, thread(), START)
    assert state["turn"] == 3 and state["claimed_outcome"] == "succeeded"
    assistants = [m for m in state["messages"] if m["role"] == "assistant"]
    assert [a["content"] for a in assistants] == ["thinking", "still thinking", None]
    assert assistants[0]["tool_calls"] == []
    assert executor.calls == []
    assert [m["role"] for m in state["messages"]].count("tool") == 1  # only report_done


def test_several_calls_in_one_turn_run_in_order_and_each_gets_a_tool_message():
    model = SequenceModel([turn_of(charge("a"), charge("b"), done())])
    graph, recorder, executor = memory_graph(model)
    state = run_episode(graph, thread(), START)
    assert [c.arguments["idempotency_key"] for c in executor.calls] == ["a", "b"]
    tools = [m for m in state["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["c0", "c1", "c2"]
    assert [e["turn"] for e in recorder.tool_events] == [0, 0, 0]


def test_calls_after_report_done_in_the_same_turn_are_recorded_but_not_executed():
    model = SequenceModel([turn_of(charge("a"), done("succeeded"), charge("b"), done("failed"))])
    graph, recorder, executor = memory_graph(model)
    state = run_episode(graph, thread(), START)
    assert [c.arguments["idempotency_key"] for c in executor.calls] == ["a"]
    assert state["claimed_outcome"] == "succeeded"  # the first report, not the last
    assert [(e["tool"], e["outcome"].rule) for e in recorder.tool_events] == [
        ("charge", "agent_directed"),
        ("report_done", "report_done"),
        ("charge", "not_executed_after_report_done"),
        ("report_done", "not_executed_after_report_done"),
    ]
    skipped = recorder.tool_events[2]["outcome"]
    assert skipped.requested_key == "b"
    assert skipped.effective_args is None and skipped.effective_key is None
    assert skipped.http_status is None
    assert [m["role"] for m in state["messages"]][-4:] == ["tool"] * 4


# The turn cap


def test_the_turn_cap_ends_the_run_as_unresolved_and_says_why():
    model = SequenceModel([turn_of(charge("k1"))])  # never reports
    graph, recorder, executor = memory_graph(model, max_turns=3)
    state = run_episode(graph, thread(), START)
    assert state["done"] is True
    assert state["ended_by"] == "max_turns"
    assert state["claimed_outcome"] == "unresolved"
    assert state["turn"] == 3
    assert model.calls == 3
    # The last allowed turn's tool calls still ran: the cap stops asking, it does not skip.
    assert len(executor.calls) == 3
    assert [e["turn"] for e in recorder.tool_events] == [0, 1, 2]
    assert [c["turn"] for c in recorder.model_calls] == [0, 1, 2]


def test_a_report_on_the_last_allowed_turn_is_a_report_not_a_cap():
    model = SequenceModel([turn_of(charge("k1")), turn_of(charge("k1")), turn_of(done("failed"))])
    graph, _, _ = memory_graph(model, max_turns=3)
    state = run_episode(graph, thread(), START)
    assert (state["ended_by"], state["claimed_outcome"], state["turn"]) == (
        "report_done",
        "failed",
        3,
    )


def test_turns_without_tool_calls_count_towards_the_cap():
    model = SequenceModel([turn_of(text="hm")])
    graph, _, _ = memory_graph(model, max_turns=2)
    state = run_episode(graph, thread(), START)
    assert (state["ended_by"], state["claimed_outcome"], model.calls) == (
        "max_turns",
        "unresolved",
        2,
    )


def test_one_turn_is_enough_to_run():
    graph, _, executor = memory_graph(SequenceModel([turn_of(charge())]), max_turns=1)
    state = run_episode(graph, thread(), START)
    assert state["ended_by"] == "max_turns" and len(executor.calls) == 1


@pytest.mark.parametrize("max_turns", [0, -1])
def test_a_cap_below_one_is_refused(max_turns):
    with pytest.raises(ValueError, match="max_turns"):
        memory_graph(ScriptedModel("give_up"), max_turns=max_turns)


# Durability


def test_the_default_durability_is_sync():
    parameter = inspect.signature(run_episode).parameters["durability"]
    assert parameter.default == "sync"
    assert inspect.signature(run_episode).parameters["allow_non_sync"].default is False


class SpyGraph:
    """Wraps a compiled graph and keeps what `invoke` was called with."""

    def __init__(self, graph):
        self._graph = graph
        self.invocations = []

    def get_state(self, config):
        return self._graph.get_state(config)

    def invoke(self, payload, config, **kwargs):
        self.invocations.append((payload, config, kwargs))
        return self._graph.invoke(payload, config, **kwargs)


def test_sync_reaches_graph_invoke_by_default():
    graph, _, _ = memory_graph(ScriptedModel("give_up"))
    spy = SpyGraph(graph)
    run_episode(spy, "t-spy", START)
    assert [kwargs for _, _, kwargs in spy.invocations] == [{"durability": "sync"}]


def test_a_resume_also_passes_sync():
    graph, _, _ = memory_graph(SequenceModel([turn_of(charge())]), max_turns=2)
    run_episode(graph, "t-spy2", START)
    spy = SpyGraph(graph)
    run_episode(spy, "t-spy2", None)
    assert spy.invocations[0][0] is None
    assert spy.invocations[0][2] == {"durability": "sync"}


@pytest.mark.parametrize("durability", ["async", "exit"])
def test_other_durability_is_refused_without_the_flag(durability):
    graph, _, _ = memory_graph(ScriptedModel("give_up"))
    spy = SpyGraph(graph)
    with pytest.raises(ValueError, match="sync"):
        run_episode(spy, "t-refused", START, durability=durability)
    assert spy.invocations == []  # refused before anything ran


@pytest.mark.parametrize("durability", ["async", "exit"])
def test_other_durability_reaches_invoke_only_with_the_flag(durability):
    graph, _, _ = memory_graph(ScriptedModel("give_up"))
    spy = SpyGraph(graph)
    run_episode(spy, "t-flag", START, durability=durability, allow_non_sync=True)
    assert spy.invocations[0][2] == {"durability": durability}


@pytest.mark.parametrize("durability", ["", "SYNC", "sync ", "eventual", None])
def test_a_value_langgraph_does_not_accept_is_refused_even_with_the_flag(durability):
    graph, _, _ = memory_graph(ScriptedModel("give_up"))
    with pytest.raises(ValueError, match="durability"):
        run_episode(graph, "t-bad", START, durability=durability, allow_non_sync=True)


# Starting and resuming a thread


def test_resuming_a_thread_with_no_checkpoint_is_an_error_not_a_silent_no_op():
    graph, _, _ = memory_graph(ScriptedModel("give_up"))
    with pytest.raises(ValueError, match="no checkpoint"):
        run_episode(graph, "t-never-started", None)


def test_starting_over_an_existing_thread_is_refused():
    graph, _, _ = memory_graph(ScriptedModel("give_up"))
    run_episode(graph, "t-twice", START)
    with pytest.raises(ValueError, match="already has a checkpoint"):
        run_episode(graph, "t-twice", START)


def test_resuming_a_finished_thread_returns_its_final_state_and_does_nothing_more():
    model = ScriptedModel("give_up")
    graph, recorder, executor = memory_graph(model)
    first = run_episode(graph, "t-finished", START)
    second = run_episode(graph, "t-finished", None)
    assert second == first
    assert len(executor.calls) == 1 and len(recorder.model_calls) == 2


# Against Postgres and the real simulator


@pytest.fixture
def run(runtime_db):
    """An experiment and a run row (the recorder's tables reference them), removed after."""
    suffix = uuid.uuid4().hex[:8]
    ids = SimpleNamespace(
        experiment=f"e-{suffix}", run=f"r-{suffix}", thread=f"t-{suffix}", db=runtime_db["svc"]
    )
    with Connection.connect(runtime_db["svc"], autocommit=True, row_factory=dict_row) as conn:
        conn.execute(
            "INSERT INTO experiments (id, name, manifest_sha256, config_json)"
            " VALUES (%s, 'graph', 'abc', '{}')",
            (ids.experiment,),
        )
        conn.execute(
            "INSERT INTO runs (id, experiment_id, episode_id, namespace, mode, thread_id, state)"
            " VALUES (%s, %s, 'ep', %s, 'agent_directed', %s, 'running')",
            (ids.run, ids.experiment, NAMESPACE, ids.thread),
        )
        ids.conn = conn
        yield ids
        conn.execute("DELETE FROM experiments WHERE id = %s", (ids.experiment,))


@pytest.fixture
def saver(run):
    with PostgresSaver.from_conn_string(run.db) as saver:
        yield saver
        saver.delete_thread(run.thread)


@pytest.fixture
def recorder(run):
    return PostgresRecorder(run.conn, run.run)


@pytest.fixture
def sim(svc_url):
    pool = ConnectionPool(
        svc_url,
        min_size=1,
        max_size=5,
        open=True,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    public = TestClient(create_public_app(pool, TOKEN))
    internal = TestClient(create_internal_app(pool, "control-t"))
    r = internal.put(
        f"/internal/namespaces/{NAMESPACE}",
        headers=CONTROL,
        json={
            "profile": "controlled",
            "fault_plan": world_fault_plan(World.LOST_ACK).model_dump(mode="json"),
        },
    )
    assert r.status_code == 200, r.text
    yield public
    pool.close()


@pytest.fixture
def direct(sim):
    return DirectExecutor(SimulatorClient("http://testserver", TOKEN, CTX, http_client=sim))


def rows(run, table, order="id"):
    return run.conn.execute(
        f"SELECT * FROM {table} WHERE run_id = %s ORDER BY {order}", (run.run,)
    ).fetchall()


def sim_charge_calls(conn):
    return conn.execute("SELECT count(*) AS n FROM call_log WHERE endpoint = 'charge'").fetchone()[
        "n"
    ]


def test_an_episode_against_the_simulator_is_recorded_row_by_row(
    run, saver, recorder, direct, sim, conn
):
    model = ScriptedModel("stable_key")
    graph = build_graph(model, direct, TOOLS, 6, recorder=recorder, checkpointer=saver)
    state = run_episode(graph, run.thread, START)

    assert (state["done"], state["ended_by"], state["claimed_outcome"]) == (
        True,
        "report_done",
        "succeeded",
    )
    # The world is lost_ack: the first charge committed and errored, the retry with the same
    # key was answered from the ledger, so the customer is charged once.
    assert len(charges(conn, NAMESPACE)) == 1

    calls = rows(run, "model_calls", "turn")
    assert [c["turn"] for c in calls] == [0, 1, 2]
    assert {c["provider_model_id"] for c in calls} == {"scripted/stable_key"}
    assert [c["tool_calls_json"][0]["name"] for c in calls] == ["charge", "charge", "report_done"]
    assert calls[0]["response_json"] == {"scripted": True, "policy": "stable_key", "turn": 0}
    assert calls[0]["request_json"]["messages"] == START
    assert calls[0]["request_json"]["tools"] == TOOLS
    assert len(calls[1]["request_json"]["messages"]) == len(START) + 2  # + assistant + tool
    assert [c["malformed"] for c in calls] == [False] * 3
    assert all(c["latency_ms"] is not None and c["latency_ms"] >= 0 for c in calls)

    events = rows(run, "tool_events")
    assert [(e["turn"], e["tool"], e["rule"]) for e in events] == [
        (0, "charge", "agent_directed"),
        (1, "charge", "agent_directed"),
        (2, "report_done", "report_done"),
    ]
    first, second, report = events
    assert first["http_status"] != 200 and second["http_status"] == 200
    for event in (first, second):
        assert event["requested_args_json"] == event["effective_args_json"]
        assert event["requested_key"] == event["effective_key"] == "k1"
    assert second["observation"].startswith('{"http_status":200,"body":')
    assert report["http_status"] is None
    assert report["effective_args_json"] is None and report["effective_key"] is None
    assert report["requested_args_json"] == {"outcome": "succeeded"}


def test_a_malformed_turn_is_flagged_on_its_row(run, saver, recorder, direct):
    graph = build_graph(
        ScriptedModel("malformed_call"), direct, TOOLS, 4, recorder=recorder, checkpointer=saver
    )
    run_episode(graph, run.thread, START)
    assert [c["malformed"] for c in rows(run, "model_calls", "turn")] == [True, False]
    first = rows(run, "tool_events")[0]
    assert first["http_status"] == 400  # the simulator rejected it; the loop passed that on


def test_a_namespace_argument_is_recorded_as_denied_and_never_reaches_the_simulator(
    run, saver, recorder, direct, conn
):
    args = {**charge()[1], "namespace": "ns-victim"}
    model = SequenceModel([turn_of(("charge", args)), turn_of(done("failed"))])
    graph = build_graph(model, direct, TOOLS, 4, recorder=recorder, checkpointer=saver)
    run_episode(graph, run.thread, START)

    denied = rows(run, "tool_events")[0]
    assert denied["rule"] == "namespace_argument_denied"
    assert denied["http_status"] is None
    assert denied["requested_args_json"] == args
    assert denied["effective_args_json"] is None and denied["effective_key"] is None
    assert denied["requested_key"] == "k1"
    assert sim_charge_calls(conn) == 0
    assert charges(conn, NAMESPACE) == []


def test_the_episode_state_is_checkpointed_after_every_step(run, saver, recorder, direct):
    graph = build_graph(
        ScriptedModel("give_up"), direct, TOOLS, 4, recorder=recorder, checkpointer=saver
    )
    run_episode(graph, run.thread, START)
    config = {"configurable": {"thread_id": run.thread}}
    history = list(graph.get_state_history(config))
    # input, then after each of: model, tool, model, tool.
    assert len(history) >= 5
    assert history[0].values["done"] is True
    assert history[0].next == ()


def test_recording_a_model_call_twice_for_one_turn_keeps_the_first(run, recorder):
    result = turn_of(charge())
    for text in ("first", "second"):
        recorder.record_model_call(
            turn=0,
            provider_model_id=text,
            messages=START,
            tools=TOOLS,
            result=result,
            latency_ms=1.0,
        )
    calls = rows(run, "model_calls")
    assert [(c["turn"], c["provider_model_id"]) for c in calls] == [(0, "first")]


def test_recording_a_tool_event_twice_keeps_both(run, recorder):
    outcome = FakeExecutor().execute(ToolCall(id="c", name="charge", arguments=charge()[1]))
    for _ in range(2):
        recorder.record_tool_event(turn=0, tool="charge", outcome=outcome)
    assert len(rows(run, "tool_events")) == 2


def test_the_recorder_requires_an_autocommit_connection(runtime_db, run):
    with Connection.connect(runtime_db["svc"]) as plain:
        with pytest.raises(ValueError, match="autocommit"):
            PostgresRecorder(plain, run.run)


# Interrupted and resumed in-process. Task 7 does the same with a real SIGKILL.


class Crash(Exception):
    """Stands in for the process dying at a chosen point."""


class CrashAfterRequest:
    """Sends the nth call through the real executor, then 'dies' before the loop can record it."""

    def __init__(self, inner, crash_on_call):
        self._inner = inner
        self._crash_on_call = crash_on_call
        self.count = 0

    def execute(self, call):
        self.count += 1
        outcome = self._inner.execute(call)
        if self.count == self._crash_on_call:
            raise Crash("died after the request was sent")
        return outcome


def test_a_run_interrupted_after_a_checkpoint_resumes_on_the_same_thread_and_finishes(
    run, saver, recorder, direct, conn
):
    config = {"configurable": {"thread_id": run.thread}}
    model = ScriptedModel("stable_key")

    # First process: the second charge reaches the simulator, then the process dies.
    crashing = CrashAfterRequest(direct, crash_on_call=2)
    graph = build_graph(model, crashing, TOOLS, 6, recorder=recorder, checkpointer=saver)
    with pytest.raises(Crash):
        run_episode(graph, run.thread, START)

    # The model node's step was checkpointed with sync durability, so the thread waits at the
    # tool node. What was written before the death is already in the database.
    snapshot = graph.get_state(config)
    assert snapshot.next == ("tool",)
    assert snapshot.values["turn"] == 2 and snapshot.values["done"] is False
    assert [c["turn"] for c in rows(run, "model_calls", "turn")] == [0, 1]
    assert [e["turn"] for e in rows(run, "tool_events")] == [0]  # turn 1's event was lost
    assert sim_charge_calls(conn) == 2

    # Second process: a new graph on a new executor, the same saver and thread, no messages.
    resumed = build_graph(model, direct, TOOLS, 6, recorder=recorder, checkpointer=saver)
    state = run_episode(resumed, run.thread, None)

    assert (state["done"], state["ended_by"], state["claimed_outcome"]) == (
        True,
        "report_done",
        "succeeded",
    )
    # The interrupted tool node ran again from its start: the same charge went out a second
    # time, and its tool event is written now. The model node did not repeat.
    assert sim_charge_calls(conn) == 3
    assert [e["turn"] for e in rows(run, "tool_events")] == [0, 1, 2]
    assert [c["turn"] for c in rows(run, "model_calls", "turn")] == [0, 1, 2]
    # The stable key kept the ledger at one charge, whatever the repeat count.
    assert len(charges(conn, NAMESPACE)) == 1


class CrashAfterModelRow:
    """Writes the first model call's row, then 'dies' before the step can be checkpointed."""

    def __init__(self, inner):
        self._inner = inner
        self._armed = True

    def record_model_call(self, **kwargs):
        self._inner.record_model_call(**kwargs)
        if self._armed:
            self._armed = False
            raise Crash("died between the model row and its checkpoint")

    def record_tool_event(self, **kwargs):
        self._inner.record_tool_event(**kwargs)


def test_a_replayed_model_call_does_not_crash_the_resume(run, saver, recorder, direct):
    graph = build_graph(
        ScriptedModel("give_up"),
        direct,
        TOOLS,
        4,
        recorder=CrashAfterModelRow(recorder),
        checkpointer=saver,
    )
    with pytest.raises(Crash):
        run_episode(graph, run.thread, START)
    assert [c["turn"] for c in rows(run, "model_calls", "turn")] == [0]
    assert rows(run, "tool_events") == []

    # Only the input was checkpointed, so the resume asks the model for turn 0 again. Its
    # row is already there; the insert is skipped instead of raising UniqueViolation.
    state = run_episode(graph, run.thread, None)
    assert state["done"] is True and state["claimed_outcome"] == "failed"
    assert [c["turn"] for c in rows(run, "model_calls", "turn")] == [0, 1]
    assert [e["tool"] for e in rows(run, "tool_events")] == ["charge", "report_done"]


def test_a_real_simulator_outage_propagates_instead_of_being_recorded_as_an_outcome(
    run, saver, recorder
):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    http = httpx.Client(transport=httpx.MockTransport(refuse))
    client = SimulatorClient("http://sim:8100", TOKEN, CTX, http_client=http)
    graph = build_graph(
        ScriptedModel("give_up"), DirectExecutor(client), TOOLS, 4, recorder=recorder,
        checkpointer=saver,
    )  # fmt: skip
    with pytest.raises(httpx.ConnectError):
        run_episode(graph, run.thread, START)
    assert rows(run, "tool_events") == []
