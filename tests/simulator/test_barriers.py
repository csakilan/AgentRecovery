import threading
import time

import psycopg
from psycopg.rows import dict_row

from lab.protocol.faults import FaultPlan
from lab.protocol.task import Profile
from lab.simulator import service
from tests.helpers import CHARGE, charges


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached in time")


def _charge_in_background(svc_url, namespace, body, **kwargs):
    result = {}

    def run():
        with psycopg.connect(svc_url, autocommit=True, row_factory=dict_row) as c:
            result["response"] = service.charge(c, namespace, body, **kwargs)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_after_commit_hold_exposes_committed_but_unacknowledged_state(svc_url, conn):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan(hold_after_commit=(1,)))
    thread, result = _charge_in_background(svc_url, "ns", dict(CHARGE))
    _wait_until(lambda: service.barrier_state(conn, "ns", "after_commit:1")["reached"])
    assert len(charges(conn, "ns")) == 1
    assert "response" not in result
    service.release_barrier(conn, "ns", "after_commit:1")
    thread.join(5)
    assert result["response"].http_status == 200


def test_before_commit_hold_exposes_uncommitted_state(svc_url, conn):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan(hold_before_commit=(1,)))
    thread, result = _charge_in_background(svc_url, "ns", dict(CHARGE))
    _wait_until(lambda: service.barrier_state(conn, "ns", "before_commit:1")["reached"])
    assert charges(conn, "ns") == []
    service.release_barrier(conn, "ns", "before_commit:1")
    thread.join(5)
    assert result["response"].http_status == 200
    assert len(charges(conn, "ns")) == 1


def test_pre_released_barrier_does_not_block(conn):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan(hold_after_commit=(1,)))
    service.release_barrier(conn, "ns", "after_commit:1")
    started = time.monotonic()
    assert service.charge(conn, "ns", dict(CHARGE)).http_status == 200
    assert time.monotonic() - started < 2


def test_unreleased_barrier_times_out_and_continues(conn):
    """The call waits out the timeout, then carries on rather than raising."""
    timeout_s = 0.3
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan(hold_after_commit=(1,)))
    started = time.monotonic()
    r = service.charge(conn, "ns", dict(CHARGE), barrier_timeout_s=timeout_s)
    elapsed = time.monotonic() - started
    assert r.http_status == 200
    assert len(charges(conn, "ns")) == 1
    # Generous lower bound, no upper bound: a loaded machine may be slow but never early.
    assert elapsed >= timeout_s * 0.9, f"returned after {elapsed:.3f}s without waiting"
    assert service.barrier_state(conn, "ns", "after_commit:1") == {
        "reached": True,
        "released": False,
    }


def test_barrier_state_for_unknown_barrier(conn):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan())
    assert service.barrier_state(conn, "ns", "after_commit:9") == {
        "reached": False,
        "released": False,
    }
