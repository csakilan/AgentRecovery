"""Adapts the lab's internal tool and message shapes to OpenRouter's wire format.

OpenRouter serves an OpenAI-compatible chat completions API. The lab keeps its own shapes:
tool schemas as `{name, description, input_schema}`, and graph messages whose tool-call
arguments are dicts. The functions here translate in both directions, and
`OpenRouterClient` calls them around one HTTP round trip with retry and pacing.
"""

from __future__ import annotations

import copy
import json
import os
import time
from typing import Any

import httpx

from lab.agent.model import ModelTurn, ToolCall
from lab.runtime.budget import RequestBudget, assert_model_allowed

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_BACKOFF_S = 60.0
_ERROR_TEXT_CHARS = 300


class OpenRouterError(RuntimeError):
    """A request that could not be completed: retries exhausted, or a non-retryable status."""


class OpenRouterClient:
    """A `ModelClient` over OpenRouter's chat completions endpoint.

    Only `:free` models are accepted. Consecutive requests are spaced at least
    `min_interval_s` apart. Transport errors and 429/5xx statuses are retried with exponential
    backoff (honouring `Retry-After`); a malformed response body is returned as a malformed
    turn rather than raised. The API key comes from the argument or `OPENROUTER_API_KEY`
    and goes nowhere except the Authorization header. When a `RequestBudget` is given, each
    call to `complete` claims one request from it before the first attempt, and raises
    `BudgetExceeded` without sending anything if the daily ceiling would be passed.
    """

    def __init__(
        self,
        model_id: str,
        *,
        api_key: str | None = None,
        http_client: httpx.Client | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
        min_interval_s: float = 3.0,
        max_retries: int = 5,
        timeout_s: float = 60.0,
        budget: RequestBudget | None = None,
    ) -> None:
        assert_model_allowed(model_id)
        key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise OpenRouterError("no OpenRouter API key: pass api_key or set OPENROUTER_API_KEY")
        self.model_id = model_id
        self._key = key
        self._http = http_client if http_client is not None else httpx.Client(timeout=timeout_s)
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._min_interval_s = min_interval_s
        self._max_retries = max_retries
        self._budget = budget
        self._last_request_monotonic: float | None = None

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        body: dict[str, Any] = {
            "model": self.model_id,
            "messages": to_openai_messages(messages),
        }
        if tools:
            body["tools"] = to_openai_tools(tools)
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        failure = "no attempt made"
        if self._budget is not None:
            # One claim per logical request, before anything is sent. Retries do not re-claim,
            # and BudgetExceeded propagates so an over-budget request never leaves.
            self._budget.claim(1)
        for attempt in range(self._max_retries + 1):
            self._pace()
            wait = min(2.0**attempt, _MAX_BACKOFF_S)
            try:
                response = self._http.post(self._url, headers=headers, json=body)
            except httpx.TransportError as exc:
                failure = f"transport error {type(exc).__name__}"
            else:
                if response.is_success:
                    return _turn_from_response(response)
                text = response.text[:_ERROR_TEXT_CHARS]
                failure = f"HTTP {response.status_code}: {text}"
                if response.status_code not in _RETRYABLE_STATUSES:
                    raise OpenRouterError(failure)
                advised = _retry_after(response)
                if advised is not None:
                    wait = advised
            if attempt < self._max_retries:
                _sleep(wait)
        raise OpenRouterError(f"gave up after {self._max_retries + 1} attempts: {failure}")

    def _pace(self) -> None:
        now = time.monotonic()
        if self._last_request_monotonic is not None:
            remaining = self._min_interval_s - (now - self._last_request_monotonic)
            if remaining > 0:
                _sleep(remaining)
                now = time.monotonic()
        self._last_request_monotonic = now


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _retry_after(response: httpx.Response) -> float | None:
    """The `Retry-After` header as seconds, if it is a sane number (dates are ignored)."""
    try:
        seconds = float(response.headers.get("retry-after", ""))
    except ValueError:
        return None
    if seconds != seconds or seconds < 0:
        return None
    return min(seconds, _MAX_BACKOFF_S)


def _turn_from_response(response: httpx.Response) -> ModelTurn:
    try:
        body = response.json()
    except (ValueError, RecursionError):
        return ModelTurn(
            text=None,
            tool_calls=(),
            raw={"raw_body": response.text[:2000]},
            malformed=True,
        )
    return parse_completion(body)


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
    if not isinstance(body, dict):
        return _malformed({"unparseable_body": body}, None, None, None)
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
    except (ValueError, RecursionError):
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
