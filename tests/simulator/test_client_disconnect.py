"""docs/protocol.md promises that a client disconnecting mid-request does not change the
outcome: the simulator still completes the request and records the result.

Two tests pin that promise from opposite ends. The structural one pins the reason it holds
today. The behavioural one pins the promise itself against a real server.
"""

import inspect
import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.routing import APIRoute
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from lab.protocol.faults import FaultPlan
from lab.protocol.task import Profile
from lab.simulator import service
from lab.simulator.app import create_internal_app, create_public_app
from tests.helpers import CHARGE, charges

TOKEN = "client-t"
DISCONNECT_GRACE_S = 0.5


def _pool(url):
    return ConnectionPool(
        url,
        min_size=1,
        max_size=5,
        open=True,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )


def _handlers(app):
    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    assert routes, "no API routes found, so this test would pass vacuously"
    return {(route.path, tuple(sorted(route.methods))): route.endpoint for route in routes}


@pytest.mark.parametrize("which", ["public", "internal"])
def test_request_handlers_are_synchronous_functions(svc_url, which):
    """The disconnect guarantee in docs/protocol.md depends on this, so do not "fix" it.

    Starlette runs a plain `def` handler in a worker thread, and a thread cannot be
    cancelled: once the request has started, it runs to its end whether or not the client
    is still there, so the effect commits and the call-log row is finalised. An
    `async def` handler runs on the event loop instead, where anything that cancels the
    request task (a client disconnect seen by a middleware, a timeout, an async database
    driver awaiting mid-call) interrupts it between two awaits. That can leave a slot
    claimed and a row `in_flight` for a request that would otherwise have finished, and
    `in_flight` is exactly what the crash measurement reads as a crash.

    If you are converting a handler to async on purpose, the guarantee has to be rebuilt
    first (shield the work from cancellation, finalise the row in a `finally`) and this
    test and docs/protocol.md changed in the same commit. The behavioural test below
    shows whether it was.
    """
    pool = _pool(svc_url)
    try:
        app = (create_public_app if which == "public" else create_internal_app)(pool, TOKEN)
        handlers = _handlers(app)
        coroutine_handlers = sorted(
            path for (path, _), fn in handlers.items() if inspect.iscoroutinefunction(fn)
        )
        assert coroutine_handlers == [], (
            f"async handlers on the {which} app break the client-disconnect guarantee in "
            f"docs/protocol.md: {coroutine_handlers}"
        )
    finally:
        pool.close()


@pytest.fixture
def live_public_server(svc_url):
    """The public app on a real uvicorn server, so a socket close is a real disconnect.

    TestClient calls the app in-process and has no connection to drop, which is why this
    cannot use it.
    """
    pool = _pool(svc_url)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    config = uvicorn.Config(
        create_public_app(pool, TOKEN), log_level="warning", lifespan="off", loop="asyncio"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "the test server did not start"
    yield server, listener.getsockname()[1]
    server.should_exit = True
    thread.join(10)
    assert not thread.is_alive(), "the test server did not stop"
    listener.close()
    pool.close()


def _wait_until(predicate, what, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def _call_log(conn, ns):
    return conn.execute(
        "SELECT call_index, http_status, outcome FROM call_log WHERE namespace = %s ORDER BY id",
        (ns,),
    ).fetchall()


@pytest.mark.parametrize("barrier", ["before_commit:1", "after_commit:1"])
def test_charge_completes_after_the_client_disconnects(conn, live_public_server, barrier):
    """Hold a charge, hang up on it, release it: the effect and its record still land.

    Held before the commit, the effect exists only if the request survived the
    disconnect. Held after the commit, the effect exists already, but the finished
    call-log row does only if the request survived to write it.
    """
    server, port = live_public_server
    hold = "hold_before_commit" if barrier.startswith("before") else "hold_after_commit"
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan(**{hold: (1,)}))

    body = json.dumps(CHARGE).encode()
    request = (
        b"POST /v1/charges HTTP/1.1\r\nHost: localhost\r\n"
        + f"Authorization: Bearer {TOKEN}\r\nX-Lab-Namespace: ns\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )
    sock = socket.create_connection(("127.0.0.1", port))
    try:
        sock.sendall(request)
        _wait_until(
            lambda: service.barrier_state(conn, "ns", barrier)["reached"],
            f"the request to reach {barrier}",
        )
        # Held: the slot is claimed and the row is in flight, nothing has been answered.
        assert [r["outcome"] for r in _call_log(conn, "ns")] == [service.IN_FLIGHT]
        assert len(server.server_state.tasks) == 1
    finally:
        # Reset rather than a polite close, so the server sees the connection die at once.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        sock.close()

    # Wait until the server has noticed the client is gone, so the release below is
    # provably after the disconnect and not racing it. Then give anything that reacts to a
    # disconnect (a handler polling for it, a middleware) time to do so. The pause only
    # makes the test stricter: a correct server is still holding the request afterwards.
    _wait_until(lambda: not server.server_state.connections, "the server to see the disconnect")
    time.sleep(DISCONNECT_GRACE_S)
    assert len(server.server_state.tasks) == 1, "the request ended before it was released"

    service.release_barrier(conn, "ns", barrier)
    _wait_until(lambda: not server.server_state.tasks, "the abandoned request to finish")

    assert [(r["call_index"], r["http_status"], r["outcome"]) for r in _call_log(conn, "ns")] == [
        (1, 200, "committed")
    ]
    assert [row["idempotency_key"] for row in charges(conn, "ns")] == ["k1"]
