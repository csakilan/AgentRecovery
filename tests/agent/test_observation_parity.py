"""The agent loop shows the model RetryLedger's text, byte for byte, in the compat profile.

The compat profile promises that the model sees tool results identical to the original
benchmark. tests/oracle/test_parity.py checks that for the simulator and `lab.protocol.render`;
this file checks it for the thing the model actually reads, the `tool` messages of a real
episode. A scripted model plays a call sequence through the real graph and `DirectExecutor`,
against the simulator's public app on a throwaway database in the compat profile. The same
sequence goes to the vendored `RetryLedgerEpisode.handle()`, and every tool message must equal
the original's answer exactly.

The sequences follow tests/oracle/test_parity.py: they are well-formed (compat parity is only
claimed for those, see docs/protocol.md) and run in the two hidden worlds.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from lab.agent.client import SimulatorClient, TrustedContext
from lab.agent.executor import DirectExecutor
from lab.agent.graph import build_graph, run_episode
from lab.agent.model import ModelTurn, ToolCall
from lab.agent.tools import tool_schemas
from lab.protocol.faults import World, world_fault_plan
from lab.protocol.task import Profile
from lab.simulator import service
from lab.simulator.app import create_public_app
from tests.helpers import charges
from tests.reference.retryledger_original import RetryLedgerEpisode

TOKEN = "client-parity"
NAMESPACE = "ns-obs-parity"
CTX = TrustedContext(
    tenant="tenant-1", namespace=NAMESPACE, run_id="run-1", episode_id="ep-1", task_id="task-1"
)
TOOLS = tool_schemas(Profile.COMPAT, status_available=True)
START = [
    {"role": "system", "content": "You are a payments agent."},
    {"role": "user", "content": "Charge order 1234 for 49.99 USD."},
]


def charge(key, amount=49.99):
    # The compat profile's argument set: the simulator rejects any other field.
    return ("charge", {"amount": amount, "idempotency_key": key})


def status(key):
    return ("get_payment_status", {"idempotency_key": key})


def done(outcome):
    return ("report_done", {"outcome": outcome})


# Each policy charges twice and checks status where it can, plus a status read of a key that
# was never charged and a call to a tool that does not exist (RetryLedger's fixed strings).
# Every script ends in report_done.
SEQUENCES = {
    "give_up": [charge("k1"), done("failed")],
    "retry_same_key": [charge("k1"), charge("k1"), done("succeeded")],
    "retry_new_key": [charge("k1"), charge("k2"), done("succeeded")],
    "check_then_same_key": [charge("k1"), status("k1"), charge("k1"), done("succeeded")],
    "check_then_new_key": [charge("k1"), status("k1"), charge("k2"), status("k2"), done("failed")],
    "check_before_any_charge": [
        status("k1"),
        charge("k1"),
        charge("k1"),
        status("k1"),
        done("succeeded"),
    ],
    "check_a_key_never_used": [charge("k1"), status("k9"), charge("k1"), done("succeeded")],
    "three_new_keys": [charge("k1"), charge("k2"), charge("k3"), done("failed")],
    "unknown_tool_in_the_middle": [
        charge("k1"),
        ("refund", {"idempotency_key": "k1"}),
        charge("k1"),
        done("succeeded"),
    ],
}


class Scripted:
    """Plays one call per turn, chosen by how many tool messages the conversation holds."""

    model_id = "test/compat-script"

    def __init__(self, steps):
        self._steps = steps

    def complete(self, messages, tools):
        turn = sum(1 for m in messages if m.get("role") == "tool")
        name, arguments = self._steps[min(turn, len(self._steps) - 1)]
        return ModelTurn(
            text=None,
            tool_calls=(ToolCall(id=f"c{turn}", name=name, arguments=dict(arguments)),),
            raw={"scripted": True, "turn": turn},
        )


class NullRecorder:
    def record_model_call(self, **kwargs):
        pass

    def record_tool_event(self, **kwargs):
        pass


@pytest.fixture
def public(svc_url):
    pool = ConnectionPool(
        svc_url,
        min_size=1,
        max_size=3,
        open=True,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    yield TestClient(create_public_app(pool, TOKEN))
    pool.close()


def play(public, conn, world, steps, thread):
    """Run `steps` through the real graph and executor. Returns the tool messages."""
    service.configure_namespace(conn, NAMESPACE, Profile.COMPAT, world_fault_plan(World(world)))
    executor = DirectExecutor(SimulatorClient("http://testserver", TOKEN, CTX, http_client=public))
    graph = build_graph(
        Scripted(steps),
        executor,
        TOOLS,
        len(steps) + 2,
        recorder=NullRecorder(),
        checkpointer=InMemorySaver(),
    )
    state = run_episode(graph, thread, START)
    return [m for m in state["messages"] if m["role"] == "tool"], state


def original_answers(world, steps):
    episode = RetryLedgerEpisode(world, verify=True)
    return [episode.handle(name, dict(arguments))[0] for name, arguments in steps], episode


@pytest.mark.parametrize("world", ["lost_ack", "true_fail"])
@pytest.mark.parametrize("name", sorted(SEQUENCES))
def test_every_tool_message_is_what_retryledger_returns_for_the_same_calls(
    public, conn, world, name
):
    steps = SEQUENCES[name]
    messages, state = play(public, conn, world, steps, f"t-{world}-{name}")
    expected, original = original_answers(world, steps)

    # Every script ends in report_done, so the episode ran exactly the steps and no more.
    assert state["ended_by"] == "report_done" and len(messages) == len(steps)
    for message, (tool, _), want in zip(messages, steps, expected, strict=True):
        assert message["name"] == tool
        assert message["content"].encode() == want.encode()
    # Both sides saw the same calls, so the ledgers agree too (a sequence that drifted would
    # show up here before it showed up as a confusing text diff).
    assert len(charges(conn, NAMESPACE)) == len(original.committed)


@pytest.mark.parametrize("world", ["lost_ack", "true_fail"])
def test_the_scripted_episodes_do_exercise_every_text_retryledger_has(public, conn, world):
    # Guard against a vacuous parity test: across the sequences the model is shown each of
    # RetryLedger's distinct answers at least once, in this world.
    seen = set()
    for name, steps in SEQUENCES.items():
        messages, _ = play(public, conn, world, steps, f"t-cover-{world}-{name}")
        seen.update(m["content"] for m in messages)
    assert {"Recorded.", "Unknown tool.", "Charge succeeded."} <= seen
    assert any(text.startswith("HTTP 5") for text in seen)
    # Only in lost_ack does a same-key retry find a committed charge to answer from.
    assert any(text.startswith("Idempotent hit: ") for text in seen) == (world == "lost_ack")
    assert any("SUCCEEDED — $49.99 charged." in text for text in seen)
    assert any("NOT_FOUND — no charge recorded." in text for text in seen)


# What the model is told when the simulator's answer is not one RetryLedger could have given.
# There is no original to compare with, so these pin the executor's own, plain-text rendering
# of the real app's responses.


def test_a_rejected_token_is_one_plain_line_not_json(public):
    bad = SimulatorClient("http://testserver", "wrong-token", CTX, http_client=public)
    outcome = DirectExecutor(bad).execute(ToolCall(id="c", name="charge", arguments=charge("k")[1]))
    assert outcome.http_status == 401
    assert outcome.observation.startswith("HTTP 401 ")
    assert not outcome.observation.startswith("{") and "\n" not in outcome.observation


def test_an_unrouted_status_lookup_is_one_plain_line_not_json(public, conn):
    service.configure_namespace(conn, NAMESPACE, Profile.COMPAT, world_fault_plan(World.LOST_ACK))
    executor = DirectExecutor(SimulatorClient("http://testserver", TOKEN, CTX, http_client=public))
    outcome = executor.execute(ToolCall(id="c", name="get_payment_status", arguments={
        "idempotency_key": ""
    }))  # fmt: skip
    assert outcome.http_status is not None and outcome.http_status >= 400
    assert outcome.observation.startswith(f"HTTP {outcome.http_status} ")
    assert not outcome.observation.startswith("{") and "\n" not in outcome.observation
