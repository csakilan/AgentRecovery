import threading
import time
from decimal import Decimal

import psycopg
import pytest
from psycopg.rows import dict_row

from lab.oracle.ledger import CallSummary, LedgerCharge, read_ledger
from lab.oracle.score import legacy_score
from lab.protocol.faults import FaultPlan, World, world_fault_plan
from lab.protocol.task import Profile
from lab.simulator import service
from tests.helpers import CHARGE, COMPAT_CHARGE


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached in time")


def _charge_in_background(svc_url, namespace, body):
    result = {}

    def run():
        with psycopg.connect(svc_url, autocommit=True, row_factory=dict_row) as c:
            result["response"] = service.charge(c, namespace, body)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def _boom(*args, **kwargs):
    raise RuntimeError("injected failure")


def test_read_ledger_through_the_read_only_role(payments_db, conn):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, world_fault_plan(World.LOST_ACK))
    service.charge(conn, "ns", dict(CHARGE))
    service.status(conn, "ns", "k1")
    service.charge(conn, "ns", {**CHARGE, "idempotency_key": "k2"})
    service.charge(conn, "ns", {**CHARGE, "amount": 1.001})  # invalid, not an attempt

    with psycopg.connect(payments_db["reader"], autocommit=True) as reader:
        found, calls = read_ledger(reader, "ns")

    assert found == [
        LedgerCharge("k1", "1234", Decimal("49.99"), "USD", 1),
        LedgerCharge("k2", "1234", Decimal("49.99"), "USD", 2),
    ]
    assert calls == CallSummary(charge_keys=("k1", "k2"), status_calls=1)


def test_read_ledger_ignores_other_namespaces(conn):
    for ns in ("a", "b"):
        service.configure_namespace(conn, ns, Profile.CONTROLLED, world_fault_plan(World.HEALTHY))
    service.charge(conn, "a", dict(CHARGE))
    found, calls = read_ledger(conn, "b")
    assert found == []
    assert calls == CallSummary(charge_keys=(), status_calls=0)


def test_read_ledger_amounts_are_decimals_not_floats(conn):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, world_fault_plan(World.HEALTHY))
    service.charge(conn, "ns", {**CHARGE, "amount": 0.1})
    service.charge(conn, "ns", {**CHARGE, "idempotency_key": "k2", "amount": 49.99})
    found, _ = read_ledger(conn, "ns")
    assert [type(c.amount) for c in found] == [Decimal, Decimal]
    assert [c.amount for c in found] == [Decimal("0.1"), Decimal("49.99")]


def test_a_crashed_attempt_still_counts_as_a_charge_attempt(conn, monkeypatch):
    """The original counts at the start of the call, so a crash must not hide the attempt."""
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan())
    with monkeypatch.context() as m:
        m.setattr(service, "_insert_or_get", _boom)
        with pytest.raises(RuntimeError, match="injected failure"):
            service.charge(conn, "ns", dict(CHARGE))
    found, calls = read_ledger(conn, "ns")
    assert found == []
    assert calls == CallSummary(charge_keys=("k1",), status_calls=0)
    assert legacy_score(found, calls).n_charge_attempts == 1

    # A later retry on the same key commits; the crashed attempt still counts toward both
    # the attempt total and the reused-key flag.
    service.charge(conn, "ns", dict(CHARGE))
    found, calls = read_ledger(conn, "ns")
    score = legacy_score(found, calls)
    assert len(found) == 1
    assert (score.n_charge_attempts, score.reused_key_on_retry) == (2, 1)


def test_a_failed_compat_attempt_counts_without_filtering_on_outcome(conn):
    service.configure_namespace(conn, "ns", Profile.COMPAT, world_fault_plan(World.TRUE_FAIL))
    service.charge(conn, "ns", dict(COMPAT_CHARGE))  # first call fails, nothing committed
    outcomes = [
        r["outcome"]
        for r in conn.execute("SELECT outcome FROM call_log WHERE namespace = 'ns'").fetchall()
    ]
    assert outcomes == ["error_no_commit"]
    found, calls = read_ledger(conn, "ns")
    assert found == []
    assert legacy_score(found, calls).n_charge_attempts == 1


def test_read_ledger_during_a_request_counts_the_attempt_but_not_yet_the_effect(
    payments_db, svc_url, conn
):
    """A claimed slot is an attempt from the moment it is claimed (an in_flight row)."""
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan(hold_before_commit=(1,)))
    thread, result = _charge_in_background(svc_url, "ns", dict(CHARGE))
    with psycopg.connect(payments_db["reader"], autocommit=True) as reader:
        _wait_until(lambda: service.barrier_state(conn, "ns", "before_commit:1")["reached"])
        found, calls = read_ledger(reader, "ns")
        assert found == []
        assert calls == CallSummary(charge_keys=("k1",), status_calls=0)
        assert legacy_score(found, calls).n_charge_attempts == 1

        service.release_barrier(conn, "ns", "before_commit:1")
        thread.join(5)
        assert result["response"].http_status == 200
        found, calls = read_ledger(reader, "ns")
        assert [c.idempotency_key for c in found] == ["k1"]
        assert calls == CallSummary(charge_keys=("k1",), status_calls=0)


def test_read_ledger_after_commit_but_before_the_response_sees_one_effect_one_attempt(
    payments_db, svc_url, conn
):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan(hold_after_commit=(1,)))
    thread, result = _charge_in_background(svc_url, "ns", dict(CHARGE))
    with psycopg.connect(payments_db["reader"], autocommit=True) as reader:
        _wait_until(lambda: service.barrier_state(conn, "ns", "after_commit:1")["reached"])
        found, calls = read_ledger(reader, "ns")
        assert [c.idempotency_key for c in found] == ["k1"]
        assert calls == CallSummary(charge_keys=("k1",), status_calls=0)
        service.release_barrier(conn, "ns", "after_commit:1")
        thread.join(5)
    assert result["response"].http_status == 200


def test_read_ledger_orders_by_claim_not_by_completion(payments_db, svc_url, conn):
    """Call 1 is held before its commit, so call 2 commits first. Order must still be 1, 2."""
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan(hold_before_commit=(1,)))
    thread, result = _charge_in_background(svc_url, "ns", dict(CHARGE))
    _wait_until(lambda: service.barrier_state(conn, "ns", "before_commit:1")["reached"])
    assert service.charge(conn, "ns", {**CHARGE, "idempotency_key": "k2"}).http_status == 200
    service.release_barrier(conn, "ns", "before_commit:1")
    thread.join(5)
    assert result["response"].http_status == 200

    commit_order = [
        r["idempotency_key"]
        for r in conn.execute(
            "SELECT idempotency_key FROM charges WHERE namespace = 'ns' ORDER BY id"
        ).fetchall()
    ]
    assert commit_order == ["k2", "k1"]  # the premise: completion order differs from claim order

    with psycopg.connect(payments_db["reader"], autocommit=True) as reader:
        found, calls = read_ledger(reader, "ns")
    assert [(c.idempotency_key, c.call_index) for c in found] == [("k1", 1), ("k2", 2)]
    assert calls.charge_keys == ("k1", "k2")
