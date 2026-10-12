"""DirectExecutor: what it sends, what it records, and what the model is told.

Runs over an `httpx.MockTransport` that records every request, so each test can state exactly
what went on the wire (or that nothing did).
"""

import json

import httpx
import pytest
from pydantic import ValidationError

from lab.agent.client import SimulatorClient, TrustedContext
from lab.agent.executor import (
    DirectExecutor,
    ToolOutcome,
    not_sent_outcome,
    render_observation,
    requested_key_of,
)
from lab.agent.model import ToolCall

CTX = TrustedContext(
    tenant="tenant-1",
    namespace="ns-trusted",
    run_id="run-1",
    episode_id="ep-1",
    task_id="task-1",
)
CHARGE_ARGS = {"order_id": "1234", "amount": 49.99, "currency": "USD", "idempotency_key": "k1"}


def make_executor(handler=None):
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request) if handler else httpx.Response(200, json={"ok": True})

    http = httpx.Client(transport=httpx.MockTransport(record))
    client = SimulatorClient("http://sim:8100", "t", CTX, http_client=http)
    return DirectExecutor(client), seen


def call(name, arguments, call_id="c1"):
    return ToolCall(id=call_id, name=name, arguments=arguments)


def test_a_charge_is_sent_as_asked_and_recorded_with_effective_equal_to_requested():
    executor, seen = make_executor(lambda r: httpx.Response(200, json={"status": "SUCCEEDED"}))
    outcome = executor.execute(call("charge", CHARGE_ARGS))
    assert json.loads(seen[0].content) == CHARGE_ARGS
    assert outcome == ToolOutcome(
        requested_args=CHARGE_ARGS,
        requested_key="k1",
        effective_args=CHARGE_ARGS,
        effective_key="k1",
        rule="agent_directed",
        observation='{"http_status":200,"body":{"status":"SUCCEEDED"}}',
        http_status=200,
    )


def test_a_status_lookup_uses_the_key_and_is_recorded_like_a_charge():
    executor, seen = make_executor(lambda r: httpx.Response(200, json={"status": "NOT_FOUND"}))
    outcome = executor.execute(call("get_payment_status", {"idempotency_key": "k9"}))
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/v1/payments/k9")]
    assert outcome.requested_args == outcome.effective_args == {"idempotency_key": "k9"}
    assert outcome.requested_key == outcome.effective_key == "k9"
    assert (outcome.rule, outcome.http_status) == ("agent_directed", 200)


def test_a_server_error_is_recorded_with_its_status_and_body_unjudged():
    executor, _ = make_executor(lambda r: httpx.Response(504, json={"error": {"code": "timeout"}}))
    outcome = executor.execute(call("charge", CHARGE_ARGS))
    assert outcome.http_status == 504
    assert json.loads(outcome.observation) == {
        "http_status": 504,
        "body": {"error": {"code": "timeout"}},
    }


def test_a_charge_without_a_string_key_is_still_sent_and_has_no_key():
    # The simulator decides what a bad charge body means (422); the executor does not.
    executor, seen = make_executor(lambda r: httpx.Response(422, json={"detail": []}))
    outcome = executor.execute(call("charge", {"amount": 1, "idempotency_key": 7}))
    assert len(seen) == 1
    assert outcome.requested_key is None and outcome.effective_key is None
    assert outcome.effective_args == {"amount": 1, "idempotency_key": 7}
    assert outcome.http_status == 422


def test_a_namespace_argument_is_denied_and_nothing_is_sent():
    executor, seen = make_executor()
    args = {**CHARGE_ARGS, "namespace": "ns-victim"}
    outcome = executor.execute(call("charge", args))
    assert seen == []
    assert outcome.rule == "namespace_argument_denied"
    assert outcome.http_status is None
    assert outcome.effective_args is None and outcome.effective_key is None
    assert outcome.requested_args == args and outcome.requested_key == "k1"
    error = json.loads(outcome.observation)["error"]
    assert error["code"] == "namespace_argument_denied"
    assert "ns-victim" not in outcome.observation


def test_an_unknown_tool_sends_nothing():
    executor, seen = make_executor()
    outcome = executor.execute(call("refund", {"idempotency_key": "k1"}))
    assert seen == []
    assert (outcome.rule, outcome.http_status) == ("unknown_tool", None)
    assert outcome.effective_args is None and outcome.effective_key is None
    assert outcome.requested_key == "k1"
    assert json.loads(outcome.observation)["error"]["code"] == "unknown_tool"


def test_report_done_is_not_a_tool_the_executor_knows():
    executor, seen = make_executor()
    outcome = executor.execute(call("report_done", {"outcome": "succeeded"}))
    assert seen == [] and outcome.rule == "unknown_tool"


@pytest.mark.parametrize("arguments", [{}, {"idempotency_key": 7}, {"idempotency_key": None}])
def test_a_status_call_without_a_string_key_sends_nothing(arguments):
    executor, seen = make_executor()
    outcome = executor.execute(call("get_payment_status", arguments))
    assert seen == []
    assert (outcome.rule, outcome.http_status) == ("invalid_arguments", None)
    assert outcome.requested_args == arguments
    assert outcome.requested_key is None
    assert outcome.effective_args is None and outcome.effective_key is None
    assert json.loads(outcome.observation)["error"]["code"] == "invalid_arguments"


def test_the_outcome_is_frozen():
    executor, _ = make_executor()
    outcome = executor.execute(call("charge", CHARGE_ARGS))
    with pytest.raises(ValidationError):
        outcome.rule = "other"


def test_the_outcome_does_not_alias_the_calls_arguments():
    executor, _ = make_executor()
    c = call("charge", dict(CHARGE_ARGS))
    outcome = executor.execute(c)
    assert outcome.requested_args is not c.arguments
    assert outcome.effective_args is not outcome.requested_args


def test_observations_are_compact_json_made_in_one_place():
    assert render_observation("agent_directed", http_status=200, body={"a": 1}) == (
        '{"http_status":200,"body":{"a":1}}'
    )
    for rule in ("namespace_argument_denied", "unknown_tool", "invalid_arguments"):
        error = json.loads(render_observation(rule))["error"]
        assert error["code"] == rule and error["message"]


def test_the_graph_rules_are_rendered_here_as_fixed_notices_not_errors():
    for rule in ("report_done", "not_executed_after_report_done"):
        notice = json.loads(render_observation(rule))["notice"]
        assert notice["code"] == rule and notice["message"]
        assert render_observation(rule) == render_observation(rule)  # fixed text


def test_not_sent_outcome_is_the_one_shape_for_every_call_that_sends_nothing():
    for rule in ("report_done", "not_executed_after_report_done", "unknown_tool"):
        c = call("x", {"idempotency_key": "k1", "outcome": "succeeded"})
        outcome = not_sent_outcome(c, rule)
        assert outcome == ToolOutcome(
            requested_args=c.arguments,
            requested_key="k1",
            effective_args=None,
            effective_key=None,
            rule=rule,
            observation=render_observation(rule),
            http_status=None,
        )


def test_the_requested_key_is_a_string_idempotency_key_or_none():
    assert requested_key_of({"idempotency_key": "k"}) == "k"
    for arguments in ({}, {"idempotency_key": 7}, {"idempotency_key": None}):
        assert requested_key_of(arguments) is None
        assert not_sent_outcome(call("x", arguments), "unknown_tool").requested_key is None
