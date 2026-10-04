from decimal import Decimal

import pytest

from lab.protocol.task import (
    DEFAULT_TASK,
    Profile,
    args_hash,
    is_minor_unit_amount,
    parse_amount,
)


def test_default_task_matches_retryledger_constants():
    assert DEFAULT_TASK.order_id == "1234"
    assert DEFAULT_TASK.amount == Decimal("49.99")
    assert DEFAULT_TASK.currency == "USD"


def test_profiles_are_labelled():
    assert {p.value for p in Profile} == {"compat", "controlled"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (49.99, Decimal("49.99")),
        ("49.99", Decimal("49.99")),
        (50, Decimal("50")),
        (50.0, Decimal("50.0")),
        (4999, Decimal("4999")),
    ],
)
def test_parse_amount_keeps_the_decimal_the_caller_meant(raw, expected):
    assert parse_amount(raw) == expected
    assert str(parse_amount(raw)) == str(expected)


@pytest.mark.parametrize("raw", [True, None, "abc", float("nan"), float("inf"), "NaN", [1]])
def test_parse_amount_rejects_non_numbers(raw):
    with pytest.raises(ValueError):
        parse_amount(raw)


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("49.99", True),
        ("50", True),
        ("49.999", False),
        ("0", False),
        ("-1", False),
        ("1000000.01", False),
    ],
)
def test_minor_unit_amounts(raw, ok):
    assert is_minor_unit_amount(Decimal(raw)) is ok


def test_args_hash_is_stable_across_equivalent_amounts():
    a = args_hash("1234", Decimal("49.99"), "USD")
    b = args_hash("1234", Decimal("49.990"), "USD")
    assert a == b
    assert len(a) == 64


def test_args_hash_changes_with_any_argument():
    base = args_hash("1234", Decimal("49.99"), "USD")
    assert args_hash("1235", Decimal("49.99"), "USD") != base
    assert args_hash("1234", Decimal("4999"), "USD") != base
    assert args_hash("1234", Decimal("49.99"), "EUR") != base
    assert args_hash(None, Decimal("49.99"), None) != base
