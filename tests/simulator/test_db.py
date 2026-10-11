import uuid

import psycopg
import pytest

from lab import db
from lab.simulator import SIM_MIGRATIONS
from tests.conftest import ADMIN_URL, PAYMENT_SVC_PASSWORD


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
        "VALUES (1000, 'seed', 'charge', '{}', 200, '{}', 'committed', 1.0)",
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
            "outcome, server_ms) VALUES ('seed', 'charge', '{}', 200, '{}', 'committed', 1.0)"
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


# What the agent-facing service role must be refused. These are the writes that would let a
# compromised or buggy service rewrite the evidence the scorer reads. Each is valid against
# the seeded rows, so a missing privilege is the only thing that can make it fail.
SERVICE_DENIED = {
    "charges-UPDATE-amount": "UPDATE charges SET amount = amount + 1",
    "charges-UPDATE-key": "UPDATE charges SET idempotency_key = 'rewritten'",
    "charges-DELETE": "DELETE FROM charges",
    "charges-TRUNCATE": "TRUNCATE charges",
    "call_log-DELETE": "DELETE FROM call_log",
    "call_log-TRUNCATE": "TRUNCATE call_log",
    "call_log-UPDATE-call_index": "UPDATE call_log SET call_index = 99",
    "call_log-UPDATE-request": "UPDATE call_log SET request = '{}'",
    "call_log-UPDATE-key": "UPDATE call_log SET idempotency_key = 'rewritten'",
    "call_log-UPDATE-namespace": "UPDATE call_log SET namespace = 'seed'",
    "barriers-DELETE": "DELETE FROM barriers",
    "sim_namespaces-UPDATE-fault_plan": "UPDATE sim_namespaces SET fault_plan = '{}'",
    "sim_namespaces-UPDATE-profile": "UPDATE sim_namespaces SET profile = 'compat'",
}

# What it must still be allowed, or the simulator stops working.
SERVICE_ALLOWED = {
    "charges-INSERT": "INSERT INTO charges "
    "(namespace, idempotency_key, amount, args_hash, call_index) "
    "VALUES ('seed', 'k-new', 1, 'h', 2)",
    "call_log-INSERT": "INSERT INTO call_log (namespace, endpoint, request, http_status, "
    "response, outcome, server_ms) VALUES ('seed', 'charge', '{}', 0, '{}', 'in_flight', 0)",
    "call_log-UPDATE-result": "UPDATE call_log SET http_status = 200, response = '{}', "
    "outcome = 'committed', server_ms = 2.5",
    "barriers-upsert": "INSERT INTO barriers (namespace, name, reached_at) "
    "VALUES ('seed', 'b', now()) "
    "ON CONFLICT (namespace, name) DO UPDATE SET reached_at = now()",
    "barriers-release-upsert": "INSERT INTO barriers (namespace, name, released_at) "
    "VALUES ('seed', 'b2', now()) "
    "ON CONFLICT (namespace, name) DO UPDATE SET released_at = now()",
    "sim_namespaces-counter": "UPDATE sim_namespaces SET charge_calls = charge_calls + 1, "
    "status_calls = status_calls + 1",
}


@pytest.fixture
def seeded_service(seeded_ledger, payments_db):
    """The same one row per table as seeded_ledger, handed to the service role."""
    return payments_db["svc"]


@pytest.mark.parametrize("statement", SERVICE_DENIED.values(), ids=SERVICE_DENIED.keys())
def test_service_role_cannot_rewrite_or_remove_the_evidence(seeded_service, statement):
    # One autocommit connection per case, for the same reason as the reader tests above.
    with psycopg.connect(seeded_service, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)


@pytest.mark.parametrize("statement", SERVICE_ALLOWED.values(), ids=SERVICE_ALLOWED.keys())
def test_service_role_keeps_the_writes_the_simulator_needs(seeded_service, statement):
    with psycopg.connect(seeded_service, autocommit=True) as conn:
        conn.execute(statement)


def test_deleting_a_namespace_cascades_without_delete_on_the_child_tables(
    seeded_service, payments_db
):
    """The service role may delete a namespace, and its evidence goes with it by cascade.

    This is the one deletion path left, and it is why the control API still works. It also
    shows that dropping DELETE on the child tables did not break a namespace reset.
    """
    with psycopg.connect(seeded_service, autocommit=True) as conn:
        conn.execute("DELETE FROM sim_namespaces WHERE namespace = 'seed'")
    with psycopg.connect(payments_db["admin"], autocommit=True) as admin:
        for table in ("charges", "call_log", "barriers"):
            assert admin.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_existing_database_is_narrowed_by_the_new_migration(payments_db, tmp_path):
    """A database migrated before 0002 keeps working data and loses the extra privileges."""
    first_only = tmp_path / "migrations"
    first_only.mkdir()
    (first_only / "0001_init.sql").write_text((SIM_MIGRATIONS / "0001_init.sql").read_text())
    name = f"payments_upgrade_{uuid.uuid4().hex[:8]}"
    db.ensure_database(ADMIN_URL, name)
    try:
        admin_url = db.with_database(ADMIN_URL, name)
        svc_url = db.with_database(ADMIN_URL, name, "payment_svc", PAYMENT_SVC_PASSWORD)
        assert db.apply_migrations(admin_url, first_only) == ["0001_init.sql"]
        with psycopg.connect(svc_url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO sim_namespaces (namespace, profile, fault_plan) "
                "VALUES ('x', 'controlled', '{}')"
            )
            conn.execute("DELETE FROM call_log")  # allowed before, which is the exposure

        applied = db.apply_migrations(admin_url, SIM_MIGRATIONS)
        assert applied[0] == "0002_narrow_payment_svc_grants.sql"

        with psycopg.connect(svc_url, autocommit=True) as conn:
            assert conn.execute("SELECT count(*) FROM sim_namespaces").fetchone()[0] == 1
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM call_log")
    finally:
        db.drop_database(ADMIN_URL, name)


def _database_at(tmp_path, upto):
    """A scratch database migrated only as far as the named migration. Caller drops it."""
    only = tmp_path / "migrations"
    only.mkdir()
    for path in sorted(SIM_MIGRATIONS.glob("*.sql")):
        if path.name <= upto:
            (only / path.name).write_text(path.read_text())
    name = f"payments_upgrade_{uuid.uuid4().hex[:8]}"
    db.ensure_database(ADMIN_URL, name)
    admin_url = db.with_database(ADMIN_URL, name)
    db.apply_migrations(admin_url, only)
    return name, admin_url


_SEED_NS = (
    "INSERT INTO sim_namespaces (namespace, profile, fault_plan) VALUES ('x', 'compat', '{}')"
)
_SEED_ROW = (
    "INSERT INTO call_log (namespace, endpoint, request, http_status, response, outcome, "
    "server_ms) VALUES ('x', 'charge', '{}', 200, '{}', %s, 1.0)"
)


def test_outcome_check_is_added_to_an_existing_database_and_keeps_its_rows(payments_db, tmp_path):
    name, admin_url = _database_at(tmp_path, "0002_narrow_payment_svc_grants.sql")
    try:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(_SEED_NS)
            conn.execute(_SEED_ROW, ("committed",))
        assert db.apply_migrations(admin_url, SIM_MIGRATIONS) == ["0003_call_log_outcome_check.sql"]
        with psycopg.connect(admin_url, autocommit=True) as conn:
            assert conn.execute("SELECT count(*) FROM call_log").fetchone()[0] == 1
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(_SEED_ROW, ("commited",))
    finally:
        db.drop_database(ADMIN_URL, name)


def test_outcome_check_migration_refuses_to_run_over_an_unknown_outcome(payments_db, tmp_path):
    """A database holding a value the code never writes must fail loudly, and change nothing."""
    name, admin_url = _database_at(tmp_path, "0002_narrow_payment_svc_grants.sql")
    try:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(_SEED_NS)
            conn.execute(_SEED_ROW, ("ok",))
        with pytest.raises(psycopg.errors.CheckViolation):
            db.apply_migrations(admin_url, SIM_MIGRATIONS)
        with psycopg.connect(admin_url, autocommit=True) as conn:
            assert not conn.execute(
                "SELECT 1 FROM pg_constraint WHERE conname = 'call_log_outcome_check'"
            ).fetchall()
            applied = [r[0] for r in conn.execute("SELECT name FROM schema_migrations")]
            assert "0003_call_log_outcome_check.sql" not in applied
    finally:
        db.drop_database(ADMIN_URL, name)
