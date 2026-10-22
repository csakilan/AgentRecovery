"""Isolated runtime fixtures; pure executor tests do not request a database."""

import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from lab.agent.client import TrustedContext


@pytest.fixture
def journal_conn(runtime_db):
    with psycopg.connect(runtime_db["svc"], autocommit=True, row_factory=dict_row) as conn:
        yield conn


@pytest.fixture
def journal_ctx(journal_conn):
    token = uuid.uuid4().hex
    journal_conn.execute(
        "INSERT INTO experiments(id,name,manifest_sha256,config_json) VALUES(%s,%s,'sha','{}')",
        (token, "journal-test"),
    )
    journal_conn.execute(
        "INSERT INTO runs(id,experiment_id,episode_id,namespace,mode,thread_id,state) "
        "VALUES(%s,%s,'episode',%s,'durable_gateway',%s,'queued')",
        (token, token, token, token),
    )
    yield TrustedContext(
        tenant="lab", namespace=token, run_id=token, episode_id="episode", task_id="charge"
    )
    journal_conn.execute("DELETE FROM experiments WHERE id=%s", (token,))
