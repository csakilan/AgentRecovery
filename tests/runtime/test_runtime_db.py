import psycopg
import pytest

from lab import db
from lab.runtime import RUNTIME_MIGRATIONS


def test_migrations_are_idempotent(runtime_db):
    assert db.apply_migrations(runtime_db["admin"], RUNTIME_MIGRATIONS) == []


def test_runtime_role_can_write_its_own_tables(runtime_db):
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO experiments (id, name, manifest_sha256, config_json)"
            " VALUES ('e1', 'test', 'abc', '{}')"
        )
        assert conn.execute("SELECT count(*) FROM experiments").fetchone()[0] == 1


def test_runtime_role_cannot_reach_the_payment_ledger(runtime_db, payments_db):
    """The evaluator's independence is structural, not conventional."""
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UndefinedTable):
            conn.execute("SELECT count(*) FROM charges")


def test_runs_require_a_known_experiment(runtime_db):
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO runs (id, experiment_id, episode_id, namespace, mode,"
                " thread_id, state) VALUES ('r1', 'nope', 'ep', 'ns', 'agent_directed',"
                " 't1', 'queued')"
            )


def test_run_state_is_constrained(runtime_db):
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO experiments (id, name, manifest_sha256, config_json)"
            " VALUES ('e1', 'test', 'abc', '{}')"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO runs (id, experiment_id, episode_id, namespace, mode,"
                " thread_id, state) VALUES ('r1', 'e1', 'ep', 'ns', 'agent_directed',"
                " 't1', 'banana')"
            )


def test_durability_is_recorded_on_the_experiment(runtime_db):
    """D11: the comparison is only fair if every mode shares one setting, so the
    setting must be stored where a reader can check it."""
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO experiments (id, name, manifest_sha256, config_json)"
            " VALUES ('e1', 'test', 'abc', '{\"durability\": \"sync\"}')"
        )
        row = conn.execute(
            "SELECT config_json->>'durability' FROM experiments WHERE id = 'e1'"
        ).fetchone()
        assert row[0] == "sync"
