"""Hidden fault plans. Never shown to the model."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChargeFault = Literal["commit_then_error", "error_no_commit"]


class World(StrEnum):
    HEALTHY = "healthy"
    LOST_ACK = "lost_ack"
    TRUE_FAIL = "true_fail"
    PERMANENT_FAIL = "permanent_fail"


class FaultPlan(BaseModel):
    """What the simulator does to each validated call in one namespace.

    Call indexes are 1-based and count validated charge calls (or status calls) in
    arrival order. Holds pause a charge call at a named barrier until released.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    charge: dict[int, ChargeFault] = Field(default_factory=dict)
    charge_default: Literal["ok", "error_no_commit"] = "ok"
    status_unavailable_calls: int = Field(default=0, ge=0)
    hold_before_commit: tuple[int, ...] = ()
    hold_after_commit: tuple[int, ...] = ()

    def charge_fault(self, call_index: int) -> ChargeFault | None:
        if call_index in self.charge:
            return self.charge[call_index]
        if self.charge_default == "error_no_commit":
            return "error_no_commit"
        return None


def world_fault_plan(world: World) -> FaultPlan:
    match world:
        case World.HEALTHY:
            return FaultPlan()
        case World.LOST_ACK:
            return FaultPlan(charge={1: "commit_then_error"})
        case World.TRUE_FAIL:
            return FaultPlan(charge={1: "error_no_commit"})
        case World.PERMANENT_FAIL:
            return FaultPlan(charge_default="error_no_commit")
    raise ValueError(f"unknown world: {world}")
