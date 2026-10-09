import logging
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from lab.simulator import app as app_module
from lab.simulator.app import create_internal_app, create_public_app
from tests.helpers import CHARGE

CLIENT = {"Authorization": "Bearer client-t"}
CONTROL = {"Authorization": "Bearer control-t"}


@pytest.fixture
def apps(svc_url):
    pool = ConnectionPool(
        svc_url,
        min_size=1,
        max_size=5,
        open=True,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    yield (
        TestClient(create_public_app(pool, "client-t")),
        TestClient(create_internal_app(pool, "control-t")),
    )
    pool.close()


def configure(internal, ns, plan=None):
    body = {"profile": "controlled", "fault_plan": plan or {}}
    r = internal.put(f"/internal/namespaces/{ns}", headers=CONTROL, json=body)
    assert r.status_code == 200, r.text


def test_lost_ack_end_to_end(apps):
    public, internal = apps
    configure(internal, "ns", {"charge": {"1": "commit_then_error"}})
    headers = {**CLIENT, "X-Lab-Namespace": "ns"}
    first = public.post("/v1/charges", headers=headers, json=CHARGE)
    assert first.status_code == 504
    status = public.get("/v1/payments/k1", headers=headers)
    assert status.status_code == 200
    assert status.json()["status"] == "SUCCEEDED"


def test_public_requires_the_client_token(apps):
    public, internal = apps
    configure(internal, "ns")
    for headers in ({}, {"Authorization": "Bearer wrong"}, CONTROL):
        r = public.post("/v1/charges", headers={**headers, "X-Lab-Namespace": "ns"}, json=CHARGE)
        assert r.status_code == 401


def test_internal_requires_the_control_token(apps):
    _, internal = apps
    for headers in ({}, CLIENT):
        r = internal.put("/internal/namespaces/ns", headers=headers, json={"profile": "controlled"})
        assert r.status_code == 401


def _route_pairs(client):
    """Every (path, method) the app registers. A non-APIRoute (a Mount, a static
    route) has no method list, so it is reported as (path, None) and cannot hide."""
    pairs = set()
    for route in client.app.routes:
        if isinstance(route, APIRoute):
            pairs.update((route.path, method) for method in route.methods)
        else:
            pairs.add((route.path, None))
    return pairs


def test_public_app_registers_exactly_the_two_payment_routes(apps):
    public, _ = apps
    assert _route_pairs(public) == {
        ("/v1/charges", "POST"),
        ("/v1/payments/{idempotency_key}", "GET"),
    }


def test_public_app_rejects_every_internal_route(apps):
    """Behavioural twin of the route-set test: try each internal route on the
    public app, with both tokens, and require a rejection every time."""
    public, internal = apps
    internal_routes = _route_pairs(internal)
    assert len(internal_routes) >= 4  # the list is derived, so make sure it is not empty
    for path, method in sorted(internal_routes):
        concrete = re.sub(r"\{[^}]+\}", "x", path)
        for headers in (CONTROL, CLIENT):
            r = public.request(method, concrete, headers=headers, json={"profile": "controlled"})
            assert r.status_code in (401, 403, 404, 405), (method, concrete, headers, r.text)


def test_public_app_publishes_no_schema(apps):
    public, _ = apps
    assert public.get("/openapi.json").status_code == 404


def test_unknown_namespace_is_404(apps):
    public, _ = apps
    r = public.post("/v1/charges", headers={**CLIENT, "X-Lab-Namespace": "nope"}, json=CHARGE)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_namespace"


def test_barrier_routes(apps):
    _, internal = apps
    configure(internal, "ns", {"hold_after_commit": [1]})
    url = "/internal/namespaces/ns/barriers/after_commit:1"
    assert internal.get(url, headers=CONTROL).json() == {"reached": False, "released": False}
    assert internal.post(f"{url}/release", headers=CONTROL).status_code == 200
    assert internal.get(url, headers=CONTROL).json() == {"reached": False, "released": True}


def test_release_on_unknown_namespace_is_404(apps):
    _, internal = apps
    r = internal.post("/internal/namespaces/nope/barriers/x/release", headers=CONTROL)
    assert r.status_code == 404
    # A route that does not exist also answers 404, with {"detail": "Not Found"}.
    assert r.json() == {"error": {"code": "unknown_namespace", "message": "unknown namespace"}}


def test_delete_namespace(apps):
    public, internal = apps
    configure(internal, "ns")
    assert internal.delete("/internal/namespaces/ns", headers=CONTROL).status_code == 204
    r = public.post("/v1/charges", headers={**CLIENT, "X-Lab-Namespace": "ns"}, json=CHARGE)
    assert r.status_code == 404


def test_namespace_config_rejects_unknown_fault_fields(apps):
    _, internal = apps
    r = internal.put(
        "/internal/namespaces/ns",
        headers=CONTROL,
        json={"profile": "controlled", "fault_plan": {"explode": True}},
    )
    assert r.status_code == 422


# --- unhandled failures keep the JSON error envelope ------------------------------------


@pytest.fixture
def tight_pool(svc_url):
    """One connection, a short wait: holding it makes the next request time out."""
    pool = ConnectionPool(
        svc_url,
        min_size=1,
        max_size=1,
        timeout=0.3,
        open=True,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    yield pool
    pool.close()


def _envelope(r):
    assert r.headers["content-type"].startswith("application/json"), r.text
    body = r.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    return body["error"]


def test_pool_timeout_on_the_public_app_is_a_503_envelope(tight_pool, caplog):
    public = TestClient(create_public_app(tight_pool, "client-t"))
    headers = {**CLIENT, "X-Lab-Namespace": "ns"}
    with caplog.at_level(logging.ERROR, logger="lab.simulator.app"):
        with tight_pool.connection():  # hold the only connection
            for r in (
                public.post("/v1/charges", headers=headers, json=CHARGE),
                public.get("/v1/payments/k1", headers=headers),
            ):
                assert r.status_code == 503
                assert _envelope(r)["code"] == "service_unavailable"
    assert any(rec.exc_info and "PoolTimeout" in rec.exc_info[0].__name__ for rec in caplog.records)


def test_pool_timeout_on_the_internal_app_is_a_503_envelope(tight_pool):
    internal = TestClient(create_internal_app(tight_pool, "control-t"))
    with tight_pool.connection():
        r = internal.get("/internal/namespaces/ns/barriers/x", headers=CONTROL)
    assert r.status_code == 503
    assert _envelope(r)["code"] == "service_unavailable"


def test_unhandled_exception_on_the_public_app_is_a_500_envelope(apps, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(app_module.service, "charge", boom)
    public, _ = apps
    # The response is sent, then Starlette re-raises for the server to log; a test
    # client must be told not to re-raise it into the test.
    public = TestClient(public.app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="lab.simulator.app"):
        r = public.post("/v1/charges", headers={**CLIENT, "X-Lab-Namespace": "ns"}, json=CHARGE)
    assert r.status_code == 500
    err = _envelope(r)
    assert err["code"] == "unhandled_error"
    assert "kaboom" not in r.text  # internals stay server-side
    assert any(rec.exc_info and rec.exc_info[0] is RuntimeError for rec in caplog.records)


def test_unhandled_exception_on_the_internal_app_is_a_500_envelope(apps, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(app_module.service, "configure_namespace", boom)
    _, internal = apps
    internal = TestClient(internal.app, raise_server_exceptions=False)
    r = internal.put("/internal/namespaces/ns", headers=CONTROL, json={"profile": "controlled"})
    assert r.status_code == 500
    assert _envelope(r)["code"] == "unhandled_error"
