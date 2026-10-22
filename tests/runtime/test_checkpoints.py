"""LangGraph's checkpoint tables live in the runtime database, created as admin.

`runtime_svc` is the role the graph runs as. It must be able to checkpoint (read, write and
delete those tables) and must not be able to create anything, so the graph cannot quietly
build its own schema at run time.
"""

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.rows import dict_row
from typing_extensions import TypedDict

from lab.runtime.checkpoints import CHECKPOINT_TABLES, setup_checkpoint_tables


def _public_tables(url):
    with psycopg.connect(url, autocommit=True) as conn:
        return {
            row[0]
            for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }


def _svc_saver(url):
    return PostgresSaver.from_conn_string(url)


def test_setup_creates_exactly_the_tables_the_grant_list_names(runtime_db):
    # The grant list is a literal. If a langgraph upgrade adds a table, this fails instead
    # of leaving the table without a grant.
    tables = _public_tables(runtime_db["admin"])
    created_by_langgraph = tables - {
        "experiments",
        "runs",
        "model_calls",
        "tool_events",
        "crash_points",
        "results",
        "request_budget",
        "schema_migrations",
        "operations",
        "attempts",
        "recovery_events",
        "outbox",
        "model_invocations",
        "run_deliveries",
    }
    assert created_by_langgraph == set(CHECKPOINT_TABLES)


def test_setup_is_safe_to_run_again(runtime_db):
    setup_checkpoint_tables(runtime_db["admin"])
    setup_checkpoint_tables(runtime_db["admin"])
    assert set(CHECKPOINT_TABLES) <= _public_tables(runtime_db["admin"])


@pytest.mark.parametrize("table", CHECKPOINT_TABLES)
def test_the_runtime_role_holds_dml_on_every_checkpoint_table(runtime_db, table):
    with psycopg.connect(runtime_db["admin"], autocommit=True) as conn:
        held = {
            privilege
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
            if conn.execute(
                "SELECT has_table_privilege('runtime_svc', %s, %s)", (table, privilege)
            ).fetchone()[0]
        }
    assert held == {"SELECT", "INSERT", "UPDATE", "DELETE"}


class _State(TypedDict):
    value: int


def _counter_graph(saver):
    builder = StateGraph(_State)
    builder.add_node("bump", lambda s: {"value": s["value"] + 1})
    builder.add_edge(START, "bump")
    builder.add_edge("bump", END)
    return builder.compile(checkpointer=saver)


def test_the_runtime_role_can_checkpoint_resume_and_delete(runtime_db):
    config = {"configurable": {"thread_id": "t-checkpoint-grants"}}
    with _svc_saver(runtime_db["svc"]) as saver:
        graph = _counter_graph(saver)
        assert graph.invoke({"value": 1}, config, durability="sync") == {"value": 2}
        assert graph.get_state(config).values == {"value": 2}
        saver.delete_thread("t-checkpoint-grants")
        assert graph.get_state(config).created_at is None


def test_the_runtime_role_cannot_create_tables(runtime_db):
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE scratch (id INTEGER)")


def test_the_runtime_role_cannot_run_setup(runtime_db):
    """The graph never calls `setup()`, and could not: it needs CREATE."""
    with psycopg.connect(
        runtime_db["svc"], autocommit=True, prepare_threshold=0, row_factory=dict_row
    ) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            PostgresSaver(conn).setup()
