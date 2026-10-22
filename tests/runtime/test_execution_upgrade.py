"""Upgrade real legacy records without inventing resumable input."""

import shutil
import uuid

import psycopg

from lab import db
from lab.runtime import RUNTIME_MIGRATIONS


def test_execution_upgrade_preserves_legacy_rows(admin_url, runtime_db, tmp_path):
    name = "runtime_upgrade_" + uuid.uuid4().hex[:8]
    url = db.with_database(admin_url, name)
    db.ensure_database(admin_url, name)
    try:
        for path in sorted(RUNTIME_MIGRATIONS.glob("*.sql"))[:3]:
            shutil.copy(path, tmp_path / path.name)
        db.apply_migrations(url, tmp_path)
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("INSERT INTO experiments VALUES('e','legacy','sha','{}',NULL,now())")
            conn.execute(
                "INSERT INTO runs(id,experiment_id,episode_id,namespace,mode,thread_id,state) "
                "VALUES('r','e','ep','n','agent_directed','t','completed')"
            )
            conn.execute(
                "INSERT INTO model_calls(run_id,turn,provider_model_id,request_json,response_json) "
                "VALUES('r',1,'scripted','{}','{\"content\":\"original\"}')"
            )
            conn.execute(
                "INSERT INTO tool_events(run_id,turn,tool,requested_args_json,rule,observation) "
                "VALUES('r',1,'charge','{}','agent_directed','original')"
            )
            conn.execute(
                "INSERT INTO results(run_id,evaluator_version,verdict,correct_recovery,score_json) "
                "VALUES('r','v1','correct',true,'{}')"
            )
            before = {
                t: conn.execute(f"SELECT * FROM {t}").fetchall()
                for t in ("model_calls", "tool_events", "results")
            }
            run_before = conn.execute("SELECT * FROM runs").fetchone()
        assert db.apply_migrations(url, RUNTIME_MIGRATIONS) == ["0004_execution_recovery.sql"]
        assert db.apply_migrations(url, RUNTIME_MIGRATIONS) == []
        with psycopg.connect(url, autocommit=True) as conn:
            assert conn.execute("SELECT resume_version FROM runs").fetchone() == (None,)
            assert conn.execute("SELECT * FROM runs").fetchone()[: len(run_before)] == run_before
            for table, rows in before.items():
                after = conn.execute(f"SELECT * FROM {table}").fetchall()
                assert [r[: len(rows[0])] for r in after] == rows
            assert conn.execute("SELECT count(*) FROM model_invocations").fetchone() == (0,)
    finally:
        db.drop_database(admin_url, name)
