"""How a model's tool call becomes a request, and what the model is told afterwards.

The loop asks an `Executor` to run one tool call and gets back a `ToolOutcome`. The outcome
keeps what the model asked for (`requested_*`) apart from what was actually sent
(`effective_*`), because that gap is the experiment: a runtime that rewrites the idempotency
key is an infrastructure fix, not evidence of better model reasoning. `DirectExecutor` is
mode A (`agent_directed`): it sends exactly what the model asked for, so effective equals
requested. Plan 3's other executors will differ in what they send, not in this record.

`report_done` never reaches an executor. It is not a request to the simulator, and the graph
handles it.

What the model reads. The text of a tool result comes from `lab.protocol.render`
(`render_charge` and `render_status`), not from this module, because the compat profile
promises that the model sees tool results byte-identical to the original RetryLedger
benchmark, and `render.py` is where that promise is kept and tested (tests/oracle and
tests/simulator). Rendering the simulator's JSON here instead would break the promise in the
one place the model actually reads it. `render_observation` is the only place this package
produces that text; it adds only what `render.py` cannot say (a body it has no case for, and
the calls that sent nothing). tests/agent/test_observation_parity.py compares a whole agent
episode, tool message by tool message, with the vendored RetryLedger. The wording here is a
pilot placeholder for the cases RetryLedger never had; Plan 3 owns any change to it, since
how a failure is worded is itself an experimental variable there.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from lab.agent.client import DENIED_STATUS, NAMESPACE_DENIED, SimulatorClient
from lab.agent.model import ToolCall
from lab.protocol.render import reason_phrase, render_charge, render_status

# `tool_events.rule` values this module produces.
RULE_AGENT_DIRECTED = "agent_directed"
RULE_UNKNOWN_TOOL = "unknown_tool"
RULE_INVALID_ARGUMENTS = "invalid_arguments"
# The two rules the graph assigns itself (it never passes report_done to an executor).
RULE_REPORT_DONE = "report_done"
RULE_NOT_EXECUTED_AFTER_DONE = "not_executed_after_report_done"

TOOL_CHARGE = "charge"
TOOL_STATUS = "get_payment_status"

# The model-facing text of every call that sent no request, keyed by `rule`. Fixed on
# purpose: none of it may echo anything a model wrote. The first two are RetryLedger's own
# strings (its `handle()` answers "Recorded." to report_done and "Unknown tool." to a name it
# does not know); RetryLedger had no case for the other three, so each is one plain sentence.
_NOT_SENT_TEXT = {
    RULE_REPORT_DONE: "Recorded.",
    RULE_UNKNOWN_TOOL: "Unknown tool.",
    NAMESPACE_DENIED: (
        "Not executed: the namespace is fixed for this run and cannot be set by tool arguments."
    ),
    RULE_INVALID_ARGUMENTS: (
        "Not executed: the arguments for this tool call are missing or invalid."
    ),
    RULE_NOT_EXECUTED_AFTER_DONE: "Not executed: the task ended earlier in this turn.",
}

_RENDERERS = {TOOL_CHARGE: render_charge, TOOL_STATUS: render_status}


def render_observation(
    rule: str,
    *,
    tool: str | None = None,
    http_status: int | None = None,
    body: dict[str, Any] | None = None,
) -> str:
    """The text the model reads as the result of a tool call. The only place it is made.

    A request that was sent (`http_status` given, with `tool` "charge" or "get_payment_status")
    is rendered by `render_charge` or `render_status` from lab.protocol.render, so the text is
    the one RetryLedger showed. Those functions assume the bodies the simulator's own handlers
    produce. A body they have no case for (a 401, 422 or router 404 carrying `detail`, a
    `{"raw_body": ...}` from a response that was not JSON, any missing key) is rendered as one
    line in the same style, `HTTP <status> <reason>: <body as compact JSON>`. The renderer
    never raises because of a response body.

    A call that sent nothing is rendered from the fixed `_NOT_SENT_TEXT`, keyed by `rule`.
    Every tool message the graph appends comes from here.
    """
    if http_status is not None:
        return _render_response(tool, http_status, body)
    return _NOT_SENT_TEXT[rule]


def _render_response(tool: str | None, http_status: int, body: dict[str, Any] | None) -> str:
    render = _RENDERERS.get(tool) if tool is not None else None
    if render is None:
        # A caller bug, not something a response can cause.
        raise ValueError(
            f"a sent request is rendered for 'charge' or 'get_payment_status', not {tool!r}"
        )
    try:
        text = render(http_status, body)
    except (LookupError, TypeError):
        text = None
    if isinstance(text, str):
        return text
    # ensure_ascii stays on: the escaped form is what a database and a log can always hold.
    compact = json.dumps(body, separators=(",", ":"), default=str)
    return f"HTTP {http_status} {reason_phrase(http_status)}: {compact}"


class ToolOutcome(BaseModel):
    """One executed (or refused) tool call, as the loop records it in `tool_events`.

    `effective_args` is None when nothing was sent, and so is `effective_key`. `http_status`
    is None when no HTTP request was made, which includes a client denial: the client's
    `DENIED_STATUS` is not an HTTP status and never appears here.
    """

    model_config = ConfigDict(frozen=True)

    requested_args: dict[str, Any]
    requested_key: str | None
    effective_args: dict[str, Any] | None
    effective_key: str | None
    rule: str
    observation: str
    http_status: int | None


class Executor(Protocol):
    def execute(self, call: ToolCall) -> ToolOutcome: ...


def requested_key_of(arguments: dict[str, Any]) -> str | None:
    """The `requested_key` of a tool call: its idempotency_key when that is a string.

    The single definition of what `tool_events.requested_key` holds, for every row."""
    key = arguments.get("idempotency_key")
    return key if isinstance(key, str) else None


def not_sent_outcome(call: ToolCall, rule: str) -> ToolOutcome:
    """The outcome of a call for which no request was made. `rule` picks the fixed text.

    Used by the executor for its refusals and by the graph for `report_done` and for calls
    skipped after it, so all of those rows are built the same way."""
    return ToolOutcome(
        requested_args=dict(call.arguments),
        requested_key=requested_key_of(call.arguments),
        effective_args=None,
        effective_key=None,
        rule=rule,
        observation=render_observation(rule),
        http_status=None,
    )


class DirectExecutor:
    """Mode A: send the model's arguments as given, through a client bound to its run."""

    def __init__(self, client: SimulatorClient) -> None:
        self._client = client

    def execute(self, call: ToolCall) -> ToolOutcome:
        if call.name == "charge":
            return self._send(call, self._client.charge(dict(call.arguments)))
        if call.name == "get_payment_status":
            key = call.arguments.get("idempotency_key")
            if not isinstance(key, str):
                return not_sent_outcome(call, RULE_INVALID_ARGUMENTS)
            return self._send(call, self._client.status(key))
        return not_sent_outcome(call, RULE_UNKNOWN_TOOL)

    @staticmethod
    def _send(call: ToolCall, result: tuple[int, dict[str, Any]]) -> ToolOutcome:
        status, body = result
        if status == DENIED_STATUS:
            return not_sent_outcome(call, NAMESPACE_DENIED)
        return ToolOutcome(
            requested_args=dict(call.arguments),
            requested_key=requested_key_of(call.arguments),
            effective_args=dict(call.arguments),
            effective_key=requested_key_of(call.arguments),
            rule=RULE_AGENT_DIRECTED,
            observation=render_observation(
                RULE_AGENT_DIRECTED, tool=call.name, http_status=status, body=body
            ),
            http_status=status,
        )
