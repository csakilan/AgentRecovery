"""Normal C graph path through real HTTP and an independent payment-ledger read."""

from datetime import UTC, datetime, timedelta

import httpx
import psycopg
from langgraph.checkpoint.memory import InMemorySaver
from psycopg_pool import ConnectionPool

from lab.agent.client import SimulatorClient
from lab.agent.graph import build_graph, run_episode
from lab.agent.scripted import ScriptedModel
from lab.agent.tools import tool_schemas
from lab.execution.clock import DeadlineBudget, NoopGuard, SystemClock
from lab.execution.config import ExecutionConfig
from lab.execution.factory import make_executor
from lab.execution.journal import Journal
from lab.protocol.faults import World, world_fault_plan
from lab.protocol.manifest import Mode, make_spec
from lab.protocol.task import Profile
from lab.runner.episode import score_ledger
from lab.simulator import service
from lab.simulator.app import create_public_app
from tests.execution.test_retry_simulator import MemoryRecorder
from tests.runner.conftest import serve


def test_normal_c_graph_scores_real_effect(journal_conn, journal_ctx, conn, payments_db):
    service.configure_namespace(
        conn, journal_ctx.namespace, Profile.CONTROLLED, world_fault_plan(World.HEALTHY)
    )
    clock = SystemClock()
    config = ExecutionConfig()
    with ConnectionPool(
        payments_db["svc"], min_size=1, max_size=2, kwargs={"autocommit": True}
    ) as pool:
        with serve(create_public_app(pool, "client")) as url, httpx.Client() as http:
            client = SimulatorClient(url, "client", journal_ctx, http_client=http)
            executor = make_executor(
                Mode.DURABLE_GATEWAY,
                client,
                config,
                DeadlineBudget(datetime.now(UTC) + timedelta(minutes=5), clock),
                clock=clock,
                guard=NoopGuard(),
                journal=Journal(journal_conn),
            )
            recorder = MemoryRecorder()
            graph = build_graph(
                ScriptedModel("false_success"),
                executor,
                tool_schemas(Profile.CONTROLLED, True),
                10,
                recorder=recorder,
                checkpointer=InMemorySaver(),
            )
            state = run_episode(graph, "normal-c", [{"role": "user", "content": "charge"}])
    spec = make_spec("normal-c", World.HEALTHY, Mode.DURABLE_GATEWAY, True, 1)
    with psycopg.connect(payments_db["reader"], autocommit=True) as reader:
        score = score_ledger(
            reader,
            journal_ctx.namespace,
            spec,
            state["claimed_outcome"],
            completed_before_deadline=True,
        )
    assert score.n_effects == score.n_intended == 1
    assert score.n_wrong == 0
    assert recorder.events[0]["outcome"].evidence.operation_state == "CONFIRMED_SUCCESS"
