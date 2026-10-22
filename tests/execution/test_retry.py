import json
from contextlib import contextmanager
from datetime import timedelta

import httpx
import pytest

from lab.agent.client import SimulatorClient, TrustedContext
from lab.agent.model import ToolCall
from lab.execution.clock import DeadlineBudget, DeadlineExceeded, NoopGuard
from lab.execution.config import ExecutionConfig
from tests.execution.helpers import FakeClock
from tests.helpers import CHARGE

CTX = TrustedContext(
    tenant="lab", run_id="run-001", task_id="charge-order-42", namespace="ns", episode_id="ep"
)
KEY = "arl-112acdfc-c3cc-539a-9db0-5cc42dfa5e27"
AMBIGUOUS = {"error": {"code": "gateway_timeout", "message": "Payment status is UNKNOWN."}}
SUCCEEDED = {
    "message": "Charge succeeded.",
    "replayed": False,
    "charge": {
        "charge_id": 1,
        "idempotency_key": KEY,
        "order_id": "1234",
        "amount": "49.99",
        "currency": "USD",
    },
}


def charge(arguments=None):
    return ToolCall(
        id="charge-1", name="charge", arguments=dict(CHARGE if arguments is None else arguments)
    )


@contextmanager
def execution(handler, *, seconds=300, config=None, guard=None, clock=None):
    from lab.execution.retry import RetryExecutor

    clock = clock or FakeClock()
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)

    with httpx.Client(transport=httpx.MockTransport(record), timeout=30) as http:
        client = SimulatorClient("http://sim", "client-test", CTX, http_client=http)
        budget = DeadlineBudget(clock.now() + timedelta(seconds=seconds), clock)
        executor = RetryExecutor(
            client, config or ExecutionConfig(), budget, clock=clock, guard=guard or NoopGuard()
        )
        yield executor, seen, clock


def test_retry_uses_one_key_and_two_waits():
    with execution(lambda r: httpx.Response(504, json=AMBIGUOUS)) as (executor, seen, clock):
        outcome = executor.execute(charge())
    bodies = [json.loads(r.content) for r in seen]
    assert bodies == [{**CHARGE, "idempotency_key": KEY}] * 3
    assert clock.sleeps == [1.0, 2.0]
    assert outcome.requested_args == CHARGE
    assert outcome.requested_key == "k1" and outcome.effective_key == KEY
    assert outcome.effective_args == {**CHARGE, "idempotency_key": KEY}
    assert outcome.http_status == 504
    assert outcome.observation == "HTTP 504 Gateway Timeout: Payment status is UNKNOWN."
    assert outcome.rule == "deterministic_retry"
    assert outcome.evidence.request_count == 3
    assert {r.headers["X-Lab-Namespace"] for r in seen} == {"ns"}


def test_transport_exhaustion_is_uncertain_with_no_fabricated_status():
    def timeout(request):
        raise httpx.ReadTimeout("contains untrusted secret text", request=request)

    with execution(timeout) as (executor, seen, clock):
        outcome = executor.execute(charge())
    assert len(seen) == 3 and clock.sleeps == [1, 2]
    assert outcome.observation == "Payment outcome is unknown because no response was received."
    assert outcome.http_status is None
    assert outcome.effective_key == KEY
    assert outcome.evidence.source_kind == "transport_uncertainty"
    assert outcome.evidence.source_http_status is None


def test_success_keeps_existing_renderer_and_preserves_wrong_requested_amount():
    with execution(lambda r: httpx.Response(200, json=SUCCEEDED)) as (executor, seen, clock):
        outcome = executor.execute(charge({**CHARGE, "amount": "17.25"}))
    assert json.loads(seen[0].content)["amount"] == "17.25"
    assert len(seen) == 1 and clock.sleeps == []
    assert outcome.observation == "Charge succeeded."
    assert outcome.evidence.request_count == 1


def test_retry_stops_after_a_success_and_uses_a_local_argument_snapshot():
    request = charge()
    count = 0

    def handler(r):
        nonlocal count
        count += 1
        request.arguments["amount"] = 999
        return (
            httpx.Response(504, json=AMBIGUOUS)
            if count == 1
            else httpx.Response(200, json=SUCCEEDED)
        )

    with execution(handler) as (executor, seen, clock):
        outcome = executor.execute(request)
    assert [json.loads(r.content)["amount"] for r in seen] == [49.99, 49.99]
    assert outcome.requested_args["amount"] == 49.99
    assert clock.sleeps == [1]
    assert outcome.http_status == 200


@pytest.mark.parametrize("status", [400, 401, 404, 409, 422, 429])
def test_definitive_statuses_do_not_retry(status):
    with execution(lambda r: httpx.Response(status, json={"detail": "refused"})) as (
        executor,
        seen,
        clock,
    ):
        outcome = executor.execute(charge())
    assert len(seen) == 1 and clock.sleeps == []
    assert outcome.http_status == status


@pytest.mark.parametrize(
    "arguments",
    [
        {**CHARGE, "idempotency_key": 5},
        {**CHARGE, "idempotency_key": ""},
        {**CHARGE, "idempotency_key": "\x00"},
        {"idempotency_key": "k1"},
        {**CHARGE, "amount": True},
        {**CHARGE, "amount": "0.001"},
        {**CHARGE, "currency": "usd"},
        {**CHARGE, "extra": "bad"},
    ],
)
def test_invalid_source_is_never_repaired_or_retried(arguments):
    with execution(
        lambda r: httpx.Response(
            400, json={"error": {"code": "invalid_request", "message": "invalid"}}
        )
    ) as (executor, seen, clock):
        outcome = executor.execute(charge(arguments))
    assert len(seen) == 1
    assert json.loads(seen[0].content) == arguments
    assert clock.sleeps == [] and outcome.http_status == 400
    assert outcome.requested_args == outcome.effective_args == arguments


def test_namespace_arguments_and_unknown_tools_remain_unsent():
    with execution(lambda r: httpx.Response(200, json=SUCCEEDED)) as (executor, seen, clock):
        denied = executor.execute(charge({**CHARGE, "X_Lab_Namespace": "other"}))
        unknown = executor.execute(ToolCall(id="x", name="delete_namespace", arguments={}))
    assert seen == [] and clock.sleeps == []
    assert denied.rule == "namespace_argument_denied" and denied.http_status is None
    assert unknown.rule == "unknown_tool" and unknown.http_status is None


def test_status_maps_valid_requested_key_only_and_is_not_automatically_retried():
    with execution(lambda r: httpx.Response(503, json=AMBIGUOUS)) as (executor, seen, clock):
        outcome = executor.execute(
            ToolCall(
                id="s",
                name="get_payment_status",
                arguments={"idempotency_key": "model-chose-another-key"},
            )
        )
    assert [r.url.path for r in seen] == [f"/v1/payments/{KEY}"]
    assert outcome.requested_key == "model-chose-another-key"
    assert outcome.effective_args == {"idempotency_key": KEY}
    assert outcome.evidence.source_kind == "status_response"
    assert clock.sleeps == []


@pytest.mark.parametrize("key", [None, 3, "", "x" * 256, "\x00"])
def test_invalid_status_keys_keep_the_original_refusal_path(key):
    with execution(lambda r: httpx.Response(404, json={"detail": "Not Found"})) as (
        executor,
        seen,
        clock,
    ):
        outcome = executor.execute(
            ToolCall(id="s", name="get_payment_status", arguments={"idempotency_key": key})
        )
    if isinstance(key, str):
        assert len(seen) == 1
        assert KEY not in seen[0].url.path
        assert outcome.effective_key == key
    else:
        assert seen == [] and outcome.rule == "invalid_arguments"
    assert clock.sleeps == []


def test_deadline_stops_wait_and_next_send():
    with execution(lambda r: httpx.Response(504, json=AMBIGUOUS), seconds=0.5) as (
        executor,
        seen,
        clock,
    ):
        with pytest.raises(DeadlineExceeded):
            executor.execute(charge())
    assert len(seen) == 1
    assert clock.sleeps == [0.5]
    assert set(seen[0].extensions["timeout"].values()) == {0.5}


def test_expired_budget_makes_no_request_or_wait():
    with execution(lambda r: httpx.Response(200, json=SUCCEEDED), seconds=0) as (
        executor,
        seen,
        clock,
    ):
        with pytest.raises(DeadlineExceeded):
            executor.execute(charge())
    assert seen == [] and clock.sleeps == []


def test_lost_ownership_stops_after_the_first_send():
    class Guard:
        live = True

        def check(self):
            if not self.live:
                raise RuntimeError("lease lost")

    guard = Guard()

    def handler(request):
        guard.live = False
        return httpx.Response(504, json=AMBIGUOUS)

    with execution(handler, guard=guard) as (executor, seen, clock):
        with pytest.raises(RuntimeError, match="lease lost"):
            executor.execute(charge())
    assert len(seen) == 1 and clock.sleeps == []


def test_attempt_limit_is_local_to_each_model_tool_invocation():
    with execution(lambda r: httpx.Response(504, json=AMBIGUOUS)) as (executor, seen, clock):
        one = executor.execute(charge())
        two = executor.execute(charge({**CHARGE, "idempotency_key": "k2"}))
    assert len(seen) == 6
    assert {json.loads(r.content)["idempotency_key"] for r in seen} == {KEY}
    assert one.evidence.request_count == two.evidence.request_count == 3


def test_configured_one_send_limit_never_waits():
    with execution(
        lambda r: httpx.Response(504, json=AMBIGUOUS),
        config=ExecutionConfig(b_max_attempts=1, b_backoff_s=()),
    ) as (executor, seen, clock):
        executor.execute(charge())
    assert len(seen) == 1 and clock.sleeps == []
