"""The one task this lab studies, and the canonical form of its arguments."""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

_MAX_AMOUNT = Decimal("1000000")
_CENT = Decimal("0.01")


class Profile(StrEnum):
    COMPAT = "compat"
    CONTROLLED = "controlled"


class ChargeTask(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str = "1234"
    amount: Decimal = Decimal("49.99")
    currency: str = "USD"


DEFAULT_TASK = ChargeTask()


def parse_amount(value: Any) -> Decimal:
    """Turn a JSON amount into the Decimal the caller wrote.

    Floats go through repr so 49.99 becomes Decimal("49.99"), not its binary expansion.
    """
    if isinstance(value, bool):
        raise ValueError("amount must be a number")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("amount must be finite")
        return Decimal(repr(value))
    if isinstance(value, int | str):
        try:
            amount = Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise ValueError("amount must be a number") from exc
        if not amount.is_finite():
            raise ValueError("amount must be finite")
        return amount
    raise ValueError("amount must be a number")


def is_minor_unit_amount(amount: Decimal) -> bool:
    """Positive, at most two decimal places, within a sane ceiling."""
    if not (Decimal(0) < amount <= _MAX_AMOUNT):
        return False
    return amount == amount.quantize(_CENT)


def args_hash(order_id: str | None, amount: Decimal, currency: str | None) -> str:
    canonical = json.dumps(
        {"order_id": order_id, "amount": format(amount.normalize(), "f"), "currency": currency},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
