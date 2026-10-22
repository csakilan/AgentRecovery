"""Versioned, serializable limits for the execution treatments."""

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_Count = Annotated[int, Field(strict=True, gt=0)]
_Version = Annotated[int, Field(strict=True, ge=1, le=1)]
_Seconds = Annotated[float, Field(strict=True, gt=0)]


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", allow_inf_nan=False, validate_default=True
    )

    execution_protocol_version: _Version = 1
    resume_protocol_version: _Version = 1
    b_max_attempts: _Count = 3
    b_backoff_s: tuple[_Seconds, ...] = (1.0, 2.0)
    c_max_attempts: _Count = 3
    operation_timeout_s: _Seconds = 120.0
    lease_s: _Seconds = 30.0
    renew_every_s: _Seconds = 10.0
    status_backoff_s: tuple[_Seconds, ...] = Field(default=(1.0, 2.0, 4.0, 8.0), min_length=1)
    tick_s: _Seconds = 2.0

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        if len(self.b_backoff_s) < self.b_max_attempts - 1:
            raise ValueError("B needs a backoff interval for each retry")
        if self.renew_every_s >= self.lease_s:
            raise ValueError("renew_every_s must be less than lease_s")
        return self
