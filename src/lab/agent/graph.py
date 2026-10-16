"""The agent loop: a model node and a tool node, checkpointed after every step.

    START -> model -> tool -> (done? END : model)

The model node asks the model for one turn. The tool node runs that turn's tool calls in
order, appends one `tool` message per call, and decides whether the episode is over. A turn
with no tool calls passes through the tool node unchanged and the loop asks the model again.

Why it is shaped for crash testing (Task 7 kills a process mid-episode and resumes the same
`thread_id` in a fresh one):

* Every step is checkpointed with `durability="sync"`, so the checkpoint for step N is on
  disk before step N+1 starts. A process killed while the tool node is running resumes with
  the model node's checkpoint and runs the whole tool node again. Whether that repeats a
  charge is the thing being measured, so nothing here tries to prevent it.
* The `Recorder` writes each `model_calls` / `tool_events` row on its own autocommit
  connection as it happens, so a SIGKILL leaves the rows already written. `tool_events` is
  append-only: a tool node that runs twice leaves two sets of rows, which is the evidence.

Turn numbering: `state["turn"]` counts model calls made. Rows use the 0-based index of the
call, so the first model call is turn 0 and the tool events it requested are also turn 0.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Literal, Protocol, TypedDict, get_args

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Durability
from psycopg import Connection
from psycopg.types.json import Jsonb

from lab.agent.executor import (
    RULE_NOT_EXECUTED_AFTER_DONE,
    RULE_REPORT_DONE,
    Executor,
    ToolOutcome,
    not_sent_outcome,
)
from lab.agent.model import ModelClient, ModelTurn, ToolCall
from lab.obs import NO_TRACE, EpisodeTrace

REPORT_DONE = "report_done"

EndedBy = Literal["report_done", "max_turns"]


class EpisodeState(TypedDict):
    messages: list[dict[str, Any]]
    turn: int
    claimed_outcome: str | None
    done: bool
    # Why `done` became true. A model that reports "unresolved" ends by "report_done"; a
    # model cut off by the turn cap ends by "max_turns", and both claim "unresolved".
    ended_by: EndedBy | None


class Recorder(Protocol):
    def record_model_call(
        self,
        *,
        turn: int,
        provider_model_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        result: ModelTurn,
        latency_ms: float,
    ) -> None: ...

    def record_tool_event(self, *, turn: int, tool: str, outcome: ToolOutcome) -> None: ...


class PostgresRecorder:
    """Writes one run's `model_calls` and `tool_events` rows through a `runtime_svc` connection.

    The connection must be in autocommit mode (checked), so each row is committed the moment
    it is written and survives a SIGKILL of this process. It is the caller's to close.
    """

    def __init__(self, conn: Connection[Any], run_id: str) -> None:
        if not conn.autocommit:
            raise ValueError(
                "the recorder needs an autocommit connection so rows survive a killed process"
            )
        self._conn = conn
        self._run_id = run_id

    def record_model_call(
        self,
        *,
        turn: int,
        provider_model_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        result: ModelTurn,
        latency_ms: float,
    ) -> None:
        """Insert the row for model call `turn`, unless it is already there.

        ON CONFLICT (run_id, turn) DO NOTHING, because a resumed run replays a model call
        whose row a killed process had already written (the kill landed after the row and
        before the checkpoint). Such a replay must not crash the resume, and the first row
        is kept: it is the call that was actually made first.
        """
        self._conn.execute(
            "INSERT INTO model_calls (run_id, turn, provider_model_id, request_json,"
            " response_json, tool_calls_json, input_tokens, output_tokens, latency_ms,"
            " malformed) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (run_id, turn) DO NOTHING",
            (
                self._run_id,
                turn,
                provider_model_id,
                Jsonb({"messages": messages, "tools": tools}),
                Jsonb(result.raw),
                Jsonb([call.model_dump() for call in result.tool_calls]),
                result.input_tokens,
                result.output_tokens,
                latency_ms,
                result.malformed,
            ),
        )

    def record_tool_event(self, *, turn: int, tool: str, outcome: ToolOutcome) -> None:
        """Append one row. No conflict handling: a re-executed tool node must leave a second
        row, because the repeat is what the experiment looks for."""
        self._conn.execute(
            "INSERT INTO tool_events (run_id, turn, tool, requested_args_json, requested_key,"
            " effective_args_json, effective_key, rule, observation, http_status)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                self._run_id,
                turn,
                tool,
                Jsonb(outcome.requested_args),
                outcome.requested_key,
                None if outcome.effective_args is None else Jsonb(outcome.effective_args),
                outcome.effective_key,
                outcome.rule,
                outcome.observation,
                outcome.http_status,
            ),
        )


def _claimed_outcome(arguments: dict[str, Any]) -> str | None:
    outcome = arguments.get("outcome")
    return None if outcome is None else str(outcome)


def build_graph(
    model: ModelClient,
    executor: Executor,
    tools: list[dict[str, Any]],
    max_turns: int,
    *,
    recorder: Recorder,
    checkpointer: BaseCheckpointSaver,
    trace: EpisodeTrace = NO_TRACE,
) -> Any:
    """Compile the loop. `tools` is the schema list passed to every `model.complete` call.

    `trace` is the runner's tracing context (`lab.obs.episode`): each model call and each
    tool call runs inside a span of it. The default does nothing. Tracing never feeds back
    into the loop, and the `Recorder` rows stay the record of what happened.

    `checkpointer` is a `PostgresSaver` on a `runtime_svc` connection in production
    (`PostgresSaver.from_conn_string` sets the autocommit, dict_row and prepare_threshold=0
    it needs); tests may pass any saver. `max_turns` is the number of model calls allowed.
    """
    if max_turns < 1:
        raise ValueError(f"max_turns must be at least 1, got {max_turns}")

    def model_node(state: EpisodeState) -> dict[str, Any]:
        turn = state["turn"]
        messages = state["messages"]
        with trace.model_call(model_id=model.model_id, turn=turn) as span:
            started = time.perf_counter()
            result = model.complete(copy.deepcopy(messages), copy.deepcopy(tools))
            latency_ms = (time.perf_counter() - started) * 1000
            span.record_usage(result.input_tokens, result.output_tokens)
        recorder.record_model_call(
            turn=turn,
            provider_model_id=model.model_id,
            messages=messages,
            tools=tools,
            result=result,
            latency_ms=latency_ms,
        )
        assistant = {
            "role": "assistant",
            "content": result.text,
            "tool_calls": [call.model_dump() for call in result.tool_calls],
        }
        return {"messages": [*messages, assistant], "turn": turn + 1}

    def tool_node(state: EpisodeState) -> dict[str, Any]:
        turn = state["turn"] - 1  # the model call that requested these tool calls
        messages = list(state["messages"])
        done = False
        claimed: str | None = None
        for raw in messages[-1]["tool_calls"]:
            call = ToolCall(**raw)
            with trace.tool_call(tool=call.name, call_id=call.id, turn=turn) as span:
                if done:
                    outcome = not_sent_outcome(call, RULE_NOT_EXECUTED_AFTER_DONE)
                elif call.name == REPORT_DONE:
                    done = True
                    claimed = _claimed_outcome(call.arguments)
                    outcome = not_sent_outcome(call, RULE_REPORT_DONE)
                else:
                    outcome = executor.execute(call)
                span.record_tool_outcome(outcome.rule, outcome.http_status)
            recorder.record_tool_event(turn=turn, tool=call.name, outcome=outcome)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": outcome.observation,
                }
            )
        update: dict[str, Any] = {"messages": messages}
        if done:
            update.update(claimed_outcome=claimed, done=True, ended_by="report_done")
        elif state["turn"] >= max_turns:
            # Out of turns without a report: record that as unresolved, never as a silent stop.
            update.update(claimed_outcome="unresolved", done=True, ended_by="max_turns")
        return update

    def route(state: EpisodeState) -> str:
        return END if state["done"] else "model"

    builder = StateGraph(EpisodeState)
    builder.add_node("model", model_node)
    builder.add_node("tool", tool_node)
    builder.add_edge(START, "model")
    builder.add_edge("model", "tool")
    builder.add_conditional_edges("tool", route, {END: END, "model": "model"})
    return builder.compile(checkpointer=checkpointer)


_DURABILITY_VALUES = get_args(Durability)


def _initial_state(messages: list[dict[str, Any]]) -> EpisodeState:
    return {
        "messages": list(messages),
        "turn": 0,
        "claimed_outcome": None,
        "done": False,
        "ended_by": None,
    }


def run_episode(
    graph: Any,
    thread_id: str,
    messages: list[dict[str, Any]] | None = None,
    *,
    durability: str = "sync",
    allow_non_sync: bool = False,
) -> EpisodeState:
    """Run the loop on `thread_id` until it ends, and return the final state.

    `messages` starts a new thread. `None` resumes the thread's last checkpoint
    (`graph.invoke(None, config)`), which is what a fresh process does after a crash.

    Durability is always passed explicitly, because LangGraph's own default is "async"
    (langgraph/pregel/main.py), and D11 requires "sync" in every mode so no mode is favoured
    by weaker checkpointing. Any other value is refused unless `allow_non_sync=True`, which
    only an experiment config may set. A value LangGraph does not accept is refused always.
    """
    if durability not in _DURABILITY_VALUES:
        raise ValueError(f"durability must be one of {_DURABILITY_VALUES}, got {durability!r}")
    if durability != "sync" and not allow_non_sync:
        raise ValueError(
            f"durability {durability!r} is refused: D11 requires 'sync' for every mode "
            "unless the experiment config sets allow_non_sync"
        )
    config = {"configurable": {"thread_id": thread_id}}
    has_checkpoint = graph.get_state(config).created_at is not None
    if messages is None and not has_checkpoint:
        raise ValueError(f"cannot resume thread {thread_id!r}: it has no checkpoint")
    if messages is not None and has_checkpoint:
        raise ValueError(
            f"thread {thread_id!r} already has a checkpoint; resume it with messages=None"
        )
    payload = None if messages is None else _initial_state(messages)
    return graph.invoke(payload, config, durability=durability)
