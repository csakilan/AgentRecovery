"""Tracing is optional, silent by default, and never part of the measurement.

Three things are pinned. With no OTEL_EXPORTER_OTLP_ENDPOINT nothing is created, started,
exported or raised. With an in-memory exporter installed, one episode is one trace: the
episode span is the root and every model call and tool call is its child, with the run and
episode ids from the runner (not from the model) on each. And a tracing component that
breaks, an exporter or the tracer itself, changes nothing about the episode.

Graph-level tests use an in-memory checkpointer and fakes, so they need no database. The
runner-level tests run whole episodes against the real simulator and compare the spans with
the `model_calls` and `tool_events` rows of the same run: those rows stay the source of
record, and the spans must agree with them.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from lab import obs
from lab.agent.executor import ToolOutcome
from lab.agent.graph import build_graph, run_episode
from lab.agent.model import ModelTurn, ToolCall
from lab.agent.scripted import ScriptedModel
from lab.agent.tools import tool_schemas
from lab.protocol.faults import World
from lab.protocol.manifest import Mode, make_spec
from lab.protocol.task import Profile
from lab.runner.episode import run_spec
from tests.runner.conftest import EXPERIMENT_NAME_PREFIX

TOOLS = tool_schemas(Profile.CONTROLLED, status_available=True)
START = [
    {"role": "system", "content": "You are a payments agent."},
    {"role": "user", "content": "Charge order 1234 for 49.99 USD."},
]
RUN_ID = "run-trusted-1"
EPISODE_ID = "ep-trusted-1"
SHA = "b" * 64


class NullRecorder:
    def record_model_call(self, **kwargs: Any) -> None:
        pass

    def record_tool_event(self, **kwargs: Any) -> None:
        pass


class OkExecutor:
    def execute(self, call: ToolCall) -> ToolOutcome:
        key = call.arguments.get("idempotency_key")
        return ToolOutcome(
            requested_args=call.arguments,
            requested_key=key,
            effective_args=call.arguments,
            effective_key=key,
            rule="agent_directed",
            observation="Charge succeeded.",
            http_status=200,
        )


class SequenceModel:
    model_id = "test/sequence"

    def __init__(self, turns: list[ModelTurn]) -> None:
        self._turns = turns
        self.calls = 0

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        turn = self._turns[min(self.calls, len(self._turns) - 1)]
        self.calls += 1
        return turn


def turn_of(*calls: tuple[str, dict[str, Any]], tokens: tuple[int, int] | None = None):
    return ModelTurn(
        text=None,
        tool_calls=tuple(
            ToolCall(id=f"c{i}", name=name, arguments=args) for i, (name, args) in enumerate(calls)
        ),
        raw={"fake": True},
        input_tokens=None if tokens is None else tokens[0],
        output_tokens=None if tokens is None else tokens[1],
    )


def graph_for(model: Any, *, trace: obs.EpisodeTrace | None = None, max_turns: int = 10):
    return build_graph(
        model,
        OkExecutor(),
        TOOLS,
        max_turns,
        recorder=NullRecorder(),
        checkpointer=InMemorySaver(),
        **({} if trace is None else {"trace": trace}),
    )


def traced_episode(model: Any, **kwargs: Any) -> dict[str, Any]:
    """Run `model` through the graph inside an episode span, the way the runner does."""
    with obs.episode(RUN_ID, EPISODE_ID) as trace:
        graph = graph_for(model, trace=trace, **kwargs)
        return run_episode(graph, f"t-{uuid.uuid4().hex[:8]}", START)


class RaisingExporter(SpanExporter):
    def __init__(self) -> None:
        self.attempts = 0

    def export(self, spans: Any) -> SpanExportResult:
        self.attempts += 1
        raise RuntimeError("the collector is on fire")

    def shutdown(self) -> None:
        pass


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    memory = InMemorySpanExporter()
    obs.install_exporter(memory)
    return memory


def by_name(spans: Any, prefix: str) -> list[Any]:
    return [s for s in spans if s.name.startswith(prefix)]


# With no endpoint: silent


def test_without_an_endpoint_configure_creates_nothing(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    made: list[object] = []
    monkeypatch.setattr(obs, "_make_exporter", lambda: made.append(object()))
    threads_before = {t.ident for t in threading.enumerate()}

    assert obs.configure() is False

    assert obs.is_enabled() is False
    assert made == []  # no exporter was even constructed
    assert {t.ident for t in threading.enumerate()} == threads_before


def test_an_empty_endpoint_counts_as_unset(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "  ")
    assert obs.configure() is False
    assert obs.is_enabled() is False


def test_a_scripted_episode_with_no_endpoint_runs_and_exports_nothing(network_guard, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    blocked_before = len(network_guard.blocked)
    untraced = run_episode(graph_for(ScriptedModel("check_status")), "t-plain", START)

    obs.configure()
    state = traced_episode(ScriptedModel("check_status"))

    assert state["done"] and state["ended_by"] == "report_done"
    assert state["messages"] == untraced["messages"]
    assert obs.is_enabled() is False
    assert len(network_guard.blocked) == blocked_before  # no connection was even attempted


def test_the_runner_with_no_endpoint_writes_its_records_and_no_spans(
    runner_config, rt, network_guard
):
    blocked_before = len(network_guard.blocked)
    spec = make_spec("runner", World.LOST_ACK, Mode.AGENT_DIRECTED, True, 0)
    # An exporter *would* catch spans if a provider existed; none is installed here.
    result = run_spec(
        spec,
        ScriptedModel("stable_key"),
        runner_config,
        manifest_sha256=SHA,
        experiment_name=EXPERIMENT_NAME_PREFIX,
    )
    rows = rt.execute(
        "SELECT count(*) AS n FROM model_calls WHERE run_id = %s", (result.run_id,)
    ).fetchone()
    assert rows["n"] == result.turns
    assert obs.is_enabled() is False
    assert len(network_guard.blocked) == blocked_before


# With an endpoint


def test_with_an_endpoint_configure_installs_a_batching_provider_once(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    memory = InMemorySpanExporter()
    made: list[object] = []
    monkeypatch.setattr(obs, "_make_exporter", lambda: made.append(memory) or memory)

    assert obs.configure() is True
    assert obs.configure() is True  # idempotent: the first provider is kept

    assert obs.is_enabled() is True
    assert len(made) == 1
    traced_episode(ScriptedModel("give_up"))
    obs.flush()
    assert len(memory.get_finished_spans()) == 1 + 2 + 2  # episode, 2 model calls, 2 tool calls


def test_sdk_disabled_wins_over_an_endpoint(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert obs.configure() is False
    assert obs.is_enabled() is False


def test_a_broken_exporter_factory_leaves_tracing_off_without_raising(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")

    def broken() -> None:
        raise RuntimeError("bad exporter configuration")

    monkeypatch.setattr(obs, "_make_exporter", broken)
    assert obs.configure() is False
    assert obs.is_enabled() is False


def test_the_real_otlp_exporter_is_built_from_the_environment(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    exporter = obs._make_exporter()
    try:
        assert type(exporter).__module__.startswith("opentelemetry.exporter.otlp.proto.http")
        assert exporter._endpoint == "http://127.0.0.1:4318/v1/traces"
    finally:
        exporter.shutdown()


# One episode, one trace


def test_one_episode_is_one_trace_and_the_calls_are_children_of_the_episode(exporter):
    state = traced_episode(ScriptedModel("check_status"))

    spans = exporter.get_finished_spans()
    assert state["turn"] == 4
    episodes = by_name(spans, "episode")
    models = by_name(spans, "chat ")
    tools = by_name(spans, "execute_tool ")
    assert (len(episodes), len(models), len(tools)) == (1, 4, 4)
    assert len(spans) == 9
    assert len({s.context.trace_id for s in spans}) == 1
    (root,) = episodes
    assert root.parent is None
    for child in [*models, *tools]:
        assert child.parent is not None
        assert child.parent.span_id == root.context.span_id
    # A tool span follows the model span that asked for it.
    ordered = sorted([*models, *tools], key=lambda s: s.start_time)
    assert [s.name.split(" ")[0] for s in ordered] == ["chat", "execute_tool"] * 4


def test_an_episode_inside_another_span_is_still_its_own_root(exporter):
    ambient = TracerProvider().get_tracer("ambient")
    with ambient.start_as_current_span("ambient") as outer:
        assert outer.is_recording()
        traced_episode(ScriptedModel("give_up"))
    (root,) = by_name(exporter.get_finished_spans(), "episode")
    assert root.parent is None


def test_every_span_carries_the_run_and_episode_ids_the_runner_supplied(exporter):
    traced_episode(ScriptedModel("stable_key"))
    spans = exporter.get_finished_spans()
    assert len(spans) == 1 + 3 + 3
    for span in spans:
        assert span.attributes["lab.run_id"] == RUN_ID
        assert span.attributes["lab.episode_id"] == EPISODE_ID


def test_ids_in_model_output_do_not_reach_the_spans(exporter):
    hostile = {
        "run_id": "run-from-the-model",
        "episode_id": "ep-from-the-model",
        "lab.run_id": "run-from-the-model",
        "idempotency_key": "k1",
    }
    model = SequenceModel([turn_of(("charge", hostile)), turn_of(("report_done", hostile))])
    traced_episode(model)
    for span in exporter.get_finished_spans():
        assert span.attributes["lab.run_id"] == RUN_ID
        assert span.attributes["lab.episode_id"] == EPISODE_ID
        assert not any("from-the-model" in str(v) for v in span.attributes.values())


def test_model_and_tool_spans_carry_the_documented_attributes(exporter):
    model = SequenceModel(
        [
            turn_of(("charge", {"idempotency_key": "k1"}), tokens=(11, 7)),
            turn_of(
                ("report_done", {"outcome": "succeeded"}),
                ("charge", {"idempotency_key": "k2"}),
            ),
        ]
    )
    traced_episode(model)
    spans = exporter.get_finished_spans()

    first_model, second_model = sorted(by_name(spans, "chat "), key=lambda s: s.start_time)
    assert first_model.attributes["gen_ai.operation.name"] == "chat"
    assert first_model.attributes["gen_ai.request.model"] == "test/sequence"
    assert first_model.attributes["gen_ai.usage.input_tokens"] == 11
    assert first_model.attributes["gen_ai.usage.output_tokens"] == 7
    assert first_model.attributes["lab.turn"] == 0
    assert "gen_ai.usage.input_tokens" not in second_model.attributes  # unknown, so absent

    charge, done, skipped = sorted(by_name(spans, "execute_tool "), key=lambda s: s.start_time)
    assert charge.attributes["gen_ai.operation.name"] == "execute_tool"
    assert charge.attributes["gen_ai.tool.name"] == "charge"
    assert charge.attributes["gen_ai.tool.call.id"] == "c0"
    assert charge.attributes["lab.tool.rule"] == "agent_directed"
    assert charge.attributes["lab.tool.http_status"] == 200
    assert done.attributes["gen_ai.tool.name"] == "report_done"
    assert done.attributes["lab.tool.rule"] == "report_done"
    assert "lab.tool.http_status" not in done.attributes  # no request was made
    assert skipped.attributes["lab.tool.rule"] == "not_executed_after_report_done"
    assert skipped.attributes["lab.turn"] == 1


def test_no_span_attribute_contains_a_credential(exporter, runner_config):
    spec = make_spec("runner", World.LOST_ACK, Mode.AGENT_DIRECTED, True, 0)
    run_spec(
        spec,
        ScriptedModel("stable_key"),
        runner_config,
        manifest_sha256=SHA,
        experiment_name=EXPERIMENT_NAME_PREFIX,
    )
    secrets = [runner_config.client_token, runner_config.control_token]
    secrets += [runner_config.runtime_url, runner_config.ledger_url]
    for span in exporter.get_finished_spans():
        text = " ".join(f"{k}={v}" for k, v in span.attributes.items())
        assert not any(secret in text for secret in secrets)


def test_the_runner_traces_an_episode_that_agrees_with_its_database_rows(
    exporter, runner_config, rt
):
    spec = make_spec("runner", World.LOST_ACK, Mode.AGENT_DIRECTED, True, 0)
    result = run_spec(
        spec,
        ScriptedModel("check_status"),
        runner_config,
        manifest_sha256=SHA,
        experiment_name=EXPERIMENT_NAME_PREFIX,
    )

    spans = exporter.get_finished_spans()
    models = by_name(spans, "chat ")
    tools = by_name(spans, "execute_tool ")
    (root,) = by_name(spans, "episode")
    assert len({s.context.trace_id for s in spans}) == 1
    assert root.attributes["lab.run_id"] == result.run_id
    assert root.attributes["lab.episode_id"] == spec.episode_id
    assert root.attributes["lab.ended_by"] == "report_done"
    assert root.attributes["lab.turns"] == result.turns
    assert all(s.parent.span_id == root.context.span_id for s in [*models, *tools])
    assert all(s.attributes["lab.run_id"] == result.run_id for s in spans)
    assert all(s.attributes["lab.episode_id"] == spec.episode_id for s in spans)

    model_rows = rt.execute(
        "SELECT turn, provider_model_id FROM model_calls WHERE run_id = %s ORDER BY turn",
        (result.run_id,),
    ).fetchall()
    tool_rows = rt.execute(
        "SELECT turn, tool, rule, http_status FROM tool_events WHERE run_id = %s ORDER BY id",
        (result.run_id,),
    ).fetchall()
    assert len(models) == len(model_rows) == 4
    assert len(tools) == len(tool_rows) == 4
    assert sorted(
        (s.attributes["lab.turn"], s.attributes["gen_ai.request.model"]) for s in models
    ) == [(r["turn"], r["provider_model_id"]) for r in model_rows]
    assert sorted(
        (
            s.attributes["lab.turn"],
            s.attributes["gen_ai.tool.name"],
            s.attributes["lab.tool.rule"],
            s.attributes.get("lab.tool.http_status"),
        )
        for s in tools
    ) == sorted((r["turn"], r["tool"], r["rule"], r["http_status"]) for r in tool_rows)


def test_a_failing_episode_propagates_its_own_error_and_marks_the_span(exporter):
    class Boom(Exception):
        pass

    class FailingModel:
        model_id = "test/failing"

        def complete(self, messages: Any, tools: Any) -> ModelTurn:
            raise Boom("the provider fell over")

    with pytest.raises(Boom, match="the provider fell over"):
        traced_episode(FailingModel())

    spans = exporter.get_finished_spans()
    (root,) = by_name(spans, "episode")
    (model_span,) = by_name(spans, "chat ")
    assert root.status.status_code is StatusCode.ERROR
    assert model_span.status.status_code is StatusCode.ERROR
    assert model_span.attributes["error.type"] == "Boom"
    assert "fell over" not in " ".join(str(v) for v in model_span.attributes.values())


# Tracing that breaks changes nothing


def test_an_exporter_that_raises_does_not_fail_or_alter_the_episode():
    untraced = run_episode(graph_for(ScriptedModel("check_status")), "t-plain", START)
    raising = RaisingExporter()
    obs.install_exporter(raising)

    state = traced_episode(ScriptedModel("check_status"))

    assert raising.attempts == 9  # it really was asked to export every span
    assert state == untraced


class ExplodingSpan:
    """A span whose every method raises."""

    def __getattr__(self, name: str) -> Any:
        def explode(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"span.{name} exploded")

        return explode


class ExplodingTracer:
    def __init__(self, *, start_fails: bool) -> None:
        self.start_fails = start_fails

    def start_span(self, *args: Any, **kwargs: Any) -> Any:
        if self.start_fails:
            raise RuntimeError("start_span exploded")
        return ExplodingSpan()


@pytest.mark.parametrize("start_fails", [True, False], ids=["start_span", "span_methods"])
def test_a_tracer_that_raises_does_not_fail_or_alter_the_episode(monkeypatch, start_fails):
    untraced = run_episode(graph_for(ScriptedModel("check_status")), "t-plain", START)
    memory = InMemorySpanExporter()
    obs.install_exporter(memory)
    monkeypatch.setattr(obs, "_tracer", ExplodingTracer(start_fails=start_fails))

    state = traced_episode(ScriptedModel("check_status"))

    assert state == untraced


def test_the_runner_finishes_and_scores_an_episode_whose_exporter_raises(runner_config, rt):
    raising = RaisingExporter()
    obs.install_exporter(raising)
    spec = make_spec("runner", World.LOST_ACK, Mode.AGENT_DIRECTED, True, 0)

    result = run_spec(
        spec,
        ScriptedModel("stable_key"),
        runner_config,
        manifest_sha256=SHA,
        experiment_name=EXPERIMENT_NAME_PREFIX,
    )

    assert raising.attempts > 0
    state = rt.execute("SELECT state FROM runs WHERE id = %s", (result.run_id,)).fetchone()
    verdict = rt.execute("SELECT verdict FROM results WHERE run_id = %s", (result.run_id,))
    assert state["state"] == "completed"
    assert verdict.fetchone()["verdict"] == "correct"


# Teardown


def test_reset_stops_tracing_and_a_new_exporter_can_be_installed():
    first = InMemorySpanExporter()
    obs.install_exporter(first)
    traced_episode(ScriptedModel("give_up"))
    assert len(first.get_finished_spans()) == 5

    obs.reset()
    assert obs.is_enabled() is False
    traced_episode(ScriptedModel("give_up"))

    second = InMemorySpanExporter()
    obs.install_exporter(second)
    traced_episode(ScriptedModel("give_up"))
    assert len(second.get_finished_spans()) == 5


# Compose


def test_jaeger_is_opt_in_pinned_and_published_on_loopback_only():
    import re
    from pathlib import Path

    import yaml

    compose = yaml.safe_load((Path(__file__).parents[2] / "compose.yaml").read_text())
    jaeger = compose["services"]["jaeger"]
    assert jaeger["profiles"] == ["tracing"]  # a plain `docker compose up` does not start it
    assert re.fullmatch(r"[\w./-]+:\d+\.\d+\.\d+", jaeger["image"]), jaeger["image"]
    assert jaeger["ports"]
    assert all(port.startswith("127.0.0.1:") for port in jaeger["ports"])
    others = {name: svc for name, svc in compose["services"].items() if name != "jaeger"}
    assert not any("tracing" in svc.get("profiles", []) for svc in others.values())
    assert not any("jaeger" in str(svc.get("depends_on", "")) for svc in others.values())
