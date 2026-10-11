import psycopg
import pytest
from psycopg import sql


@pytest.fixture(scope="session")
def truncate_runtime_tables(runtime_db):
    """Empty every runtime table. The list comes from the database, so a table a later
    migration adds is cleaned without anyone remembering to edit this file.

    `schema_migrations` is the one table left alone: truncating it would make
    `apply_migrations` run 0001 again and fail with "table already exists", which would
    break the idempotence test for the wrong reason. TRUNCATE needs the admin connection,
    because `runtime_svc` is deliberately not granted it.
    """

    def truncate() -> None:
        with psycopg.connect(runtime_db["admin"], autocommit=True) as conn:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT tablename FROM pg_tables"
                    " WHERE schemaname = 'public' AND tablename <> 'schema_migrations'"
                    " ORDER BY tablename"
                )
            ]
            if tables:
                conn.execute(
                    sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                        sql.SQL(", ").join(sql.Identifier(t) for t in tables)
                    )
                )

    return truncate


@pytest.fixture(autouse=True)
def _empty_runtime_tables(truncate_runtime_tables):
    """Give every runtime test an empty database, as `svc_url` does for payments.

    `runtime_db` is session-scoped, so without this the rows one test inserts would
    collide with the next.
    """
    truncate_runtime_tables()


@pytest.fixture
def runtime_conn(runtime_db, _empty_runtime_tables):
    """A service-role connection with default (tuple) rows, as the runtime code indexes
    results by position.

    It lives here, below `_empty_runtime_tables`, because it relies on that fixture to hand
    it empty tables, and asks for it by name so the order is enforced rather than assumed.
    Defined in the root conftest it would be visible to tests that have no such fixture and
    would silently share rows between them.
    """
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        yield conn
