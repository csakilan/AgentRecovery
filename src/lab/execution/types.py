"""Immutable journal snapshots and ownership tokens."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from lab.agent.client import TrustedContext
from lab.agent.executor import ToolOutcome
from lab.execution.config import ExecutionConfig


class OperationState(StrEnum):
    READY = "READY"
    IN_FLIGHT = "IN_FLIGHT"
    UNKNOWN = "UNKNOWN"
    CONFIRMED_NO_EFFECT = "CONFIRMED_NO_EFFECT"
    CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"
    UNRESOLVED = "UNRESOLVED"


TERMINAL_STATES = frozenset(
    {OperationState.CONFIRMED_SUCCESS, OperationState.TERMINAL_FAILURE, OperationState.UNRESOLVED}
)


class IntentConflict(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class OperationClaim(BaseModel):
    model_config = ConfigDict(frozen=True)
    operation_id: str
    owner: str
    version: int


class Operation(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    ctx: TrustedContext
    requested_args: dict[str, Any]
    args_hash: str
    effective_key: str
    config: ExecutionConfig
    state: OperationState
    attempt_count: int
    version: int
    claim_owner: str | None
    lease_expires_at: datetime | None
    next_check_at: datetime | None
    status_check_count: int
    deadline_at: datetime
    created_at: datetime
    finished_at: datetime | None
    last_evidence: dict[str, Any] | None
    terminal_outcome: ToolOutcome | None


class Attempt(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    number: int
    fence_version: int
    effective_args: dict[str, Any]


class OperationUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    state: OperationState
    evidence: dict[str, Any]
    next_check_at: datetime | None = None
    status_check_count: int | None = None
    attempt_id: str | None = None
    http_status: int | None = None
    response_body: dict[str, Any] | None = None
    error_type: str | None = None
    terminal_outcome: ToolOutcome | None = None
