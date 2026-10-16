"""The batch runner: every episode once, resumable, one experiment per model."""

from __future__ import annotations

from lab.agent.openrouter import OpenRouterError
from lab.agent.scripted import ScriptedModel
from lab.protocol.manifest import behavior_matrix
from lab.runner.batch import run_manifest

NAME = "runner-test-behaviour"  # the EXPERIMENT_NAME_PREFIX, so clean_experiments reaps it


def test_a_small_manifest_runs_every_episode_once_and_is_resumable(runner_config):
    manifest = behavior_matrix(reps=1)  # 2 worlds x 2 status x 1 = 4 episodes
    model = ScriptedModel("stable_key")
    first = run_manifest(manifest, model, runner_config, experiment_name=NAME)
    assert first.completed == 4 and first.skipped == 0 and first.failed == 0
    # Re-running the same manifest skips everything already scored.
    second = run_manifest(manifest, model, runner_config, experiment_name=NAME)
    assert second.completed == 0 and second.skipped == 4
    assert second.experiment_id == first.experiment_id


class _FailsThenWorks(ScriptedModel):
    """The same model id throughout, so both batches target one experiment."""

    def __init__(self, policy: str, *, fail: bool) -> None:
        super().__init__(policy)
        self.fail = fail

    def complete(self, messages, tools):
        if self.fail:
            raise OpenRouterError("gave up after 6 attempts: 503")
        return super().complete(messages, tools)


def test_an_episode_left_crashed_is_cleared_and_retried_on_the_next_run(runner_config, rt):
    manifest = behavior_matrix(reps=1)
    broken = _FailsThenWorks("stable_key", fail=True)
    first = run_manifest(manifest, broken, runner_config, experiment_name=NAME)
    assert first.completed == 0 and first.skipped == 0 and first.failed == 4
    states = rt.execute(
        "SELECT state FROM runs WHERE experiment_id = %s", (first.experiment_id,)
    ).fetchall()
    assert [row["state"] for row in states] == ["crashed"] * 4

    second = run_manifest(
        manifest, _FailsThenWorks("stable_key", fail=False), runner_config, experiment_name=NAME
    )
    assert second.experiment_id == first.experiment_id
    assert second.completed == 4 and second.skipped == 0 and second.failed == 0
    rows = rt.execute(
        "SELECT runs.state, results.verdict FROM runs"
        " LEFT JOIN results ON results.run_id = runs.id WHERE runs.experiment_id = %s",
        (first.experiment_id,),
    ).fetchall()
    assert len(rows) == 4
    assert all(row["state"] == "completed" and row["verdict"] for row in rows)


def test_each_model_is_its_own_experiment(runner_config):
    manifest = behavior_matrix(reps=1)
    a = run_manifest(manifest, ScriptedModel("stable_key"), runner_config, experiment_name=NAME)
    b = run_manifest(manifest, ScriptedModel("give_up"), runner_config, experiment_name=NAME)
    assert a.experiment_id != b.experiment_id
    assert b.completed == 4 and b.skipped == 0
