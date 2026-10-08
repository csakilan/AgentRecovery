import psycopg
import pytest

from lab.protocol.faults import FaultPlan, World, world_fault_plan
from lab.protocol.render import render_charge
from lab.protocol.task import Profile
from lab.simulator import service
from tests.helpers import CHARGE, COMPAT_CHARGE, charges


def make_ns(conn, world=World.HEALTHY, profile=Profile.CONTROLLED, ns="ns", plan=None):
    service.configure_namespace(conn, ns, profile, plan or world_fault_plan(world))
    return ns


def test_healthy_charge_commits_once(conn):
    ns = make_ns(conn)
    r = service.charge(conn, ns, dict(CHARGE))
    assert r.http_status == 200
    assert r.body["message"] == service.SUCCESS_MESSAGE
    assert r.body["replayed"] is False
    assert r.body["charge"]["amount"] == "49.99"
    assert len(charges(conn, ns)) == 1


def test_same_key_replays_without_a_new_effect(conn):
    ns = make_ns(conn)
    service.charge(conn, ns, dict(CHARGE))
    r = service.charge(conn, ns, dict(CHARGE))
    assert r.http_status == 200
    assert r.body["replayed"] is True
    assert r.body["message"] == service.REPLAY_MESSAGE
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
