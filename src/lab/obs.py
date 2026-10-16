"""Optional OpenTelemetry tracing of an episode, for debugging and the demo.

Postgres is the source of record. The `model_calls` and `tool_events` rows decide every
verdict and every timeline; a trace is a second view of the same run and is never read back
by anything that scores. So tracing is built to be absent without anyone noticing:

* Off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set (and `OTEL_SDK_DISABLED` is not "true").
  Off means no provider, no exporter, no thread, no socket, and every call below returns at
  once. Tests and CI run this way.
* Never an error. Every call into OpenTelemetry is wrapped, so a collector that is down, an
  exporter that raises or a tracer that is broken costs a trace and nothing else. Exceptions
  from the *episode* pass through a span untouched; only the span's own machinery is
  swallowed.
* Never global. The provider lives in this module, not in OpenTelemetry's process-wide
  `set_tracer_provider` (which accepts one call per process and so cannot be reset between
  tests). `configure()` builds it from the environment, `install_exporter()` builds it around
  a given exporter (tests), and `reset()` drops it.

Spans: one `episode` span as the root of a trace, wrapping the graph run; under it one
`chat <model>` span per model call and one `execute_tool <tool>` span per tool call
(including `report_done` and calls that were not sent). The run and episode ids on every
span come from the runner, through `episode()`, and never from anything a model wrote. The
tool and call names do come from the model and are recorded as data, truncated. Arguments,
observations and message text are never attached, so neither a token nor a prompt can reach
a collector through here.

Attribute names beginning `gen_ai.` are borrowed from OpenTelemetry's GenAI semantic
conventions, which are still changing; nothing here is conformant to them or depends on
their values. The lab's own attributes begin `lab.`.

To see traces locally, start Jaeger with `docker compose --profile tracing up -d jaeger` and
run with `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318` (UI at http://127.0.0.1:16686).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Status, StatusCode

ENDPOINT_VARIABLE = "OTEL_EXPORTER_OTLP_ENDPOINT"
DISABLED_VARIABLE = "OTEL_SDK_DISABLED"
SERVICE_NAME = "agent-recovery-lab"

# Longest string attribute value or span name. Names and ids from a model are cut to this.
MAX_ATTRIBUTE_LENGTH = 200
FLUSH_TIMEOUT_MS = 5000

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_provider: TracerProvider | None = None
_tracer: Any = None


# Provider lifecycle


def _make_exporter() -> SpanExporter:
    """The OTLP/HTTP exporter. It reads the endpoint and headers from the OTEL_EXPORTER_*
    variables itself, and appends /v1/traces to a base endpoint."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter()


def _install(provider: TracerProvider) -> None:
    global _provider, _tracer
    _provider = provider
    _tracer = provider.get_tracer(SERVICE_NAME)


def is_enabled() -> bool:
    return _tracer is not None


def configure(environ: Mapping[str, str] | None = None) -> bool:
    """Turn tracing on if the environment asks for it. True when tracing is on afterwards.

    A no-op without `OTEL_EXPORTER_OTLP_ENDPOINT` (or with `OTEL_SDK_DISABLED=true`), and
    when a provider is already installed, whether by an earlier call or by
    `install_exporter`. A failure to build the exporter leaves tracing off."""
    env = os.environ if environ is None else environ
    with _lock:
        if _provider is not None:
            return True
        if not env.get(ENDPOINT_VARIABLE, "").strip():
            return False
        if env.get(DISABLED_VARIABLE, "").strip().lower() == "true":
            return False
        try:
            provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
            provider.add_span_processor(BatchSpanProcessor(_make_exporter()))
        except Exception:
            _log.warning("tracing is off: the exporter could not be built", exc_info=True)
            return False
        _install(provider)
        return True


def install_exporter(exporter: SpanExporter) -> None:
    """Install a provider that hands every ended span straight to `exporter` (tests).

    Replaces any provider already installed. The synchronous processor exports each span as
    it ends, so a test can read the exporter right after an episode. Pair with `reset()`."""
    reset()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with _lock:
        _install(provider)


def flush() -> None:
    """Send any spans still queued. Bounded by `FLUSH_TIMEOUT_MS`; never raises."""
    provider = _provider
    if provider is None:
        return
    try:
        provider.force_flush(FLUSH_TIMEOUT_MS)
    except Exception:
        _log.debug("tracing flush failed", exc_info=True)


def reset() -> None:
    """Shut the provider down and forget it. Safe when tracing is off."""
    global _provider, _tracer
    with _lock:
        provider, _provider, _tracer = _provider, None, None
    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception:
        _log.debug("tracing shutdown failed", exc_info=True)


# Spans


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_ATTRIBUTE_LENGTH]
    return value


class SpanHandle:
    """An open span, or nothing. Every method is safe to call whatever state tracing is in."""

    __slots__ = ("_span",)

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        if self._span is None:
            return
        try:
            for key, value in attributes.items():
                if value is not None:
                    self._span.set_attribute(key, _clean(value))
        except Exception:
            _log.debug("tracing: set_attribute failed", exc_info=True)

    def record_usage(self, input_tokens: int | None, output_tokens: int | None) -> None:
        self.set_attributes(
            {
                "gen_ai.usage.input_tokens": input_tokens,
                "gen_ai.usage.output_tokens": output_tokens,
            }
        )

    def record_tool_outcome(self, rule: str, http_status: int | None) -> None:
        self.set_attributes({"lab.tool.rule": rule, "lab.tool.http_status": http_status})

    def _fail(self, exc: BaseException) -> None:
        """Mark the span failed. Only the exception's type is kept: its message may quote a
        request or a response."""
        if self._span is None:
            return
        try:
            self._span.set_attribute("error.type", type(exc).__name__)
            self._span.set_status(Status(StatusCode.ERROR))
        except Exception:
            _log.debug("tracing: marking a span failed did not work", exc_info=True)

    def _end(self) -> None:
        if self._span is None:
            return
        try:
            self._span.end()
        except Exception:
            _log.debug("tracing: ending a span failed", exc_info=True)


_NO_SPAN = SpanHandle(None)


def _start(name: str, attributes: Mapping[str, Any], parent: Context) -> SpanHandle:
    tracer = _tracer
    if tracer is None:
        return _NO_SPAN
    try:
        cleaned = {key: _clean(value) for key, value in attributes.items() if value is not None}
        span = tracer.start_span(name[:MAX_ATTRIBUTE_LENGTH], context=parent, attributes=cleaned)
        return SpanHandle(span)
    except Exception:
        _log.debug("tracing: starting a span failed", exc_info=True)
        return _NO_SPAN


@contextmanager
def _span(name: str, attributes: Mapping[str, Any], parent: Context) -> Iterator[SpanHandle]:
    handle = _start(name, attributes, parent)
    try:
        yield handle
    except BaseException as exc:
        handle._fail(exc)
        raise
    finally:
        handle._end()


class EpisodeTrace:
    """The tracing context of one episode: where its model and tool spans attach, and the
    ids they carry. Built by `episode()`; the graph only calls `model_call` and `tool_call`.

    The parent is held explicitly rather than read from a context variable, so the spans
    attach correctly whichever thread LangGraph runs a node on."""

    def __init__(
        self, run_id: str, episode_id: str, root: SpanHandle, parent: Context | None
    ) -> None:
        self.run_id = run_id
        self.episode_id = episode_id
        self._root = root
        self._parent = parent

    def _ids(self) -> dict[str, Any]:
        return {"lab.run_id": self.run_id, "lab.episode_id": self.episode_id}

    @contextmanager
    def model_call(self, *, model_id: str, turn: int) -> Iterator[SpanHandle]:
        if self._parent is None:
            yield _NO_SPAN
            return
        attributes = {
            **self._ids(),
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": model_id,
            "lab.turn": turn,
        }
        with _span(f"chat {model_id}", attributes, self._parent) as handle:
            yield handle

    @contextmanager
    def tool_call(self, *, tool: str, call_id: str, turn: int) -> Iterator[SpanHandle]:
        if self._parent is None:
            yield _NO_SPAN
            return
        attributes = {
            **self._ids(),
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": tool,
            "gen_ai.tool.call.id": call_id,
            "lab.turn": turn,
        }
        with _span(f"execute_tool {tool}", attributes, self._parent) as handle:
            yield handle

    def finish(self, *, ended_by: str | None, turns: int) -> None:
        """Record how the episode ended, on its span. Call before leaving `episode()`."""
        self._root.set_attributes({"lab.ended_by": ended_by, "lab.turns": turns})


# What the graph uses when it is built without a trace: every call is a no-op.
NO_TRACE = EpisodeTrace("", "", _NO_SPAN, None)


@contextmanager
def episode(run_id: str, episode_id: str) -> Iterator[EpisodeTrace]:
    """Open the root span of one episode's trace and yield the context for its child spans.

    The span is a root even when called inside another span: an episode is always its own
    trace. When tracing is off this yields a trace whose spans do nothing."""
    ids = {"lab.run_id": run_id, "lab.episode_id": episode_id}
    root = _start("episode", ids, Context())
    parent: Context | None = None
    if root._span is not None:
        try:
            parent = trace.set_span_in_context(root._span)
        except Exception:
            _log.debug("tracing: building the parent context failed", exc_info=True)
    try:
        yield EpisodeTrace(run_id, episode_id, root, parent)
    except BaseException as exc:
        root._fail(exc)
        raise
    finally:
        root._end()
