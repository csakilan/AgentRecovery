"""LangGraph's checkpoint tables, created as admin and handed to `runtime_svc` for DML only.

The checkpoints live in the runtime database, beside `runs` and `tool_events`, so there is
one database to snapshot and one set of grants to reason about. LangGraph owns the table
definitions, so they are not in `migrations/*.sql`: `PostgresSaver.setup()` creates them and
keeps its own `checkpoint_migrations` table. It builds three indexes with
`CREATE INDEX CONCURRENTLY`, which cannot run inside a transaction, so the connection here
is autocommit (the same settings `PostgresSaver.from_conn_string` uses).

`setup()` is idempotent and so are the grants, so this runs on every `migrate`. At run time
the graph connects as `runtime_svc`, which can read and write these tables but cannot create
anything, and never calls `setup()`.
"""

from __future__ import annotations

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql
from psycopg.rows import dict_row

# What `PostgresSaver.setup()` creates in langgraph-checkpoint-postgres 3.1.2
# (BasePostgresSaver.MIGRATIONS). tests/runtime/test_checkpoints.py compares this to the
# catalog after setup, so a library upgrade that adds a table fails a test instead of
# leaving the table ungranted.
CHECKPOINT_TABLES = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)

SERVICE_ROLE = "runtime_svc"


def setup_checkpoint_tables(runtime_admin_url: str) -> None:
    """Create LangGraph's checkpoint tables in the database at `runtime_admin_url`, and
    grant `runtime_svc` SELECT, INSERT, UPDATE and DELETE on them plus its sequence rights.

    `runtime_admin_url` must be an owner/superuser connection to the runtime database, and
    the `runtime_svc` role must already exist (`_migrate()` and the test fixture both create
    it first).
    """
    with psycopg.connect(
        runtime_admin_url, autocommit=True, prepare_threshold=0, row_factory=dict_row
    ) as conn:
        PostgresSaver(conn).setup()
        role = sql.Identifier(SERVICE_ROLE)
        tables = sql.SQL(", ").join(sql.Identifier(t) for t in CHECKPOINT_TABLES)
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(tables, role)
        )
        conn.execute(
            sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role)
        )
