"""A consumed fault-schedule slot and its call-log record must never disagree (D9)."""

import threading
import time

import psycopg
import pytest
from psycopg.rows import dict_row

from lab.protocol.faults import FaultPlan
from lab.protocol.task import Profile
from lab.simulator import service
from tests.helpers import CHARGE


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
