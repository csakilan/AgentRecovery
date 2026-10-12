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

# The model-facing text of the three refusals. Fixed on purpose: a refusal must not echo
# anything a model wrote.
_REFUSALS = {
    NAMESPACE_DENIED: (
        "Tool arguments must not contain a namespace. "
        "The namespace is fixed for this run and cannot be set by a tool call."
    ),
    RULE_UNKNOWN_TOOL: "No such tool.",
    RULE_INVALID_ARGUMENTS: "The arguments for this tool call are missing or invalid.",
}


def render_observation(
    rule: str, *, http_status: int | None = None, body: dict[str, Any] | None = None
) -> str:
    """The text the model reads as the result of a tool call. The only place it is made.

    A request that was sent is rendered as `{"http_status": ..., "body": ...}`, compact, so
    the model sees exactly the status and body the simulator answered with. A call that
    never left the runtime (denied, unknown tool, invalid arguments) is rendered as a fixed
    `{"error": {"code": ..., "message": ...}}` keyed by `rule`.

    This wording is a placeholder for the pilot. Plan 3 owns the final observation format,
    because how a failure is worded is itself an experimental variable there.
    """
    if http_status is not None:
        return json.dumps({"http_status": http_status, "body": body}, separators=(",", ":"))
    message = _REFUSALS[rule]
    return json.dumps({"error": {"code": rule, "message": message}}, separators=(",", ":"))


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


def _key_of(arguments: dict[str, Any]) -> str | None:
    key = arguments.get("idempotency_key")
    return key if isinstance(key, str) else None


def _not_sent(call: ToolCall, rule: str) -> ToolOutcome:
    return ToolOutcome(
        requested_args=dict(call.arguments),
        requested_key=_key_of(call.arguments),
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
                return _not_sent(call, RULE_INVALID_ARGUMENTS)
            return self._send(call, self._client.status(key))
        return _not_sent(call, RULE_UNKNOWN_TOOL)

    @staticmethod
    def _send(call: ToolCall, result: tuple[int, dict[str, Any]]) -> ToolOutcome:
        status, body = result
        if status == DENIED_STATUS:
            return _not_sent(call, NAMESPACE_DENIED)
        return ToolOutcome(
            requested_args=dict(call.arguments),
            requested_key=_key_of(call.arguments),
            effective_args=dict(call.arguments),
            effective_key=_key_of(call.arguments),
            rule=RULE_AGENT_DIRECTED,
            observation=render_observation(RULE_AGENT_DIRECTED, http_status=status, body=body),
            http_status=status,
        )
