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


@dataclass(frozen=True)
class CallSummary:
    charge_keys: tuple[str, ...]
    status_calls: int
