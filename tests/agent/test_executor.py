"""DirectExecutor: what it sends, what it records, and what the model is told.

Runs over an `httpx.MockTransport` that records every request, so each test can state exactly
what went on the wire (or that nothing did). The observation text is RetryLedger's, produced
by lab.protocol.render; tests/agent/test_observation_parity.py compares a whole episode with
the vendored original, and the tests here pin the executor's own cases.
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
from lab.protocol.render import render_charge, render_status

CTX = TrustedContext(
    tenant="tenant-1",
    namespace="ns-trusted",
    run_id="run-1",
    episode_id="ep-1",
    task_id="task-1",
)
CHARGE_ARGS = {"order_id": "1234", "amount": 49.99, "currency": "USD", "idempotency_key": "k1"}
CHARGED = {"message": "Charge succeeded.", "replayed": False}


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
    executor, seen = make_executor(lambda r: httpx.Response(200, json=CHARGED))
    outcome = executor.execute(call("charge", CHARGE_ARGS))
    assert json.loads(seen[0].content) == CHARGE_ARGS
    assert outcome == ToolOutcome(
        requested_args=CHARGE_ARGS,
        requested_key="k1",
        effective_args=CHARGE_ARGS,
        effective_key="k1",
        rule="agent_directed",
        observation="Charge succeeded.",
        http_status=200,
    )


def test_a_status_lookup_uses_the_key_and_is_recorded_like_a_charge():
    body = {"idempotency_key": "k9", "status": "NOT_FOUND"}
    executor, seen = make_executor(lambda r: httpx.Response(200, json=body))
    outcome = executor.execute(call("get_payment_status", {"idempotency_key": "k9"}))
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/v1/payments/k9")]
    assert outcome.requested_args == outcome.effective_args == {"idempotency_key": "k9"}
    assert outcome.requested_key == outcome.effective_key == "k9"
    assert (outcome.rule, outcome.http_status) == ("agent_directed", 200)
    assert outcome.observation == "Status for 'k9': NOT_FOUND — no charge recorded."


def test_a_server_error_is_recorded_with_its_status_and_body_unjudged():
    body = {"error": {"code": "timeout", "message": "the upstream did not confirm."}}
    executor, _ = make_executor(lambda r: httpx.Response(504, json=body))
    outcome = executor.execute(call("charge", CHARGE_ARGS))
    assert outcome.http_status == 504
    assert outcome.observation == "HTTP 504 Gateway Timeout: the upstream did not confirm."


def test_a_charge_without_a_string_key_is_still_sent_and_has_no_key():
    # The simulator decides what a bad charge body means (422); the executor does not.
    executor, seen = make_executor(lambda r: httpx.Response(422, json={"detail": []}))
    outcome = executor.execute(call("charge", {"amount": 1, "idempotency_key": 7}))
    assert len(seen) == 1
    assert outcome.requested_key is None and outcome.effective_key is None
    assert outcome.effective_args == {"amount": 1, "idempotency_key": 7}
    assert outcome.http_status == 422
    assert outcome.observation == 'HTTP 422 Error: {"detail":[]}'


def test_a_namespace_argument_is_denied_and_nothing_is_sent():
    executor, seen = make_executor()
    args = {**CHARGE_ARGS, "namespace": "ns-victim"}
    outcome = executor.execute(call("charge", args))
    assert seen == []
    assert outcome.rule == "namespace_argument_denied"
    assert outcome.http_status is None
    assert outcome.effective_args is None and outcome.effective_key is None
    assert outcome.requested_args == args and outcome.requested_key == "k1"
    assert outcome.observation == render_observation("namespace_argument_denied")
    assert "ns-victim" not in outcome.observation


def test_an_unknown_tool_sends_nothing():
    executor, seen = make_executor()
    outcome = executor.execute(call("refund", {"idempotency_key": "k1"}))
    assert seen == []
    assert (outcome.rule, outcome.http_status) == ("unknown_tool", None)
    assert outcome.effective_args is None and outcome.effective_key is None
    assert outcome.requested_key == "k1"
    assert outcome.observation == "Unknown tool."


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
    assert outcome.observation == render_observation("invalid_arguments")


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


# What a sent request is rendered as. Every body here is one the simulator's own handlers
# produce, so the text must be exactly what lab.protocol.render makes of it.
HANDLED = [
    ("charge", 200, {"message": "Charge succeeded.", "replayed": False}),
    ("charge", 200, {"message": "Idempotent hit: already succeeded.", "replayed": True}),
    ("charge", 504, {"error": {"code": "timeout", "message": "did not confirm."}}),
    ("charge", 500, {"error": {"code": "failed", "message": "not processed."}}),
    ("charge", 409, {"error": {"code": "conflict", "message": "different parameters."}}),
    ("charge", 400, {"error": {"code": "invalid", "message": "amount must be a number"}}),
    ("charge", 418, {"error": {"code": "odd", "message": "a status render.py has no name for"}}),
    (
        "get_payment_status",
        200,
        {"idempotency_key": "k", "status": "SUCCEEDED", "charge": {"amount": "49.99"}},
    ),  # fmt: skip
    ("get_payment_status", 200, {"idempotency_key": "k", "status": "NOT_FOUND"}),
    ("get_payment_status", 503, {"error": {"code": "down", "message": "unavailable."}}),
]


@pytest.mark.parametrize(("tool", "status", "body"), HANDLED)
def test_a_body_render_py_handles_is_rendered_by_render_py(tool, status, body):
    render = render_charge if tool == "charge" else render_status
    observed = render_observation("agent_directed", tool=tool, http_status=status, body=body)
    assert observed == render(status, body)


def test_the_texts_are_the_ones_retryledger_prints():
    def text(tool, status, body):
        return render_observation("agent_directed", tool=tool, http_status=status, body=body)

    assert text("charge", 200, {"message": "Charge succeeded."}) == "Charge succeeded."
    assert text("charge", 504, {"error": {"message": "x"}}) == "HTTP 504 Gateway Timeout: x"
    status = {"idempotency_key": "k", "status": "SUCCEEDED", "charge": {"amount": "49.99"}}
    assert text("get_payment_status", 200, status) == "Status for 'k': SUCCEEDED — $49.99 charged."


# Bodies render.py cannot render, each with the one line they become. None may raise.
CHARGE, STATUS = "charge", "get_payment_status"
ISE = "HTTP 500 Internal Server Error"
SUCCEEDED = {"idempotency_key": "k", "status": "SUCCEEDED"}
UNHANDLED = [
    (
        CHARGE,
        401,
        {"detail": "Not authenticated"},
        'HTTP 401 Error: {"detail":"Not authenticated"}',
    ),
    (
        CHARGE,
        422,
        {"detail": [{"loc": ["body"], "msg": "bad"}]},
        'HTTP 422 Error: {"detail":[{"loc":["body"],"msg":"bad"}]}',
    ),
    (STATUS, 404, {"detail": "Not Found"}, 'HTTP 404 Not Found: {"detail":"Not Found"}'),
    (
        CHARGE,
        502,
        {"raw_body": "<html>Bad Gateway</html>"},
        'HTTP 502 Error: {"raw_body":"<html>Bad Gateway</html>"}',
    ),
    (CHARGE, 200, {"raw_body": ""}, 'HTTP 200 OK: {"raw_body":""}'),
    (CHARGE, 200, {"replayed": False}, 'HTTP 200 OK: {"replayed":false}'),
    (CHARGE, 200, {"message": 7}, 'HTTP 200 OK: {"message":7}'),
    (CHARGE, 500, {}, f"{ISE}: {{}}"),
    (CHARGE, 500, {"error": "boom"}, f'{ISE}: {{"error":"boom"}}'),
    (CHARGE, 500, {"error": {"code": "x"}}, f'{ISE}: {{"error":{{"code":"x"}}}}'),
    (CHARGE, 500, {"error": ["a"]}, f'{ISE}: {{"error":["a"]}}'),
    (STATUS, 200, {"status": "SUCCEEDED"}, 'HTTP 200 OK: {"status":"SUCCEEDED"}'),
    (STATUS, 200, SUCCEEDED, 'HTTP 200 OK: {"idempotency_key":"k","status":"SUCCEEDED"}'),
    (
        STATUS,
        200,
        {**SUCCEEDED, "charge": {}},
        'HTTP 200 OK: {"idempotency_key":"k","status":"SUCCEEDED","charge":{}}',
    ),
    (
        STATUS,
        200,
        {**SUCCEEDED, "charge": "x"},
        'HTTP 200 OK: {"idempotency_key":"k","status":"SUCCEEDED","charge":"x"}',
    ),
    (STATUS, 500, {"message": "no error key"}, f'{ISE}: {{"message":"no error key"}}'),
    # Non-ASCII is escaped, so the line is safe in any log or database.
    (CHARGE, 500, {"detail": "ün\u2014"}, f'{ISE}: {{"detail":"\\u00fcn\\u2014"}}'),
]


@pytest.mark.parametrize(("tool", "status", "body", "expected"), UNHANDLED)
def test_a_body_render_py_cannot_render_becomes_one_plain_line(tool, status, body, expected):
    observed = render_observation("agent_directed", tool=tool, http_status=status, body=body)
    assert observed == expected
    assert "\n" not in observed


def test_the_renderer_does_not_raise_for_a_body_of_the_wrong_shape():
    for tool in ("charge", "get_payment_status"):
        for status in (200, 400, 404, 500):
            for body in (None, [], "text", 5, {"error": None}, {"message": None}):
                observed = render_observation(
                    "agent_directed", tool=tool, http_status=status, body=body
                )
                assert isinstance(observed, str) and observed.startswith("HTTP ")


def test_a_sent_request_needs_a_tool_it_can_render():
    for tool in (None, "report_done", "refund"):
        with pytest.raises(ValueError, match="charge"):
            render_observation("agent_directed", tool=tool, http_status=200, body={})


def test_what_was_not_sent_is_retryledgers_text_or_one_fixed_sentence():
    # RetryLedger's own answers to report_done and to a name it does not know.
    assert render_observation("report_done") == "Recorded."
    assert render_observation("unknown_tool") == "Unknown tool."
    # Cases RetryLedger never had: one sentence each, no JSON, nothing a model wrote.
    for rule in (
        "namespace_argument_denied",
        "invalid_arguments",
        "not_executed_after_report_done",
    ):
        text = render_observation(rule)
        assert text.startswith("Not executed: ") and text.endswith(".")
        assert not text.startswith(("{", "[")) and "\n" not in text
        assert text == render_observation(rule)  # fixed


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
