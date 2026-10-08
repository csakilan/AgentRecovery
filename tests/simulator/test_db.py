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


def test_payment_service_role_can_write(svc_url):
    with psycopg.connect(svc_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO sim_namespaces (namespace, profile, fault_plan) "
            "VALUES ('x', 'controlled', '{}')"
        )
        assert conn.execute("SELECT count(*) FROM sim_namespaces").fetchone()[0] == 1
