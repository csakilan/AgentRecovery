from decimal import Decimal

import pytest

from lab.protocol.task import (
    DEFAULT_TASK,
    Profile,
    args_hash,
    is_minor_unit_amount,
    is_storable_amount,
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


def test_args_hash_has_a_pinned_value():
    """The hash is a payment's identity across processes and across time.

    Stored hashes are compared with freshly computed ones to tell a replay from a
    conflict, so changing the canonical form (key order, separators, amount formatting,
    the digest) would silently reclassify every existing record. These literals are the
    SHA-256 of the exact text shown, computed independently of args_hash. If one of them
    has to change, that is a data migration, not a test update.
    """
    # {"amount":"49.99","currency":"USD","order_id":"1234"}
    expected = "167d1ccd9ced3fb69741c67a087bee9bd38caf9f193efd3cd4320fca3e6f5834"
    assert args_hash("1234", Decimal("49.99"), "USD") == expected
    assert args_hash("1234", Decimal("49.990"), "USD") == expected
    # {"amount":"49.99","currency":null,"order_id":null}, the compat profile's form.
    assert (
        args_hash(None, Decimal("49.99"), None)
        == "491dbf547b23edc0fc6735bd675c1ab9437b13714ab2813883c57c44f119fd27"
    )


def test_args_hash_changes_with_any_argument():
    base = args_hash("1234", Decimal("49.99"), "USD")
    assert args_hash("1235", Decimal("49.99"), "USD") != base
    assert args_hash("1234", Decimal("4999"), "USD") != base
    assert args_hash("1234", Decimal("49.99"), "EUR") != base
    assert args_hash(None, Decimal("49.99"), None) != base


# The limits of an unconstrained Postgres NUMERIC: 131072 digits before the decimal point and
# 16383 after it. tests/simulator/test_service.py checks the same boundaries against a live
# server, so this table and the database cannot drift apart unnoticed.
@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("49.99", True),
        ("-49.99", True),
        ("0", True),
        ("1e131071", True),
        ("-1e131071", True),
        ("9" * 131072, True),
        ("1e131072", False),
        ("-1e131072", False),
        ("9" * 131073, False),
        ("1e999999", False),
        ("1e999999999999999999", False),
        ("1e-16383", True),
        ("1e-16384", False),
        ("1e-999999", False),
        ("1." + "0" * 16383, True),
        ("1." + "0" * 16384, False),
        # Zero is stored at any magnitude but still has a scale.
        ("0e999999", True),
        ("0e-16383", True),
        ("0e-16384", False),
        ("9" * 131072 + "." + "9" * 16383, True),
    ],
    ids=lambda v: v if isinstance(v, bool) else (v if len(v) < 24 else f"{v[:6]}..len{len(v)}"),
)
def test_is_storable_amount_marks_the_edge_of_what_numeric_can_hold(raw, ok):
    assert is_storable_amount(parse_amount(raw)) is ok
