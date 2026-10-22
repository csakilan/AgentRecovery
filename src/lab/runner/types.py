"""Saved worker inputs, deliberately excluding supervisor-only fault and scoring data."""

import math
from datetime import datetime
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lab.agent.client import TrustedContext
from lab.agent.openrouter import DEFAULT_BASE_URL
from lab.agent.scripted import SCRIPTED_POLICIES
from lab.execution.config import ExecutionConfig
from lab.protocol.manifest import Mode, PromptVariant
from lab.protocol.task import ChargeTask, Profile


class ModelDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["scripted", "openrouter", "injected"]
    model_id: str
    policy: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_descriptor(self) -> Self:
        if not self.model_id:
            raise ValueError("model ID is required")
        if self.kind == "scripted":
            if self.policy not in SCRIPTED_POLICIES or self.parameters:
                raise ValueError("invalid scripted descriptor")
        elif self.policy is not None:
            raise ValueError("only scripted models have policies")
        if self.kind == "injected" and self.parameters:
            raise ValueError("injected models have no reconstructible parameters")
        if self.kind == "openrouter":
            defaults = dict(
                base_url=DEFAULT_BASE_URL, min_interval_s=3.0, max_retries=5, timeout_s=60.0
            )
            if self.parameters.keys() - defaults.keys():
                raise ValueError("unknown or secret provider parameter")
            params = defaults | self.parameters
            url = urlsplit(params["base_url"])
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("base URL must not contain credentials or query data")
            for k in ("min_interval_s", "timeout_s"):
                if (
                    isinstance(params[k], bool)
                    or not isinstance(params[k], (int, float))
                    or not math.isfinite(params[k])
                    or params[k] < 0
                    or (k == "timeout_s" and params[k] == 0)
                ):
                    raise ValueError("invalid provider duration")
            if type(params["max_retries"]) is not int or params["max_retries"] < 0:
                raise ValueError("invalid provider retries")
            object.__setattr__(self, "parameters", params)
        return self


class WorkerInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ctx: TrustedContext
    profile: Profile
    task: ChargeTask
    prompt_variant: PromptVariant
    status_available: bool
    model: ModelDescriptor
    max_turns: int
    http_timeout_s: float
    execution: ExecutionConfig


class PreparedRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: str
    experiment_id: str
    episode_id: str
    namespace: str
    thread_id: str
    mode: Mode
    worker_input: WorkerInput
    initial_messages: list[dict[str, Any]]
    started_at: datetime
    deadline_at: datetime
    setup_state: Literal["pending", "ready"]
