"""How a model's tool call becomes a request, and what the model is told afterwards.

The loop asks an `Executor` to run one tool call and gets back a `ToolOutcome`. The outcome
keeps what the model asked for (`requested_*`) apart from what was actually sent
(`effective_*`), because that gap is the experiment: a runtime that rewrites the idempotency
key is an infrastructure fix, not evidence of better model reasoning. `DirectExecutor` is
mode A (`agent_directed`): it sends exactly what the model asked for, so effective equals
requested. Plan 3's other executors will differ in what they send, not in this record.

`report_done` never reaches an executor. It is not a request to the simulator, and the graph
handles it.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from lab.agent.client import DENIED_STATUS, NAMESPACE_DENIED, SimulatorClient
from lab.agent.model import ToolCall

# `tool_events.rule` values this module produces.
RULE_AGENT_DIRECTED = "agent_directed"
RULE_UNKNOWN_TOOL = "unknown_tool"
RULE_INVALID_ARGUMENTS = "invalid_arguments"
# The two rules the graph assigns itself (it never passes report_done to an executor).
RULE_REPORT_DONE = "report_done"
RULE_NOT_EXECUTED_AFTER_DONE = "not_executed_after_report_done"

# The model-facing text of every call that sent no request, keyed by `rule`. Fixed on
# purpose: none of it may echo anything a model wrote. The three refusals render under
# "error"; the two graph rules are not errors and render under "notice".
_ERRORS = {
    NAMESPACE_DENIED: (
        "Tool arguments must not contain a namespace. "
        "The namespace is fixed for this run and cannot be set by a tool call."
    ),
    RULE_UNKNOWN_TOOL: "No such tool.",
    RULE_INVALID_ARGUMENTS: "The arguments for this tool call are missing or invalid.",
}
_NOTICES = {
    RULE_REPORT_DONE: "Outcome recorded. The task has ended.",
    RULE_NOT_EXECUTED_AFTER_DONE: "Not executed: the task ended earlier in this turn.",
}


def render_observation(
    rule: str, *, http_status: int | None = None, body: dict[str, Any] | None = None
) -> str:
    """The text the model reads as the result of a tool call. The only place it is made.

    A request that was sent is rendered as `{"http_status": ..., "body": ...}`, compact, so
    the model sees exactly the status and body the simulator answered with. A call that
    never left the runtime (denied, unknown tool, invalid arguments) is rendered as a fixed
    `{"error": {"code": ..., "message": ...}}` keyed by `rule`. The two calls the graph
    handles itself (`report_done`, and a call after it in the same turn) are rendered as a
    fixed `{"notice": {"code": ..., "message": ...}}`. Every tool message the graph appends
    comes from here, so a change of wording or format here applies to all of them.

    This wording is a placeholder for the pilot. Plan 3 owns the final observation format,
    because how a failure is worded is itself an experimental variable there.
    """
    if http_status is not None:
        return json.dumps({"http_status": http_status, "body": body}, separators=(",", ":"))
    if rule in _NOTICES:
        return json.dumps(
            {"notice": {"code": rule, "message": _NOTICES[rule]}}, separators=(",", ":")
        )
    return json.dumps({"error": {"code": rule, "message": _ERRORS[rule]}}, separators=(",", ":"))


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
            observation=render_observation(RULE_AGENT_DIRECTED, http_status=status, body=body),
            http_status=status,
        )
