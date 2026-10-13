import os
import uuid

# Before any test module imports langgraph or langsmith: no episode state may be uploaded to
# LangSmith, whatever the shell has set. Set here directly (not through lab.runner) so the
# test session is safe even if the runner's own guard were removed; test_no_live_calls.py
# checks both.
for _tracing_variable in (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
):
    os.environ[_tracing_variable] = "false"

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from lab import db  # noqa: E402
from lab.simulator import SIM_MIGRATIONS  # noqa: E402

# The session-wide network guard and the fixture that fails a test which tripped it. Both
# are autouse; see tests/network_guard.py.
from tests.network_guard import (  # noqa: E402, F401
    fail_on_unacknowledged_network_attempts,
    network_guard,
)

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
    try:
        admin_url = db.with_database(ADMIN_URL, name)
        db.apply_migrations(admin_url, SIM_MIGRATIONS)
        yield {
            "name": name,
            "admin": admin_url,
            "svc": db.with_database(ADMIN_URL, name, "payment_svc", PAYMENT_SVC_PASSWORD),
            "reader": db.with_database(ADMIN_URL, name, "ledger_reader", LEDGER_READER_PASSWORD),
        }
    finally:
        # Also when a migration fails during setup, which would otherwise leak the database.
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


RUNTIME_SVC_PASSWORD = os.environ.get("LAB_RUNTIME_SVC_PASSWORD", "dev-runtime-svc")


@pytest.fixture(scope="session")
def runtime_db():
    from lab.runtime import RUNTIME_MIGRATIONS
    from lab.runtime.checkpoints import setup_checkpoint_tables

    name = f"runtime_test_{uuid.uuid4().hex[:8]}"
    db.ensure_role(ADMIN_URL, "runtime_svc", RUNTIME_SVC_PASSWORD)
    db.ensure_database(ADMIN_URL, name)
    try:
        admin_url = db.with_database(ADMIN_URL, name)
        db.apply_migrations(admin_url, RUNTIME_MIGRATIONS)
        setup_checkpoint_tables(admin_url)
        yield {
            "name": name,
            "admin": admin_url,
            "svc": db.with_database(ADMIN_URL, name, "runtime_svc", RUNTIME_SVC_PASSWORD),
        }
    finally:
        # Also when a migration fails during setup, which would otherwise leak the database.
        db.drop_database(ADMIN_URL, name)


ROLE_PASSWORDS = {
    "payment_svc": PAYMENT_SVC_PASSWORD,
    "ledger_reader": LEDGER_READER_PASSWORD,
    "runtime_svc": RUNTIME_SVC_PASSWORD,
}


@pytest.fixture(scope="session")
def role_url():
    """Build a connection URL for any of the lab's login roles on any database. The roles
    exist once `payments_db` and `runtime_db` have both been requested."""

    def build(role: str, dbname: str) -> str:
        return db.with_database(ADMIN_URL, dbname, role, ROLE_PASSWORDS[role])

    return build


@pytest.fixture(scope="session")
def admin_url():
    """The superuser connection URL (to the maintenance database)."""
    return ADMIN_URL


@pytest.fixture(scope="session")
def runtime_svc_password():
    return RUNTIME_SVC_PASSWORD


@pytest.fixture(autouse=True)
def isolate_tracing(monkeypatch):
    """No test inherits an OpenTelemetry setting from the shell, and none leaves a tracing
    provider behind (lab.obs keeps its own, not OpenTelemetry's process-global one)."""
    from lab import obs

    for name in [n for n in os.environ if n.startswith("OTEL_")]:
        monkeypatch.delenv(name)
    obs.reset()
    yield
    obs.reset()
