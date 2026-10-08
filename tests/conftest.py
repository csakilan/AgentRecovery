import os
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from lab import db
from lab.simulator import SIM_MIGRATIONS

ADMIN_URL = os.environ.get(
    "LAB_PG_ADMIN_URL", "postgresql://postgres:postgres@localhost:54329/postgres"
)
PAYMENT_SVC_PASSWORD = os.environ.get("LAB_PAYMENT_SVC_PASSWORD", "dev-payment-svc")
LEDGER_READER_PASSWORD = os.environ.get("LAB_LEDGER_READER_PASSWORD", "dev-ledger-reader")


@pytest.fixture(scope="session")
def payments_db():
    name = f"payments_test_{uuid.uuid4().hex[:8]}"
    db.ensure_role(ADMIN_URL, "payment_svc", PAYMENT_SVC_PASSWORD)
    db.ensure_role(ADMIN_URL, "ledger_reader", LEDGER_READER_PASSWORD)
    db.ensure_database(ADMIN_URL, name)
    admin_url = db.with_database(ADMIN_URL, name)
    db.apply_migrations(admin_url, SIM_MIGRATIONS)
    yield {
        "admin": admin_url,
        "svc": db.with_database(ADMIN_URL, name, "payment_svc", PAYMENT_SVC_PASSWORD),
        "reader": db.with_database(ADMIN_URL, name, "ledger_reader", LEDGER_READER_PASSWORD),
    }
    db.drop_database(ADMIN_URL, name)


@pytest.fixture
def svc_url(payments_db):
    with psycopg.connect(payments_db["admin"], autocommit=True) as conn:
        conn.execute(
            "TRUNCATE sim_namespaces, charges, call_log, barriers RESTART IDENTITY CASCADE"
        )
    return payments_db["svc"]


@pytest.fixture
def conn(svc_url):
    with psycopg.connect(svc_url, autocommit=True, row_factory=dict_row) as c:
        yield c
