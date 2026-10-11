import psycopg
import pytest


@pytest.fixture(autouse=True)
def _empty_runtime_tables(runtime_db):
    """Give every runtime test an empty database, as `svc_url` does for payments.

    `runtime_db` is session-scoped, so without this the rows one test inserts would
    collide with the next. TRUNCATE needs the admin connection: `runtime_svc` is
    deliberately not granted it. `schema_migrations` is left alone.
    """
    with psycopg.connect(runtime_db["admin"], autocommit=True) as conn:
        conn.execute(
            "TRUNCATE experiments, runs, model_calls, tool_events, crash_points,"
            " results, request_budget RESTART IDENTITY CASCADE"
        )
