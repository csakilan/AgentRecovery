"""Run one `EpisodeSpec` end to end and score it.

Steps, in order, for `run_spec`:

1. Refuse a mode other than `agent_directed` (this plan has one executor, `DirectExecutor`).
2. Open the runtime connection and the ledger connection, and check the ledger one: it must
   be the `ledger_reader` role, on a database other than the runtime one, able to read the
   ledger tables. A wrong URL fails here, before anything is written and before a model is
   called, so a misconfigured evaluator is found at no cost.
3. Create or reuse the `experiments` row, then write a `runs` row in state `queued`.
4. Move it to `running`, stamping `started_at` and `deadline_at`.
5. Configure a fresh namespace through the simulator's control API (the spec's profile and
   fault plan), build the graph and run it with `durability="sync"`.
6. Move the run to `completed` (or `crashed`, re-raising) and stamp `finished_at`.
7. Score the ledger with the oracle through the connection opened in step 2 and write one
   `results` row. If that fails the run becomes `score_failed` (not a bare `completed`), and
   `rescore_run` can finish it later.

What the model controls and what it does not. The model chooses tool arguments and the
final claim. Everything that decides where a charge lands comes from here: the tenant is a
configuration constant, the namespace is generated per run, the run id and thread id are
generated per run, and the episode id and task id come from the spec. They are put in a
`TrustedContext`, which the simulator client reads once, and nothing in it is read back from
a model message. The run and episode ids also go to the tracing context (`lab.obs.episode`),
which stamps them on the episode, model and tool spans; tracing is off unless
`OTEL_EXPORTER_OTLP_ENDPOINT` is set and never changes what the episode does.

Evaluator and runner are kept apart, and the split is the constraint "the evaluator reads the
payment ledger through `ledger_reader` and never reads the runtime database to decide a
verdict".

* The evaluator is `open_ledger`, `score_ledger` and `evaluate_episode` here, with the oracle
  (`lab.oracle`) underneath. Their inputs are a ledger connection (or URL) and explicit
  values: the spec, the namespace, the claim and `completed_before_deadline`. None has a
  runtime parameter, and none touches the runtime database or imports a runtime module
  (tests/runner/test_episode.py checks signatures, identifiers and imports).
* The runner is everything around it. It owns the run record (the `runs` row) and decides
  what to hand the evaluator. `run_spec` hands it the claim and the finish time it has in
  memory from the graph's return value. `rescore_run` has no memory of the episode, so it
  hands the evaluator the same two facts as the runner recorded them on the `runs` row. It
  reads that row's runner-written columns only, never `model_calls`, `tool_events`,
  `crash_points` or the checkpoint tables, which are the agent's own logs.

The checkpointer is a `PostgresSaver` as `runtime_svc`. The runner never calls `setup()`:
the checkpoint tables are created by `python -m lab.runtime migrate`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.types.json import Jsonb

from lab import obs
from lab.agent.client import SimulatorClient, TrustedContext
from lab.agent.executor import DirectExecutor
from lab.agent.graph import EpisodeState, PostgresRecorder, build_graph, run_episode
from lab.agent.model import ModelClient
from lab.agent.prompts import PROMPT_VERSION, system_prompt, user_prompt
from lab.agent.tools import TOOL_SCHEMA_VERSION, tool_schemas
from lab.oracle import EVALUATOR_VERSION
from lab.oracle.ledger import read_ledger
from lab.oracle.score import EpisodeScore, Expectation, score_episode
from lab.protocol.manifest import EpisodeSpec, Mode

# D11: every mode checkpoints with "sync", so no mode is favoured by weaker checkpointing.
# A constant, not a setting: nothing in this module can pass anything else.
DURABILITY = "sync"

LEDGER_ROLE = "ledger_reader"

DEFAULT_PUBLIC_URL = "http://127.0.0.1:8100"
DEFAULT_CONTROL_URL = "http://127.0.0.1:8101"


class RunnerError(Exception):
    """Base class for refusals and setup failures the runner raises itself."""


class UnsupportedModeError(RunnerError):
    """The spec's mode has no executor in this plan."""


class ConfigError(RunnerError):
    """The runner's own configuration is incomplete."""


class NamespaceConfigError(RunnerError):
    """The simulator's control API refused to set up the run's namespace."""


class ExperimentMismatchError(RunnerError):
    """An experiment id is being reused with a different name, manifest or config."""


class EpisodeAlreadyRunError(RunnerError):
    """The experiment already has a run for this episode id."""


class LedgerConfigError(RunnerError):
    """The ledger URL cannot be used to evaluate: it does not connect, or it is not the
    evaluator's view of the payments database."""


class EvaluatorRoleError(LedgerConfigError):
    """The ledger connection is not the `ledger_reader` role."""


class ScoringError(RunnerError):
    """The episode ran but its verdict could not be written. The run is `score_failed`;
    `rescore_run` finishes it."""


@dataclass(frozen=True)
class RunnerConfig:
    """Where the runner connects and the limits it applies. Secrets are left out of repr."""

    sim_public_url: str
    sim_control_url: str
    client_token: str = field(repr=False)
    control_token: str = field(repr=False)
    runtime_url: str = field(repr=False)
    ledger_url: str = field(repr=False)
    tenant: str = "lab"
    max_turns: int = 10
    episode_deadline_s: float = 300.0
    http_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        if not self.tenant:
            raise ConfigError("tenant must not be empty")
        if self.max_turns < 1:
            raise ConfigError(f"max_turns must be at least 1, got {self.max_turns}")
        if self.episode_deadline_s < 0:
            raise ConfigError(f"episode_deadline_s must not be negative: {self.episode_deadline_s}")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> RunnerConfig:
        """Build a config from the environment of `.env.example`.

        Required: SIM_CLIENT_TOKEN, SIM_CONTROL_TOKEN, LAB_RUNTIME_URL, LAB_LEDGER_READER_URL.
        Optional: SIM_PUBLIC_URL and SIM_CONTROL_URL (loopback ports 8100 and 8101 by default).
        Keyword `overrides` replace any field.
        """
        env = os.environ if environ is None else environ
        required = {
            "client_token": "SIM_CLIENT_TOKEN",
            "control_token": "SIM_CONTROL_TOKEN",
            "runtime_url": "LAB_RUNTIME_URL",
            "ledger_url": "LAB_LEDGER_READER_URL",
        }
        missing = [var for key, var in required.items() if key not in overrides and var not in env]
        if missing:
            raise ConfigError(f"missing environment variables: {', '.join(missing)}")
        values: dict[str, Any] = {
            "sim_public_url": env.get("SIM_PUBLIC_URL", DEFAULT_PUBLIC_URL),
            "sim_control_url": env.get("SIM_CONTROL_URL", DEFAULT_CONTROL_URL),
        }
        values.update({key: env[var] for key, var in required.items() if var in env})
        values.update(overrides)
        return cls(**values)


@dataclass(frozen=True)
class EpisodeResult:
    """What `run_spec` returns. The authoritative record is the database rows it wrote."""

    experiment_id: str
    run_id: str
    episode_id: str
    namespace: str
    thread_id: str
    claimed_outcome: str | None
    ended_by: str | None
    turns: int
    started_at: datetime
    deadline_at: datetime
    finished_at: datetime
    score: EpisodeScore


def experiment_config(model_id: str, config: RunnerConfig, manifest_sha256: str) -> dict[str, Any]:
    """The `experiments.config_json` of a run: everything that changes what is measured.

    Durability sits beside the model id and the prompt version, as the plan's global
    constraint requires."""
    return {
        "durability": DURABILITY,
        "model_id": model_id,
        "prompt_version": PROMPT_VERSION,
        "tool_schema_version": TOOL_SCHEMA_VERSION,
        "max_turns": config.max_turns,
        "episode_deadline_s": config.episode_deadline_s,
        "evaluator_version": EVALUATOR_VERSION,
        "tenant": config.tenant,
        "manifest_sha256": manifest_sha256,
    }


def derive_experiment_id(name: str, cfg: Mapping[str, Any]) -> str:
    """A stable id for a name and config, so the same setup reuses one experiments row."""
    canonical = json.dumps({"name": name, "config": cfg}, sort_keys=True, separators=(",", ":"))
    return "exp-" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _slug(text: str, limit: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")
    return slug or "x"


def new_namespace(experiment_id: str, episode_id: str, run_token: str) -> str:
    """A namespace for one run: lowercase letters, digits and hyphens only, so it is safe as
    a URL path segment and an HTTP header value. The simulator puts no limit on a namespace
    (a TEXT key), so the only rules here are those two. `run_token` is fresh per run, which
    is what makes the whole string unique."""
    return f"{_slug(experiment_id, 24)}-{_slug(episode_id, 40)}-{run_token}"


def _new_run_token() -> str:
    return uuid.uuid4().hex[:20]


def ensure_experiment(
    conn: psycopg.Connection[Any],
    experiment_id: str,
    name: str,
    manifest_sha256: str,
    cfg: Mapping[str, Any],
) -> None:
    """Create the experiments row, or confirm the existing one is identical.

    A row that exists with a different name, manifest hash or config is an error: reusing it
    would record this run under settings it did not run with."""
    conn.execute(
        "INSERT INTO experiments (id, name, manifest_sha256, config_json)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (experiment_id, name, manifest_sha256, Jsonb(dict(cfg))),
    )
    row = conn.execute(
        "SELECT name, manifest_sha256, config_json FROM experiments WHERE id = %s",
        (experiment_id,),
    ).fetchone()
    assert row is not None
    stored = {"name": row[0], "manifest_sha256": row[1], "config": row[2]}
    wanted = {"name": name, "manifest_sha256": manifest_sha256, "config": dict(cfg)}
    if stored != wanted:
        raise ExperimentMismatchError(
            f"experiment {experiment_id!r} exists with different settings: "
            f"stored {stored}, requested {wanted}"
        )


def _insert_queued_run(
    conn: psycopg.Connection[Any],
    *,
    run_id: str,
    experiment_id: str,
    spec: EpisodeSpec,
    namespace: str,
    thread_id: str,
) -> None:
    try:
        conn.execute(
            "INSERT INTO runs (id, experiment_id, episode_id, namespace, mode, thread_id, state)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'queued')",
            (run_id, experiment_id, spec.episode_id, namespace, spec.mode.value, thread_id),
        )
    except psycopg.errors.UniqueViolation as exc:
        raise EpisodeAlreadyRunError(
            f"experiment {experiment_id!r} already has a run for episode {spec.episode_id!r}"
        ) from exc


def run_is_complete(rt: psycopg.Connection[Any], experiment_id: str, episode_id: str) -> bool:
    """True iff the episode has a `completed` run with a `results` row: it ran and was scored."""
    row = rt.execute(
        "SELECT 1 FROM runs JOIN results ON results.run_id = runs.id"
        " WHERE runs.experiment_id = %s AND runs.episode_id = %s AND runs.state = 'completed'",
        (experiment_id, episode_id),
    ).fetchone()
    return row is not None


# States whose run holds nothing worth keeping: it never reached a verdict. `score_failed` is
# left out on purpose, because its agent side is complete and `rescore_run` finishes it.
_CLEARABLE_STATES = ("queued", "running", "crashed")


def clear_incomplete_run(rt: psycopg.Connection[Any], experiment_id: str, episode_id: str) -> None:
    """Delete the episode's run row so the episode can be run again, if it never finished.

    Only a `queued`, `running` or `crashed` run is deleted (its model_calls, tool_events and
    crash_points go with it by cascade). A `completed` or `score_failed` run is never touched,
    which is what makes resuming a batch safe: a scored result cannot be destroyed."""
    rt.execute(
        "DELETE FROM runs WHERE experiment_id = %s AND episode_id = %s AND state = ANY(%s)",
        (experiment_id, episode_id, list(_CLEARABLE_STATES)),
    )


def _transition(
    conn: psycopg.Connection[Any], run_id: str, source: str, target: str, **columns: Any
) -> None:
    """Move a run from `source` to `target` and set `columns`; refuse any other starting state."""
    assignments = ", ".join(["state = %s", *(f"{name} = %s" for name in columns)])
    cursor = conn.execute(
        f"UPDATE runs SET {assignments} WHERE id = %s AND state = %s",
        (target, *columns.values(), run_id, source),
    )
    if cursor.rowcount != 1:
        raise RunnerError(f"run {run_id!r} was not in state {source!r}; cannot move to {target!r}")


def configure_namespace(config: RunnerConfig, namespace: str, spec: EpisodeSpec) -> None:
    """PUT the spec's profile and fault plan to the control API, creating (or resetting) the
    namespace. Raises `NamespaceConfigError` on any answer but 200."""
    response = httpx.put(
        f"{config.sim_control_url.rstrip('/')}/internal/namespaces/{quote(namespace, safe='')}",
        json={
            "profile": spec.profile.value,
            "fault_plan": spec.fault_plan.model_dump(mode="json"),
        },
        headers={"Authorization": f"Bearer {config.control_token}"},
        timeout=config.http_timeout_s,
    )
    if response.status_code != 200:
        raise NamespaceConfigError(
            f"the control API answered {response.status_code} configuring namespace "
            f"{namespace!r}: {response.text[:200]}"
        )


LEDGER_TABLES = ("charges", "call_log")


@contextmanager
def open_ledger(ledger_url: str) -> Iterator[psycopg.Connection[Any]]:
    """Connect to the ledger as the evaluator and check the connection is the right one.

    Checked before the connection is returned: it opens; the user is `ledger_reader`
    (`EvaluatorRoleError` otherwise); and the ledger tables can be read. Anything wrong is a
    `LedgerConfigError`. This is evaluator code: it knows nothing of the runtime database
    (the runner checks the two are different databases, in `check_ledger_is_not_runtime`)."""
    try:
        conn = psycopg.connect(ledger_url, autocommit=True)
    except psycopg.OperationalError as exc:
        raise LedgerConfigError(f"cannot connect to the ledger: {exc}") from exc
    try:
        user, dbname = conn.execute("SELECT current_user, current_database()").fetchone()
        if user != LEDGER_ROLE:
            raise EvaluatorRoleError(
                f"the evaluator must read the ledger as {LEDGER_ROLE!r}, not {user!r}"
            )
        for table in LEDGER_TABLES:
            try:
                conn.execute(f"SELECT 1 FROM {table} LIMIT 0")
            except psycopg.Error as exc:
                raise LedgerConfigError(
                    f"{LEDGER_ROLE} cannot read {table!r} in database {dbname!r}: {exc}"
                ) from exc
        yield conn
    finally:
        conn.close()


def check_ledger_is_not_runtime(
    ledger: psycopg.Connection[Any], runtime: psycopg.Connection[Any]
) -> None:
    """Runner-side check that the ledger URL does not point at the agent's own database.

    It compares two database names and reads nothing else. It lives here, not in
    `open_ledger`, so that the evaluator code has no use for a runtime connection."""
    ledger_db = ledger.execute("SELECT current_database()").fetchone()[0]
    runtime_db = runtime.execute("SELECT current_database()").fetchone()[0]
    if ledger_db == runtime_db:
        raise LedgerConfigError(f"the ledger URL points at the runtime database {runtime_db!r}")


def score_ledger(
    ledger: psycopg.Connection[Any],
    namespace: str,
    spec: EpisodeSpec,
    raw_claim: str | None,
    completed_before_deadline: bool,
) -> EpisodeScore:
    """Score one episode from an open ledger connection (see `open_ledger`) and the claim."""
    charges, calls = read_ledger(ledger, namespace)
    return score_episode(
        charges, calls, Expectation.from_spec(spec), raw_claim, completed_before_deadline
    )


def evaluate_episode(
    ledger_url: str,
    namespace: str,
    spec: EpisodeSpec,
    raw_claim: str | None,
    completed_before_deadline: bool,
) -> EpisodeScore:
    """Score one episode from the ledger and the claim, opening its own connection.

    The ledger is read through `ledger_url` only, and the connection must be the
    `ledger_reader` role (`EvaluatorRoleError` otherwise). There is no runtime database
    parameter, so nothing the agent side recorded can reach the verdict. The claim is the one
    the graph returned, which the oracle treats as untrusted."""
    with open_ledger(ledger_url) as ledger:
        return score_ledger(ledger, namespace, spec, raw_claim, completed_before_deadline)


def _insert_result(conn: psycopg.Connection[Any], run_id: str, score: EpisodeScore) -> None:
    conn.execute(
        "INSERT INTO results (run_id, evaluator_version, verdict, correct_recovery, score_json)"
        " VALUES (%s, %s, %s, %s, %s)",
        (
            run_id,
            EVALUATOR_VERSION,
            score.verdict.value,
            score.correct_recovery,
            Jsonb(dataclasses.asdict(score)),
        ),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run_spec(
    spec: EpisodeSpec,
    model: ModelClient,
    config: RunnerConfig,
    *,
    manifest_sha256: str,
    experiment_name: str = "adhoc",
    experiment_id: str | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> EpisodeResult:
    """Run `spec` with `model`, write the runtime records and the result, return a summary.

    `experiment_id` defaults to an id derived from the name and the config, so the same setup
    shares one row. `clock` is read for `started_at` and `finished_at`: twice on a run that
    completes, and once more if it crashes.

    An exception from the namespace setup or the graph leaves the run `crashed` with
    `finished_at` set, and is re-raised. An exception from scoring happens after the run was
    marked `completed`: the run stays completed with no results row, and the ledger can be
    scored again later.
    """
    if spec.mode is not Mode.AGENT_DIRECTED:
        raise UnsupportedModeError(
            f"mode {spec.mode.value!r} is not implemented in Plan 2, which has only the "
            f"{Mode.AGENT_DIRECTED.value!r} executor; the other modes arrive in Plan 4"
        )

    cfg = experiment_config(model.model_id, config, manifest_sha256)
    experiment = experiment_id or derive_experiment_id(experiment_name, cfg)
    run_token = _new_run_token()
    run_id = f"run-{run_token}"
    thread_id = f"thread-{run_token}"
    namespace = new_namespace(experiment, spec.episode_id, run_token)
    ctx = TrustedContext(
        tenant=config.tenant,
        namespace=namespace,
        run_id=run_id,
        episode_id=spec.episode_id,
        task_id=f"charge-order-{spec.task.order_id}",
    )

    with (
        psycopg.connect(config.runtime_url, autocommit=True) as rt,
        open_ledger(config.ledger_url) as ledger,
    ):
        check_ledger_is_not_runtime(ledger, rt)
        ensure_experiment(rt, experiment, experiment_name, manifest_sha256, cfg)
        _insert_queued_run(
            rt,
            run_id=run_id,
            experiment_id=experiment,
            spec=spec,
            namespace=namespace,
            thread_id=thread_id,
        )
        started_at = clock()
        deadline_at = started_at + timedelta(seconds=config.episode_deadline_s)
        _transition(rt, run_id, "queued", "running", started_at=started_at, deadline_at=deadline_at)

        client: SimulatorClient | None = None
        try:
            client = SimulatorClient(
                config.sim_public_url, config.client_token, ctx, timeout=config.http_timeout_s
            )
            configure_namespace(config, namespace, spec)
            messages = [
                {"role": "system", "content": system_prompt(spec.prompt_variant)},
                {"role": "user", "content": user_prompt(spec.task, spec.profile)},
            ]
            with (
                PostgresSaver.from_conn_string(config.runtime_url) as saver,
                obs.episode(run_id, spec.episode_id) as trace,
            ):
                graph = build_graph(
                    model,
                    DirectExecutor(client),
                    tool_schemas(spec.profile, spec.status_available),
                    config.max_turns,
                    recorder=PostgresRecorder(rt, run_id),
                    checkpointer=saver,
                    trace=trace,
                )
                state: EpisodeState = run_episode(graph, thread_id, messages, durability=DURABILITY)
                trace.finish(ended_by=state["ended_by"], turns=state["turn"])
        except BaseException:
            _transition(rt, run_id, "running", "crashed", finished_at=clock())
            raise
        finally:
            if client is not None:
                client.close()

        finished_at = clock()
        _transition(
            rt,
            run_id,
            "running",
            "completed",
            claimed_outcome=state["claimed_outcome"],
            finished_at=finished_at,
        )
        try:
            score = score_ledger(
                ledger,
                namespace,
                spec,
                state["claimed_outcome"],
                completed_before_deadline=finished_at <= deadline_at,
            )
            _insert_result(rt, run_id, score)
        except Exception as exc:
            _transition(rt, run_id, "completed", "score_failed")
            raise ScoringError(
                f"run {run_id!r} finished but could not be scored and is now 'score_failed'; "
                f"rescore it with rescore_run: {exc}"
            ) from exc

    return EpisodeResult(
        experiment_id=experiment,
        run_id=run_id,
        episode_id=spec.episode_id,
        namespace=namespace,
        thread_id=thread_id,
        claimed_outcome=state["claimed_outcome"],
        ended_by=state["ended_by"],
        turns=state["turn"],
        started_at=started_at,
        deadline_at=deadline_at,
        finished_at=finished_at,
        score=score,
    )


@dataclass(frozen=True)
class RunRecord:
    """What the runner wrote about one run, read back from its `runs` row."""

    state: str
    episode_id: str
    namespace: str
    claimed_outcome: str | None
    finished_at: datetime
    deadline_at: datetime


def load_run_record(rt: psycopg.Connection[Any], run_id: str) -> RunRecord:
    """Read the runner-written columns of one `runs` row, and nothing else.

    These are the values `run_spec` wrote from the graph's return value and its own clock
    (`claimed_outcome`, `finished_at`, `deadline_at`) and from its own generation
    (`namespace`, `episode_id`). It must not grow a query on `model_calls`, `tool_events`,
    `crash_points` or the checkpoint tables: those are the agent's logs."""
    row = rt.execute(
        "SELECT state, episode_id, namespace, claimed_outcome, finished_at, deadline_at"
        " FROM runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    if row is None:
        raise RunnerError(f"no run {run_id!r}")
    return RunRecord(*row)


def rescore_run(spec: EpisodeSpec, run_id: str, config: RunnerConfig) -> EpisodeScore:
    """Finish a `score_failed` run: score its ledger, write the results row, mark it completed.

    This is runner code, not the evaluator. The episode's claim and finish time were in
    memory when `run_spec` scored the run; here they are gone, so they are read from the
    runner's own run record (`load_run_record`: the `runs` row the runner wrote from the
    graph's return value and its clock). That is the same claim and the same
    `finished_at <= deadline_at` the normal path hands the evaluator, so the verdict is the
    one the normal path would have produced. The agent's tool and model logs are never read.
    The evaluator (`score_ledger`) then gets them as plain arguments, like any other caller.

    The spec must be the one the run was made from (checked by episode id), and a run in any
    state but `score_failed` is refused. Run it again after fixing whatever broke the
    evaluator. The two writes are one transaction, so a failure leaves the run `score_failed`."""
    with (
        psycopg.connect(config.runtime_url, autocommit=True) as rt,
        open_ledger(config.ledger_url) as ledger,
    ):
        check_ledger_is_not_runtime(ledger, rt)
        record = load_run_record(rt, run_id)
        if record.state != "score_failed":
            raise RunnerError(
                f"run {run_id!r} is {record.state!r}; only a 'score_failed' run is rescored"
            )
        if record.episode_id != spec.episode_id:
            raise RunnerError(
                f"run {run_id!r} is episode {record.episode_id!r}, not {spec.episode_id!r}"
            )
        score = score_ledger(
            ledger,
            record.namespace,
            spec,
            record.claimed_outcome,
            record.finished_at <= record.deadline_at,
        )
        with rt.transaction():
            _insert_result(rt, run_id, score)
            _transition(rt, run_id, "score_failed", "completed")
    return score
