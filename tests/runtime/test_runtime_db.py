import psycopg
import pytest
from psycopg import sql

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


PAYMENT_TABLES = {"sim_namespaces", "charges", "call_log", "barriers"}
RUNTIME_TABLES = {
    "experiments",
    "runs",
    "model_calls",
    "tool_events",
    "crash_points",
    "results",
    "request_budget",
}
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")


def _public_tables(url):
    """Every table in the public schema of the database at `url`, from the catalog."""
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {row[0] for row in rows}


def _public_sequences(url):
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute("SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'")
        return {row[0] for row in rows}


def _privileges_held(url, role):
    """Every (relation, privilege) the role holds in the database at `url`, at table or
    column level, including on sequences. Asked of the catalog, so it does not depend on
    the role being able to connect."""
    held = []
    with psycopg.connect(url, autocommit=True) as conn:
        for table in sorted(_public_tables(url)):
            for privilege in TABLE_PRIVILEGES:
                allowed = conn.execute(
                    "SELECT has_any_column_privilege(%s, %s, %s)", (role, table, privilege)
                ).fetchone()[0]
                if allowed:
                    held.append((table, privilege))
            if conn.execute(
                "SELECT has_table_privilege(%s, %s, 'DELETE')", (role, table)
            ).fetchone()[0]:
                held.append((table, "DELETE"))
        for sequence in sorted(_public_sequences(url)):
            for privilege in ("USAGE", "SELECT", "UPDATE"):
                if conn.execute(
                    "SELECT has_sequence_privilege(%s, %s, %s)", (role, sequence, privilege)
                ).fetchone()[0]:
                    held.append((sequence, privilege))
    return held


def test_runtime_role_cannot_reach_the_payment_ledger(runtime_db, payments_db):
    """The evaluator's independence is structural, not conventional.

    The premise is established first: the payment tables exist, in a different database,
    and are readable through the payments connection. Without that, "the runtime role cannot
    see them" would also be true if the ledger did not exist at all."""
    assert runtime_db["name"] != payments_db["name"]

    payment_tables = _public_tables(payments_db["admin"]) - {"schema_migrations"}
    assert PAYMENT_TABLES <= payment_tables
    with psycopg.connect(payments_db["svc"], autocommit=True) as conn:
        for table in sorted(payment_tables):
            conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()

    assert not payment_tables & _public_tables(runtime_db["admin"])
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        for table in sorted(payment_tables):
            with pytest.raises(psycopg.errors.UndefinedTable):
                conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))


@pytest.mark.parametrize("role", ["payment_svc", "ledger_reader"])
def test_payment_roles_hold_nothing_on_the_runtime_tables(role, runtime_db, payments_db):
    """The reverse direction: neither the role the agent-facing service uses nor the one the
    evaluator reads the ledger with may touch the agent's own records, whether or not it can
    connect."""
    runtime_tables = _public_tables(runtime_db["admin"])
    assert RUNTIME_TABLES <= runtime_tables
    assert _public_sequences(runtime_db["admin"])
    assert _privileges_held(runtime_db["admin"], role) == []


def test_runtime_role_holds_nothing_on_the_payment_tables(runtime_db, payments_db):
    """The runtime role has no privilege on any payment table or sequence, so the ledger is
    out of its reach even if a connection to the payments database were allowed."""
    payment_tables = _public_tables(payments_db["admin"]) - {"schema_migrations"}
    assert PAYMENT_TABLES <= payment_tables
    assert _public_sequences(payments_db["admin"])
    assert _privileges_held(payments_db["admin"], "runtime_svc") == []


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


MODES = ["agent_directed", "deterministic_retry", "durable_gateway"]


def _experiment(conn, experiment_id="e1"):
    conn.execute(
        "INSERT INTO experiments (id, name, manifest_sha256, config_json)"
        " VALUES (%s, 'test', 'abc', '{}')",
        (experiment_id,),
    )


def _run(conn, run_id="r1", experiment_id="e1", episode_id="ep", mode="agent_directed"):
    conn.execute(
        "INSERT INTO runs (id, experiment_id, episode_id, namespace, mode, thread_id, state)"
        " VALUES (%s, %s, %s, 'ns', %s, 't1', 'queued')",
        (run_id, experiment_id, episode_id, mode),
    )


@pytest.mark.parametrize("mode", MODES)
def test_every_defined_run_mode_is_accepted(runtime_db, mode):
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        _experiment(conn)
        _run(conn, mode=mode)


@pytest.mark.parametrize("mode", ["bogus", "Agent_Directed", ""])
def test_run_mode_is_constrained(runtime_db, mode):
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        _experiment(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            _run(conn, mode=mode)


def test_an_episode_runs_once_per_experiment(runtime_db):
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        _experiment(conn, "e1")
        _experiment(conn, "e2")
        _run(conn, "r1", "e1", "ep")
        with pytest.raises(psycopg.errors.UniqueViolation):
            _run(conn, "r2", "e1", "ep")
        # The same episode under another experiment is a different run.
        _run(conn, "r3", "e2", "ep")


def test_a_run_has_one_model_call_per_turn(runtime_db):
    insert = (
        "INSERT INTO model_calls (run_id, turn, provider_model_id, request_json)"
        " VALUES ('r1', %s, 'm', '{}')"
    )
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        _experiment(conn)
        _run(conn)
        conn.execute(insert, (1,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert, (1,))
        conn.execute(insert, (2,))


def test_a_run_reaches_each_crash_point_once(runtime_db):
    insert = "INSERT INTO crash_points (run_id, name, worker_pid) VALUES ('r1', %s, 1)"
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        _experiment(conn)
        _run(conn)
        conn.execute(insert, ("after_charge",))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert, ("after_charge",))
        conn.execute(insert, ("before_charge",))


def test_the_runtime_role_can_use_the_sequences_it_was_granted(runtime_db):
    """`experiments` has no sequence, so the write test above cannot notice a missing
    sequence grant. `model_calls` and `tool_events` take their ids from one."""
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        _experiment(conn)
        _run(conn)
        call_ids = [
            conn.execute(
                "INSERT INTO model_calls (run_id, turn, provider_model_id, request_json)"
                " VALUES ('r1', %s, 'm', '{}') RETURNING id",
                (turn,),
            ).fetchone()[0]
            for turn in (1, 2)
        ]
        event_id = conn.execute(
            "INSERT INTO tool_events (run_id, turn, tool, requested_args_json, rule, observation)"
            " VALUES ('r1', 1, 'charge', '{}', 'none', 'ok') RETURNING id"
        ).fetchone()[0]
    assert call_ids[0] < call_ids[1]
    assert event_id is not None


def test_deleting_an_experiment_removes_everything_under_it(runtime_db):
    children = ["runs", "model_calls", "tool_events", "crash_points", "results"]
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        _experiment(conn)
        _run(conn)
        conn.execute(
            "INSERT INTO model_calls (run_id, turn, provider_model_id, request_json)"
            " VALUES ('r1', 1, 'm', '{}')"
        )
        conn.execute(
            "INSERT INTO tool_events (run_id, turn, tool, requested_args_json, rule, observation)"
            " VALUES ('r1', 1, 'charge', '{}', 'none', 'ok')"
        )
        conn.execute("INSERT INTO crash_points (run_id, name, worker_pid) VALUES ('r1', 'x', 1)")
        conn.execute(
            "INSERT INTO results (run_id, evaluator_version, verdict, correct_recovery, score_json)"
            " VALUES ('r1', 'v1', 'ok', true, '{}')"
        )
        for table in children:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1, table

        conn.execute("DELETE FROM experiments WHERE id = 'e1'")

        for table in children:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def test_truncation_covers_tables_added_later_and_spares_the_migration_record(
    runtime_db, truncate_runtime_tables
):
    """The list of tables to empty between tests is read from the database, so a table a
    later migration adds is emptied too. schema_migrations is the one exception."""
    with psycopg.connect(runtime_db["admin"], autocommit=True) as conn:
        conn.execute("CREATE TABLE added_by_a_later_migration (id INTEGER PRIMARY KEY)")
        try:
            conn.execute("INSERT INTO added_by_a_later_migration VALUES (1)")
            _experiment(conn)
            recorded = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
            assert recorded > 0

            truncate_runtime_tables()

            assert (
                conn.execute("SELECT count(*) FROM added_by_a_later_migration").fetchone()[0] == 0
            )
            assert conn.execute("SELECT count(*) FROM experiments").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == recorded
        finally:
            conn.execute("DROP TABLE added_by_a_later_migration")
