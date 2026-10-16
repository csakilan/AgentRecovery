"""Fixtures for the runner tests: a real simulator on loopback and a `RunnerConfig` for it.

Both simulator apps (public and control) run under uvicorn on background threads against the
throwaway payments database, the way tests/agent/test_replay.py does it. The runner talks to
them over HTTP exactly as it would to the Compose simulator, which these tests never use.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
import uvicorn
from fastapi import FastAPI
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from lab.runner.episode import RunnerConfig
from lab.simulator.app import create_internal_app, create_public_app

CLIENT_TOKEN = "client-runner-test"
CONTROL_TOKEN = "control-runner-test"
EXPERIMENT_NAME_PREFIX = "runner-test"
# Experiment names the CLI tests produce (they cannot choose a name, so they cannot use the prefix).
CLI_EXPERIMENT_NAMES = ("cli", "from-file")


@contextmanager
def serve(app: FastAPI) -> Iterator[str]:
    """Serve `app` on an ephemeral loopback port and yield its base URL."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="off", loop="asyncio")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "the simulator did not start"
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(15)
        listener.close()
    assert not thread.is_alive(), "the simulator did not stop"


@pytest.fixture(scope="module")
def simulator(payments_db) -> Iterator[dict[str, str]]:
    pool = ConnectionPool(
        payments_db["svc"],
        min_size=1,
        max_size=8,
        open=True,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    try:
        with (
            serve(create_public_app(pool, CLIENT_TOKEN)) as public_url,
            serve(create_internal_app(pool, CONTROL_TOKEN)) as control_url,
        ):
            yield {"public": public_url, "control": control_url}
    finally:
        pool.close()


@pytest.fixture
def runner_config(simulator, payments_db, runtime_db, svc_url) -> RunnerConfig:
    """`svc_url` empties the payments tables first, so every test starts from a clean ledger."""
    return RunnerConfig(
        sim_public_url=simulator["public"],
        sim_control_url=simulator["control"],
        client_token=CLIENT_TOKEN,
        control_token=CONTROL_TOKEN,
        runtime_url=runtime_db["svc"],
        ledger_url=payments_db["reader"],
        max_turns=10,
        episode_deadline_s=300.0,
    )


@pytest.fixture(autouse=True)
def clean_experiments(runtime_db) -> Iterator[None]:
    """Delete the experiments a test made (named with the prefix, or by the CLI) afterwards, which
    cascades to runs and results. Autouse, so a test that never asks for `rt` is cleaned too."""
    yield
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        conn.execute(
            "DELETE FROM experiments WHERE name LIKE %s OR name = ANY(%s)",
            (EXPERIMENT_NAME_PREFIX + "%", list(CLI_EXPERIMENT_NAMES)),
        )


@pytest.fixture
def rt(runtime_db) -> Iterator[psycopg.Connection]:
    """A runtime_svc connection that reads rows as dicts."""
    with psycopg.connect(runtime_db["svc"], autocommit=True, row_factory=dict_row) as conn:
        yield conn
