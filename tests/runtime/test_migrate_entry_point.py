import uuid

import psycopg
import pytest

from lab import db
from lab.runtime.__main__ import main
from lab.runtime.checkpoints import CHECKPOINT_TABLES


@pytest.fixture
def scratch_runtime_name(monkeypatch, admin_url, runtime_svc_password):
    """Point the entry point at a throwaway database name, and drop it afterwards."""
    name = f"runtime_cli_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("LAB_PG_ADMIN_URL", admin_url)
    monkeypatch.setenv("LAB_RUNTIME_DB", name)
    monkeypatch.setenv("LAB_RUNTIME_SVC_PASSWORD", runtime_svc_password)
    try:
        yield name, admin_url
    finally:
        db.drop_database(admin_url, name)


def test_migrate_creates_the_database_named_by_the_environment(
    scratch_runtime_name, capsys, runtime_svc_password
):
    name, admin = scratch_runtime_name
    svc_password = runtime_svc_password
    main(["lab.runtime", "migrate"])
    assert "0001_init.sql" in capsys.readouterr().out
    with psycopg.connect(db.with_database(admin, name), autocommit=True) as conn:
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables")}
    assert {"experiments", "runs", "model_calls", "schema_migrations"} <= tables
    # LangGraph's checkpoint tables are created by the same command, and runtime_svc can use
    # them: it is the role the graph connects as.
    assert set(CHECKPOINT_TABLES) <= tables
    with psycopg.connect(db.with_database(admin, name, "runtime_svc", svc_password)) as conn:
        for table in CHECKPOINT_TABLES:
            conn.execute(f"SELECT count(*) FROM {table}")


def test_migrate_is_safe_to_run_twice(scratch_runtime_name, capsys):
    main(["lab.runtime", "migrate"])
    capsys.readouterr()
    main(["lab.runtime", "migrate"])
    assert "nothing new" in capsys.readouterr().out


def test_an_unknown_command_is_refused():
    with pytest.raises(SystemExit):
        main(["lab.runtime", "serve"])
