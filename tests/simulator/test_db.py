import psycopg
import pytest

from lab import db
from lab.simulator import SIM_MIGRATIONS


def test_with_database_swaps_name_and_credentials():
    url = "postgresql://postgres:postgres@localhost:54329/postgres"
    assert db.with_database(url, "payments") == (
        "postgresql://postgres:postgres@localhost:54329/payments"
    )
    assert db.with_database(url, "payments", "payment_svc", "pw") == (
        "postgresql://payment_svc:pw@localhost:54329/payments"
    )


def test_migrations_are_idempotent(payments_db):
    assert db.apply_migrations(payments_db["admin"], SIM_MIGRATIONS) == []


def test_ledger_reader_can_read_but_not_write(payments_db, svc_url):
    with psycopg.connect(payments_db["reader"], autocommit=True) as conn:
        conn.execute("SELECT count(*) FROM charges").fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO sim_namespaces (namespace, profile, fault_plan) "
                "VALUES ('x', 'controlled', '{}')"
            )


LEDGER_TABLES = ["sim_namespaces", "charges", "call_log"]

# One statement per (table, verb), each valid against the seeded rows below so that
# a missing privilege is the only thing that can make it fail. The BIGSERIAL inserts
# give an explicit id: otherwise nextval() is denied on the sequence (which the reader
# is never granted) and the table-level INSERT privilege would go untested.
WRITE_STATEMENTS = {
    "sim_namespaces": {
        "INSERT": "INSERT INTO sim_namespaces (namespace, profile, fault_plan) "
        "VALUES ('other', 'controlled', '{}')",
        "UPDATE": "UPDATE sim_namespaces SET charge_calls = charge_calls + 1",
        "DELETE": "DELETE FROM sim_namespaces",
    },
    "charges": {
        "INSERT": "INSERT INTO charges "
        "(id, namespace, idempotency_key, amount, args_hash, call_index) "
        "VALUES (1000, 'seed', 'k-new', 1, 'h', 2)",
        "UPDATE": "UPDATE charges SET amount = amount + 1",
        "DELETE": "DELETE FROM charges",
    },
    "call_log": {
        "INSERT": "INSERT INTO call_log (id, namespace, endpoint, request, http_status, "
        "response, outcome, server_ms) "
        "VALUES (1000, 'seed', 'charge', '{}', 200, '{}', 'ok', 1.0)",
        "UPDATE": "UPDATE call_log SET http_status = 500",
        "DELETE": "DELETE FROM call_log",
    },
    "barriers": {
        "INSERT": "INSERT INTO barriers (namespace, name) VALUES ('seed', 'b-new')",
        "UPDATE": "UPDATE barriers SET reached_at = now()",
        "DELETE": "DELETE FROM barriers",
    },
}
WRITE_CASES = [
    pytest.param(sql, id=f"{table}-{verb}")
    for table, verbs in WRITE_STATEMENTS.items()
    for verb, sql in verbs.items()
]


@pytest.fixture
def seeded_ledger(payments_db, svc_url):
    """One row in every table, written by the admin role, for the reader to attack."""
    with psycopg.connect(payments_db["admin"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO sim_namespaces (namespace, profile, fault_plan) "
            "VALUES ('seed', 'controlled', '{}')"
        )
        conn.execute(
            "INSERT INTO charges (namespace, idempotency_key, amount, args_hash, call_index) "
            "VALUES ('seed', 'k', 1, 'h', 1)"
        )
        conn.execute(
            "INSERT INTO call_log (namespace, endpoint, request, http_status, response, "
            "outcome, server_ms) VALUES ('seed', 'charge', '{}', 200, '{}', 'ok', 1.0)"
        )
        conn.execute("INSERT INTO barriers (namespace, name) VALUES ('seed', 'b')")
    return payments_db["reader"]


@pytest.mark.parametrize("table", LEDGER_TABLES)
def test_ledger_reader_can_select_every_ledger_table(seeded_ledger, table):
    with psycopg.connect(seeded_ledger, autocommit=True) as conn:
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1


@pytest.mark.parametrize("statement", WRITE_CASES)
def test_ledger_reader_cannot_write_any_ledger_table(seeded_ledger, statement):
    # A failed statement aborts its transaction and later statements on the same
    # connection would raise InFailedSqlTransaction. Every case therefore opens its
    # own autocommit connection, so the privilege check is the only thing tested.
    with psycopg.connect(seeded_ledger, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)


def test_payment_service_role_can_write(svc_url):
    with psycopg.connect(svc_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO sim_namespaces (namespace, profile, fault_plan) "
            "VALUES ('x', 'controlled', '{}')"
        )
        assert conn.execute("SELECT count(*) FROM sim_namespaces").fetchone()[0] == 1
