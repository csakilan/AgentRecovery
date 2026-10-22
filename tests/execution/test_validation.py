from decimal import Decimal

import pytest

from lab.protocol.task import Profile
from lab.simulator import service
from tests.helpers import CHARGE

MISSING = object()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("idempotency_key", MISSING, "idempotency_key must be a string of 1 to 255 characters"),
        ("idempotency_key", 7, "idempotency_key must be a string of 1 to 255 characters"),
        ("idempotency_key", "", "idempotency_key must be a string of 1 to 255 characters"),
        ("idempotency_key", "k" * 256, "idempotency_key must be a string of 1 to 255 characters"),
        (
            "idempotency_key",
            "k\x00",
            "idempotency_key must not contain a NUL character or an unpaired surrogate",
        ),
        (
            "idempotency_key",
            "\ud800",
            "idempotency_key must not contain a NUL character or an unpaired surrogate",
        ),
        ("amount", MISSING, "amount must be a number"),
        ("amount", True, "amount must be a number"),
        ("amount", "NaN", "amount must be finite"),
        ("amount", float("inf"), "amount must be finite"),
        ("amount", 0, "amount must be positive with at most two decimal places"),
        ("amount", "0.001", "amount must be positive with at most two decimal places"),
        ("order_id", MISSING, "order_id must be a string of 1 to 64 characters"),
        ("order_id", "x\x00", "order_id must not contain a NUL character or an unpaired surrogate"),
        ("currency", "usd", "currency must be a three-letter uppercase code"),
        ("currency", "USD\n", "currency must be a three-letter uppercase code"),
        ("extra", 1, "unexpected fields: extra"),
    ],
)
def test_source_validation_matches_simulator(field, value, message):
    payload = dict(CHARGE)
    if value is MISSING:
        del payload[field]
    else:
        payload[field] = value
    expected = {"error": {"code": "invalid_request", "message": message}}
    original = service._validate_charge(Profile.CONTROLLED, payload)
    assert original.http_status == 400
    assert original.body == expected

    from lab.protocol.validation import ChargeRefusal, validate_charge

    refusal = validate_charge(Profile.CONTROLLED, payload)
    assert isinstance(refusal, ChargeRefusal)
    assert refusal.http_status == 400
    assert refusal.body == expected


@pytest.mark.parametrize("amount", [49.99, "49.990", " 49.99 ", "4.999e1"])
def test_valid_decimal_forms_have_the_same_payment_arguments(amount):
    from lab.protocol.validation import ValidCharge, validate_charge

    result = validate_charge(Profile.CONTROLLED, {**CHARGE, "amount": amount})
    assert result == ValidCharge("k1", "1234", Decimal("49.99"), "USD")


def test_compat_validation_retains_its_distinct_rules():
    from lab.protocol.validation import ChargeRefusal, ValidCharge, validate_charge

    assert validate_charge(Profile.COMPAT, {"idempotency_key": "k", "amount": "-0.001"}) == (
        ValidCharge("k", None, Decimal("-0.001"), None)
    )
    extra = validate_charge(Profile.COMPAT, CHARGE)
    assert isinstance(extra, ChargeRefusal)
    assert extra.body["error"]["message"] == "unexpected fields: currency, order_id"
    huge = validate_charge(Profile.COMPAT, {"idempotency_key": "k", "amount": "1e131072"})
    assert huge.body["error"]["message"] == (
        "amount must have at most 131072 digits before the decimal point and 16383 after it"
    )


@pytest.mark.parametrize("key", ["a/b", "tab\tkey", "你好", "k" * 255])
def test_prevalidation_does_not_narrow_the_existing_key_contract(key):
    from lab.protocol.validation import ValidCharge, validate_charge

    assert isinstance(
        validate_charge(Profile.CONTROLLED, {**CHARGE, "idempotency_key": key}), ValidCharge
    )
