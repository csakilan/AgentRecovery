import inspect

import psycopg
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

from lab.execution.types import LeaseLost
from lab.runner.ownership import claim_run
from lab.runtime.fenced_checkpoints import FencedPostgresSaver


@pytest.fixture
def claimed(runtime_conn):
    runtime_conn.execute(
        "INSERT INTO experiments(id,name,manifest_sha256,config_json) "
        "VALUES('e','fenced-test','s','{}')"
    )
    runtime_conn.execute(
        "INSERT INTO runs(id,experiment_id,episode_id,namespace,mode,thread_id,state,"
        "resume_version,"
        "episode_spec_json,worker_input_json,execution_config_json,initial_messages_json,model_descriptor_json,"
        "setup_state,started_at,deadline_at) "
        "VALUES('r','e','ep','ns','agent_directed','thread','queued',1,"
        "'{}','{}','{}','{}','{}','ready',clock_timestamp(),clock_timestamp()+interval '5 minutes')"
    )
    return claim_run(runtime_conn, "r", "worker")


def test_stale_checkpoint_and_pending_writes_rejected(runtime_db, runtime_conn, claimed):
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        saver = FencedPostgresSaver(conn, claimed)
        config = {"configurable": {"thread_id": "thread", "checkpoint_ns": ""}}
        checkpoint = empty_checkpoint()
        config = saver.put(config, checkpoint, {}, {})
        saver.put_writes(config, [("messages", ["fresh"])], "task")
        before = {
            t: runtime_conn.execute(f"SELECT * FROM {t}").fetchall()
            for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
        }
        runtime_conn.execute(
            "UPDATE runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id='r'"
        )
        assert claim_run(runtime_conn, "r", "replacement").version > claimed.version
        with pytest.raises(LeaseLost):
            saver.put(config, empty_checkpoint(), {}, {})
        with pytest.raises(LeaseLost):
            saver.put_writes(config, [("messages", ["stale"])], "task")
        for table, rows in before.items():
            assert runtime_conn.execute(f"SELECT * FROM {table}").fetchall() == rows


def test_expiry_inside_transaction_rolls_back(runtime_db, runtime_conn, claimed):
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        saver = FencedPostgresSaver(conn, claimed)
        with pytest.raises(LeaseLost), saver._cursor(pipeline=True) as cur:
            cur.execute(
                "UPDATE runs SET lease_expires_at=clock_timestamp()-interval '1 second' "
                "WHERE id='r'"
            )
            cur.execute(
                "INSERT INTO checkpoint_blobs(thread_id,checkpoint_ns,channel,version,type,blob) "
                "VALUES('thread','','messages','v','json','x')"
            )
            cur.execute(
                "INSERT INTO checkpoints(thread_id,checkpoint_ns,checkpoint_id,"
                "checkpoint,metadata) "
                "VALUES('thread','','c','{}','{}')"
            )
            cur.execute(
                "INSERT INTO checkpoint_writes(thread_id,checkpoint_ns,checkpoint_id,"
                "task_id,idx,channel,type,blob) "
                "VALUES('thread','','c','task',0,'messages','json','x')"
            )
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            assert runtime_conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_installed_writes_use_atomic_cursor():
    for method in (PostgresSaver.put, PostgresSaver.put_writes):
        assert "self._cursor(pipeline=True)" in inspect.getsource(method)


def test_wrong_thread_and_administration_disabled(runtime_db, claimed):
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        saver = FencedPostgresSaver(conn, claimed)
        with pytest.raises(ValueError):
            saver.get_tuple({"configurable": {"thread_id": "other"}})
        with pytest.raises(ValueError):
            list(saver.list(None))
        with pytest.raises(ValueError):
            saver.setup()
        with pytest.raises(ValueError):
            saver.delete_thread("thread")


def test_real_graph_round_trip(runtime_db, claimed):
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class State(TypedDict):
        value: int

    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        saver = FencedPostgresSaver(conn, claimed)
        builder = StateGraph(State)
        builder.add_node("increment", lambda state: {"value": state["value"] + 1})
        builder.add_edge(START, "increment")
        builder.add_edge("increment", END)
        graph = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "thread"}}
        assert graph.invoke({"value": 1}, config, durability="sync") == {"value": 2}
        assert graph.get_state(config).values == {"value": 2}
        assert list(saver.list(config))
