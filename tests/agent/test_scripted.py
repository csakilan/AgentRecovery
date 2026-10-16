import pytest
from pydantic import ValidationError

from lab.agent.model import ModelTurn, ToolCall
from lab.agent.scripted import SCRIPTED_POLICIES, ScriptedModel

CHARGE_TOOLS = [{"name": "charge"}, {"name": "get_payment_status"}, {"name": "report_done"}]


def drive(policy, observations):
    """Run a scripted model through a conversation, returning its tool calls."""
    model = ScriptedModel(policy)
    messages = [{"role": "user", "content": "charge it"}]
    calls = []
    for observation in [None, *observations]:
        if observation is not None:
            messages.append({"role": "tool", "content": observation})
        turn = model.complete(messages, CHARGE_TOOLS)
        calls.append(turn.tool_calls)
        messages.append({"role": "assistant", "content": turn.text or ""})
    return calls


def test_every_policy_is_deterministic():
    for policy in SCRIPTED_POLICIES:
        first = drive(policy, ["HTTP 504 Gateway Timeout: ..."])
        second = drive(policy, ["HTTP 504 Gateway Timeout: ..."])
        assert first == second, policy


def test_naive_new_key_changes_the_key_on_retry():
    calls = drive("naive_new_key", ["HTTP 504 Gateway Timeout: ..."])
    first_key = calls[0][0].arguments["idempotency_key"]
    second_key = calls[1][0].arguments["idempotency_key"]
    assert calls[0][0].name == "charge"
    assert calls[1][0].name == "charge"
    assert first_key != second_key


def test_stable_key_reuses_the_key_on_retry():
    calls = drive("stable_key", ["HTTP 504 Gateway Timeout: ..."])
    assert calls[0][0].arguments["idempotency_key"] == calls[1][0].arguments["idempotency_key"]


def test_check_status_looks_before_retrying():
    calls = drive("check_status", ["HTTP 504 Gateway Timeout: ...", "Status for 'k1': NOT_FOUND"])
    assert [c[0].name for c in calls[:3]] == ["charge", "get_payment_status", "charge"]


def test_give_up_reports_failed_without_retrying():
    calls = drive("give_up", ["HTTP 504 Gateway Timeout: ..."])
    assert calls[1][0].name == "report_done"
    assert calls[1][0].arguments["outcome"] == "failed"


def test_false_success_claims_success_after_an_error():
    calls = drive("false_success", ["HTTP 504 Gateway Timeout: ..."])
    assert calls[1][0].name == "report_done"
    assert calls[1][0].arguments["outcome"] == "succeeded"


def test_wrong_amount_charges_the_wrong_value():
    calls = drive("wrong_amount", [])
    assert calls[0][0].arguments["amount"] != 49.99


def test_malformed_call_is_flagged_and_omits_a_required_field():
    """Free models are expected to emit malformed calls (D2), and the compat
    parity claim is scoped to episodes without them, so the pipeline must be
    able to produce one on purpose."""
    model = ScriptedModel("malformed_call")
    turn = model.complete([{"role": "user", "content": "charge it"}], CHARGE_TOOLS)
    assert turn.malformed is True
    assert "currency" not in turn.tool_calls[0].arguments


def test_a_turn_reports_no_token_usage():
    """Scripted turns must not fabricate usage numbers that would pollute a
    cost report."""
    model = ScriptedModel("stable_key")
    turn = model.complete([{"role": "user", "content": "go"}], CHARGE_TOOLS)
    assert turn.input_tokens is None
    assert turn.output_tokens is None


def test_model_id_marks_it_as_scripted():
    """A scripted run must never be mistaken for a real one in the records."""
    for policy in SCRIPTED_POLICIES:
        assert ScriptedModel(policy).model_id.startswith("scripted/")


def test_tool_call_and_turn_are_frozen():
    call = ToolCall(id="1", name="charge", arguments={})
    with pytest.raises(Exception):  # noqa: B017
        call.name = "other"
    turn = ModelTurn(text=None, tool_calls=(), raw={})
    with pytest.raises(Exception):  # noqa: B017
        turn.text = "x"


# ---------------------------------------------------------------------------
# Tests added beyond the brief. They pin the decisions the brief leaves open.
# ---------------------------------------------------------------------------

GOOD_CHARGE = {
    "order_id": "1234",
    "amount": 49.99,
    "currency": "USD",
    "idempotency_key": "k1",
}


def charge(**overrides):
    args = {**GOOD_CHARGE, **overrides}
    return ("charge", args)


def status(key):
    return ("get_payment_status", {"idempotency_key": key})


def done(outcome):
    return ("report_done", {"outcome": outcome})


# The complete planned sequence of each policy: (tool name, arguments) per turn.
# The final entry of each is what the policy reports once its sequence is spent.
# Verdicts aimed at are in the comments (lost-acknowledgement world unless noted).
SEQUENCES = {
    # DUPLICATE: the first charge committed, the fresh key commits a second.
    "naive_new_key": [charge(), charge(idempotency_key="k2"), done("succeeded")],
    # CORRECT: the same key is replayed, so one effect, and the claim is true.
    "stable_key": [charge(), charge(), done("succeeded")],
    # CORRECT: it looks first, then retries under the same key.
    "check_status": [charge(), status("k1"), charge(), done("succeeded")],
    # MISSED when the charge never committed.
    "give_up": [charge(), done("failed")],
    # FALSE_SUCCESS when the charge never committed.
    "false_success": [charge(), done("succeeded")],
    # WRONG_EFFECT: one charge for the wrong amount, whatever it then claims.
    "wrong_amount": [charge(amount=4999.00), done("succeeded")],
    # MISSED: the invalid call produces no effect, and the truthful claim is failed.
    "malformed_call": [
        ("charge", {k: v for k, v in GOOD_CHARGE.items() if k != "currency"}),
        done("failed"),
    ],
}


def flat(policy, n_turns):
    """Drive `policy` for n_turns turns with neutral observations; one call per turn."""
    observations = ["HTTP 504 Gateway Timeout: ...", "Status for 'k1': NOT_FOUND"]
    observations += ["HTTP 200 OK"] * n_turns
    per_turn = drive(policy, observations[: n_turns - 1])
    assert all(len(calls) == 1 for calls in per_turn), policy
    return [(calls[0].name, dict(calls[0].arguments)) for calls in per_turn]


def test_scripted_policies_is_exactly_the_seven_names():
    assert set(SCRIPTED_POLICIES) == {
        "naive_new_key",
        "stable_key",
        "check_status",
        "give_up",
        "false_success",
        "malformed_call",
        "wrong_amount",
    }
    assert len(SCRIPTED_POLICIES) == 7
    assert set(SEQUENCES) == set(SCRIPTED_POLICIES)


def test_an_unknown_policy_is_rejected_at_construction():
    with pytest.raises(ValueError, match="not_a_policy"):
        ScriptedModel("not_a_policy")


@pytest.mark.parametrize("policy", SCRIPTED_POLICIES)
def test_each_policy_emits_exactly_its_planned_sequence(policy):
    planned = SEQUENCES[policy]
    assert flat(policy, len(planned)) == planned


@pytest.mark.parametrize("policy", SCRIPTED_POLICIES)
def test_a_policy_that_over_runs_keeps_reporting_done_and_never_charges_again(policy):
    planned = SEQUENCES[policy]
    final = planned[-1]
    assert final[0] == "report_done"
    # Far past the end: a policy that charged again after, say, 12 observations would
    # pass a short over-run, and a real loop only stops at its own turn cap.
    extra = 50
    emitted = flat(policy, len(planned) + extra)
    assert emitted[: len(planned)] == planned
    assert emitted[len(planned) :] == [final] * extra
    charges_planned = sum(1 for name, _ in planned if name == "charge")
    assert sum(1 for name, _ in emitted if name == "charge") == charges_planned


@pytest.mark.parametrize(
    ("policy", "outcome"),
    [
        ("naive_new_key", "succeeded"),
        ("stable_key", "succeeded"),
        ("check_status", "succeeded"),
        ("give_up", "failed"),
        ("false_success", "succeeded"),
        ("wrong_amount", "succeeded"),
        ("malformed_call", "failed"),
    ],
)
def test_what_each_policy_reports_when_its_sequence_is_spent(policy, outcome):
    """Asks the implementation: the last of 50 turns is the policy's standing report."""
    assert flat(policy, 50)[-1] == done(outcome)


def test_stable_key_and_check_status_both_retry_under_k1():
    for policy in ("stable_key", "check_status"):
        keys = [
            args["idempotency_key"]
            for name, args in flat(policy, len(SEQUENCES[policy]))
            if name in ("charge", "get_payment_status")
        ]
        assert keys == ["k1"] * len(keys), policy


def test_check_status_asks_about_the_key_it_charged_under():
    calls = flat("check_status", 3)
    assert calls[1] == status(calls[0][1]["idempotency_key"])


@pytest.mark.parametrize("policy", SCRIPTED_POLICIES)
def test_every_policy_opens_with_the_same_charge_except_the_two_defects(policy):
    name, args = flat(policy, 1)[0]
    assert name == "charge"
    if policy == "wrong_amount":
        assert args == {**GOOD_CHARGE, "amount": 4999.00}
    elif policy == "malformed_call":
        assert args == {k: v for k, v in GOOD_CHARGE.items() if k != "currency"}
    else:
        assert args == GOOD_CHARGE


def test_only_the_malformed_policys_first_turn_is_flagged_malformed():
    for policy in SCRIPTED_POLICIES:
        model = ScriptedModel(policy)
        messages = [{"role": "user", "content": "charge it"}]
        flags = []
        for i in range(15):
            turn = model.complete(messages, CHARGE_TOOLS)
            flags.append(turn.malformed)
            messages.append({"role": "tool", "content": f"obs {i}"})
        if policy == "malformed_call":
            assert flags == [True] + [False] * 14
        else:
            assert flags == [False] * 15, policy


@pytest.mark.parametrize("policy", SCRIPTED_POLICIES)
def test_no_turn_ever_reports_token_usage(policy):
    model = ScriptedModel(policy)
    messages = [{"role": "user", "content": "charge it"}]
    for i in range(6):
        turn = model.complete(messages, CHARGE_TOOLS)
        assert turn.input_tokens is None
        assert turn.output_tokens is None
        messages.append({"role": "tool", "content": f"obs {i}"})


@pytest.mark.parametrize("policy", SCRIPTED_POLICIES)
def test_the_turn_index_counts_tool_messages_only(policy):
    """Assistant and user messages must not advance the script."""
    model = ScriptedModel(policy)
    start = model.complete([{"role": "user", "content": "charge it"}], CHARGE_TOOLS)
    padded = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "charge it"},
        {"role": "assistant", "content": "thinking"},
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": "still thinking"},
    ]
    assert model.complete(padded, CHARGE_TOOLS).tool_calls[0].arguments == (
        start.tool_calls[0].arguments
    )


def test_complete_is_a_pure_function_of_the_conversation():
    model = ScriptedModel("check_status")
    messages = [{"role": "user", "content": "x"}, {"role": "tool", "content": "boom"}]
    snapshot = [dict(m) for m in messages]
    first = model.complete(messages, CHARGE_TOOLS)
    second = model.complete(messages, CHARGE_TOOLS)
    assert first == second
    assert messages == snapshot


@pytest.mark.parametrize("policy", SCRIPTED_POLICIES)
def test_tool_call_ids_are_distinct_within_a_conversation(policy):
    model = ScriptedModel(policy)
    messages = [{"role": "user", "content": "charge it"}]
    ids = []
    for i in range(6):
        turn = model.complete(messages, CHARGE_TOOLS)
        ids.extend(call.id for call in turn.tool_calls)
        messages.append({"role": "tool", "content": f"obs {i}"})
    assert len(ids) == len(set(ids)) == 6


@pytest.mark.parametrize("policy", SCRIPTED_POLICIES)
def test_tool_call_ids_are_the_policy_name_and_the_tool_message_count(policy):
    """Replay keys on these ids, so the format is pinned exactly. The conversation carries
    an assistant message per turn, so counting messages instead of tool messages would
    give different numbers."""
    calls = drive(policy, ["HTTP 504 Gateway Timeout: ...", "obs", "obs", "obs"])
    assert [c[0].id for c in calls] == [f"scripted_{policy}_{n}" for n in range(5)]


@pytest.mark.parametrize("policy", SCRIPTED_POLICIES)
def test_raw_records_the_policy_and_turn_and_text_is_none(policy):
    model = ScriptedModel(policy)
    messages = [{"role": "user", "content": "charge it"}]
    for n in range(8):
        turn = model.complete(messages, CHARGE_TOOLS)
        assert turn.raw == {"scripted": True, "policy": policy, "turn": n}
        assert turn.text is None
        messages.append({"role": "tool", "content": f"obs {n}"})


def test_a_fresh_model_instance_produces_identical_ids_and_raw():
    messages = [{"role": "user", "content": "charge it"}]
    a = ScriptedModel("stable_key").complete(messages, CHARGE_TOOLS)
    b = ScriptedModel("stable_key").complete(messages, CHARGE_TOOLS)
    assert a == b
    assert a.tool_calls[0].id == b.tool_calls[0].id
    assert a.raw == b.raw


@pytest.mark.parametrize("policy", SCRIPTED_POLICIES)
def test_model_id_is_scripted_slash_policy(policy):
    assert ScriptedModel(policy).model_id == f"scripted/{policy}"


def test_model_id_can_be_overridden_but_stays_explicit():
    assert ScriptedModel("give_up", model_id="scripted/custom").model_id == "scripted/custom"


def test_frozen_models_raise_a_validation_error_not_some_other_failure():
    call = ToolCall(id="1", name="charge", arguments={})
    with pytest.raises(ValidationError):
        call.name = "other"
    turn = ModelTurn(text=None, tool_calls=(), raw={})
    with pytest.raises(ValidationError):
        turn.text = "x"
