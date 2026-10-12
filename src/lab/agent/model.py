"""What the runtime needs from a model, and nothing more.

Two implementations exist: `ScriptedModel`, used by every test, and the
OpenRouter client added in Plan 3. Keeping the surface this small is what lets
the entire pipeline be tested without spending a request.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any]


class ModelTurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str | None
    tool_calls: tuple[ToolCall, ...]
    raw: dict[str, Any]
    input_tokens: int | None = None
    output_tokens: int | None = None
    malformed: bool = False


class ModelClient(Protocol):
    model_id: str

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn: ...
