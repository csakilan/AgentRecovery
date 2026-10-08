"""Ledger records as the evaluator sees them."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class LedgerCharge:
    idempotency_key: str
    order_id: str | None
    amount: Decimal
    currency: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError(f"amount must be a Decimal, got {type(self.amount).__name__}")


@dataclass(frozen=True)
class CallSummary:
    charge_keys: tuple[str, ...]
    status_calls: int
