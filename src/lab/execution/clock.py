"""Explicit clocks and deadline checks; waiting never resets a saved deadline."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class DeadlineExceeded(RuntimeError):
    """No more work may begin within this run/operation's original deadline."""


@dataclass(frozen=True)
class DeadlineBudget:
    deadline_at: datetime
    clock: Clock

    def __post_init__(self) -> None:
        if self.deadline_at.utcoffset() is None:
            raise ValueError("deadline_at must be timezone-aware")

    def remaining_s(self) -> float:
        return max(0.0, (self.deadline_at - self.clock.now()).total_seconds())

    def check(self) -> None:
        if self.remaining_s() <= 0:
            raise DeadlineExceeded("the execution deadline has passed")


class BoundaryGuard(Protocol):
    def check(self) -> None: ...


class NoopGuard:
    """Compatibility for callers without run ownership; versioned workers supply a guard."""

    def check(self) -> None:
        pass
