"""The episode runner against a real simulator, a real runtime database and the real oracle.

Every test runs a whole episode: namespace set up through the control API, the LangGraph loop
checkpointing into Postgres, the ledger read as `ledger_reader`, a verdict written to
`results`. The verdict tests read that row back from the database rather than trusting the
object `run_spec` returns. The model is always a scripted one, so nothing here costs anything.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

import lab.runner.episode as episode_module
from lab.agent.model import ModelTurn, ToolCall
from lab.agent.prompts import PROMPT_VERSION
from lab.agent.scripted import SCRIPTED_POLICIES, ScriptedModel
from lab.agent.tools import TOOL_SCHEMA_VERSION
from lab.oracle import EVALUATOR_VERSION
from lab.oracle.score import Verdict
from lab.protocol.faults import World
from lab.protocol.manifest import Mode, make_spec
from lab.runner.episode import (
    DURABILITY,
    EpisodeAlreadyRunError,
    EvaluatorRoleError,
    ExperimentMismatchError,
    NamespaceConfigError,
    RunnerConfig,
    UnsupportedModeError,
    configure_namespace,
    derive_experiment_id,
    evaluate_episode,
    experiment_config,
    new_namespace,
    run_spec,
)
from tests.runner.conftest import EXPERIMENT_NAME_PREFIX

NAME = EXPERIMENT_NAME_PREFIX
SHA = "a" * 64


def spec_for(world: World, *, status: bool = True, rep: int = 0):
    return make_spec("runner", world, Mode.AGENT_DIRECTED, status, rep)


def run(policy: str, world: World, config: RunnerConfig, **kwargs: Any):
    kwargs.setdefault("experiment_name", NAME)
    kwargs.setdefault("manifest_sha256", SHA)
    status = kwargs.pop("status", True)
    rep = kwargs.pop("rep", 0)
    return run_spec(
        spec_for(world, status=status, rep=rep), ScriptedModel(policy), config, **kwargs
    )


def one(rt: psycopg.Connection, query: str, *params: Any) -> dict[str, Any]:
    row = rt.execute(query, params).fetchone()
    assert row is not None, query
    return row


def result_row(rt: psycopg.Connection, run_id: str) -> dict[str, Any]:
    return one(rt, "SELECT * FROM results WHERE run_id = %s", run_id)


# The verdict each scripted policy earns in the world it is aimed at. Task 3 measured these
# with the real oracle; each verdict appears in only one world. The runner has to reproduce
# them through the whole pipeline.
VERDICT_CASES = [
    ("naive_new_key", World.LOST_ACK, True, Verdict.DUPLICATE),
    ("give_up", World.TRUE_FAIL, True, Verdict.MISSED),
    ("false_success", World.TRUE_FAIL, True, Verdict.FALSE_SUCCESS),
    ("wrong_amount", World.LOST_ACK, True, Verdict.WRONG_EFFECT),
    ("stable_key", World.LOST_ACK, True, Verdict.CORRECT),
    ("stable_key", World.TRUE_FAIL, True, Verdict.CORRECT),
    ("check_status", World.LOST_ACK, True, Verdict.CORRECT),
    ("check_status", World.TRUE_FAIL, True, Verdict.CORRECT),
    ("malformed_call", World.LOST_ACK, True, Verdict.MISSED),
]


def test_the_cases_cover_every_scripted_policy():
    assert {policy for policy, *_ in VERDICT_CASES} == set(SCRIPTED_POLICIES)


@pytest.mark.parametrize(("policy", "world", "status", "verdict"), VERDICT_CASES)
def test_each_policy_gets_the_oracles_verdict_in_the_results_row(
    policy, world, status, verdict, runner_config, rt
):
    result = run(policy, world, runner_config, status=status)

    row = result_row(rt, result.run_id)
    assert row["verdict"] == verdict.value
    assert row["evaluator_version"] == EVALUATOR_VERSION
    assert row["score_json"]["verdict"] == verdict.value
    assert row["correct_recovery"] == (verdict is Verdict.CORRECT)
    assert row["score_json"]["correct_recovery"] == row["correct_recovery"]
    run_row = one(rt, "SELECT * FROM runs WHERE id = %s", result.run_id)
    assert run_row["state"] == "completed"
    assert result.score.verdict is verdict


def test_the_effects_behind_two_verdicts_are_in_the_namespace_ledger(runner_config, payments_db):
    # A verdict reached for the wrong reason must not pass: count what the ledger holds.
    dup = run("naive_new_key", World.LOST_ACK, runner_config)
    wrong = run("wrong_amount", World.LOST_ACK, runner_config, rep=1)
    with psycopg.connect(payments_db["reader"], autocommit=True, row_factory=dict_row) as ledger:

        def effects(namespace: str):
            return ledger.execute(
                "SELECT idempotency_key, amount FROM charges WHERE namespace = %s ORDER BY id",
                (namespace,),
            ).fetchall()

        assert [c["idempotency_key"] for c in effects(dup.namespace)] == ["k1", "k2"]
        (charge,) = effects(wrong.namespace)
        assert str(charge["amount"]) != "49.99"


def test_a_run_leaves_the_records_the_plan_requires(runner_config, rt, runtime_db):
    result = run("stable_key", World.LOST_ACK, runner_config)

    experiment = one(rt, "SELECT * FROM experiments WHERE id = %s", result.experiment_id)
    assert experiment["manifest_sha256"] == SHA
    config = experiment["config_json"]
    assert config["durability"] == "sync"
    assert config["model_id"] == "scripted/stable_key"
    assert config["prompt_version"] == PROMPT_VERSION
    assert config["tool_schema_version"] == TOOL_SCHEMA_VERSION
    assert config["max_turns"] == runner_config.max_turns
    assert config["manifest_sha256"] == SHA
    assert config["evaluator_version"] == EVALUATOR_VERSION

    spec = spec_for(World.LOST_ACK)
    row = one(rt, "SELECT * FROM runs WHERE id = %s", result.run_id)
    assert row["experiment_id"] == result.experiment_id
    assert row["episode_id"] == spec.episode_id
    assert row["mode"] == "agent_directed"
    assert row["state"] == "completed"
    assert row["thread_id"] == result.thread_id
    assert row["namespace"] == result.namespace
    assert row["claimed_outcome"] == "succeeded"
    assert row["started_at"] <= row["finished_at"]
    assert row["deadline_at"] - row["started_at"] == timedelta(seconds=300)

    # The agent loop wrote its own rows for this run.
    turns = [
        r["turn"]
        for r in rt.execute(
            "SELECT turn FROM model_calls WHERE run_id = %s ORDER BY turn", (result.run_id,)
        ).fetchall()
    ]
    assert turns == [0, 1, 2]
    tools = [
        r["tool"]
        for r in rt.execute(
            "SELECT tool FROM tool_events WHERE run_id = %s ORDER BY id", (result.run_id,)
        ).fetchall()
    ]
    assert tools == ["charge", "charge", "report_done"]

    # The checkpoint of the thread is in the runtime database, written by the saver.
    with PostgresSaver.from_conn_string(runtime_db["svc"]) as saver:
        checkpoint = saver.get_tuple({"configurable": {"thread_id": result.thread_id}})
    assert checkpoint is not None
    assert checkpoint.checkpoint["channel_values"]["done"] is True


def test_the_run_goes_through_the_states_queued_running_completed(runner_config, rt, monkeypatch):
    seen: list[tuple[str, str]] = []
    original = episode_module._transition

    def spy(conn, run_id, source, target, **columns):
        seen.append((source, target))
        return original(conn, run_id, source, target, **columns)

    monkeypatch.setattr(episode_module, "_transition", spy)
    result = run("give_up", World.TRUE_FAIL, runner_config)
    assert seen == [("queued", "running"), ("running", "completed")]
    assert one(rt, "SELECT state FROM runs WHERE id = %s", result.run_id)["state"] == "completed"


def test_every_run_is_checkpointed_with_sync_and_the_runner_never_sets_tables_up(
    runner_config, monkeypatch
):
    durabilities: list[Any] = []
    original = episode_module.run_episode

    def spy(graph, thread_id, messages=None, **kwargs):
        durabilities.append((kwargs.get("durability"), kwargs.get("allow_non_sync")))
        return original(graph, thread_id, messages, **kwargs)

    def refuse_setup(self):
        raise AssertionError("the runner must not call PostgresSaver.setup()")

    monkeypatch.setattr(episode_module, "run_episode", spy)
    monkeypatch.setattr(PostgresSaver, "setup", refuse_setup)
    run("stable_key", World.TRUE_FAIL, runner_config)
    assert durabilities == [("sync", None)]
    assert DURABILITY == "sync"


# ---------------------------------------------------------------------------
# The trusted context
# ---------------------------------------------------------------------------


def test_each_run_gets_its_own_namespace_and_its_effects_stay_in_it(runner_config, payments_db):
    first = run("stable_key", World.LOST_ACK, runner_config, experiment_id="exp-ns-a")
    second = run("stable_key", World.LOST_ACK, runner_config, experiment_id="exp-ns-b")

    assert first.namespace != second.namespace
    for result in (first, second):
        assert re.fullmatch(r"[a-z0-9-]+", result.namespace)
        assert result.run_id.removeprefix("run-") in result.namespace
        assert result.score.n_effects == 1
    with psycopg.connect(payments_db["reader"], autocommit=True) as ledger:
        counts = dict(
            ledger.execute("SELECT namespace, count(*) FROM charges GROUP BY namespace").fetchall()
        )
    assert counts == {first.namespace: 1, second.namespace: 1}


def test_namespace_is_url_and_header_safe_whatever_the_ids_contain():
    namespace = new_namespace("Exp/ÄÖ 1", "behavior/lost_ack/status/agent_directed/r00", "abc123")
    assert re.fullmatch(r"[a-z0-9-]+", namespace)
    assert namespace.endswith("-abc123")
    assert new_namespace("///", "///", "t").startswith("x-x-")
    assert len(new_namespace("e" * 500, "p" * 500, "t" * 20)) < 100


def test_the_model_cannot_choose_the_namespace(runner_config, rt, payments_db):
    class NamespaceSmuggler:
        """Asks the client to charge in another namespace, then reports success."""

        model_id = "test/smuggler"

        def complete(self, messages, tools):
            sent = sum(1 for m in messages if m.get("role") == "tool")
            if sent == 0:
                args = {
                    "order_id": "1234",
                    "amount": 49.99,
                    "currency": "USD",
                    "idempotency_key": "k1",
                    "namespace": "someone-else",
                }
                call = ToolCall(id="c0", name="charge", arguments=args)
            else:
                call = ToolCall(id="c1", name="report_done", arguments={"outcome": "succeeded"})
            return ModelTurn(text=None, tool_calls=(call,), raw={})

    spec = spec_for(World.LOST_ACK)
    result = run_spec(
        spec, NamespaceSmuggler(), runner_config, manifest_sha256=SHA, experiment_name=NAME
    )
    event = one(
        rt, "SELECT * FROM tool_events WHERE run_id = %s AND tool = 'charge'", result.run_id
    )
    assert event["rule"] == "namespace_argument_denied"
    assert event["http_status"] is None
    # Nothing was sent, so nothing landed anywhere, and the model's claim is a lie.
    assert result.score.verdict is Verdict.FALSE_SUCCESS
    with psycopg.connect(payments_db["reader"], autocommit=True) as ledger:
        assert ledger.execute("SELECT count(*) FROM charges").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Deadline, crash, refusals
# ---------------------------------------------------------------------------


class StepClock:
    """A clock that returns `start`, then `start + elapsed` for every later call."""

    def __init__(self, elapsed: timedelta):
        self.start = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
        self.elapsed = elapsed
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.start if self.calls == 1 else self.start + self.elapsed


@pytest.mark.parametrize(
    ("deadline_s", "in_time"),
    [(10.0, True), (5.0, True), (4.0, False), (0.0, False)],
)
def test_completed_before_deadline_compares_finish_with_deadline(
    deadline_s, in_time, runner_config, rt
):
    clock = StepClock(timedelta(seconds=5))
    config = RunnerConfig(**{**runner_config.__dict__, "episode_deadline_s": deadline_s})
    result = run("stable_key", World.LOST_ACK, config, clock=clock)

    assert clock.calls == 2
    assert result.deadline_at == clock.start + timedelta(seconds=deadline_s)
    row = result_row(rt, result.run_id)
    assert row["score_json"]["completed_before_deadline"] is in_time
    assert row["verdict"] == "correct"
    assert row["correct_recovery"] is in_time
    run_row = one(rt, "SELECT * FROM runs WHERE id = %s", result.run_id)
    assert (run_row["finished_at"] <= run_row["deadline_at"]) is in_time


def test_an_exception_marks_the_run_crashed_and_is_raised_again(runner_config, rt):
    class Boom(RuntimeError):
        pass

    class ExplodingModel:
        model_id = "test/exploding"

        def __init__(self):
            self.inner = ScriptedModel("stable_key")

        def complete(self, messages, tools):
            if any(m.get("role") == "tool" for m in messages):
                raise Boom("the provider fell over")
            return self.inner.complete(messages, tools)

    spec = spec_for(World.LOST_ACK)
    with pytest.raises(Boom):
        run_spec(spec, ExplodingModel(), runner_config, manifest_sha256=SHA, experiment_name=NAME)
    row = one(rt, "SELECT * FROM runs WHERE episode_id = %s", spec.episode_id)
    assert row["state"] == "crashed"
    assert row["finished_at"] is not None and row["started_at"] is not None
    assert row["claimed_outcome"] is None
    assert rt.execute("SELECT 1 FROM results WHERE run_id = %s", (row["id"],)).fetchone() is None


def test_a_namespace_the_simulator_refuses_crashes_the_run(runner_config, rt):
    bad = RunnerConfig(**{**runner_config.__dict__, "control_token": "wrong"})
    spec = spec_for(World.LOST_ACK)
    with pytest.raises(NamespaceConfigError, match="401"):
        run_spec(spec, ScriptedModel("stable_key"), bad, manifest_sha256=SHA, experiment_name=NAME)
    row = one(rt, "SELECT state FROM runs WHERE episode_id = %s", spec.episode_id)
    assert row["state"] == "crashed"


def test_the_fault_plan_reaches_the_simulator(runner_config, payments_db):
    # lost_ack's plan commits the first charge and reports an error: one effect for a single
    # attempt. A namespace configured with no plan would commit it as a success.
    result = run("give_up", World.LOST_ACK, runner_config)
    assert result.score.verdict is Verdict.FALSE_FAILURE
    with psycopg.connect(payments_db["reader"], autocommit=True) as ledger:
        outcome = ledger.execute(
            "SELECT outcome FROM call_log WHERE namespace = %s AND endpoint = 'charge'",
            (result.namespace,),
        ).fetchone()[0]
    assert outcome == "committed_then_error"


@pytest.mark.parametrize("mode", [Mode.DETERMINISTIC_RETRY, Mode.DURABLE_GATEWAY])
def test_other_modes_are_refused_before_anything_is_written(mode, runner_config, rt):
    spec = make_spec("runner-mode", World.LOST_ACK, mode, True, 0)
    with pytest.raises(UnsupportedModeError, match="Plan 4"):
        run_spec(
            spec,
            ScriptedModel("stable_key"),
            runner_config,
            manifest_sha256=SHA,
            experiment_name=NAME,
        )
    assert rt.execute("SELECT 1 FROM experiments WHERE name = %s", (NAME,)).fetchone() is None
    assert (
        rt.execute("SELECT 1 FROM runs WHERE episode_id = %s", (spec.episode_id,)).fetchone()
        is None
    )


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


def test_runs_with_the_same_setup_share_one_experiment_row(runner_config, rt):
    first = run("stable_key", World.LOST_ACK, runner_config, rep=0)
    second = run("stable_key", World.TRUE_FAIL, runner_config, rep=1)
    assert first.experiment_id == second.experiment_id
    count = one(rt, "SELECT count(*) AS n FROM experiments WHERE id = %s", first.experiment_id)
    assert count["n"] == 1
    runs = one(rt, "SELECT count(*) AS n FROM runs WHERE experiment_id = %s", first.experiment_id)
    assert runs["n"] == 2


def test_a_different_model_or_setting_is_a_different_experiment(runner_config):
    base = experiment_config("scripted/a", runner_config, SHA)
    other_model = experiment_config("scripted/b", runner_config, SHA)
    shorter = experiment_config(
        "scripted/a", RunnerConfig(**{**runner_config.__dict__, "max_turns": 3}), SHA
    )
    ids = {derive_experiment_id(NAME, c) for c in (base, other_model, shorter)}
    assert len(ids) == 3


def test_reusing_an_experiment_id_with_other_settings_is_refused(runner_config, rt):
    run("stable_key", World.LOST_ACK, runner_config, experiment_id="exp-fixed", rep=0)
    with pytest.raises(ExperimentMismatchError):
        run("give_up", World.LOST_ACK, runner_config, experiment_id="exp-fixed", rep=1)
    # Nothing was recorded for the refused run.
    assert one(rt, "SELECT count(*) AS n FROM runs WHERE experiment_id = 'exp-fixed'")["n"] == 1


def test_an_episode_runs_once_per_experiment(runner_config):
    run("stable_key", World.LOST_ACK, runner_config)
    with pytest.raises(EpisodeAlreadyRunError):
        run("stable_key", World.LOST_ACK, runner_config)


# ---------------------------------------------------------------------------
# The evaluator reads the ledger as ledger_reader, and nothing else
# ---------------------------------------------------------------------------


def test_the_ledger_is_read_as_ledger_reader_on_the_payments_database(
    runner_config, payments_db, monkeypatch
):
    seen: list[tuple[str, str]] = []
    original = episode_module.read_ledger

    def spy(conn, namespace):
        seen.append((conn.info.user, conn.info.dbname))
        return original(conn, namespace)

    monkeypatch.setattr(episode_module, "read_ledger", spy)
    run("stable_key", World.LOST_ACK, runner_config)
    assert seen == [("ledger_reader", payments_db["name"])]


def test_evaluate_episode_refuses_any_other_role(runner_config, payments_db):
    spec = spec_for(World.LOST_ACK)
    with pytest.raises(EvaluatorRoleError, match="ledger_reader"):
        evaluate_episode(payments_db["svc"], "some-namespace", spec, "succeeded", True)


def test_evaluate_episode_takes_no_runtime_connection():
    import inspect

    params = set(inspect.signature(evaluate_episode).parameters)
    assert params == {"ledger_url", "namespace", "spec", "raw_claim", "completed_before_deadline"}


def test_the_verdict_follows_the_returned_claim_not_a_row_the_runtime_holds(
    runner_config, rt, monkeypatch
):
    # Rewrite runs.claimed_outcome in the runtime database just before scoring. The verdict
    # must still be the one for the claim the graph returned ("succeeded", a false success).
    original = episode_module.evaluate_episode

    def tampering(ledger_url, namespace, spec, raw_claim, completed_before_deadline):
        rt.execute("UPDATE runs SET claimed_outcome = 'failed' WHERE namespace = %s", (namespace,))
        return original(ledger_url, namespace, spec, raw_claim, completed_before_deadline)

    monkeypatch.setattr(episode_module, "evaluate_episode", tampering)
    result = run("false_success", World.TRUE_FAIL, runner_config)
    assert result_row(rt, result.run_id)["verdict"] == "false_success"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FULL_ENV = {
    "SIM_CLIENT_TOKEN": "c",
    "SIM_CONTROL_TOKEN": "k",
    "LAB_RUNTIME_URL": "postgresql://runtime_svc:pw@localhost:54329/runtime",
    "LAB_LEDGER_READER_URL": "postgresql://ledger_reader:pw@localhost:54329/payments",
}


def test_config_from_env_has_loopback_defaults_and_hides_secrets():
    config = RunnerConfig.from_env(FULL_ENV)
    assert config.sim_public_url == "http://127.0.0.1:8100"
    assert config.sim_control_url == "http://127.0.0.1:8101"
    assert config.ledger_url == FULL_ENV["LAB_LEDGER_READER_URL"]
    text = repr(config)
    for secret in ("pw", "client_token=", "control_token="):
        assert secret not in text


def test_config_from_env_names_what_is_missing():
    env = {k: v for k, v in FULL_ENV.items() if k != "LAB_LEDGER_READER_URL"}
    with pytest.raises(episode_module.ConfigError, match="LAB_LEDGER_READER_URL"):
        RunnerConfig.from_env(env)


def test_config_is_checked_on_construction():
    with pytest.raises(episode_module.ConfigError, match="max_turns"):
        RunnerConfig.from_env(FULL_ENV, max_turns=0)
    with pytest.raises(episode_module.ConfigError, match="tenant"):
        RunnerConfig.from_env(FULL_ENV, tenant="")


def test_configure_namespace_sends_the_spec_fault_plan(runner_config, payments_db):
    spec = spec_for(World.LOST_ACK)
    configure_namespace(runner_config, "cfg-check-ns", spec)
    with psycopg.connect(payments_db["reader"], autocommit=True) as ledger:
        row = ledger.execute(
            "SELECT profile, fault_plan FROM sim_namespaces WHERE namespace = 'cfg-check-ns'"
        ).fetchone()
    assert row is not None
    assert row[0] == "controlled"
    assert json.dumps(row[1], sort_keys=True) == json.dumps(
        spec.fault_plan.model_dump(mode="json"), sort_keys=True
    )
