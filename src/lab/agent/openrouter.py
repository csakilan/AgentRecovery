"""Adapts the lab's internal tool and message shapes to OpenRouter's wire format.

OpenRouter serves an OpenAI-compatible chat completions API. The lab keeps its own shapes:
tool schemas as `{name, description, input_schema}`, and graph messages whose tool-call
arguments are dicts. The functions here translate in both directions. Plan 3 owns this
module, and the HTTP client that calls these functions is added to it in Plan 3 as well.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from lab.agent.model import ModelTurn, ToolCall


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert `{name, description, input_schema}` entries to OpenAI function tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": copy.deepcopy(tool["input_schema"]),
            },
        }
        for tool in tools
    ]


def to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert graph messages to the OpenAI chat wire shape.

    Tool-call arguments become compact JSON strings. Tool results are keyed by
    `tool_call_id` alone, because the OpenAI tool role has no `name` field.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "assistant":
            out.append(_assistant_message(message))
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": message["tool_call_id"],
                    "content": message["content"],
                }
            )
        else:
            out.append(dict(message))
    return out


def parse_completion(body: dict[str, Any]) -> ModelTurn:
    """Read one chat completion into a `ModelTurn`. A bad response never raises.

    A response whose choice, content or tool-call arguments cannot be read comes back with
    `malformed=True` and no tool calls, so the caller can record the failure and carry on.
    """
    input_tokens, output_tokens = _usage(body)
    message = _first_message(body)
    if message is None:
        return _malformed(body, None, input_tokens, output_tokens)
    text = message.get("content")
    if text is not None and not isinstance(text, str):
        return _malformed(body, None, input_tokens, output_tokens)
    calls = _tool_calls(message.get("tool_calls"))
    if calls is None:
        return _malformed(body, text, input_tokens, output_tokens)
    return ModelTurn(
        text=text,
        tool_calls=calls,
        raw=body,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        malformed=False,
    )


def _assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    wire: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
    calls = message.get("tool_calls")
    if calls:
        wire["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": json.dumps(call["arguments"], separators=(",", ":")),
                },
            }
            for call in calls
        ]
    return wire


def _malformed(
    body: dict[str, Any],
    text: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
) -> ModelTurn:
    return ModelTurn(
        text=text,
        tool_calls=(),
        raw=body,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        malformed=True,
    )


def _first_message(body: dict[str, Any]) -> dict[str, Any] | None:
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    return message if isinstance(message, dict) else None


def _tool_calls(raw_calls: Any) -> tuple[ToolCall, ...] | None:
    """The parsed tool calls, or None if any entry or the list itself is misshaped."""
    if raw_calls is None:
        return ()
    if not isinstance(raw_calls, list):
        return None
    calls: list[ToolCall] = []
    for raw in raw_calls:
        call = _tool_call(raw)
        if call is None:
            return None
        calls.append(call)
    return tuple(calls)


def _tool_call(raw: Any) -> ToolCall | None:
    function = raw.get("function") if isinstance(raw, dict) else None
    if not isinstance(function, dict):
        return None
    call_id = raw.get("id")
    name = function.get("name")
    arguments_json = function.get("arguments")
    if not (isinstance(call_id, str) and isinstance(name, str) and isinstance(arguments_json, str)):
        return None
    try:
        arguments = json.loads(arguments_json)
    except ValueError:
        return None
    if not isinstance(arguments, dict):
        return None
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _usage(body: dict[str, Any]) -> tuple[int | None, int | None]:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None, None
    return _token_count(usage.get("prompt_tokens")), _token_count(usage.get("completion_tokens"))


def _token_count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None
