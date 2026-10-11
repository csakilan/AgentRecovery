"""A consumed fault-schedule slot and its call-log record must never disagree (D9)."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.rows import dict_row

from lab.protocol.faults import FaultPlan
from lab.protocol.task import Profile
from lab.simulator import service
from tests.helpers import CHARGE, COMPAT_CHARGE


def _counters(conn, ns):
    return conn.execute(
        "SELECT charge_calls, status_calls FROM sim_namespaces WHERE namespace = %s", (ns,)
    ).fetchone()


def _log(conn, ns, endpoint):
    return conn.execute(
        "SELECT call_index, idempotency_key, http_status, outcome FROM call_log"
        " WHERE namespace = %s AND endpoint = %s ORDER BY id",
        (ns, endpoint),
    ).fetchall()


def _boom(*args, **kwargs):
    raise RuntimeError("injected failure")


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached in time")


@pytest.mark.parametrize("target", ["_insert_or_get", "_charge_result", "hold"])
def test_failure_after_charge_slot_is_consumed_still_leaves_a_log_row(conn, monkeypatch, target):
    plan = FaultPlan(hold_before_commit=(1,)) if target == "hold" else FaultPlan()
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, plan)
    monkeypatch.setattr(service, target, _boom)
    with pytest.raises(RuntimeError, match="injected failure"):
        service.charge(conn, "ns", dict(CHARGE))
    assert _counters(conn, "ns")["charge_calls"] == 1
    rows = _log(conn, "ns", "charge")
    assert [(r["call_index"], r["idempotency_key"], r["outcome"]) for r in rows] == [
        (1, "k1", "server_error")
    ]
    assert rows[0]["http_status"] == 500


def test_failed_attempt_does_not_shift_the_fault_schedule(conn, monkeypatch):
    service.configure_namespace(
        conn, "ns", Profile.CONTROLLED, FaultPlan(charge={2: "commit_then_error"})
    )
    with monkeypatch.context() as m:
        m.setattr(service, "_insert_or_get", _boom)
        with pytest.raises(RuntimeError):
            service.charge(conn, "ns", dict(CHARGE))
    second = service.charge(conn, "ns", dict(CHARGE))
    assert second.http_status == 504
    assert [(r["call_index"], r["outcome"]) for r in _log(conn, "ns", "charge")] == [
        (1, "server_error"),
        (2, "committed_then_error"),
    ]
    assert _counters(conn, "ns")["charge_calls"] == 2


def test_failure_after_status_slot_is_consumed_still_leaves_a_log_row(conn, monkeypatch):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan())

    class ExplodingPlan:
        @property
        def status_unavailable_calls(self):
            raise RuntimeError("injected failure")

    monkeypatch.setattr(service, "_load", lambda c, n: (Profile.CONTROLLED, ExplodingPlan()))
    with pytest.raises(RuntimeError, match="injected failure"):
        service.status(conn, "ns", "k1")
    assert _counters(conn, "ns")["status_calls"] == 1
    assert [(r["call_index"], r["outcome"]) for r in _log(conn, "ns", "status")] == [
        (1, "server_error")
    ]


def test_invalid_request_still_consumes_no_slot_and_logs_no_index(conn):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan())
    service.charge(conn, "ns", {"idempotency_key": "k1"})
    assert _counters(conn, "ns")["charge_calls"] == 0
    assert [(r["call_index"], r["outcome"]) for r in _log(conn, "ns", "charge")] == [
        (None, "invalid")
    ]


def test_slot_and_log_row_appear_together_while_the_request_is_held(svc_url, conn):
    """A worker killed inside a barrier leaves this state behind: the slot is accounted for."""
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan(hold_before_commit=(1,)))
    result = {}

    def run():
        with psycopg.connect(svc_url, autocommit=True, row_factory=dict_row) as c:
            result["response"] = service.charge(c, "ns", dict(CHARGE))

    thread = threading.Thread(target=run)
    thread.start()
    _wait_until(lambda: service.barrier_state(conn, "ns", "before_commit:1")["reached"])
    assert _counters(conn, "ns")["charge_calls"] == 1
    assert [
        (r["call_index"], r["idempotency_key"], r["outcome"]) for r in _log(conn, "ns", "charge")
    ] == [(1, "k1", service.IN_FLIGHT)]
    service.release_barrier(conn, "ns", "before_commit:1")
    thread.join(5)
    assert result["response"].http_status == 200
    assert [(r["call_index"], r["outcome"]) for r in _log(conn, "ns", "charge")] == [
        (1, "committed")
    ]


def _assert_slots_and_log_agree(conn, ns):
    """Each counter equals its endpoint's indexed log rows, and the indexes are 1..N."""
    counters = _counters(conn, ns)
    for endpoint, counter in (("charge", "charge_calls"), ("status", "status_calls")):
        indexes = sorted(
            r["call_index"] for r in _log(conn, ns, endpoint) if r["call_index"] is not None
        )
        assert counters[counter] == len(indexes), endpoint
        assert indexes == list(range(1, len(indexes) + 1)), endpoint


@pytest.mark.parametrize("counter", ["charge_calls", "status_calls"])
def test_claim_is_atomic_when_the_log_insert_fails(conn, counter):
    """The slot is consumed in the same statement as the log row, never apart from it.

    An endpoint outside the call_log CHECK constraint makes the insert half of the claim
    fail after its update half has produced a row. One statement rolls both back. Two
    statements would leave the counter advanced with no row, which is the state a worker
    killed between them leaves behind, and D9 exists to make that state impossible.
    """
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan())
    with pytest.raises(psycopg.errors.CheckViolation):
        service._claim_call(conn, "ns", "not-an-endpoint", counter, "k1", {})
    assert _counters(conn, "ns")[counter] == 0
    assert _log(conn, "ns", "charge") == []
    assert _log(conn, "ns", "status") == []
    # The schedule is untouched: the next real call still gets index 1.
    assert service._claim_call(conn, "ns", "charge", counter, "k1", {})[0] == 1


def test_slots_and_log_rows_agree_across_mixed_outcomes(conn, monkeypatch):
    plan = FaultPlan(
        charge={2: "commit_then_error", 3: "error_no_commit"}, status_unavailable_calls=1
    )
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, plan)
    service.charge(conn, "ns", dict(CHARGE))  # 1: committed
    service.charge(conn, "ns", dict(CHARGE))  # 2: existing_then_error
    service.charge(conn, "ns", dict(CHARGE))  # 3: error_no_commit
    service.charge(conn, "ns", dict(CHARGE))  # 4: replayed
    service.charge(conn, "ns", {**CHARGE, "order_id": "other"})  # 5: conflict
    service.charge(conn, "ns", {"idempotency_key": "k1"})  # invalid: no slot
    service.status(conn, "ns", "k1")  # 1: unavailable
    service.status(conn, "ns", "k1")  # 2: succeeded
    service.status(conn, "ns", "nope")  # 3: not found
    with monkeypatch.context() as m:
        m.setattr(service, "_insert_or_get", _boom)
        with pytest.raises(RuntimeError):
            service.charge(conn, "ns", {**CHARGE, "idempotency_key": "k2"})  # 6: server_error
    service.charge(conn, "ns", {**CHARGE, "idempotency_key": "k3"})  # 7: committed
    _assert_slots_and_log_agree(conn, "ns")
    assert _counters(conn, "ns") == {"charge_calls": 7, "status_calls": 3}
    assert [r["outcome"] for r in _log(conn, "ns", "charge") if r["call_index"]] == [
        "committed",
        "existing_then_error",
        "error_no_commit",
        "replayed",
        "conflict",
        "server_error",
        "committed",
    ]


def test_slots_and_log_rows_agree_under_concurrent_calls(svc_url, conn):
    workers = 16

    def call(args):
        i, start = args
        with psycopg.connect(svc_url, autocommit=True, row_factory=dict_row) as c:
            start.wait()
            if i % 4 == 3:
                return service.status(c, ns, "k0")
            # Half the charges share a key, so replays and fresh commits race together.
            return service.charge(c, ns, {**COMPAT_CHARGE, "idempotency_key": f"k{i % 2}"})

    for round_ in range(5):
        ns = f"race-{round_}"
        service.configure_namespace(conn, ns, Profile.COMPAT, FaultPlan())
        start = threading.Barrier(workers)
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(call, [(i, start) for i in range(workers)]))
        _assert_slots_and_log_agree(conn, ns)
        assert _counters(conn, ns) == {"charge_calls": 12, "status_calls": 4}
        assert not [r for r in _log(conn, ns, "charge") if r["outcome"] == service.IN_FLIGHT]
        assert not [r for r in _log(conn, ns, "status") if r["outcome"] == service.IN_FLIGHT]
