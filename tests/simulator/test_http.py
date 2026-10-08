import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

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


def test_public_app_has_no_control_routes(apps):
    public, _ = apps
    r = public.put("/internal/namespaces/ns", headers=CONTROL, json={"profile": "controlled"})
    assert r.status_code in (404, 405)


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
