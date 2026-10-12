"""The simulator client takes its namespace from the trusted context and nothing else.

Most tests run the client over an `httpx.MockTransport` that records every request, so they
can state exactly what went on the wire (or that nothing did). The last group runs it against
the real public app and a throwaway database, to show where a charge and a status lookup land.
"""

import dataclasses
import inspect
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from lab.agent.client import (
    DENIED_STATUS,
    NAMESPACE_DENIED,
    Denial,
    SimulatorClient,
    TrustedContext,
)
from lab.simulator.app import create_internal_app, create_public_app
from tests.helpers import CHARGE, charges

TOKEN = "client-t"
CONTROL = {"Authorization": "Bearer control-t"}
CTX = TrustedContext(
    tenant="tenant-1",
    namespace="ns-trusted",
    run_id="run-1",
    episode_id="ep-1",
    task_id="task-1",
)


def make_client(handler=None, ctx=CTX, **kwargs):
    """A SimulatorClient over a MockTransport. Returns (client, the list of requests seen)."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request) if handler else httpx.Response(200, json={"ok": True})

    http = httpx.Client(transport=httpx.MockTransport(record))
    return SimulatorClient("http://sim:8100", TOKEN, ctx, http_client=http, **kwargs), seen


# TrustedContext


def test_trusted_context_is_frozen_strict_and_complete():
    with pytest.raises(ValidationError):
        CTX.namespace = "other"
    with pytest.raises(ValidationError):
        TrustedContext(**CTX.model_dump(), extra_field="x")
    for name in ("tenant", "namespace", "run_id", "episode_id", "task_id"):
        fields = CTX.model_dump()
        fields[name] = ""
        with pytest.raises(ValidationError):
            TrustedContext(**fields)
        del fields[name]
        with pytest.raises(ValidationError):
            TrustedContext(**fields)
    with pytest.raises(ValidationError):
        TrustedContext(**{**CTX.model_dump(), "namespace": 7})


# The namespace header always equals ctx.namespace


# Arguments that are forwarded: none has a namespace-like top-level key, but several mention
# one in a place that is not a key, or in a key that is not about a namespace.
FORWARDED_ARGS = [
    {"amount": 49.99, "idempotency_key": "k1"},
    {"amount": 49.99, "idempotency_key": "ns-other", "order_id": "namespace"},
    {"amount": 1, "idempotency_key": "k", "metadata": {"namespace": "ns-other"}},
    {"amount": 1, "idempotency_key": "k", "tenant": "ns-other", "run_id": "ns-other"},
    {"amount": 1, "idempotency_key": "k", "X-Lab-Header": "ns-other", "name": "space"},
    {},
]


@pytest.mark.parametrize("args", FORWARDED_ARGS)
def test_the_namespace_header_is_the_contexts_whatever_the_charge_args_contain(args):
    client, seen = make_client()
    client.charge(args)
    assert len(seen) == 1
    assert seen[0].headers["X-Lab-Namespace"] == CTX.namespace
    assert json.loads(seen[0].content) == args


@pytest.mark.parametrize("key", ["ns-other", "namespace", "X-Lab-Namespace", "k1", "a b"])
def test_the_namespace_header_is_the_contexts_whatever_the_status_key_is(key):
    client, seen = make_client()
    client.status(key)
    assert [r.headers["X-Lab-Namespace"] for r in seen] == [CTX.namespace]


def test_each_client_uses_its_own_contexts_namespace():
    other = CTX.model_copy(update={"namespace": "ns-other"})
    first, seen_first = make_client(ctx=CTX)
    second, seen_second = make_client(ctx=other)
    first.status("k")
    second.status("k")
    assert seen_first[0].headers["X-Lab-Namespace"] == "ns-trusted"
    assert seen_second[0].headers["X-Lab-Namespace"] == "ns-other"


def test_a_namespace_header_already_on_an_injected_client_is_overridden():
    seen = []
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: (seen.append(r), httpx.Response(200, json={}))[1]),
        headers={"X-Lab-Namespace": "ns-planted", "Authorization": "Bearer planted"},
    )
    client = SimulatorClient("http://sim:8100", TOKEN, CTX, http_client=http)
    client.charge({"amount": 1, "idempotency_key": "k"})
    client.status("k")
    for request in seen:
        assert request.headers.get_list("X-Lab-Namespace") == [CTX.namespace]
        assert request.headers.get_list("Authorization") == [f"Bearer {TOKEN}"]


def test_every_request_carries_the_client_token():
    client, seen = make_client()
    client.charge({"amount": 1, "idempotency_key": "k"})
    client.status("k")
    assert [r.headers["Authorization"] for r in seen] == [f"Bearer {TOKEN}"] * 2


def test_a_charge_posts_to_the_charges_route_and_a_status_gets_the_payments_route():
    client, seen = make_client()
    client.charge({"amount": 1, "idempotency_key": "k"})
    client.status("k")
    assert [(r.method, r.url.raw_path) for r in seen] == [
        ("POST", b"/v1/charges"),
        ("GET", b"/v1/payments/k"),
    ]


def test_a_trailing_slash_on_the_base_url_is_ignored():
    seen = []
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: (seen.append(r), httpx.Response(200, json={}))[1])
    )
    SimulatorClient("http://sim:8100/", TOKEN, CTX, http_client=http).status("k")
    assert seen[0].url.raw_path == b"/v1/payments/k"


# A namespace key in the arguments is denied, not forwarded and not raised

DENIED_KEYS = [
    "namespace",
    "Namespace",
    "NAMESPACE",
    "x-lab-namespace",
    "X-Lab-Namespace",
    "X_Lab_Namespace",
    "x lab namespace",
    "target_namespace_id",
    "names​pace",  # a zero-width space inside the word
    "ｎamespace",  # full-width n
]


@pytest.mark.parametrize("key", DENIED_KEYS)
def test_a_namespace_like_key_is_denied_and_nothing_is_sent(key):
    client, seen = make_client()
    args = {"amount": 49.99, "idempotency_key": "k1", key: "ns-victim"}

    result = client.charge(args)  # does not raise

    assert seen == []
    status, body = result
    assert status == DENIED_STATUS == 0
    assert body["error"]["code"] == "namespace_argument_denied"
    assert key in body["error"]["message"]
    assert len(client.denials) == 1


def test_the_denial_record_has_what_the_agent_loop_needs_for_a_tool_events_row():
    client, _ = make_client()
    args = {"amount": 49.99, "idempotency_key": "k1", "Namespace": "ns-victim", "n": {"a": [1]}}

    status, body = client.charge(args)

    (denial,) = client.denials
    assert isinstance(denial, Denial)
    assert denial.tool == "charge"
    assert denial.requested_args == args
    assert denial.rule == NAMESPACE_DENIED == "namespace_argument_denied"
    assert denial.reason == body["error"]["message"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        denial.rule = "other"
    # The record is a snapshot: the caller changing its dict later does not rewrite history.
    args["n"]["a"].append(2)
    assert client.denials[0].requested_args["n"] == {"a": [1]}


def test_a_denied_call_is_denied_whole_and_not_stripped():
    client, seen = make_client()
    client.charge({"amount": 49.99, "idempotency_key": "k1", "namespace": "ns-victim"})
    assert seen == []


def test_denials_accumulate_in_order_and_do_not_block_later_calls():
    client, seen = make_client()
    client.charge({"idempotency_key": "a", "namespace": "x"})
    ok_status, _ = client.charge({"amount": 1, "idempotency_key": "b"})
    client.charge({"idempotency_key": "c", "Namespace": "y"})

    assert ok_status == 200
    assert len(seen) == 1
    assert [d.requested_args["idempotency_key"] for d in client.denials] == ["a", "c"]


def test_the_denials_property_returns_a_copy():
    client, _ = make_client()
    client.denials.append("junk")
    assert client.denials == []
    client.charge({"namespace": "x"})
    snapshot = client.denials
    client.charge({"namespace": "y"})
    assert len(snapshot) == 1 and len(client.denials) == 2


def test_only_top_level_keys_are_checked():
    # The body is not an isolation boundary; the header is. A nested key is data.
    client, seen = make_client()
    status, _ = client.charge({"amount": 1, "idempotency_key": "k", "meta": {"namespace": "x"}})
    assert status == 200 and len(seen) == 1 and client.denials == []


def test_arguments_that_are_not_a_dict_are_sent_as_given_and_do_not_crash():
    # There are no keys to check. The simulator rejects such a body with 422, which is the
    # honest observation for the model, so the client does not pre-empt it.
    client, seen = make_client(lambda r: httpx.Response(422, json={"detail": []}))
    for args in (["namespace"], "namespace", 7):
        status, _ = client.charge(args)
        assert status == 422
    assert [json.loads(r.content) for r in seen] == [["namespace"], "namespace", 7]
    # httpx treats json=None as "no body"; the simulator answers that with 422 as well.
    assert client.charge(None)[0] == 422
    assert seen[-1].content == b""
    assert client.denials == []


# No method can take a namespace


def test_no_public_method_has_a_namespace_parameter():
    methods = [
        member
        for name, member in inspect.getmembers(SimulatorClient, inspect.isfunction)
        if name == "__init__" or not name.startswith("_")
    ]
    names = {m.__name__ for m in methods}
    assert {"__init__", "charge", "status", "close"} <= names
    for method in methods:
        for parameter in inspect.signature(method).parameters.values():
            assert "namespace" not in parameter.name.casefold(), (method.__name__, parameter)
            assert parameter.kind is not inspect.Parameter.VAR_KEYWORD, (
                f"{method.__name__} takes **{parameter.name}, which could carry a namespace"
            )


# The status key stays in its own path segment


@pytest.mark.parametrize(
    ("key", "raw_path"),
    [
        ("k1", b"/v1/payments/k1"),
        ("../charges", b"/v1/payments/..%2Fcharges"),
        ("a/b", b"/v1/payments/a%2Fb"),
        ("?x=1", b"/v1/payments/%3Fx%3D1"),
        ("k#frag", b"/v1/payments/k%23frag"),
        ("100%", b"/v1/payments/100%25"),
        ("..", b"/v1/payments/%2E%2E"),
        (".", b"/v1/payments/%2E"),
        ("k 1", b"/v1/payments/k%201"),
        ("café", b"/v1/payments/caf%C3%A9"),
    ],
)
def test_the_status_key_is_percent_encoded_into_one_path_segment(key, raw_path):
    client, seen = make_client()
    client.status(key)
    assert len(seen) == 1
    assert seen[0].url.raw_path == raw_path
    assert seen[0].url.query == b""


# Responses


def test_a_json_object_body_is_returned_with_its_status():
    client, _ = make_client(lambda r: httpx.Response(504, json={"error": {"code": "x"}}))
    assert client.charge({"amount": 1, "idempotency_key": "k"}) == (504, {"error": {"code": "x"}})


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, text="<html>Bad Gateway</html>"),
        httpx.Response(200, text=""),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json="ok"),
        httpx.Response(200, json=None),
    ],
)
def test_a_body_that_is_not_a_json_object_comes_back_as_raw_body(response):
    client, _ = make_client(lambda r: response)
    status, body = client.status("k")
    assert status == response.status_code
    assert body == {"raw_body": response.text}


def test_transport_errors_propagate():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    client, _ = make_client(boom)
    with pytest.raises(httpx.ConnectError):
        client.charge({"amount": 1, "idempotency_key": "k"})
    with pytest.raises(httpx.ConnectError):
        client.status("k")
    assert client.denials == []


def test_the_timeout_applies_to_the_client_it_makes():
    own = SimulatorClient("http://sim:8100", TOKEN, CTX, timeout=3.5)
    try:
        assert own._http.timeout == httpx.Timeout(3.5)
    finally:
        own.close()


def test_close_closes_only_a_client_it_made():
    client, _ = make_client()
    client.close()
    assert not client._http.is_closed  # the injected client belongs to the caller

    own = SimulatorClient("http://sim:8100", TOKEN, CTX)
    own.close()
    assert own._http.is_closed


# Against the real public app


@pytest.fixture
def sim(svc_url):
    pool = ConnectionPool(
        svc_url,
        min_size=1,
        max_size=5,
        open=True,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    public = TestClient(create_public_app(pool, TOKEN))
    internal = TestClient(create_internal_app(pool, "control-t"))
    for namespace in ("ns-trusted", "ns-victim"):
        r = internal.put(
            f"/internal/namespaces/{namespace}",
            headers=CONTROL,
            json={"profile": "controlled", "fault_plan": {}},
        )
        assert r.status_code == 200, r.text
    yield public
    pool.close()


def client_for(sim, namespace):
    ctx = CTX.model_copy(update={"namespace": namespace})
    return SimulatorClient("http://testserver", TOKEN, ctx, http_client=sim)


def call_log_counts(conn):
    rows = conn.execute("SELECT namespace, count(*) AS n FROM call_log GROUP BY 1").fetchall()
    return {row["namespace"]: row["n"] for row in rows}


def test_a_charge_and_a_status_lookup_land_in_the_contexts_namespace(sim, conn):
    client = client_for(sim, "ns-trusted")
    # A model that tries to charge in another namespace is denied before the wire...
    denied = client.charge({**CHARGE, "namespace": "ns-victim"})
    assert denied[0] == DENIED_STATUS
    assert call_log_counts(conn) == {}
    # ...and the same call without the key lands where the context says.
    status, body = client.charge(CHARGE)
    assert status == 200 and body["charge"]["idempotency_key"] == "k1"

    assert [c["idempotency_key"] for c in charges(conn, "ns-trusted")] == ["k1"]
    assert charges(conn, "ns-victim") == []
    assert call_log_counts(conn) == {"ns-trusted": 1}

    found = client.status("k1")
    assert found[0] == 200 and found[1]["status"] == "SUCCEEDED"
    # The same key looked up for the other namespace is a different record: not found.
    other = client_for(sim, "ns-victim").status("k1")
    assert other[0] == 200 and other[1]["status"] == "NOT_FOUND"
    assert call_log_counts(conn) == {"ns-trusted": 2, "ns-victim": 1}


def test_a_nested_namespace_in_the_args_does_not_move_the_charge(sim, conn):
    client = client_for(sim, "ns-trusted")
    status, _ = client.charge({**CHARGE, "metadata": {"namespace": "ns-victim"}})
    # Whatever the simulator makes of the extra field, it is never read as the namespace.
    assert charges(conn, "ns-victim") == []
    assert set(call_log_counts(conn)) == {"ns-trusted"}
    assert status in (200, 400)


@pytest.mark.parametrize("key", ["?x=1", "..", ".", "ns-victim", "k#frag", "100%"])
def test_an_odd_status_key_is_looked_up_as_a_key_not_as_a_route(sim, conn, key):
    client = client_for(sim, "ns-trusted")
    status, body = client.status(key)
    assert (status, body["status"], body["charge"]) == (200, "NOT_FOUND", None)
    rows = conn.execute("SELECT endpoint, idempotency_key FROM call_log").fetchall()
    assert [(r["endpoint"], r["idempotency_key"]) for r in rows] == [("status", key)]


@pytest.mark.parametrize("key", ["../charges", "a/b"])
def test_a_key_with_a_slash_reaches_no_route_at_all(sim, conn, key):
    # Starlette decodes %2F before it routes, so the simulator cannot look such a key up:
    # it answers its router's 404, not a lookup result. What matters here is that the
    # request did not land on /v1/charges or any other route: nothing was logged.
    client = client_for(sim, "ns-trusted")
    assert client.status(key) == (404, {"detail": "Not Found"})
    assert call_log_counts(conn) == {}
    assert charges(conn, "ns-trusted") == []


def test_an_unknown_namespace_is_the_simulators_404_not_a_client_error(sim):
    client = client_for(sim, "ns-does-not-exist")
    status, body = client.charge(CHARGE)
    assert status == 404 and body["error"]["code"] == "unknown_namespace"


def test_a_wrong_token_is_the_simulators_401(sim):
    client = SimulatorClient("http://testserver", "wrong", CTX, http_client=sim)
    assert client.status("k1") == (401, {"detail": "unauthorized"})
