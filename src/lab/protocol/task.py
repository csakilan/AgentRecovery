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

# What an unconstrained Postgres NUMERIC column (charges.amount, migration 0001_init.sql) can
# hold: 131072 digits before the decimal point and 16383 after it. Anything outside that
# raises NumericValueOutOfRange inside the insert. Both limits were confirmed by inserting
# the boundary values into a NUMERIC column on PostgreSQL 16.14: 1e131071 and 9 repeated 131072
# times store while 1e131072 does not, and a scale of 16383 stores while 16384 does not.
# tests/simulator/test_service.py repeats that check against whatever server the suite runs on.
NUMERIC_MAX_INTEGER_DIGITS = 131072
NUMERIC_MAX_SCALE = 16383


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


def is_storable_amount(amount: Decimal) -> bool:
    """Whether Postgres can store this finite amount in an unconstrained NUMERIC column.

    This is a storage limit, not a business rule: it allows negative, zero and sub-cent
    amounts, which the compat profile accepts on purpose. The scale is the number of digits
    after the decimal point as the caller wrote them (a Decimal's negated exponent), and it
    applies to zero as well, since Postgres keeps the scale of 0.000. Zero has no magnitude,
    so only a non-zero amount is checked against the integer-digit limit.
    """
    if not amount.is_finite():
        return False
    if -amount.as_tuple().exponent > NUMERIC_MAX_SCALE:
        return False
    return amount.is_zero() or amount.adjusted() < NUMERIC_MAX_INTEGER_DIGITS


def args_hash(order_id: str | None, amount: Decimal, currency: str | None) -> str:
    canonical = json.dumps(
        {"order_id": order_id, "amount": format(amount.normalize(), "f"), "currency": currency},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
