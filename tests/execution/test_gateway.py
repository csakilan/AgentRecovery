from datetime import UTC, datetime, timedelta

import httpx
import pytest

from lab.agent.client import SimulatorClient
from lab.agent.model import ToolCall
from lab.execution.clock import DeadlineBudget, NoopGuard
from lab.execution.config import ExecutionConfig
from lab.execution.gateway import GatewayExecutor
from lab.execution.identity import effective_key, operation_id
from lab.execution.journal import Journal
from lab.execution.outcomes import INTENT_CONFLICT, RECOVERY_UNRESOLVED
from lab.execution.reconcile import Reconciler
from lab.execution.types import OperationState
from tests.execution.helpers import FakeClock


def gateway(conn, ctx, transport, *, timeout=120):
    clock = FakeClock()
    clock.current = datetime.now(UTC)
    config = ExecutionConfig(operation_timeout_s=float(timeout))
    budget = DeadlineBudget(clock.now() + timedelta(seconds=300), clock)
    client = SimulatorClient(
        "http://sim.test", "client", ctx, http_client=httpx.Client(transport=transport)
    )
    journal = Journal(conn)
    reconciler = Reconciler(journal, client, config, clock=clock, guard=NoopGuard())
    return GatewayExecutor(
        client, journal, reconciler, config, budget, clock=clock, guard=NoopGuard()
    ), journal


def call(amount="12.00", key="model-key"):
    return ToolCall(
        id="call",
        name="charge",
        arguments=dict(idempotency_key=key, order_id="order", amount=amount, currency="USD"),
    )


def test_intent_and_attempt_precede_send_and_cached_success(journal_conn, journal_ctx):
    sent = []

    def send(request):
        import json

        from psycopg.pq import TransactionStatus

        assert journal_conn.info.transaction_status == TransactionStatus.IDLE
        args = json.loads(request.content)
        sent.append(args)
        op = Journal(journal_conn).get(str(operation_id(journal_ctx)))
        assert op.state == OperationState.IN_FLIGHT and op.attempt_count == 1
        assert op.requested_args["idempotency_key"] == "model-key"
        assert args["idempotency_key"] == effective_key(journal_ctx)
        assert (
            journal_conn.execute(
                "SELECT count(*) AS n FROM attempts WHERE operation_id=%s", (op.id,)
            ).fetchone()["n"]
            == 1
        )
        return httpx.Response(200, json={"charge": dict(args), "replayed": False})

    executor, journal = gateway(journal_conn, journal_ctx, httpx.MockTransport(send))
    first = executor.execute(call())
    assert first.evidence.operation_state == "CONFIRMED_SUCCESS"
    cached = executor.execute(call("12", "changed-key"))
    assert cached.observation == first.observation
    assert cached.evidence.cached
    assert cached.effective_args is cached.effective_key is cached.http_status is None
    assert len(sent) == 1
    conflict = executor.execute(call("49.99"))
    assert conflict.observation == INTENT_CONFLICT
    op = journal.get(str(operation_id(journal_ctx)))
    assert op.requested_args["amount"] == "12.00"
    assert (
        journal_conn.execute(
            "SELECT count(*) AS n FROM outbox WHERE operation_id=%s", (op.id,)
        ).fetchone()["n"]
        == 1
    )


@pytest.mark.parametrize(
    "status,body,state",
    [
        (400, {"error": {"code": "invalid_request"}}, "TERMINAL_FAILURE"),
        (200, {"charge": {"amount": "12"}}, "UNRESOLVED"),
        (503, {}, "UNRESOLVED"),
    ],
)
def test_refusal_and_uncertain_evidence(journal_conn, journal_ctx, status, body, state):
    sends = []

    def send(request):
        sends.append(request)
        return httpx.Response(status, json=body)

    executor, journal = gateway(journal_conn, journal_ctx, httpx.MockTransport(send), timeout=1)
    result = executor.execute(call())
    assert result.evidence.operation_state == state
    assert len(sends) == 1
    if state == "UNRESOLVED":
        assert result.observation == RECOVERY_UNRESOLVED


def test_status_before_charge_has_no_journal_and_uses_trusted_key(journal_conn, journal_ctx):
    sent = []

    def send(request):
        sent.append(request.url.path)
        return httpx.Response(200, json={"status": "NOT_FOUND"})

    executor, journal = gateway(journal_conn, journal_ctx, httpx.MockTransport(send))
    result = executor.execute(
        ToolCall(id="status", name="get_payment_status", arguments={"idempotency_key": "model-key"})
    )
    assert result.effective_key == effective_key(journal_ctx)
    assert result.rule == "durable_gateway"
    assert sent == ["/v1/payments/" + effective_key(journal_ctx)]
    with pytest.raises(KeyError):
        journal.get(str(operation_id(journal_ctx)))


def test_c_factory_requires_a_journal(journal_conn, journal_ctx):
    from lab.execution.factory import make_executor

    clock = FakeClock()
    client = SimulatorClient(
        "http://sim.test",
        "client",
        journal_ctx,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )
    with pytest.raises(ValueError, match="journal"):
        make_executor(
            "durable_gateway",
            client,
            ExecutionConfig(),
            DeadlineBudget(clock.now() + timedelta(seconds=30), clock),
            clock=clock,
            guard=NoopGuard(),
        )


def test_waiting_foreground_stops_at_deadline_without_stealing_live_claim(
    journal_conn, journal_ctx
):
    executor, journal = gateway(
        journal_conn,
        journal_ctx,
        httpx.MockTransport(lambda r: pytest.fail("live owner must not cause another send")),
        timeout=1,
    )
    requested = call()
    from lab.protocol.task import Profile
    from lab.protocol.validation import validate_charge

    op = journal.register(
        journal_ctx,
        validate_charge(Profile.CONTROLLED, requested.arguments),
        requested.arguments,
        executor.budget.deadline_at,
        config=executor.config,
    )
    claim = journal.claim(op.id, "other-live-worker", 30)
    journal.begin_attempt(claim, 3)

    class BoundedGuard:
        checks = 0

        def check(self):
            self.checks += 1
            assert self.checks <= 10, "foreground spun after its deadline"

    guard = BoundedGuard()
    executor.guard = executor.reconciler.guard = guard
    result = executor.execute(requested)
    assert result.observation == RECOVERY_UNRESOLVED
    assert result.evidence.request_count == 0
    assert result.evidence.source_kind == "deadline"
    assert result.evidence.operation_state == "IN_FLIGHT"
    assert journal.get(op.id).claim_owner == "other-live-worker"
