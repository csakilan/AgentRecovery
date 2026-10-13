"""The episode runner against a real simulator, a real runtime database and the real oracle.

Every test runs a whole episode: namespace set up through the control API, the LangGraph loop
checkpointing into Postgres, the ledger read as `ledger_reader`, a verdict written to
`results`. The verdict tests read that row back from the database rather than trusting the
object `run_spec` returns. The model is always a scripted one, so nothing here costs anything.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import lab.runner.episode as episode_module
from lab import db
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
    LedgerConfigError,
    NamespaceConfigError,
    RunnerConfig,
    ScoringError,
    UnsupportedModeError,
    configure_namespace,
    derive_experiment_id,
    evaluate_episode,
    experiment_config,
    new_namespace,
    rescore_run,
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


EVALUATOR_FUNCTIONS = ("open_ledger", "score_ledger", "evaluate_episode")


def _function_nodes(names):
    tree = ast.parse(inspect.getsource(episode_module))
    return [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names
    ]


def test_the_evaluator_entry_points_have_no_way_to_reach_the_runtime_database():
    # Signatures: ledger connection or URL plus explicit values, nothing from the runtime side.
    assert list(inspect.signature(episode_module.open_ledger).parameters) == ["ledger_url"]
    assert list(inspect.signature(episode_module.score_ledger).parameters) == [
        "ledger",
        "namespace",
        "spec",
        "raw_claim",
        "completed_before_deadline",
    ]
    assert list(inspect.signature(evaluate_episode).parameters) == [
        "ledger_url",
        "namespace",
        "spec",
        "raw_claim",
        "completed_before_deadline",
    ]
    # Bodies: no identifier or attribute that names the runtime side (docstrings and string
    # literals are not identifiers, so a docstring may still explain the rule).
    nodes = _function_nodes(EVALUATOR_FUNCTIONS)
    assert len(nodes) == len(EVALUATOR_FUNCTIONS)
    for node in nodes:
        identifiers = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
        }
        identifiers |= {a.arg for a in ast.walk(node) if isinstance(a, ast.arg)}
        assert not {i for i in identifiers if "runtime" in i.lower() or i in ("rt", "config")}, (
            node.name,
            identifiers,
        )


def test_the_oracle_imports_nothing_from_the_runtime_or_the_runner():
    root = Path(episode_module.__file__).parents[1] / "oracle"
    files = sorted(root.glob("*.py"))
    assert {f.name for f in files} >= {"ledger.py", "score.py"}
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not name.startswith(("lab.runtime", "lab.runner", "lab.agent")), (
                    path.name,
                    name,
                )


def test_the_rescore_path_names_only_the_runner_tables():
    # The runner may read its own run record, never the agent's logs. Every SQL string on the
    # rescore path must name only `runs` and `results`.
    nodes = _function_nodes(("rescore_run", "load_run_record", "_insert_result", "_transition"))
    assert len(nodes) == 4
    tables: set[str] = set()
    for node in nodes:
        docstring = ast.get_docstring(node, clean=False)
        for const in ast.walk(node):
            if isinstance(const, ast.Constant) and isinstance(const.value, str):
                if const.value == docstring:
                    continue
                tables |= set(
                    re.findall(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+(\w+)", const.value, re.IGNORECASE)
                )
    assert tables == {"runs", "results"}


def test_the_verdict_follows_the_returned_claim_not_a_row_the_runtime_holds(
    runner_config, rt, monkeypatch
):
    # Rewrite runs.claimed_outcome in the runtime database just before scoring. The verdict
    # must still be the one for the claim the graph returned ("succeeded", a false success).
    original = episode_module.score_ledger

    def tampering(ledger, namespace, spec, raw_claim, completed_before_deadline):
        rt.execute("UPDATE runs SET claimed_outcome = 'failed' WHERE namespace = %s", (namespace,))
        return original(ledger, namespace, spec, raw_claim, completed_before_deadline)

    monkeypatch.setattr(episode_module, "score_ledger", tampering)
    result = run("false_success", World.TRUE_FAIL, runner_config)
    assert result_row(rt, result.run_id)["verdict"] == "false_success"


# ---------------------------------------------------------------------------
# A misconfigured ledger URL is found before anything is spent or written
# ---------------------------------------------------------------------------


class CountingModel:
    """A scripted model that counts how often it is asked, for the 'never called' checks."""

    def __init__(self, policy: str = "stable_key"):
        self.inner = ScriptedModel(policy)
        self.model_id = self.inner.model_id
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        return self.inner.complete(messages, tools)


def with_ledger_url(config: RunnerConfig, url: str) -> RunnerConfig:
    return RunnerConfig(**{**config.__dict__, "ledger_url": url})


def assert_nothing_was_written(rt, model: CountingModel, spec) -> None:
    assert model.calls == 0
    assert rt.execute("SELECT 1 FROM experiments WHERE name = %s", (NAME,)).fetchone() is None
    assert (
        rt.execute("SELECT 1 FROM runs WHERE episode_id = %s", (spec.episode_id,)).fetchone()
        is None
    )


def run_with_ledger(config: RunnerConfig, rt) -> tuple[CountingModel, Any]:
    model = CountingModel()
    spec = spec_for(World.LOST_ACK)
    try:
        run_spec(spec, model, config, manifest_sha256=SHA, experiment_name=NAME)
    except Exception as exc:
        return model, exc
    raise AssertionError("run_spec accepted a misconfigured ledger URL")


def test_the_payment_service_role_is_refused_before_the_episode_starts(
    runner_config, payments_db, rt
):
    config = with_ledger_url(runner_config, payments_db["svc"])  # a real role, the wrong one
    model, error = run_with_ledger(config, rt)
    assert isinstance(error, EvaluatorRoleError)
    assert "payment_svc" in str(error)
    assert_nothing_was_written(rt, model, spec_for(World.LOST_ACK))


def test_a_wrong_ledger_password_is_refused_before_the_episode_starts(
    runner_config, payments_db, rt
):
    url = db.with_database(
        payments_db["admin"], payments_db["name"], "ledger_reader", "not-the-password"
    )
    model, error = run_with_ledger(with_ledger_url(runner_config, url), rt)
    assert isinstance(error, LedgerConfigError)
    assert isinstance(error.__cause__, psycopg.OperationalError)
    assert_nothing_was_written(rt, model, spec_for(World.LOST_ACK))


def test_a_ledger_host_that_is_not_there_is_refused_before_the_episode_starts(
    runner_config, payments_db, rt
):
    url = payments_db["reader"].replace("localhost:54329", "127.0.0.1:1")
    assert url != payments_db["reader"]
    model, error = run_with_ledger(with_ledger_url(runner_config, url), rt)
    assert isinstance(error, LedgerConfigError)
    assert_nothing_was_written(rt, model, spec_for(World.LOST_ACK))


def test_a_database_without_the_ledger_tables_is_refused_before_the_episode_starts(
    runner_config, rt, admin_url, role_url
):
    empty = f"empty_ledger_{uuid.uuid4().hex[:8]}"
    db.ensure_database(admin_url, empty)
    try:
        url = role_url("ledger_reader", empty)
        model, error = run_with_ledger(with_ledger_url(runner_config, url), rt)
    finally:
        db.drop_database(admin_url, empty)
    assert isinstance(error, LedgerConfigError)
    assert "charges" in str(error)
    assert_nothing_was_written(rt, model, spec_for(World.LOST_ACK))


def test_a_ledger_url_that_points_at_the_runtime_database_is_refused(
    runner_config, runtime_db, rt, role_url
):
    # By design ledger_reader cannot connect to the runtime database (CONNECT is revoked from
    # PUBLIC) and the runtime database has no ledger tables. Undo both, as one database made
    # to serve as both would be, so the runner's own check is what stops the run.
    url = role_url("ledger_reader", runtime_db["name"])
    with psycopg.connect(runtime_db["admin"], autocommit=True) as admin:
        admin.execute(f'GRANT CONNECT ON DATABASE "{runtime_db["name"]}" TO ledger_reader')
        admin.execute("CREATE TABLE charges (id int)")
        admin.execute("CREATE TABLE call_log (id int)")
        admin.execute("GRANT SELECT ON charges, call_log TO ledger_reader")
        try:
            model, error = run_with_ledger(with_ledger_url(runner_config, url), rt)
        finally:
            admin.execute("DROP TABLE charges, call_log")
            admin.execute(f'REVOKE CONNECT ON DATABASE "{runtime_db["name"]}" FROM ledger_reader')
    assert isinstance(error, LedgerConfigError)
    assert not isinstance(error, EvaluatorRoleError)
    assert "runtime database" in str(error)
    assert_nothing_was_written(rt, model, spec_for(World.LOST_ACK))


# ---------------------------------------------------------------------------
# A scoring failure after the episode is recorded, and can be repaired
# ---------------------------------------------------------------------------


class LedgerKiller:
    """A scripted model that, on its first call, terminates the evaluator's ledger session
    from outside: a real failure of the connection the runner opened before the episode."""

    def __init__(self, admin_url: str, application_name: str):
        self.inner = ScriptedModel("stable_key")
        self.model_id = self.inner.model_id
        self._admin_url = admin_url
        self._application_name = application_name
        self.killed = 0

    def complete(self, messages, tools):
        if not self.killed:
            with psycopg.connect(self._admin_url, autocommit=True) as admin:
                rows = admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE application_name = %s",
                    (self._application_name,),
                ).fetchall()
            self.killed = len(rows)
            assert self.killed == 1, "the ledger session was not found"
        return self.inner.complete(messages, tools)


def test_a_scoring_failure_leaves_a_score_failed_run_that_rescore_run_finishes(
    runner_config, payments_db, rt
):
    app = f"ledger-kill-{uuid.uuid4().hex[:8]}"
    config = with_ledger_url(runner_config, payments_db["reader"] + f"?application_name={app}")
    spec = spec_for(World.LOST_ACK)
    model = LedgerKiller(payments_db["admin"], app)

    with pytest.raises(ScoringError, match="score_failed") as raised:
        run_spec(spec, model, config, manifest_sha256=SHA, experiment_name=NAME)
    assert isinstance(raised.value.__cause__, psycopg.OperationalError)
    assert model.killed == 1

    # Not a bare 'completed': a state of its own, with the agent's side of the record intact
    # and no results row.
    row = one(rt, "SELECT * FROM runs WHERE episode_id = %s", spec.episode_id)
    assert row["state"] == "score_failed"
    assert row["claimed_outcome"] == "succeeded"
    assert row["finished_at"] is not None
    assert rt.execute("SELECT 1 FROM results WHERE run_id = %s", (row["id"],)).fetchone() is None

    # The same episode cannot be run again in this experiment ...
    with pytest.raises(EpisodeAlreadyRunError):
        run_spec(
            spec,
            ScriptedModel("stable_key"),
            runner_config,
            manifest_sha256=SHA,
            experiment_name=NAME,
        )

    # ... but its ledger can be scored, once the evaluator works again.
    score = rescore_run(spec, row["id"], runner_config)
    assert score.verdict is Verdict.CORRECT
    after = one(rt, "SELECT state FROM runs WHERE id = %s", row["id"])
    assert after["state"] == "completed"
    stored = result_row(rt, row["id"])
    assert stored["verdict"] == "correct"
    assert stored["score_json"]["completed_before_deadline"] is True
    assert stored["evaluator_version"] == EVALUATOR_VERSION

    # A run that is not score_failed is not rescored, and neither is another episode's.
    with pytest.raises(episode_module.RunnerError, match="completed"):
        rescore_run(spec, row["id"], runner_config)


def _score_failed_run(config, payments_db, rt, spec):
    app = f"ledger-kill-{uuid.uuid4().hex[:8]}"
    broken = with_ledger_url(config, payments_db["reader"] + f"?application_name={app}")
    with pytest.raises(ScoringError):
        run_spec(
            spec,
            LedgerKiller(payments_db["admin"], app),
            broken,
            manifest_sha256=SHA,
            experiment_name=NAME,
        )
    return one(rt, "SELECT * FROM runs WHERE episode_id = %s", spec.episode_id)


def test_rescore_ignores_the_agents_logs_and_matches_the_normal_path(
    runner_config, payments_db, rt
):
    spec = spec_for(World.LOST_ACK)
    row = _score_failed_run(runner_config, payments_db, rt, spec)
    assert row["claimed_outcome"] == "succeeded"

    # What the normal path would have produced for this very run: the evaluator, handed the
    # claim and the deadline comparison the runner had in memory.
    expected = evaluate_episode(
        runner_config.ledger_url,
        row["namespace"],
        spec,
        "succeeded",
        row["finished_at"] <= row["deadline_at"],
    )

    # Tamper with everything the agent side logged: a report_done event and a model call that
    # say the episode failed. A rescore that read them would return a different verdict.
    rt.execute(
        "INSERT INTO tool_events (run_id, turn, tool, requested_args_json, rule, observation)"
        " VALUES (%s, 99, 'report_done', %s, 'report_done', 'Recorded.')",
        (row["id"], Jsonb({"outcome": "failed"})),
    )
    rt.execute(
        "INSERT INTO model_calls (run_id, turn, provider_model_id, request_json, response_json,"
        " tool_calls_json) VALUES (%s, 99, 'tampered', '{}', %s, %s)",
        (
            row["id"],
            Jsonb({"claim": "failed"}),
            Jsonb([{"name": "report_done", "arguments": {"outcome": "failed"}}]),
        ),
    )
    rt.execute(
        "UPDATE tool_events SET requested_args_json = %s WHERE run_id = %s AND tool = 'report_done'"
        " AND turn <> 99",
        (Jsonb({"outcome": "failed"}), row["id"]),
    )

    score = rescore_run(spec, row["id"], runner_config)

    assert score == expected
    assert score.verdict is Verdict.CORRECT
    stored = result_row(rt, row["id"])
    assert stored["verdict"] == "correct"
    assert stored["score_json"] == json.loads(json.dumps(dataclasses.asdict(expected)))

    # And it is the verdict an untampered normal run of the same episode earns.
    normal = run("stable_key", World.LOST_ACK, runner_config, experiment_id="exp-normal-path")
    assert normal.score.verdict is score.verdict


def test_rescore_reads_only_the_run_record_from_the_runtime_database(
    runner_config, payments_db, rt, monkeypatch
):
    spec = spec_for(World.LOST_ACK)
    row = _score_failed_run(runner_config, payments_db, rt, spec)

    queries: list[tuple[str, str]] = []

    class LoggingCursor(psycopg.Cursor):
        def execute(self, query, *args, **kwargs):
            queries.append(
                (
                    self.connection.info.user,
                    str(getattr(query, "as_string", lambda _: query)(self.connection))
                    if not isinstance(query, str)
                    else query,
                )
            )
            return super().execute(query, *args, **kwargs)

    real_connect = psycopg.connect

    def logging_connect(*args, **kwargs):
        return real_connect(*args, cursor_factory=LoggingCursor, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(psycopg, "connect", logging_connect)
        rescore_run(spec, row["id"], runner_config)

    def tables(user):
        found: set[str] = set()
        for who, query in queries:
            if who == user:
                found |= set(
                    re.findall(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+(\w+)", query, re.IGNORECASE)
                )
        return found

    assert queries, "the spy saw no queries, so it proves nothing"
    assert tables("runtime_svc") == {"runs", "results"}
    assert tables("ledger_reader") == {"charges", "call_log"}


def test_rescore_run_refuses_a_spec_of_another_episode(runner_config, rt, payments_db):
    app = f"ledger-kill-{uuid.uuid4().hex[:8]}"
    config = with_ledger_url(runner_config, payments_db["reader"] + f"?application_name={app}")
    spec = spec_for(World.LOST_ACK)
    with pytest.raises(ScoringError):
        run_spec(
            spec,
            LedgerKiller(payments_db["admin"], app),
            config,
            manifest_sha256=SHA,
            experiment_name=NAME,
        )
    run_id = one(rt, "SELECT id FROM runs WHERE episode_id = %s", spec.episode_id)["id"]
    with pytest.raises(episode_module.RunnerError, match="not"):
        rescore_run(spec_for(World.TRUE_FAIL, rep=9), run_id, runner_config)
    assert one(rt, "SELECT state FROM runs WHERE id = %s", run_id)["state"] == "score_failed"


def test_a_client_that_cannot_be_built_crashes_the_run_instead_of_leaving_it_running(
    runner_config, rt, monkeypatch
):
    def broken(*args, **kwargs):
        raise RuntimeError("cannot build the client")

    monkeypatch.setattr(episode_module, "SimulatorClient", broken)
    spec = spec_for(World.LOST_ACK)
    with pytest.raises(RuntimeError, match="cannot build"):
        run_spec(
            spec,
            ScriptedModel("stable_key"),
            runner_config,
            manifest_sha256=SHA,
            experiment_name=NAME,
        )
    row = one(rt, "SELECT state, finished_at FROM runs WHERE episode_id = %s", spec.episode_id)
    assert row["state"] == "crashed"
    assert row["finished_at"] is not None


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
