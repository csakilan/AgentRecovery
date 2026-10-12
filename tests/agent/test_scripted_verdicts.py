"""Score every scripted policy with the real oracle, in both hidden worlds.

The seven policies exist to produce one oracle verdict each, on purpose, for free. This
file is the test of that purpose. It is database-free: `simulate` walks the tool calls a
policy actually emits against a world's fault plan and builds the ledger state the
simulator would have recorded, then `score_episode` (a pure function) scores it.

`simulate` reuses the simulator's own pure pieces, so it cannot drift from them:
`service._validate_charge` decides whether a charge is rejected, and
`FaultPlan.charge_fault` (from `world_fault_plan`) decides what a validated charge does.
The only thing it re-implements is the one Postgres step, `INSERT ... ON CONFLICT
(namespace, idempotency_key) DO NOTHING`, as a dict keyed on the idempotency key.

The rules it models, each read from `lab.simulator.service.charge`:

* A charge that fails validation is logged as `invalid`, claims no fault slot and never
  reaches the ledger. It is counted in `CallSummary.invalid_charge_calls`, not as an attempt.
* A validated charge claims the next call index (1-based), whose fault comes from the plan.
* `error_no_commit` returns an error and writes nothing.
* `commit_then_error` writes the effect (or finds the existing one) and then returns an error.
* No fault: writes the effect, or on a key that already exists returns the existing one.
  A reused key therefore never creates a second effect.
* A status call only increments the status counter. It has no effect on the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from lab.agent.scripted import SCRIPTED_POLICIES, Policy, ScriptedModel
from lab.oracle.ledger import CallSummary, LedgerCharge
from lab.oracle.score import Claim, EpisodeScore, Expectation, Verdict, score_episode
from lab.protocol.faults import World, world_fault_plan
from lab.protocol.manifest import Mode, make_spec
from lab.protocol.task import Profile
from lab.simulator.service import SimResponse, _validate_charge

TOOLS = [{"name": "charge"}, {"name": "get_payment_status"}, {"name": "report_done"}]
MAX_TURNS = 20

ERROR_OBSERVATION = "HTTP 504 Gateway Timeout: the upstream processor did not confirm."
INVALID_OBSERVATION = "HTTP 400 invalid_request"
OK_OBSERVATION = "HTTP 200 OK"
STATUS_OBSERVATION = "Status for key: NOT_FOUND"


@dataclass(frozen=True)
class Episode:
    charges: list[LedgerCharge]
    summary: CallSummary
    raw_claim: str


def simulate(policy: Policy, world: World) -> Episode:
    """Drive `policy` through `world` and return the evidence the oracle would read."""
    plan = world_fault_plan(world)
    ledger: dict[str, LedgerCharge] = {}
    charge_keys: list[str] = []
    status_calls = 0
    invalid_charge_calls = 0
    charge_calls = 0

    model = ScriptedModel(policy)
    messages: list[dict] = [{"role": "user", "content": "charge it"}]
    for _ in range(MAX_TURNS):
        (call,) = model.complete(messages, TOOLS).tool_calls
        if call.name == "report_done":
            return Episode(
                charges=sorted(ledger.values(), key=lambda c: c.call_index),
                summary=CallSummary(
                    charge_keys=tuple(charge_keys),
                    status_calls=status_calls,
                    invalid_charge_calls=invalid_charge_calls,
                ),
                raw_claim=call.arguments["outcome"],
            )
        if call.name == "get_payment_status":
            status_calls += 1
            observation = STATUS_OBSERVATION
        elif call.name == "charge":
            valid = _validate_charge(Profile.CONTROLLED, call.arguments)
            if isinstance(valid, SimResponse):
                invalid_charge_calls += 1
                observation = INVALID_OBSERVATION
            else:
                charge_calls += 1
                charge_keys.append(valid.key)
                fault = plan.charge_fault(charge_calls)
                if fault == "error_no_commit":
                    observation = ERROR_OBSERVATION
                else:
                    if valid.key not in ledger:
                        ledger[valid.key] = LedgerCharge(
                            valid.key, valid.order_id, valid.amount, valid.currency, charge_calls
                        )
                    observation = (
                        ERROR_OBSERVATION if fault == "commit_then_error" else OK_OBSERVATION
                    )
        else:
            raise AssertionError(f"unexpected tool {call.name!r}")
        messages.append({"role": "assistant", "content": ""})
        messages.append({"role": "tool", "content": observation})
    raise AssertionError(f"{policy} in {world} never reported done in {MAX_TURNS} turns")


def score(policy: Policy, world: World) -> EpisodeScore:
    episode = simulate(policy, world)
    spec = make_spec("verdict", world, Mode.AGENT_DIRECTED, True, 0)
    return score_episode(
        episode.charges,
        episode.summary,
        Expectation.from_spec(spec),
        episode.raw_claim,
        completed_before_deadline=True,
    )


# The verdicts the seven policies are built to produce. Every row was first computed with the
# real simulator's rules; the table is the reason the policies exist. If a row changes, a
# policy's behaviour changed, and the experiment it was built for changed with it.
EXPECTED = {
    "naive_new_key": {World.LOST_ACK: Verdict.DUPLICATE, World.TRUE_FAIL: Verdict.CORRECT},
    "stable_key": {World.LOST_ACK: Verdict.CORRECT, World.TRUE_FAIL: Verdict.CORRECT},
    "check_status": {World.LOST_ACK: Verdict.CORRECT, World.TRUE_FAIL: Verdict.CORRECT},
    "give_up": {World.LOST_ACK: Verdict.FALSE_FAILURE, World.TRUE_FAIL: Verdict.MISSED},
    "false_success": {World.LOST_ACK: Verdict.CORRECT, World.TRUE_FAIL: Verdict.FALSE_SUCCESS},
    "wrong_amount": {World.LOST_ACK: Verdict.WRONG_EFFECT, World.TRUE_FAIL: Verdict.FALSE_SUCCESS},
    "malformed_call": {World.LOST_ACK: Verdict.MISSED, World.TRUE_FAIL: Verdict.MISSED},
}

CASES = [
    (policy, world, verdict)
    for policy, by_world in EXPECTED.items()
    for world, verdict in by_world.items()
]


def test_the_table_covers_every_policy_in_both_worlds():
    assert set(EXPECTED) == set(SCRIPTED_POLICIES)
    for by_world in EXPECTED.values():
        assert set(by_world) == {World.LOST_ACK, World.TRUE_FAIL}


@pytest.mark.parametrize(("policy", "world", "verdict"), CASES)
def test_each_policy_earns_its_verdict_from_the_real_oracle(policy, world, verdict):
    assert score(policy, world).verdict is verdict


# Effects and call counts behind the verdicts, so a verdict reached for the wrong reason
# (say, DUPLICATE from two wrong-amount charges) does not pass.
EVIDENCE = {
    # policy: {world: (n_effects, charge_keys, status_calls, invalid_charge_calls)}
    "naive_new_key": {
        World.LOST_ACK: (2, ("k1", "k2"), 0, 0),
        World.TRUE_FAIL: (1, ("k1", "k2"), 0, 0),
    },
    "stable_key": {
        World.LOST_ACK: (1, ("k1", "k1"), 0, 0),
        World.TRUE_FAIL: (1, ("k1", "k1"), 0, 0),
    },
    "check_status": {
        World.LOST_ACK: (1, ("k1", "k1"), 1, 0),
        World.TRUE_FAIL: (1, ("k1", "k1"), 1, 0),
    },
    "give_up": {
        World.LOST_ACK: (1, ("k1",), 0, 0),
        World.TRUE_FAIL: (0, ("k1",), 0, 0),
    },
    "false_success": {
        World.LOST_ACK: (1, ("k1",), 0, 0),
        World.TRUE_FAIL: (0, ("k1",), 0, 0),
    },
    "wrong_amount": {
        World.LOST_ACK: (1, ("k1",), 0, 0),
        World.TRUE_FAIL: (0, ("k1",), 0, 0),
    },
    "malformed_call": {
        World.LOST_ACK: (0, (), 0, 1),
        World.TRUE_FAIL: (0, (), 0, 1),
    },
}


@pytest.mark.parametrize(("policy", "world", "verdict"), CASES)
def test_the_evidence_behind_each_verdict(policy, world, verdict):
    episode = simulate(policy, world)
    n_effects, keys, status_calls, invalid = EVIDENCE[policy][world]
    assert len(episode.charges) == n_effects
    assert episode.summary.charge_keys == keys
    assert episode.summary.status_calls == status_calls
    assert episode.summary.invalid_charge_calls == invalid


def test_the_wrong_amount_policy_commits_an_effect_that_is_not_the_task():
    effects = simulate("wrong_amount", World.LOST_ACK).charges
    assert [c.amount for c in effects] == [Decimal("4999.0")]
    assert score("wrong_amount", World.LOST_ACK).n_intended == 0


def test_only_the_stable_policies_claim_the_truth_and_keep_one_effect():
    for policy in ("stable_key", "check_status"):
        for world in (World.LOST_ACK, World.TRUE_FAIL):
            result = score(policy, world)
            assert result.claim is Claim.SUCCEEDED
            assert result.truthful_report is True


# ---------------------------------------------------------------------------
# The helper itself. These pin the simulator rules `simulate` depends on, so a wrong
# helper cannot quietly make the table above pass.
# ---------------------------------------------------------------------------


def test_the_helper_applies_the_hidden_worlds_to_the_first_charge_only():
    # lost_ack commits the first charge and errors; the retry under the same key adds nothing.
    lost = simulate("stable_key", World.LOST_ACK)
    assert [(c.idempotency_key, c.call_index) for c in lost.charges] == [("k1", 1)]
    # true_fail errors without committing; the retry is call 2 and commits.
    failed = simulate("stable_key", World.TRUE_FAIL)
    assert [(c.idempotency_key, c.call_index) for c in failed.charges] == [("k1", 2)]


def test_a_malformed_charge_is_rejected_by_the_real_validator():
    valid = {"order_id": "1234", "amount": 49.99, "idempotency_key": "k1", "currency": "USD"}
    assert not isinstance(_validate_charge(Profile.CONTROLLED, valid), SimResponse)
    no_currency = {k: v for k, v in valid.items() if k != "currency"}
    assert isinstance(_validate_charge(Profile.CONTROLLED, no_currency), SimResponse)
