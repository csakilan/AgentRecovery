from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from lab.execution.config import ExecutionConfig
from lab.execution.identity import effective_key, operation_id
from lab.execution.journal import Journal
from lab.execution.types import IntentConflict, OperationState
from lab.protocol.task import Profile
from lab.protocol.validation import validate_charge


def register(journal, ctx, amount="12.00", key="model-key"):
    args = dict(idempotency_key=key, order_id="order", amount=amount, currency="USD")
    return journal.register(
        ctx,
        validate_charge(Profile.CONTROLLED, args),
        args,
        datetime.now(UTC) + timedelta(minutes=5),
        config=ExecutionConfig(),
    )


def test_first_intent_is_immutable(journal_conn, journal_ctx):
    journal = Journal(journal_conn)
    first = register(journal, journal_ctx)
    equivalent = register(journal, journal_ctx, "12", "another-key")
    assert first == equivalent
    assert first.id == str(operation_id(journal_ctx))
    assert first.effective_key == effective_key(journal_ctx)
    assert first.state == OperationState.READY
    assert first.requested_args["amount"] == "12.00"
    with pytest.raises(IntentConflict):
        register(journal, journal_ctx, "49.99")
    assert journal.get(first.id) == first
    events = journal_conn.execute(
        "SELECT event_type FROM recovery_events WHERE operation_id=%s", (first.id,)
    ).fetchall()
    assert [r["event_type"] for r in events] == ["registered", "intent_conflict"]


def test_two_registrations_share_one_operation(runtime_db, journal_conn, journal_ctx):
    def run(_):
        with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
            return register(Journal(conn), journal_ctx).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert len(set(pool.map(run, range(2)))) == 1
    assert (
        journal_conn.execute(
            "SELECT count(*) AS n FROM operations WHERE run_id=%s", (journal_ctx.run_id,)
        ).fetchone()["n"]
        == 1
    )


def test_invalid_intent_cannot_enter_journal(journal_conn, journal_ctx):
    args = dict(idempotency_key="k", order_id="o", amount="1", currency="USD", extra="bad")
    with pytest.raises(ValueError, match="validated charge"):
        Journal(journal_conn).register(
            journal_ctx,
            validate_charge(Profile.CONTROLLED, args),
            args,
            datetime.now(UTC) + timedelta(minutes=5),
            config=ExecutionConfig(),
        )
    assert (
        journal_conn.execute(
            "SELECT count(*) AS n FROM operations WHERE run_id=%s", (journal_ctx.run_id,)
        ).fetchone()["n"]
        == 0
    )


def test_operation_deadline_cannot_extend_saved_run(journal_conn, journal_ctx):
    deadline = datetime.now(UTC) + timedelta(seconds=10)
    journal_conn.execute(
        "UPDATE runs SET deadline_at=%s WHERE id=%s", (deadline, journal_ctx.run_id)
    )
    first = register(Journal(journal_conn), journal_ctx)
    assert first.deadline_at == deadline
    assert register(Journal(journal_conn), journal_ctx).deadline_at == deadline
