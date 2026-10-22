"""Actual model responses survive replay without authorizing stale execution."""

import uuid

import psycopg
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from psycopg.rows import dict_row

from lab.agent.graph import PostgresRecorder, build_graph, run_episode
from lab.agent.invocations import InvocationJournal
from lab.agent.model import ModelTurn, ToolCall
from lab.execution.clock import DeadlineBudget, SystemClock
from lab.execution.types import LeaseLost
from lab.protocol.evidence import unpack_evidence, unpack_text
from lab.runner.ownership import RunGuard, claim_run
from lab.runtime.fenced_checkpoints import FencedPostgresSaver
from tests.agent.test_graph import START, TOOLS, FakeExecutor, SequenceModel, done, turn_of


@pytest.fixture
def invocation_run(runtime_db):
    token = uuid.uuid4().hex
    with psycopg.connect(runtime_db["svc"], autocommit=True, row_factory=dict_row) as conn:
        conn.execute(
            "INSERT INTO experiments(id,name,manifest_sha256,config_json) "
            "VALUES(%s,'invocation-test','s','{}')",
            (token,),
        )
        conn.execute(
            "INSERT INTO "
            "runs(id,experiment_id,episode_id,namespace,mode,thread_id,state,resume_version,"
            "episode_spec_json,worker_input_json,execution_config_json,"
            "initial_messages_json,model_descriptor_json,"
            "setup_state,started_at,deadline_at) "
            "VALUES(%s,%s,'e',%s,'agent_directed',%s,'queued',1,"
            "'{}','{}','{}','{}','{}','ready',clock_timestamp(),"
            "clock_timestamp()+interval '5 minutes')",
            (token, token, token, token),
        )
        yield conn, claim_run(conn, token, "first")
        conn.execute("DELETE FROM experiments WHERE id=%s", (token,))


def test_replayed_turn_retains_both_invocations(invocation_run, runtime_db):
    conn, claim = invocation_run
    recorder = PostgresRecorder(conn, claim.run_id, encoded_evidence=True)
    model = SequenceModel(
        [turn_of(done("failed"), text="first"), turn_of(done("succeeded"), text="second")]
    )
    model._turns[0] = model._turns[0].model_copy(update={"raw": {"response": "first"}})
    model._turns[1] = model._turns[1].model_copy(update={"raw": {"response": "second"}})

    class CrashAfterResponse:
        def check(self):
            if model.calls == 1:
                raise RuntimeError("crash before model update")

    with psycopg.connect(runtime_db["svc"], autocommit=True) as checkpoint_conn:
        graph = build_graph(
            model,
            FakeExecutor(),
            TOOLS,
            10,
            recorder=recorder,
            checkpointer=FencedPostgresSaver(checkpoint_conn, claim),
            invocations=InvocationJournal(conn, claim),
            guard=CrashAfterResponse(),
        )
        with pytest.raises(RuntimeError, match="before model update"):
            run_episode(graph, claim.thread_id, START)
        first = conn.execute(
            "SELECT response_json FROM model_calls WHERE run_id=%s", (claim.run_id,)
        ).fetchone()
        conn.execute(
            "UPDATE runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (claim.run_id,),
        )
        replacement = claim_run(conn, claim.run_id, "second")
        graph = build_graph(
            model,
            FakeExecutor(),
            TOOLS,
            10,
            recorder=recorder,
            checkpointer=FencedPostgresSaver(checkpoint_conn, replacement),
            invocations=InvocationJournal(conn, replacement),
            guard=RunGuard(
                conn, replacement, DeadlineBudget(replacement.deadline_at, SystemClock())
            ),
        )
        state = run_episode(graph, replacement.thread_id, None)
    rows = conn.execute(
        "SELECT * FROM model_invocations WHERE run_id=%s ORDER BY started_at", (claim.run_id,)
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["id"] != rows[1]["id"]
    assert rows[0]["delivery_version"] < rows[1]["delivery_version"]
    assert [unpack_evidence(r["response_json"])["raw"] for r in rows] == [
        {"response": "first"},
        {"response": "second"},
    ]
    assert (
        conn.execute(
            "SELECT response_json FROM model_calls WHERE run_id=%s", (claim.run_id,)
        ).fetchone()
        == first
    )
    assert state["model_invocation_id"] == rows[1]["id"]
    assert (
        conn.execute(
            "SELECT model_invocation_id FROM tool_events WHERE run_id=%s", (claim.run_id,)
        ).fetchone()["model_invocation_id"]
        == rows[1]["id"]
    )


def test_started_unknown_and_late_response_is_evidence_only(invocation_run):
    conn, claim = invocation_run
    journal = InvocationJournal(conn, claim)
    iid = journal.start(turn=0, provider_model_id="test/model", messages=START, tools=TOOLS)
    row = conn.execute(
        "SELECT response_json,finished_at FROM model_invocations WHERE id=%s", (iid,)
    ).fetchone()
    assert row == {"response_json": None, "finished_at": None}
    conn.execute(
        "UPDATE runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
        (claim.run_id,),
    )
    replacement = claim_run(conn, claim.run_id, "replacement")
    journal.finish(iid, turn_of(done(), text="late"), 1.0)
    with pytest.raises(LeaseLost):
        journal.start(turn=1, provider_model_id="test/model", messages=START, tools=TOOLS)
    with pytest.raises(ValueError):
        InvocationJournal(conn, replacement).finish(iid, turn_of(done()), 2.0)
    assert (
        conn.execute("SELECT claimed_outcome FROM runs WHERE id=%s", (claim.run_id,)).fetchone()[
            "claimed_outcome"
        ]
        is None
    )


def test_unstorable_model_and_tool_evidence_is_lossless(invocation_run):
    conn, claim = invocation_run
    value = "nul\0surrogate\ud800"
    turn = ModelTurn(
        text=value,
        tool_calls=(ToolCall(id="call", name=value, arguments={"idempotency_key": value}),),
        raw={"raw": value},
    )

    class Model:
        model_id = "test/model"

        def complete(self, messages, tools):
            return turn

    recorder = PostgresRecorder(conn, claim.run_id, encoded_evidence=True)
    graph = build_graph(
        Model(),
        FakeExecutor(),
        TOOLS,
        1,
        recorder=recorder,
        checkpointer=InMemorySaver(),
        invocations=InvocationJournal(conn, claim),
    )
    state = run_episode(graph, claim.thread_id, START)
    invocation = conn.execute(
        "SELECT * FROM model_invocations WHERE run_id=%s", (claim.run_id,)
    ).fetchone()
    assert unpack_evidence(invocation["response_json"])["raw"] == turn.raw
    assert unpack_evidence(invocation["response_json"])["text"] == value
    event = conn.execute("SELECT * FROM tool_events WHERE run_id=%s", (claim.run_id,)).fetchone()
    assert unpack_text(event["tool"]) == value
    assert unpack_text(event["requested_key"]) == value
    assert unpack_text(event["effective_key"]) == value
    assert unpack_evidence(event["requested_args_json"])["idempotency_key"] == value
    assert unpack_text(event["observation"]) == state["messages"][-1]["content"]


def test_late_actual_model_response_cannot_checkpoint_or_execute(invocation_run, runtime_db):
    conn, claim = invocation_run
    replacements = []

    class Model:
        model_id = "test/late"

        def complete(self, messages, tools):
            # The started row must already be visible through another connection.
            with psycopg.connect(runtime_db["svc"], autocommit=True) as observer:
                assert observer.execute(
                    "SELECT finished_at FROM model_invocations WHERE run_id=%s", (claim.run_id,)
                ).fetchone() == (None,)
            conn.execute(
                "UPDATE runs SET lease_expires_at=clock_timestamp()-interval '1 second' "
                "WHERE id=%s",
                (claim.run_id,),
            )
            replacements.append(claim_run(conn, claim.run_id, "replacement"))
            return turn_of(done(), text="late actual response")

    with psycopg.connect(runtime_db["svc"], autocommit=True) as cp:
        executor = FakeExecutor()
        graph = build_graph(
            Model(),
            executor,
            TOOLS,
            10,
            recorder=PostgresRecorder(conn, claim.run_id, encoded_evidence=True),
            checkpointer=FencedPostgresSaver(cp, claim),
            invocations=InvocationJournal(conn, claim),
            guard=RunGuard(conn, claim, DeadlineBudget(claim.deadline_at, SystemClock())),
        )
        with pytest.raises(LeaseLost):
            run_episode(graph, claim.thread_id, START)
        assert executor.calls == []
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM tool_events WHERE run_id=%s", (claim.run_id,)
            ).fetchone()["n"]
            == 0
        )
        saver = FencedPostgresSaver(cp, replacements[0])
        saved = saver.get_tuple({"configurable": {"thread_id": claim.thread_id}})
        assert not any(
            m["role"] == "assistant" for m in saved.checkpoint["channel_values"]["messages"]
        )
        response = conn.execute(
            "SELECT response_json FROM model_invocations WHERE run_id=%s", (claim.run_id,)
        ).fetchone()
        assert unpack_evidence(response["response_json"])["text"] == "late actual response"


@pytest.mark.parametrize("key", ["a\0b", "a\ud800b"])
def test_invalid_key_is_refused_over_http_and_recorded_exactly(
    invocation_run, conn, payments_db, runtime_db, key
):
    import httpx
    from psycopg_pool import ConnectionPool

    from lab.agent.client import SimulatorClient, TrustedContext
    from lab.agent.executor import DirectExecutor
    from lab.protocol.faults import FaultPlan
    from lab.protocol.task import Profile
    from lab.simulator import service
    from lab.simulator.app import create_public_app
    from tests.runner.conftest import serve

    runtime, claim = invocation_run
    service.configure_namespace(conn, claim.run_id, Profile.CONTROLLED, FaultPlan())
    ctx = TrustedContext(
        tenant="lab", namespace=claim.run_id, run_id=claim.run_id, episode_id="e", task_id="task"
    )
    args = {"idempotency_key": key, "amount": "49.99", "order_id": "1234", "currency": "USD"}
    model = SequenceModel([turn_of(("charge", args))])
    with ConnectionPool(
        payments_db["svc"], min_size=1, max_size=2, kwargs={"autocommit": True}
    ) as pool:
        with (
            serve(create_public_app(pool, "client")) as url,
            httpx.Client() as http,
            psycopg.connect(runtime_db["svc"], autocommit=True) as cp,
        ):
            executor = DirectExecutor(SimulatorClient(url, "client", ctx, http_client=http))
            graph = build_graph(
                model,
                executor,
                TOOLS,
                1,
                recorder=PostgresRecorder(runtime, claim.run_id, encoded_evidence=True),
                checkpointer=FencedPostgresSaver(cp, claim),
                invocations=InvocationJournal(runtime, claim),
            )
            state = run_episode(graph, claim.thread_id, START)
            saved = graph.get_state({"configurable": {"thread_id": claim.thread_id}}).values
            assert saved["messages"][2]["tool_calls"][0]["arguments"]["idempotency_key"] == key
    event = runtime.execute("SELECT * FROM tool_events WHERE run_id=%s", (claim.run_id,)).fetchone()
    assert event["http_status"] == 400
    assert unpack_evidence(event["requested_args_json"]) == args
    assert unpack_text(event["requested_key"]) == unpack_text(event["effective_key"]) == key
    assert unpack_text(event["observation"]) == state["messages"][-1]["content"]
    assert "unpaired surrogate" in state["messages"][-1]["content"]
