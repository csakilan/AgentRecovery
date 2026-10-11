import json
from decimal import Decimal

import psycopg
import pytest

from lab.protocol.faults import FaultPlan, World, world_fault_plan
from lab.protocol.render import render_charge
from lab.protocol.task import Profile, is_storable_amount
from lab.simulator import service
from tests.helpers import CHARGE, COMPAT_CHARGE, charges


def make_ns(conn, world=World.HEALTHY, profile=Profile.CONTROLLED, ns="ns", plan=None):
    service.configure_namespace(conn, ns, profile, plan or world_fault_plan(world))
    return ns


def test_healthy_charge_commits_once(conn):
    ns = make_ns(conn)
    r = service.charge(conn, ns, dict(CHARGE))
    assert r.http_status == 200
    assert r.body["message"] == "Charge succeeded."
    assert r.body["charge"]["order_id"] == "1234"
    assert r.body["replayed"] is False
    assert r.body["charge"]["amount"] == "49.99"
    assert len(charges(conn, ns)) == 1


def test_same_key_replays_without_a_new_effect(conn):
    ns = make_ns(conn)
    service.charge(conn, ns, dict(CHARGE))
    r = service.charge(conn, ns, dict(CHARGE))
    assert r.http_status == 200
    assert r.body["replayed"] is True
    assert r.body["message"] == (
        "Idempotent hit: a payment already succeeded under this key. No additional charge was made."
    )
    assert len(charges(conn, ns)) == 1


def test_new_key_creates_a_second_effect(conn):
    ns = make_ns(conn)
    service.charge(conn, ns, dict(CHARGE))
    service.charge(conn, ns, {**CHARGE, "idempotency_key": "k2"})
    assert len(charges(conn, ns)) == 2


def test_lost_ack_commits_but_reports_an_error(conn):
    ns = make_ns(conn, World.LOST_ACK)
    r = service.charge(conn, ns, dict(CHARGE))
    assert r.http_status == 504
    assert len(charges(conn, ns)) == 1
    s = service.status(conn, ns, "k1")
    assert s.body["status"] == "SUCCEEDED"


def test_lost_ack_new_key_retry_duplicates(conn):
    ns = make_ns(conn, World.LOST_ACK)
    service.charge(conn, ns, dict(CHARGE))
    r = service.charge(conn, ns, {**CHARGE, "idempotency_key": "k2"})
    assert r.http_status == 200
    assert len(charges(conn, ns)) == 2


def test_true_fail_errors_without_committing_and_same_key_retry_commits(conn):
    ns = make_ns(conn, World.TRUE_FAIL)
    r = service.charge(conn, ns, dict(CHARGE))
    assert r.http_status == 504
    assert charges(conn, ns) == []
    assert service.status(conn, ns, "k1").body["status"] == "NOT_FOUND"
    again = service.charge(conn, ns, dict(CHARGE))
    assert again.http_status == 200
    assert again.body["replayed"] is False
    assert len(charges(conn, ns)) == 1


def test_permanent_failure_never_commits(conn):
    ns = make_ns(conn, World.PERMANENT_FAIL)
    for _ in range(3):
        assert service.charge(conn, ns, dict(CHARGE)).http_status == 504
    assert charges(conn, ns) == []


def test_controlled_rejects_changed_parameters_under_the_same_key(conn):
    ns = make_ns(conn)
    service.charge(conn, ns, dict(CHARGE))
    r = service.charge(conn, ns, {**CHARGE, "amount": 4999})
    assert r.http_status == 409
    assert r.body["error"]["code"] == "idempotency_conflict"
    assert r.body["error"]["message"] == (
        "this idempotency key was already used with different parameters. No charge was made."
    )
    rows = charges(conn, ns)
    assert len(rows) == 1
    assert str(rows[0]["amount"]) == "49.99"


def test_compat_replays_even_when_parameters_change(conn):
    ns = make_ns(conn, profile=Profile.COMPAT)
    service.charge(conn, ns, dict(COMPAT_CHARGE))
    r = service.charge(conn, ns, {**COMPAT_CHARGE, "amount": 10})
    assert r.http_status == 200
    assert r.body["replayed"] is True


def test_compat_first_errors_match_the_original_text(conn):
    lost = make_ns(conn, World.LOST_ACK, Profile.COMPAT, ns="lost")
    fail = make_ns(conn, World.TRUE_FAIL, Profile.COMPAT, ns="fail")
    a = service.charge(conn, lost, dict(COMPAT_CHARGE))
    b = service.charge(conn, fail, dict(COMPAT_CHARGE))
    assert render_charge(a.http_status, a.body) == (
        "HTTP 504 Gateway Timeout: the upstream processor did not confirm. "
        "Payment status is UNKNOWN."
    )
    assert render_charge(b.http_status, b.body) == (
        "HTTP 500 Internal Server Error: the charge was not processed. Payment status is UNKNOWN."
    )


@pytest.mark.parametrize(
    "bad",
    [
        {**CHARGE, "amount": 49.999},
        {**CHARGE, "amount": -1},
        {**CHARGE, "amount": "lots"},
        {k: v for k, v in CHARGE.items() if k != "currency"},
        {**CHARGE, "currency": "usd"},
        {**CHARGE, "order_id": ""},
        {**CHARGE, "idempotency_key": ""},
        {**CHARGE, "namespace": "someone-else"},
    ],
)
def test_controlled_validation_rejects_without_side_effects(conn, bad):
    ns = make_ns(conn)
    r = service.charge(conn, ns, bad)
    assert r.http_status == 400
    assert charges(conn, ns) == []
    counters = conn.execute(
        "SELECT charge_calls FROM sim_namespaces WHERE namespace = %s", (ns,)
    ).fetchone()
    assert counters["charge_calls"] == 0


# `$` in a Python regex also matches just before a trailing newline, so a pattern ending in `$`
# let "USD\n" through. The stored value then differed from the task's "USD" and the scorer
# compared the wrong string.
MALFORMED_CURRENCIES = [
    pytest.param("USD\n", id="trailing-newline"),
    pytest.param("\nUSD", id="leading-newline"),
    pytest.param("US\nD", id="embedded-newline"),
    pytest.param("USD\r\n", id="trailing-crlf"),
    pytest.param("USD\n\n", id="two-trailing-newlines"),
    pytest.param(" USD", id="leading-space"),
    pytest.param("USD ", id="trailing-space"),
    pytest.param("usd", id="lowercase"),
    pytest.param("Usd", id="mixed-case"),
    pytest.param("\uff35\uff33\uff24", id="fullwidth-letters"),
    pytest.param("USDX", id="four-letters"),
    pytest.param("", id="empty"),
]


@pytest.mark.parametrize("bad", MALFORMED_CURRENCIES)
def test_malformed_currency_is_a_logged_400_that_claims_no_slot(conn, bad):
    ns = make_ns(conn, World.LOST_ACK)
    r = service.charge(conn, ns, {**CHARGE, "currency": bad})
    assert r.http_status == 400
    assert r.body == {
        "error": {
            "code": "invalid_request",
            "message": "currency must be a three-letter uppercase code",
        }
    }
    assert charges(conn, ns) == []
    row = _only_log_row(conn, ns)
    assert (row["call_index"], row["outcome"]) == (None, "invalid")
    # It consumed nothing: the next call is still call 1, which the lost_ack plan faults.
    assert service.charge(conn, ns, dict(CHARGE)).http_status == 504


def test_a_valid_currency_is_stored_exactly_as_sent(conn):
    ns = make_ns(conn)
    r = service.charge(conn, ns, {**CHARGE, "currency": "EUR"})
    assert (r.http_status, r.body["charge"]["currency"]) == (200, "EUR")
    assert [row["currency"] for row in charges(conn, ns)] == ["EUR"]


def test_compat_rejects_controlled_only_fields(conn):
    ns = make_ns(conn, profile=Profile.COMPAT)
    assert service.charge(conn, ns, dict(CHARGE)).http_status == 400


def test_invalid_request_does_not_consume_the_fault(conn):
    ns = make_ns(conn, World.LOST_ACK)
    service.charge(conn, ns, {**CHARGE, "amount": 49.999})
    r = service.charge(conn, ns, dict(CHARGE))
    assert r.http_status == 504
    assert len(charges(conn, ns)) == 1


def test_status_outage_then_recovery(conn):
    ns = make_ns(conn, plan=FaultPlan(status_unavailable_calls=2))
    service.charge(conn, ns, dict(CHARGE))
    codes = [service.status(conn, ns, "k1").http_status for _ in range(3)]
    assert codes == [503, 503, 200]


def test_status_unavailable_response_carries_the_original_text(conn):
    ns = make_ns(conn, plan=FaultPlan(status_unavailable_calls=1))
    r = service.status(conn, ns, "k1")
    assert r.http_status == 503
    assert r.body == {
        "error": {
            "code": "unavailable",
            "message": "payment status service is temporarily unavailable.",
        }
    }


def test_status_not_found_is_http_200_with_a_null_charge(conn):
    ns = make_ns(conn)
    r = service.status(conn, ns, "never-charged")
    assert r.http_status == 200
    assert r.body == {"idempotency_key": "never-charged", "status": "NOT_FOUND", "charge": None}


def test_unknown_namespace(conn):
    with pytest.raises(service.UnknownNamespace):
        service.charge(conn, "nope", dict(CHARGE))
    with pytest.raises(service.UnknownNamespace):
        service.status(conn, "nope", "k1")


def test_configure_resets_the_namespace(conn):
    ns = make_ns(conn)
    service.charge(conn, ns, dict(CHARGE))
    make_ns(conn)
    assert charges(conn, ns) == []
    row = conn.execute(
        "SELECT charge_calls FROM sim_namespaces WHERE namespace = %s", (ns,)
    ).fetchone()
    assert row["charge_calls"] == 0


def test_every_call_is_logged_with_its_outcome(conn):
    ns = make_ns(conn, World.LOST_ACK)
    service.charge(conn, ns, dict(CHARGE))
    service.status(conn, ns, "k1")
    service.charge(conn, ns, dict(CHARGE))
    rows = conn.execute(
        "SELECT endpoint, call_index, outcome FROM call_log WHERE namespace = %s ORDER BY id",
        (ns,),
    ).fetchall()
    assert [(r["endpoint"], r["call_index"], r["outcome"]) for r in rows] == [
        ("charge", 1, "committed_then_error"),
        ("status", 1, "succeeded"),
        ("charge", 2, "replayed"),
    ]


def test_requires_autocommit(svc_url):
    with psycopg.connect(svc_url) as c:
        with pytest.raises(ValueError, match="autocommit"):
            service.charge(c, "ns", dict(CHARGE))


def test_status_requires_autocommit(svc_url):
    with psycopg.connect(svc_url) as c:
        with pytest.raises(ValueError, match="autocommit"):
            service.status(c, "ns", "k1")


CONTROLLED_AMBIGUOUS_TEXT = (
    "HTTP 504 Gateway Timeout: the upstream processor did not confirm. Payment status is UNKNOWN."
)


def _first_charge(conn, world, profile, ns):
    payload = CHARGE if profile is Profile.CONTROLLED else COMPAT_CHARGE
    make_ns(conn, world, profile, ns=ns)
    return service.charge(conn, ns, dict(payload))


def test_controlled_hidden_worlds_are_indistinguishable_from_the_response(conn):
    lost = _first_charge(conn, World.LOST_ACK, Profile.CONTROLLED, "lost")
    fail = _first_charge(conn, World.TRUE_FAIL, Profile.CONTROLLED, "fail")
    perm = _first_charge(conn, World.PERMANENT_FAIL, Profile.CONTROLLED, "perm")

    for r in (lost, fail, perm):
        assert r.http_status == 504
        assert r.body["error"]["code"] == "gateway_timeout"

    bodies = {json.dumps(r.body, sort_keys=True) for r in (lost, fail, perm)}
    assert len(bodies) == 1
    seen = {render_charge(r.http_status, r.body) for r in (lost, fail, perm)}
    assert seen == {CONTROLLED_AMBIGUOUS_TEXT}

    # Identical from outside, different underneath: that is the experiment.
    assert len(charges(conn, "lost")) == 1
    assert charges(conn, "fail") == []
    assert charges(conn, "perm") == []


def test_compat_hidden_worlds_are_distinguishable_from_the_response(conn):
    lost = _first_charge(conn, World.LOST_ACK, Profile.COMPAT, "lost")
    fail = _first_charge(conn, World.TRUE_FAIL, Profile.COMPAT, "fail")

    assert lost.http_status == 504
    assert fail.http_status == 500
    assert json.dumps(lost.body, sort_keys=True) != json.dumps(fail.body, sort_keys=True)
    assert lost.body["error"]["code"] == "gateway_timeout"
    assert fail.body["error"]["code"] == "internal_error"
    assert render_charge(lost.http_status, lost.body) != render_charge(fail.http_status, fail.body)
    assert len(charges(conn, "lost")) == 1
    assert charges(conn, "fail") == []


def _outcomes(conn, ns):
    rows = conn.execute(
        "SELECT endpoint, outcome FROM call_log WHERE namespace = %s ORDER BY id", (ns,)
    ).fetchall()
    return [(r["endpoint"], r["outcome"]) for r in rows]


def test_call_log_outcome_committed_and_conflict(conn):
    ns = make_ns(conn)
    service.charge(conn, ns, dict(CHARGE))
    service.charge(conn, ns, {**CHARGE, "amount": 4999})
    assert _outcomes(conn, ns) == [("charge", "committed"), ("charge", "conflict")]


def test_call_log_outcome_invalid(conn):
    ns = make_ns(conn)
    service.charge(conn, ns, {**CHARGE, "amount": -1})
    assert _outcomes(conn, ns) == [("charge", "invalid")]


NON_FINITE = [(float("nan"), "NaN"), (float("inf"), "Infinity"), (float("-inf"), "-Infinity")]


@pytest.mark.parametrize("profile", [Profile.COMPAT, Profile.CONTROLLED])
@pytest.mark.parametrize(("amount", "stored"), NON_FINITE)
def test_non_finite_amount_is_rejected_with_400_and_logged_as_invalid(
    conn, profile, amount, stored
):
    """NaN and Infinity reach the service over HTTP, since json.loads accepts the literals.

    The rejected payload used to be stored verbatim, and Postgres refuses NaN in jsonb, so
    the documented 400 came back as an unhandled 500 with no call_log row at all.
    """
    ns = make_ns(conn, profile=profile)
    body = {**(CHARGE if profile is Profile.CONTROLLED else COMPAT_CHARGE), "amount": amount}
    r = service.charge(conn, ns, body)
    assert r.http_status == 400
    assert r.body["error"] == {"code": "invalid_request", "message": "amount must be finite"}
    assert _outcomes(conn, ns) == [("charge", "invalid")]
    logged = conn.execute("SELECT request FROM call_log WHERE namespace = %s", (ns,)).fetchone()
    assert logged["request"]["amount"] == stored  # the evidence is kept, as a string
    assert charges(conn, ns) == []


def test_non_finite_amount_claims_no_slot(conn):
    ns = make_ns(conn, World.LOST_ACK)
    service.charge(conn, ns, {**CHARGE, "amount": float("nan")})
    assert service.charge(conn, ns, dict(CHARGE)).http_status == 504  # still call 1


def test_a_failing_log_write_never_turns_the_400_into_a_500(conn, monkeypatch, caplog):
    """The 400 is the contract. If the log row cannot be written, the 400 still stands."""
    ns = make_ns(conn)

    def refuse(*args, **kwargs):
        raise psycopg.errors.DataError("injected")

    monkeypatch.setattr(service, "_log", refuse)
    with caplog.at_level("ERROR", logger="lab.simulator.service"):
        r = service.charge(conn, ns, {**CHARGE, "note": "x"})
    assert r.http_status == 400
    assert _outcomes(conn, ns) == []  # the row was lost, and said so
    assert any(rec.exc_info for rec in caplog.records)


# JSON strings may hold any code point, Postgres text and jsonb cannot hold these two.
UNSTORABLE = [("NUL", "a\x00b"), ("unpaired surrogate", "a\ud800b")]
ESCAPED = {"a\x00b": "a\\u0000b", "a\ud800b": "a\\ud800b"}


def _only_log_row(conn, ns):
    rows = conn.execute(
        "SELECT endpoint, call_index, idempotency_key, request, outcome FROM call_log"
        " WHERE namespace = %s ORDER BY id",
        (ns,),
    ).fetchall()
    assert len(rows) == 1, rows
    return rows[0]


@pytest.mark.parametrize("profile", [Profile.COMPAT, Profile.CONTROLLED])
@pytest.mark.parametrize(("label", "bad"), UNSTORABLE)
def test_unstorable_idempotency_key_is_a_logged_400_that_claims_no_slot(conn, profile, label, bad):
    ns = make_ns(conn, World.LOST_ACK, profile=profile)
    good = CHARGE if profile is Profile.CONTROLLED else COMPAT_CHARGE
    r = service.charge(conn, ns, {**good, "idempotency_key": bad})
    assert r.http_status == 400, label
    assert r.body == {
        "error": {
            "code": "invalid_request",
            "message": "idempotency_key must not contain a NUL character or an unpaired surrogate",
        }
    }
    row = _only_log_row(conn, ns)
    assert (row["endpoint"], row["call_index"], row["outcome"]) == ("charge", None, "invalid")
    # The evidence is kept, with the character made visible.
    assert row["idempotency_key"] == ESCAPED[bad]
    assert row["request"]["idempotency_key"] == ESCAPED[bad]
    assert charges(conn, ns) == []
    # It consumed nothing: the next call is still call 1, which the lost_ack plan faults.
    assert service.charge(conn, ns, dict(good)).http_status == 504


@pytest.mark.parametrize(("label", "bad"), UNSTORABLE)
def test_unstorable_order_id_is_a_logged_400_that_claims_no_slot(conn, label, bad):
    ns = make_ns(conn, World.LOST_ACK)
    r = service.charge(conn, ns, {**CHARGE, "order_id": bad})
    assert r.http_status == 400, label
    assert r.body["error"] == {
        "code": "invalid_request",
        "message": "order_id must not contain a NUL character or an unpaired surrogate",
    }
    row = _only_log_row(conn, ns)
    assert (row["call_index"], row["outcome"]) == (None, "invalid")
    assert row["request"]["order_id"] == ESCAPED[bad]
    assert service.charge(conn, ns, dict(CHARGE)).http_status == 504


@pytest.mark.parametrize(("label", "bad"), UNSTORABLE)
def test_unstorable_text_in_an_unexpected_field_is_still_logged(conn, label, bad):
    """Both the field's name and its value reach the request and the echoed error message."""
    ns = make_ns(conn)
    r = service.charge(conn, ns, {**CHARGE, "note" + bad: bad})
    assert r.http_status == 400
    row = _only_log_row(conn, ns)
    assert (row["call_index"], row["outcome"]) == (None, "invalid")
    assert row["request"]["note" + ESCAPED[bad]] == ESCAPED[bad]


@pytest.mark.parametrize(("label", "bad"), UNSTORABLE)
def test_unstorable_status_key_is_a_logged_not_found(conn, label, bad):
    """Status has no validation stage, so the call still claims its slot and is counted."""
    ns = make_ns(conn)
    r = service.status(conn, ns, bad)
    assert (r.http_status, r.body["status"]) == (200, "NOT_FOUND"), label
    row = _only_log_row(conn, ns)
    assert (row["endpoint"], row["call_index"], row["outcome"]) == ("status", 1, "not_found")
    assert row["idempotency_key"] == ESCAPED[bad]


@pytest.mark.parametrize(
    "ok", ["a\tb", "a\nb", "a\x01b", "a\x7fb", "a\u2028b", "a\u202eb", "a\U0001f600b"]
)
def test_other_control_and_unusual_characters_remain_valid_keys(conn, ok):
    """Postgres stores these without trouble, so rejecting them would only narrow the input."""
    ns = make_ns(conn)
    first = service.charge(conn, ns, {**CHARGE, "idempotency_key": ok, "order_id": ok})
    assert first.http_status == 200
    again = service.charge(conn, ns, {**CHARGE, "idempotency_key": ok, "order_id": ok})
    assert again.body["replayed"] is True
    assert service.status(conn, ns, ok).body["status"] == "SUCCEEDED"


def test_call_log_outcome_error_no_commit(conn):
    ns = make_ns(conn, World.TRUE_FAIL)
    service.charge(conn, ns, dict(CHARGE))
    assert _outcomes(conn, ns) == [("charge", "error_no_commit")]


def test_call_log_outcome_existing_then_error(conn):
    ns = make_ns(conn, plan=FaultPlan(charge={2: "commit_then_error"}))
    service.charge(conn, ns, dict(CHARGE))
    r = service.charge(conn, ns, dict(CHARGE))
    assert r.http_status == 504
    assert _outcomes(conn, ns) == [("charge", "committed"), ("charge", "existing_then_error")]


def test_call_log_outcome_status_not_found_and_unavailable(conn):
    ns = make_ns(conn, plan=FaultPlan(status_unavailable_calls=1))
    service.status(conn, ns, "k1")
    service.status(conn, ns, "k1")
    assert _outcomes(conn, ns) == [("status", "unavailable"), ("status", "not_found")]


def test_delete_namespace_removes_it_and_its_ledger(conn):
    ns = make_ns(conn)
    service.charge(conn, ns, dict(CHARGE))
    assert len(charges(conn, ns)) == 1
    service.delete_namespace(conn, ns)
    assert charges(conn, ns) == []
    with pytest.raises(service.UnknownNamespace):
        service.charge(conn, ns, dict(CHARGE))
    with pytest.raises(service.UnknownNamespace):
        service.status(conn, ns, "k1")


# --- Compat amount ceiling ---------------------------------------------------------------
#
# Compat does not apply the controlled business ceiling, so an amount that Postgres cannot
# store used to pass validation, claim a fault slot, then raise NumericValueOutOfRange inside
# the insert and surface as a 500 after the attempt had been counted. It must be a 400
# before the slot is claimed. A raw JSON number cannot reach this (json.loads turns 1e999999
# into inf, which is already rejected as not finite), so the amount arrives as a string.
AMOUNT_RANGE_MESSAGE = (
    "amount must have at most 131072 digits before the decimal point and 16383 after it"
)
UNSTORABLE_AMOUNTS = [
    pytest.param("1e999999", id="1e999999-the-reported-overflow"),
    pytest.param("1e131072", id="one-digit-over-the-magnitude-limit"),
    pytest.param("-1e131072", id="negative-over-the-magnitude-limit"),
    pytest.param("9" * 131073, id="131073-digits"),
    pytest.param("1e1000000", id="past-the-decimal-module-emax"),
    pytest.param("1e999999999999999999", id="exponent-near-int64"),
    pytest.param("1e-16384", id="one-place-over-the-scale-limit"),
    pytest.param("1e-999999", id="1e-999999-underflow"),
    pytest.param("0e-16384", id="zero-with-too-many-places"),
    pytest.param("1." + "0" * 16384, id="16384-places"),
]
STORABLE_LARGE_AMOUNTS = [
    pytest.param("1e131071", id="largest-magnitude"),
    pytest.param("-1e131071", id="largest-negative"),
    pytest.param("9" * 131072, id="131072-nines"),
    pytest.param("1e-16383", id="smallest-scale"),
    pytest.param("1." + "0" * 16383, id="16383-places"),
    pytest.param("0e-16383", id="zero-at-the-scale-limit"),
    pytest.param("0e999999", id="zero-with-a-huge-exponent"),
    pytest.param("1000000.01", id="just-over-the-controlled-ceiling"),
    pytest.param("0.001", id="sub-cent"),
]


def _counters(conn, ns):
    return conn.execute(
        "SELECT charge_calls, status_calls FROM sim_namespaces WHERE namespace = %s", (ns,)
    ).fetchone()


@pytest.mark.parametrize("bad", UNSTORABLE_AMOUNTS)
def test_compat_amount_postgres_cannot_store_is_a_400_before_a_slot_is_claimed(conn, bad):
    ns = make_ns(conn, World.LOST_ACK, Profile.COMPAT)
    r = service.charge(conn, ns, {"amount": bad, "idempotency_key": "k1"})
    assert r.http_status == 400
    assert r.body == {"error": {"code": "invalid_request", "message": AMOUNT_RANGE_MESSAGE}}
    assert charges(conn, ns) == []
    row = _only_log_row(conn, ns)
    assert (row["call_index"], row["outcome"]) == (None, "invalid")
    # The slot was not consumed: the namespace counter is untouched and no call has an index.
    assert _counters(conn, ns) == {"charge_calls": 0, "status_calls": 0}
    claimed = conn.execute(
        "SELECT count(*) AS n FROM call_log WHERE namespace = %s AND call_index IS NOT NULL",
        (ns,),
    ).fetchone()
    assert claimed["n"] == 0
    # The episode is still in phase: the next well-formed call is call 1, which lost_ack
    # faults by committing and then answering with the timeout.
    nxt = service.charge(conn, ns, dict(COMPAT_CHARGE))
    assert nxt.http_status == 504
    assert nxt.body["error"]["message"] == service.TIMEOUT_MESSAGE
    assert [c["call_index"] for c in charges(conn, ns)] == [1]
    assert _counters(conn, ns) == {"charge_calls": 1, "status_calls": 0}


@pytest.mark.parametrize("amount", STORABLE_LARGE_AMOUNTS)
def test_compat_still_accepts_every_amount_postgres_can_store(conn, amount):
    ns = make_ns(conn, profile=Profile.COMPAT)
    r = service.charge(conn, ns, {"amount": amount, "idempotency_key": "k1"})
    assert r.http_status == 200
    assert Decimal(r.body["charge"]["amount"]) == Decimal(amount)
    assert [c["amount"] for c in charges(conn, ns)] == [Decimal(amount)]
    assert _counters(conn, ns) == {"charge_calls": 1, "status_calls": 0}
    assert service.status(conn, ns, "k1").body["status"] == "SUCCEEDED"


# The bound is not a round number picked to look safe: it is whatever this Postgres accepts.
# Each case is inserted into a real NUMERIC column and the predicate must agree with the
# outcome, so a change in the server's limits (or a typo in ours) fails here.
EDGE_AMOUNTS = [
    *(p.values[0] for p in UNSTORABLE_AMOUNTS),
    *(p.values[0] for p in STORABLE_LARGE_AMOUNTS),
    "1e131070",
    "1e131072",
    "1." + "5" + "0" * 16382,
    "1." + "5" + "0" * 16383,
    "9" * 131072 + "." + "9" * 16383,
    "9" * 131072 + "." + "9" * 16384,
]


@pytest.mark.parametrize(
    "raw", EDGE_AMOUNTS, ids=lambda v: v if len(v) < 24 else f"{v[:6]}..len{len(v)}"
)
def test_is_storable_amount_agrees_with_a_real_numeric_column(conn, raw):
    from lab.protocol.task import parse_amount

    amount = parse_amount(raw)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS probe_amount (a NUMERIC NOT NULL)")
    conn.execute("TRUNCATE probe_amount")
    try:
        conn.execute("INSERT INTO probe_amount (a) VALUES (%s)", (amount,))
        stored = True
    except psycopg.errors.NumericValueOutOfRange:
        stored = False
    assert is_storable_amount(amount) is stored
