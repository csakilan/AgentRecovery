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


def test_read_ledger_amount_column_is_numeric_and_reads_back_as_decimal(conn):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, world_fault_plan(World.HEALTHY))
    service.charge(conn, "ns", {**CHARGE, "amount": 0.1})
    service.charge(conn, "ns", {**CHARGE, "idempotency_key": "k2", "amount": 49.99})
    found, _ = read_ledger(conn, "ns")
    assert [type(c.amount) for c in found] == [Decimal, Decimal]
    assert [c.amount for c in found] == [Decimal("0.1"), Decimal("49.99")]


def test_read_ledger_constructs_decimals_even_when_the_driver_hands_back_floats(payments_db, conn):
    """The Decimal is built by the reader, not trusted from the driver.

    psycopg returns Decimal for NUMERIC, so the old test could not tell construction from
    passthrough. Here the cursor is made to return a float; the reader must still yield
    the exact Decimal (via str, so 0.1 is Decimal("0.1"), not the binary expansion).
    """
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, world_fault_plan(World.HEALTHY))
    service.charge(conn, "ns", {**CHARGE, "amount": 0.1})

    class FloatAmountCursor(psycopg.Cursor):
        def fetchall(self):
            return [
                {**r, "amount": float(r["amount"])} if r["kind"] == "charge" else r
                for r in super().fetchall()
            ]

    with psycopg.connect(
        payments_db["reader"], autocommit=True, cursor_factory=FloatAmountCursor
    ) as reader:
        found, _ = read_ledger(reader, "ns")
    assert [c.amount for c in found] == [Decimal("0.1")]
    assert type(found[0].amount) is Decimal


def test_ledger_charge_rejects_a_non_decimal_amount():
    """The safety net behind the reader: a float that reached LedgerCharge would raise."""
    with pytest.raises(TypeError, match="amount must be a Decimal"):
        LedgerCharge("k1", "1234", 0.1, "USD", 1)  # type: ignore[arg-type]


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


def test_a_failed_compat_attempt_counts_without_filtering_on_outcome(payments_db, svc_url, conn):
    """One episode holding an error_no_commit, a server_error and an in_flight attempt."""
    plan = FaultPlan(charge={1: "error_no_commit"}, hold_before_commit=(3,))
    service.configure_namespace(conn, "ns", Profile.COMPAT, plan)
    service.charge(conn, "ns", dict(COMPAT_CHARGE))  # 1: fails, nothing committed
    with pytest.MonkeyPatch.context() as m:
        m.setattr(service, "_insert_or_get", _boom)
        with pytest.raises(RuntimeError, match="injected failure"):
            service.charge(conn, "ns", {**COMPAT_CHARGE, "idempotency_key": "k2"})  # 2: crash
    thread, result = _charge_in_background(
        svc_url, "ns", {**COMPAT_CHARGE, "idempotency_key": "k3"}
    )
    try:
        _wait_until(lambda: service.barrier_state(conn, "ns", "before_commit:3")["reached"])
        outcomes = [
            r["outcome"]
            for r in conn.execute(
                "SELECT outcome FROM call_log WHERE namespace = 'ns' ORDER BY call_index"
            ).fetchall()
        ]
        assert outcomes == ["error_no_commit", "server_error", "in_flight"]  # the premise

        with psycopg.connect(payments_db["reader"], autocommit=True) as reader:
            found, calls = read_ledger(reader, "ns")
        assert found == []
        assert calls == CallSummary(charge_keys=("k1", "k2", "k3"), status_calls=0)
        assert legacy_score(found, calls).n_charge_attempts == 3
    finally:
        service.release_barrier(conn, "ns", "before_commit:3")
        thread.join(5)
    assert result["response"].http_status == 200


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


def test_read_ledger_requires_autocommit(payments_db):
    with psycopg.connect(payments_db["reader"]) as c:
        with pytest.raises(ValueError, match="autocommit"):
            read_ledger(c, "ns")
        assert c.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_read_ledger_issues_exactly_one_sql_statement(payments_db, conn):
    """Both halves of the read must share one snapshot, which means one statement.

    A single SELECT sees one snapshot; two separate statements could straddle a commit and
    report an effect without its attempt. Counting executions pins that structurally.
    """
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan())
    service.charge(conn, "ns", dict(CHARGE))
    service.status(conn, "ns", "k1")

    executed: list[str] = []

    class CountingCursor(psycopg.Cursor):
        def execute(self, query, *args, **kwargs):
            executed.append(str(query))
            return super().execute(query, *args, **kwargs)

        def executemany(self, query, *args, **kwargs):
            executed.append(str(query))
            return super().executemany(query, *args, **kwargs)

    with psycopg.connect(
        payments_db["reader"], autocommit=True, cursor_factory=CountingCursor
    ) as reader:
        found, calls = read_ledger(reader, "ns")

    # Both halves came back, so the one statement really did cover charges and call_log.
    assert [c.idempotency_key for c in found] == ["k1"]
    assert calls == CallSummary(charge_keys=("k1",), status_calls=1)
    assert len(executed) == 1
    assert "charges" in executed[0] and "call_log" in executed[0]
