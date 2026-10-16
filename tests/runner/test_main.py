"""The command line: scripted policies only, one JSON line out, the results row as proof."""

from __future__ import annotations

import json

import pytest

from lab.protocol.faults import World
from lab.protocol.manifest import Manifest, Mode, infrastructure_matrix, make_spec
from lab.runner.__main__ import main


@pytest.fixture
def environ(runner_config):
    return {
        "SIM_PUBLIC_URL": runner_config.sim_public_url,
        "SIM_CONTROL_URL": runner_config.sim_control_url,
        "SIM_CLIENT_TOKEN": runner_config.client_token,
        "SIM_CONTROL_TOKEN": runner_config.control_token,
        "LAB_RUNTIME_URL": runner_config.runtime_url,
        "LAB_LEDGER_READER_URL": runner_config.ledger_url,
    }


def test_run_a_policy_in_a_world_prints_the_verdict_the_results_row_holds(environ, rt, capsys):
    code = main(["run", "--policy", "naive_new_key", "--world", "lost_ack"], environ)

    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["verdict"] == "duplicate"
    assert summary["claimed_outcome"] == "succeeded"
    row = rt.execute("SELECT verdict FROM results WHERE run_id = %s", (summary["run_id"],))
    assert row.fetchone()["verdict"] == "duplicate"
    experiment = rt.execute(
        "SELECT name, config_json FROM experiments WHERE id = %s", (summary["experiment_id"],)
    ).fetchone()
    assert experiment["config_json"]["model_id"] == "scripted/naive_new_key"
    assert experiment["config_json"]["durability"] == "sync"


def test_no_status_drops_the_status_tool(environ, rt, capsys):
    # check_status needs get_payment_status; without it the tool is "Unknown tool" and the
    # policy cannot do what it is for, but the run still completes.
    code = main(["run", "--policy", "check_status", "--world", "lost_ack", "--no-status"], environ)
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    request = rt.execute(
        "SELECT request_json FROM model_calls WHERE run_id = %s AND turn = 0", (summary["run_id"],)
    ).fetchone()["request_json"]
    assert [tool["name"] for tool in request["tools"]] == ["charge", "report_done"]


def test_run_an_episode_from_a_manifest_file(environ, rt, capsys, tmp_path):
    spec = make_spec("from-file", World.TRUE_FAIL, Mode.AGENT_DIRECTED, True, 3)
    manifest = Manifest(name="from-file", seed=1, episodes=(spec,))
    path = tmp_path / "m.json"
    manifest.write(path)

    code = main(
        ["run", "--policy", "give_up", "--manifest", str(path), "--episode", spec.episode_id],
        environ,
    )

    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["episode_id"] == spec.episode_id
    assert summary["verdict"] == "missed"
    stored = rt.execute(
        "SELECT name, manifest_sha256 FROM experiments WHERE id = %s", (summary["experiment_id"],)
    ).fetchone()
    assert stored == {"name": "from-file", "manifest_sha256": manifest.sha256()}


def test_a_manifest_episode_in_another_mode_is_refused_naming_plan_4(environ, capsys, tmp_path):
    manifest = infrastructure_matrix(reps=1)
    spec = next(e for e in manifest.episodes if e.mode is Mode.DURABLE_GATEWAY)
    path = tmp_path / "infra.json"
    manifest.write(path)

    code = main(
        ["run", "--policy", "stable_key", "--manifest", str(path), "--episode", spec.episode_id],
        environ,
    )

    assert code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Plan 4" in captured.err


def test_unknown_episode_missing_config_and_bad_policy_are_refused(environ, capsys, tmp_path):
    path = tmp_path / "m.json"
    spec = make_spec("x", World.LOST_ACK, Mode.AGENT_DIRECTED, True, 0)
    Manifest(name="x", seed=0, episodes=(spec,)).write(path)

    assert (
        main(
            ["run", "--policy", "stable_key", "--manifest", str(path), "--episode", "nope"], environ
        )
        == 2
    )
    assert main(["run", "--policy", "stable_key", "--manifest", str(path)], environ) == 2
    assert main(["run", "--policy", "stable_key"], {}) == 2
    assert "SIM_CLIENT_TOKEN" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exit_info:
        main(["run", "--policy", "gpt-5"], environ)
    assert exit_info.value.code == 2


def test_the_command_has_no_way_to_name_a_real_model():
    from lab.runner.__main__ import _parse

    with pytest.raises(SystemExit):
        _parse(["run"])  # --policy is required
    with pytest.raises(SystemExit):
        _parse(["run", "--policy", "stable_key", "--model", "anything:free"])


def test_study_subcommand_uses_run_manifest(monkeypatch, environ, capsys):
    import lab.runner.__main__ as m
    from lab.agent.scripted import ScriptedModel
    from lab.runner.batch import BatchSummary

    captured = {}

    def fake_run_manifest(manifest, model, config, *, experiment_name):
        captured["n"] = len(manifest.episodes)
        captured["name"] = experiment_name
        return BatchSummary("exp-x", model.model_id, len(manifest.episodes), 0, 0)

    monkeypatch.setattr(m, "run_manifest", fake_run_manifest)
    monkeypatch.setattr(m, "build_study_models", lambda config: [ScriptedModel("stable_key")])
    rc = m.main(["study", "--reps", "1", "--experiment-name", "runner-test-study"], environ)
    assert rc == 0 and captured["n"] == 4 and captured["name"] == "runner-test-study"
    assert json.loads(capsys.readouterr().out) == {
        "experiment_id": "exp-x",
        "model_id": "scripted/stable_key",
        "completed": 4,
        "skipped": 0,
        "failed": 0,
    }


def test_study_moves_to_the_next_model_only_when_one_never_worked(monkeypatch, environ, capsys):
    import lab.runner.__main__ as m
    from lab.agent.scripted import ScriptedModel
    from lab.runner.batch import BatchSummary

    tried = []
    outcomes = {"scripted/give_up": (0, 0, 4), "scripted/stable_key": (4, 0, 0)}

    def fake_run_manifest(manifest, model, config, *, experiment_name):
        tried.append(model.model_id)
        return BatchSummary("exp-" + model.model_id, model.model_id, *outcomes[model.model_id])

    monkeypatch.setattr(m, "run_manifest", fake_run_manifest)
    monkeypatch.setattr(
        m,
        "build_study_models",
        lambda config: [ScriptedModel("give_up"), ScriptedModel("stable_key")],
    )
    assert m.main(["study", "--reps", "1"], environ) == 0
    assert tried == ["scripted/give_up", "scripted/stable_key"]
    assert json.loads(capsys.readouterr().out)["model_id"] == "scripted/stable_key"

    tried.clear()
    outcomes["scripted/give_up"] = (3, 1, 0)  # completed some: it is the study, stop here
    assert m.main(["study", "--reps", "1"], environ) == 0
    assert tried == ["scripted/give_up"]


def test_run_cannot_reach_the_real_models(monkeypatch, environ):
    import lab.runner.__main__ as m

    def boom(config):
        raise AssertionError("run must not build the study models")

    monkeypatch.setattr(m, "build_study_models", boom)
    assert m.main(["run", "--policy", "stable_key", "--world", "lost_ack"], environ) == 0
