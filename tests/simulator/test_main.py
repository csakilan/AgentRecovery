"""The wiring in __main__: which token guards which app, and where each one binds.

uvicorn.run is replaced by a capture so every branch runs without a server.
"""

import pytest
from fastapi.testclient import TestClient

from lab.simulator import __main__ as sim_main
from tests.helpers import CHARGE

CLIENT = {"Authorization": "Bearer client-t"}
CONTROL = {"Authorization": "Bearer control-t"}


@pytest.fixture
def run_main(monkeypatch, svc_url):
    pools = []
    real_pool = sim_main._pool

    def tracking_pool(url):
        pool = real_pool(url)
        pools.append(pool)
        return pool

    monkeypatch.setattr(sim_main, "_pool", tracking_pool)
    monkeypatch.setenv("LAB_PAYMENTS_URL", svc_url)
    monkeypatch.setenv("SIM_CLIENT_TOKEN", "client-t")
    monkeypatch.setenv("SIM_CONTROL_TOKEN", "control-t")
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)

    def run(command):
        captured = {}

        def fake_run(app, **kwargs):
            captured["app"] = app
            captured.update(kwargs)

        monkeypatch.setattr(sim_main.uvicorn, "run", fake_run)
        sim_main.main(["lab.simulator", command])
        return captured

    yield run
    for pool in pools:
        pool.close()


def test_public_command_serves_an_app_guarded_by_the_client_token(run_main):
    client = TestClient(run_main("public")["app"])
    headers = {"X-Lab-Namespace": "nope"}
    accepted = client.post("/v1/charges", headers={**CLIENT, **headers}, json=CHARGE)
    assert accepted.status_code == 404  # past auth, then an unknown namespace
    assert accepted.json()["error"]["code"] == "unknown_namespace"
    rejected = client.post("/v1/charges", headers={**CONTROL, **headers}, json=CHARGE)
    assert rejected.status_code == 401


def test_internal_command_serves_an_app_guarded_by_the_control_token(run_main):
    client = TestClient(run_main("internal")["app"])
    url = "/internal/namespaces/ns/barriers/x"
    assert client.get(url, headers=CONTROL).status_code == 200
    assert client.get(url, headers=CLIENT).status_code == 401


@pytest.mark.parametrize(
    ("command", "host", "port"),
    [("public", "0.0.0.0", 8100), ("internal", "127.0.0.1", 8101)],
)
def test_bind_host_and_port_defaults_per_command(run_main, command, host, port):
    captured = run_main(command)
    assert (captured["host"], captured["port"]) == (host, port)


@pytest.mark.parametrize("command", ["public", "internal"])
def test_host_and_port_environment_variables_override_the_defaults(run_main, monkeypatch, command):
    monkeypatch.setenv("HOST", "192.0.2.7")
    monkeypatch.setenv("PORT", "9999")
    captured = run_main(command)
    assert (captured["host"], captured["port"]) == ("192.0.2.7", 9999)
