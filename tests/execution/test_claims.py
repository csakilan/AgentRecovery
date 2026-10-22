import psycopg
import pytest

from lab.execution.journal import Journal
from lab.execution.types import LeaseLost, OperationState, OperationUpdate
from tests.execution.test_journal import register


def test_claim_takeover_fences_attempts(runtime_db, journal_conn, journal_ctx):
    journal = Journal(journal_conn)
    op = register(journal, journal_ctx)
    first = journal.claim(op.id, "first", 30)
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        other = Journal(conn)
        assert other.claim(op.id, "second", 30) is None
        attempt = journal.begin_attempt(first, 3)
        assert attempt.number == 1
        journal_conn.execute(
            "UPDATE operations SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE id=%s",
            (op.id,),
        )
        second = other.claim(op.id, "second", 30)
        assert second.version > first.version
        assert other.get(op.id).state == OperationState.UNKNOWN
        assert journal.renew(first, 30) is False
        with pytest.raises(LeaseLost):
            journal.apply(
                first,
                OperationUpdate(state=OperationState.UNKNOWN, evidence={}, attempt_id=attempt.id),
            )
        assert other.get(op.id).attempt_count == 1


def test_lifetime_attempt_bound_and_original_args(journal_conn, journal_ctx):
    journal = Journal(journal_conn)
    op = register(journal, journal_ctx)
    for number in range(1, 4):
        claim = journal.claim(op.id, f"worker-{number}", 30)
        attempt = journal.begin_attempt(claim, 99)
        assert attempt.number == number
        assert attempt.effective_args["amount"] == "12"
        journal.apply(
            claim, OperationUpdate(state=OperationState.UNKNOWN, evidence={}, attempt_id=attempt.id)
        )
        journal.apply(
            claim,
            OperationUpdate(
                state=OperationState.CONFIRMED_NO_EFFECT, evidence={"provisional": True}
            ),
        )
        journal.release(claim)
    claim = journal.claim(op.id, "exhausted", 30)
    with pytest.raises(ValueError, match="attempt"):
        journal.begin_attempt(claim, 99)
    assert journal.get(op.id).attempt_count == 3


def test_terminal_and_attempt_roll_back_if_outbox_insert_fails(journal_conn, journal_ctx):
    from lab.agent.executor import ToolOutcome

    journal = Journal(journal_conn)
    op = register(journal, journal_ctx)
    claim = journal.claim(op.id, "worker", 30)
    attempt = journal.begin_attempt(claim, 3)
    journal_conn.execute(
        "INSERT INTO outbox(id,operation_id,run_id) VALUES(%s,%s,%s)",
        ("terminal-" + op.id, op.id, journal_ctx.run_id),
    )
    outcome = ToolOutcome(
        requested_args=op.requested_args,
        requested_key="model-key",
        effective_args=attempt.effective_args,
        effective_key=op.effective_key,
        rule="durable_gateway",
        observation="saved success",
        http_status=200,
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        journal.apply(
            claim,
            OperationUpdate(
                state=OperationState.CONFIRMED_SUCCESS,
                evidence={},
                attempt_id=attempt.id,
                http_status=200,
                terminal_outcome=outcome,
            ),
        )
    assert journal.get(op.id).state == OperationState.IN_FLIGHT
    assert journal.get(op.id).terminal_outcome is None
    assert (
        journal_conn.execute(
            "SELECT finished_at FROM attempts WHERE id=%s", (attempt.id,)
        ).fetchone()["finished_at"]
        is None
    )
