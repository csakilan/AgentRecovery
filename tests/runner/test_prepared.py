from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest

from lab.agent.scripted import ScriptedModel
from lab.execution.clock import SystemClock
from lab.protocol.faults import World
from lab.protocol.manifest import Mode, make_spec
from lab.runner.episode import ExperimentMismatchError
from lab.runner.prepared import LegacyResumeUnsupported, describe_model, load_prepared, prepare_run
from lab.runner.types import ModelDescriptor


def prepare(config, **kwargs):
    return prepare_run(
        make_spec("prepared", World.LOST_ACK, Mode.DURABLE_GATEWAY, True, 1),
        describe_model(ScriptedModel("stable_key")),
        config,
        manifest_sha256="sha",
        experiment_name="runner-test-prepared",
        clock=SystemClock(),
        **kwargs,
    )


def test_prepare_is_immutable_and_idempotent(runner_config, rt):
    first = prepare(runner_config)
    assert prepare(runner_config) == first
    assert load_prepared(rt, first.run_id) == first
    assert first.setup_state == "ready"
    safe = first.worker_input.model_dump_json()
    assert all(
        k not in safe for k in ("fault_plan", "world", "expect_effect", runner_config.control_token)
    )
    with pytest.raises(ExperimentMismatchError):
        prepare(replace(runner_config, http_timeout_s=12), experiment_id=first.experiment_id)


def test_lost_setup_ack_does_not_reset_payment_or_call_position(runner_config, rt, monkeypatch):
    original_put = httpx.put

    def lose_ack(url, **kwargs):
        response = original_put(url, **kwargs)
        assert response.status_code == 200
        namespace = url.rsplit("/", 1)[-1]
        charged = httpx.post(
            runner_config.sim_public_url + "/v1/charges",
            headers={
                "Authorization": "Bearer " + runner_config.client_token,
                "X-Lab-Namespace": namespace,
            },
            json=dict(idempotency_key="first", order_id="1234", amount="49.99", currency="USD"),
        )
        assert charged.status_code == 504
        raise httpx.ReadTimeout("setup reply lost")

    monkeypatch.setattr(httpx, "put", lose_ack)
    with pytest.raises(httpx.ReadTimeout):
        prepare(runner_config)
    run_id = rt.execute("SELECT id FROM runs WHERE episode_id LIKE 'prepared/%'").fetchone()["id"]
    first = load_prepared(rt, run_id)
    assert first.setup_state == "pending"
    monkeypatch.setattr(httpx, "put", original_put)
    resumed = prepare(runner_config)
    assert resumed == first.model_copy(update={"setup_state": "ready"})
    result = httpx.get(
        runner_config.sim_public_url + "/v1/payments/first",
        headers={
            "Authorization": "Bearer " + runner_config.client_token,
            "X-Lab-Namespace": first.namespace,
        },
    )
    assert result.status_code == 200


def test_model_descriptors_preserve_only_reconstructible_nonsecret_parameters():
    from lab.agent.openrouter import DEFAULT_BASE_URL, OpenRouterClient

    builtin = OpenRouterClient(
        "example:free", api_key="never-save-this", timeout_s=11, min_interval_s=7, max_retries=2
    )
    try:
        descriptor = describe_model(builtin)
        assert descriptor.kind == "openrouter"
        assert descriptor.parameters == dict(
            base_url=DEFAULT_BASE_URL, timeout_s=11, min_interval_s=7, max_retries=2
        )
        assert "never-save-this" not in descriptor.model_dump_json() + repr(descriptor)
        copy = builtin.parameters
        copy["timeout_s"] = 100
        assert builtin.parameters["timeout_s"] == 11
    finally:
        builtin._http.close()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as http:
        custom = OpenRouterClient("example:free", api_key="never-save-this", http_client=http)
        assert describe_model(custom).kind == "injected"
        assert describe_model(custom).parameters == {}


def test_concurrent_preparations_share_saved_row(runner_config):
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(lambda _: prepare(runner_config), range(2)))
    assert a == b


def test_descriptor_rejects_secrets_and_unknown_parameters():
    for parameters in (
        {"api_key": "secret"},
        {"temperature": 1},
        {"base_url": "https://u:secret@example.test"},
    ):
        with pytest.raises(ValueError):
            ModelDescriptor(kind="openrouter", model_id="example:free", parameters=parameters)


def test_mutated_descriptor_is_revalidated_before_storage(runner_config, rt):
    model = ModelDescriptor(kind="openrouter", model_id="example:free")
    model.parameters["api_key"] = "must-not-be-saved"
    with pytest.raises(ValueError):
        prepare_run(
            make_spec("prepared", World.HEALTHY, Mode.DURABLE_GATEWAY, True, 1),
            model,
            runner_config,
            manifest_sha256="sha",
            experiment_name="runner-test-mutated",
            clock=SystemClock(),
        )
    assert (
        rt.execute(
            "SELECT count(*) AS n FROM experiments WHERE name='runner-test-mutated'"
        ).fetchone()["n"]
        == 0
    )


def test_legacy_resume_refuses_without_mutation(rt):
    rt.execute(
        "INSERT INTO experiments(id,name,manifest_sha256,config_json) "
        "VALUES('legacy-prep','runner-test-legacy','s','{}')"
    )
    rt.execute(
        "INSERT INTO runs(id,experiment_id,episode_id,namespace,thread_id,mode,state) "
        "VALUES('legacy-prep','legacy-prep','e','n','t','agent_directed','crashed')"
    )
    with pytest.raises(LegacyResumeUnsupported):
        load_prepared(rt, "legacy-prep")
    assert (
        rt.execute("SELECT state FROM runs WHERE id='legacy-prep'").fetchone()["state"] == "crashed"
    )
