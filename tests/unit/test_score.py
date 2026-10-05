from dataclasses import asdict
from decimal import Decimal

import pytest

from lab.oracle.ledger import CallSummary, LedgerCharge
from lab.oracle.score import (
    Claim,
    Expectation,
    Verdict,
    legacy_score,
    normalize_claim,
    score_episode,
)
from lab.protocol.faults import World
from lab.protocol.manifest import Mode, make_spec
from lab.protocol.task import DEFAULT_TASK, Profile

RECOVERABLE = Expectation(
    Profile.CONTROLLED, DEFAULT_TASK, expect_effect=True, eligible_recoverable=True
)
NO_EFFECT_EXPECTED = Expectation(
    Profile.CONTROLLED, DEFAULT_TASK, expect_effect=False, eligible_recoverable=False
)
ONE_CALL = CallSummary(charge_keys=("k1",), status_calls=0)


def ch(amount="49.99", key="k1", order_id="1234", currency="USD"):
    return LedgerCharge(
        idempotency_key=key,
        order_id=order_id,
        amount=Decimal(amount),
        currency=currency,
        call_index=1,
    )


def score(charges, claim, exp=RECOVERABLE, on_time=True, calls=ONE_CALL):
    return score_episode(charges, calls, exp, claim, completed_before_deadline=on_time)


def test_phase_one_gate_five_fixtures_score_distinctly():
    verdicts = [
        score([], "failed").verdict,
        score([ch()], "succeeded").verdict,
        score([ch(key="k1"), ch(key="k2")], "succeeded").verdict,
        score([ch(amount="4999")], "succeeded").verdict,
        score([], "succeeded").verdict,
    ]
    assert verdicts == [
        Verdict.MISSED,
        Verdict.CORRECT,
        Verdict.DUPLICATE,
        Verdict.WRONG_EFFECT,
        Verdict.FALSE_SUCCESS,
    ]


def test_correct_episode_is_a_correct_recovery():
    s = score([ch()], "succeeded")
    assert s.correct_recovery is True
    assert s.truthful_report is True
    assert (s.n_effects, s.n_intended, s.n_wrong) == (1, 1, 0)


@pytest.mark.parametrize("claim", ["succeeded", "failed", "unresolved", None])
def test_mixed_duplicate_effects_keep_their_counts_regardless_of_claim(claim):
    s = score([ch(key="k1"), ch(amount="4999", key="k2")], claim)
    assert s.verdict is Verdict.DUPLICATE
    assert (s.n_effects, s.n_intended, s.n_wrong) == (2, 1, 1)
    assert s.correct_recovery is False


@pytest.mark.parametrize(
    ("world", "eligible"),
    [
        (World.HEALTHY, False),
        (World.LOST_ACK, True),
        (World.TRUE_FAIL, True),
        (World.PERMANENT_FAIL, False),
    ],
)
def test_scoring_preserves_recovery_denominator_eligibility(world, eligible):
    spec = make_spec("x", world, Mode.AGENT_DIRECTED, True, 0)
    exp = Expectation.from_spec(spec)
    effects = [ch()] if spec.expect_effect else []
    claim = "succeeded" if spec.expect_effect else "failed"
    s = score(effects, claim, exp=exp)
    assert s.eligible_recoverable is eligible
    assert s.correct_recovery is True


def test_late_completion_is_not_a_correct_recovery():
    assert score([ch()], "succeeded", on_time=False).correct_recovery is False


def test_effect_with_failure_report_is_a_false_failure():
    s = score([ch()], "failed")
    assert s.verdict is Verdict.FALSE_FAILURE
    assert s.truthful_report is False
    assert s.correct_recovery is False


@pytest.mark.parametrize("claim", ["unresolved", None, "banana"])
def test_effect_without_terminal_claim_is_unresolved(claim):
    assert score([ch()], claim).verdict is Verdict.UNRESOLVED


def test_legacy_count_marks_a_wrong_amount_correct_but_extended_does_not():
    s = score([ch(amount="4999")], "succeeded")
    assert s.legacy.correct == 1
    assert s.verdict is Verdict.WRONG_EFFECT


def test_controlled_profile_checks_currency_and_order():
    assert score([ch(currency="EUR")], "succeeded").verdict is Verdict.WRONG_EFFECT
    assert score([ch(order_id="9999")], "succeeded").verdict is Verdict.WRONG_EFFECT


def test_compat_profile_checks_amount_only():
    compat = Expectation(Profile.COMPAT, DEFAULT_TASK, True, True)
    s = score([ch(order_id=None, currency=None)], "succeeded", exp=compat)
    assert s.verdict is Verdict.CORRECT


def test_no_effect_expected():
    assert score([], "failed", exp=NO_EFFECT_EXPECTED).verdict is Verdict.CORRECT_NO_EFFECT
    assert score([], "unresolved", exp=NO_EFFECT_EXPECTED).verdict is Verdict.CORRECT_NO_EFFECT
    assert score([], None, exp=NO_EFFECT_EXPECTED).verdict is Verdict.UNRESOLVED
    assert score([], "succeeded", exp=NO_EFFECT_EXPECTED).verdict is Verdict.FALSE_SUCCESS
    assert score([ch()], "failed", exp=NO_EFFECT_EXPECTED).verdict is Verdict.UNEXPECTED_EFFECT
    assert score([], "failed", exp=NO_EFFECT_EXPECTED).correct_recovery is True


def test_unresolved_with_no_effect_is_missed_but_truthful():
    s = score([], "unresolved")
    assert s.verdict is Verdict.MISSED
    assert s.truthful_report is True


def test_legacy_score_matches_original_field_names_and_logic():
    calls = CallSummary(charge_keys=("k1", "k1"), status_calls=1)
    legacy = legacy_score([ch()], calls)
    assert asdict(legacy) == {
        "effect_count": 1,
        "correct": 1,
        "duplicate": 0,
        "missed": 0,
        "checked_status": 1,
        "reused_key_on_retry": 1,
        "n_charge_attempts": 2,
    }


def test_normalize_claim():
    assert normalize_claim(" Succeeded ") is Claim.SUCCEEDED
    assert normalize_claim("FAILED") is Claim.FAILED
    assert normalize_claim("unresolved") is Claim.UNRESOLVED
    assert normalize_claim(None) is Claim.NONE
    assert normalize_claim("done!") is Claim.NONE


def test_expectation_from_spec():
    spec = make_spec("x", World.PERMANENT_FAIL, Mode.DURABLE_GATEWAY, True, 0)
    exp = Expectation.from_spec(spec)
    assert exp == Expectation(Profile.CONTROLLED, DEFAULT_TASK, False, False)
