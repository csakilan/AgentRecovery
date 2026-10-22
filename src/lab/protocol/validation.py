"""The simulator's request rules, shared before execution policies rewrite keys."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from lab.protocol.task import (
    NUMERIC_MAX_INTEGER_DIGITS,
    NUMERIC_MAX_SCALE,
    Profile,
    is_minor_unit_amount,
    is_storable_amount,
    parse_amount,
)

# \Z, not $: in Python `$` also matches just before a trailing newline, so "USD\n" would pass.
_CURRENCY = re.compile(r"\A[A-Z]{3}\Z")
_COMPAT_FIELDS = frozenset({"amount", "idempotency_key"})
_CONTROLLED_FIELDS = frozenset({"amount", "idempotency_key", "order_id", "currency"})

UNSTORABLE_MESSAGE = "must not contain a NUL character or an unpaired surrogate"
AMOUNT_RANGE_MESSAGE = (
    f"amount must have at most {NUMERIC_MAX_INTEGER_DIGITS} digits before the decimal point"
    f" and {NUMERIC_MAX_SCALE} after it"
)


def text_is_storable(value: str) -> bool:
    """Whether Postgres text and jsonb can hold this string.

    JSON allows any code point in a string, but Postgres rejects NUL, and an unpaired
    surrogate (which json.loads accepts from "\\ud800") cannot be encoded as UTF-8. A
    value like that fails inside the first statement that carries it, which for a charge
    is the claim, so the attempt would leave no call_log row. Every other control character
    (tab, newline, DEL, the Unicode line separators) is stored without trouble and stays
    valid.
    """
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@dataclass(frozen=True)
class ValidCharge:
    key: str
    order_id: str | None
    amount: Decimal
    currency: str | None


@dataclass(frozen=True)
class ChargeRefusal:
    http_status: int
    body: dict[str, Any]


def _refusal(http_status: int, code: str, message: str) -> ChargeRefusal:
    return ChargeRefusal(http_status, {"error": {"code": code, "message": message}})


def validate_charge(profile: Profile, payload: dict[str, Any]) -> ValidCharge | ChargeRefusal:
    allowed = _COMPAT_FIELDS if profile is Profile.COMPAT else _CONTROLLED_FIELDS
    extra = sorted(set(payload) - allowed)
    if extra:
        return _refusal(400, "invalid_request", f"unexpected fields: {', '.join(extra)}")
    key = payload.get("idempotency_key")
    if not isinstance(key, str) or not 1 <= len(key) <= 255:
        return _refusal(
            400, "invalid_request", "idempotency_key must be a string of 1 to 255 characters"
        )
    if not text_is_storable(key):
        return _refusal(400, "invalid_request", f"idempotency_key {UNSTORABLE_MESSAGE}")
    try:
        amount = parse_amount(payload.get("amount"))
    except ValueError as exc:
        return _refusal(400, "invalid_request", str(exc))
    if profile is Profile.COMPAT:
        # Compat skips the controlled business rules on purpose, but an amount Postgres cannot
        # store would fail inside the insert, after the attempt was claimed and counted. This
        # runs in validation, before _claim_call, so such a call consumes no fault-schedule slot.
        if not is_storable_amount(amount):
            return _refusal(400, "invalid_request", AMOUNT_RANGE_MESSAGE)
        return ValidCharge(key, None, amount, None)
    if not is_minor_unit_amount(amount):
        return _refusal(
            400, "invalid_request", "amount must be positive with at most two decimal places"
        )
    order_id = payload.get("order_id")
    if not isinstance(order_id, str) or not 1 <= len(order_id) <= 64:
        return _refusal(400, "invalid_request", "order_id must be a string of 1 to 64 characters")
    if not text_is_storable(order_id):
        return _refusal(400, "invalid_request", f"order_id {UNSTORABLE_MESSAGE}")
    currency = payload.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.match(currency):
        return _refusal(400, "invalid_request", "currency must be a three-letter uppercase code")
    return ValidCharge(key, order_id, amount, currency)
