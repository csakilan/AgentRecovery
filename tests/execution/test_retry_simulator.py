"""Mode B in the real graph and simulator, independently graded from the payment ledger."""

import httpx
import psycopg
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from psycopg_pool import ConnectionPool

from lab.agent.client import SimulatorClient, TrustedContext
from lab.agent.graph import build_graph, run_episode
from lab.agent.prompts import system_prompt, user_prompt
from lab.agent.scripted import ScriptedModel
from lab.agent.tools import tool_schemas
from lab.execution.clock import DeadlineBudget, NoopGuard
from lab.execution.config import ExecutionConfig
from lab.protocol.faults import World
from lab.protocol.manifest import Mode, make_spec
from lab.runner.episode import score_ledger
from lab.simulator import service
from lab.simulator.app import create_public_app
from tests.execution.helpers import FakeClock
from tests.runner.conftest import serve


class MemoryRecorder:
    def __init__(self):
        self.events = []

    def record_model_call(self, **kwargs):
        pass

    def record_tool_event(self, **kwargs):
        self.events.append(kwargs)


@pytest.mark.parametrize(
    ("world", "policy", "sends", "effects", "truthful"),
    [
        (World.LOST_ACK, "false_success", 2, 1, True),
        (World.TRUE_FAIL, "false_success", 2, 1, True),
        (World.HEALTHY, "false_success", 1, 1, True),
        (World.PERMANENT_FAIL, "false_success", 3, 0, False),
        (World.LOST_ACK, "naive_new_key", 3, 1, True),
    ],
)
def test_scripted_graph_retry_has_one_key_and_independent_ledger_verdict(
    conn,
    payments_db,
    world,
    policy,
    sends,
    effects,
    truthful,
):
    from datetime import timedelta

    from lab.execution.retry import RetryExecutor

    spec = make_spec("retry", world, Mode.DETERMINISTIC_RETRY, True, 0)
    service.configure_namespace(conn, "retry-test", spec.profile, spec.fault_plan)
    ctx = TrustedContext(
        tenant="lab",
        run_id="run-001",
        task_id="charge-order-42",
        namespace="retry-test",
        episode_id=spec.episode_id,
    )
    clock = FakeClock()
    recorder = MemoryRecorder()
    with ConnectionPool(
        payments_db["svc"], min_size=1, max_size=2, kwargs={"autocommit": True}
    ) as pool:
        with (
            serve(create_public_app(pool, "client-test")) as public_url,
            httpx.Client(timeout=30) as http,
        ):
            client = SimulatorClient(public_url, "client-test", ctx, http_client=http)
            executor = RetryExecutor(
                client,
                ExecutionConfig(),
                DeadlineBudget(clock.now() + timedelta(seconds=300), clock),
                clock=clock,
                guard=NoopGuard(),
            )
            graph = build_graph(
                ScriptedModel(policy),
                executor,
                tool_schemas(spec.profile, True),
                10,
                recorder=recorder,
                checkpointer=InMemorySaver(),
            )
            state = run_episode(
                graph,
                "retry-thread",
                [
                    {"role": "system", "content": system_prompt("neutral")},
                    {"role": "user", "content": user_prompt(spec.task, spec.profile)},
                ],
            )
    with psycopg.connect(payments_db["reader"], autocommit=True) as reader:
        score = score_ledger(
            reader, ctx.namespace, spec, state["claimed_outcome"], completed_before_deadline=True
        )
        received = reader.execute(
            "SELECT count(*) FROM call_log WHERE namespace = %s AND endpoint = 'charge'",
            (ctx.namespace,),
        ).fetchone()[0]
    assert state["claimed_outcome"] == "succeeded"
    assert score.n_effects == score.n_intended == effects
    assert score.truthful_report == truthful
    assert received == sends
    charge_events = [e for e in recorder.events if e["tool"] == "charge"]
    assert {e["outcome"].effective_key for e in charge_events} == {
        "arl-112acdfc-c3cc-539a-9db0-5cc42dfa5e27"
    }
    assert sum(e["outcome"].evidence.request_count for e in charge_events) == sends
    if policy == "naive_new_key":
        assert [e["outcome"].requested_key for e in charge_events] == ["k1", "k2"]
