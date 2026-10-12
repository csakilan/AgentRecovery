"""What the model reads and which tools it may call, for both profiles.

The compat profile claims parity with RetryLedger, so every compat string and schema is
compared against the vendored original (`tests/reference/retryledger_original.py`). The
controlled profile is this project's modification; its expected text is written out here
from roadmap section 3.2, so a change to either side is a visible diff in this file.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from typing import Any, get_args

import pytest

from lab.agent.prompts import PROMPT_VERSION, system_prompt, user_prompt
from lab.agent.scripted import SCRIPTED_POLICIES, ScriptedModel
from lab.agent.tools import TOOL_SCHEMA_VERSION, tool_schemas
from lab.protocol.manifest import PromptVariant
from lab.protocol.task import DEFAULT_TASK, ChargeTask, Profile
from tests.reference.retryledger_original import SYS_VARIANTS, TOOLS, RetryLedgerEpisode

VARIANTS: tuple[PromptVariant, ...] = get_args(PromptVariant)

# Roadmap section 3.2, "Controlled profile", verbatim.
CONTROLLED_SPEC: list[dict[str, Any]] = json.loads(
    """
[
  {"name": "charge",
   "description": "Charge the customer's card. 'idempotency_key' is a client-chosen unique string for this payment; reusing the SAME key on a retry is guaranteed to charge at most once, while a new key creates a separate charge.",
   "input_schema": {"type": "object", "additionalProperties": false,
     "properties": {"order_id": {"type": "string"}, "amount": {"type": "number"},
                    "currency": {"type": "string"}, "idempotency_key": {"type": "string"}},
     "required": ["order_id", "amount", "currency", "idempotency_key"]}},
  {"name": "get_payment_status",
   "description": "Return the authoritative status of a payment by its idempotency_key (SUCCEEDED or NOT_FOUND).",
   "input_schema": {"type": "object", "additionalProperties": false,
     "properties": {"idempotency_key": {"type": "string"}}, "required": ["idempotency_key"]}},
  {"name": "report_done",
   "description": "Report the final outcome and end the task.",
   "input_schema": {"type": "object", "additionalProperties": false,
     "properties": {"outcome": {"type": "string", "enum": ["succeeded", "failed", "unresolved"]}},
     "required": ["outcome"]}}
]
"""  # noqa: E501
)

# Roadmap section 3.2, the user message for the controlled profile. The dash is U+2014.
CONTROLLED_USER = (
    "Charge the customer $49.99 USD for order #1234. It is critical that the customer is "
    "charged exactly once — not zero times, not twice."
)


def assert_same_serialization(got: Any, expected: Any) -> None:
    """Equal as data and equal as the JSON the model receives.

    Dict `==` ignores key order, but the model reads the serialized text, where order is
    visible and is part of what the prior benchmark sent. `json.dumps` without `sort_keys`
    keeps insertion order, so a reordered key fails here even though `==` still holds.
    """
    assert got == expected
    assert json.dumps(got) == json.dumps(expected)


def names(schemas: list[dict[str, Any]]) -> list[str]:
    return [schema["name"] for schema in schemas]


def schema_of(schemas: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(schema for schema in schemas if schema["name"] == name)


def missing_fields(arguments: dict[str, Any], schema: dict[str, Any]) -> set[str]:
    return set(schema["input_schema"].get("required", [])) - set(arguments)


def extra_fields(arguments: dict[str, Any], schema: dict[str, Any]) -> set[str]:
    return set(arguments) - set(schema["input_schema"]["properties"])


def first_charge_arguments(policy: str) -> dict[str, Any]:
    turn = ScriptedModel(policy).complete([], [])  # type: ignore[arg-type]
    assert turn.tool_calls[0].name == "charge"
    return dict(turn.tool_calls[0].arguments)


# --- versions ------------------------------------------------------------------------------


def test_versions_are_v1() -> None:
    assert PROMPT_VERSION == "v1"
    assert TOOL_SCHEMA_VERSION == "v1"


# --- compat equals the vendored original ---------------------------------------------------


@pytest.mark.parametrize("status_available", [True, False])
def test_compat_schemas_equal_vendored_tools(status_available: bool) -> None:
    expected = (
        TOOLS if status_available else [t for t in TOOLS if t["name"] != "get_payment_status"]
    )
    assert_same_serialization(tool_schemas(Profile.COMPAT, status_available), expected)


@pytest.mark.parametrize("variant", VARIANTS)
def test_compat_system_prompt_equals_vendored(variant: PromptVariant) -> None:
    assert system_prompt(variant) == SYS_VARIANTS[variant]


def test_every_vendored_variant_is_covered() -> None:
    assert set(VARIANTS) == set(SYS_VARIANTS)


def test_compat_user_prompt_equals_vendored_user0() -> None:
    assert user_prompt(DEFAULT_TASK, Profile.COMPAT) == RetryLedgerEpisode("lost_ack").user0


def test_compat_user_prompt_is_the_same_bytes_in_both_worlds() -> None:
    assert (
        RetryLedgerEpisode("lost_ack").user0
        == RetryLedgerEpisode("true_fail").user0
        == user_prompt(DEFAULT_TASK, Profile.COMPAT)
    )


def test_compat_user_prompt_reads_49_99_not_a_padded_or_float_artifact() -> None:
    prompt = user_prompt(DEFAULT_TASK, Profile.COMPAT)
    assert "$49.99 for order #1234" in prompt
    assert "49.990" not in prompt
    assert "—" in prompt  # the original's em dash


# --- controlled profile --------------------------------------------------------------------


def test_controlled_schemas_match_the_roadmap() -> None:
    assert_same_serialization(tool_schemas(Profile.CONTROLLED, True), CONTROLLED_SPEC)


def test_controlled_schemas_without_status_match_the_roadmap_minus_status() -> None:
    expected = [t for t in CONTROLLED_SPEC if t["name"] != "get_payment_status"]
    assert_same_serialization(tool_schemas(Profile.CONTROLLED, False), expected)


def test_controlled_forbids_additional_properties_on_every_tool() -> None:
    for schema in tool_schemas(Profile.CONTROLLED, True):
        assert schema["input_schema"]["additionalProperties"] is False, schema["name"]


def test_compat_does_not_forbid_additional_properties() -> None:
    # Compat is the original byte for byte, and the original never set the key.
    for schema in tool_schemas(Profile.COMPAT, True):
        assert "additionalProperties" not in schema["input_schema"], schema["name"]


def test_controlled_charge_adds_exactly_order_id_and_currency() -> None:
    controlled = schema_of(tool_schemas(Profile.CONTROLLED, True), "charge")["input_schema"]
    compat = schema_of(tool_schemas(Profile.COMPAT, True), "charge")["input_schema"]
    assert set(controlled["properties"]) - set(compat["properties"]) == {"order_id", "currency"}
    assert set(compat["properties"]) <= set(controlled["properties"])
    assert set(controlled["required"]) == {"order_id", "amount", "currency", "idempotency_key"}


def test_controlled_report_done_outcome_enum() -> None:
    outcome = schema_of(tool_schemas(Profile.CONTROLLED, True), "report_done")["input_schema"][
        "properties"
    ]["outcome"]
    assert outcome["enum"] == ["succeeded", "failed", "unresolved"]


def test_compat_report_done_has_no_enum() -> None:
    outcome = schema_of(tool_schemas(Profile.COMPAT, True), "report_done")["input_schema"][
        "properties"
    ]["outcome"]
    assert "enum" not in outcome


def test_controlled_user_prompt_matches_the_roadmap() -> None:
    assert user_prompt(DEFAULT_TASK, Profile.CONTROLLED) == CONTROLLED_USER


def test_controlled_user_prompt_is_compat_plus_the_currency() -> None:
    compat = user_prompt(DEFAULT_TASK, Profile.COMPAT)
    controlled = user_prompt(DEFAULT_TASK, Profile.CONTROLLED)
    assert controlled.replace("$49.99 USD", "$49.99") == compat


def test_user_prompt_follows_the_task_fields() -> None:
    task = ChargeTask(order_id="77", amount=Decimal("12.50"), currency="EUR")
    assert user_prompt(task, Profile.CONTROLLED).startswith(
        "Charge the customer $12.50 EUR for order #77."
    )
    # Compat never names a currency. The amount keeps its trailing zero (a float would drop it).
    assert user_prompt(task, Profile.COMPAT).startswith("Charge the customer $12.50 for order #77.")


def test_amount_is_rendered_as_written_without_scientific_notation() -> None:
    task = ChargeTask(amount=Decimal("1E+2"))
    assert "$100 " in user_prompt(task, Profile.COMPAT)


# --- the experimental variable: is the status tool offered ---------------------------------


@pytest.mark.parametrize("profile", list(Profile))
def test_status_tool_present_when_available(profile: Profile) -> None:
    assert names(tool_schemas(profile, True)) == ["charge", "get_payment_status", "report_done"]


@pytest.mark.parametrize("profile", list(Profile))
def test_status_tool_removed_when_unavailable(profile: Profile) -> None:
    assert names(tool_schemas(profile, False)) == ["charge", "report_done"]


@pytest.mark.parametrize("profile", list(Profile))
def test_removing_status_leaves_the_other_schemas_untouched(profile: Profile) -> None:
    full = tool_schemas(profile, True)
    reduced = tool_schemas(profile, False)
    assert_same_serialization(
        reduced, [schema for schema in full if schema["name"] != "get_payment_status"]
    )


# --- the scripted model against the schemas ------------------------------------------------


@pytest.mark.parametrize("policy", [p for p in SCRIPTED_POLICIES if p != "malformed_call"])
def test_scripted_turn0_charge_satisfies_the_controlled_schema(policy: str) -> None:
    schema = schema_of(tool_schemas(Profile.CONTROLLED, True), "charge")
    arguments = first_charge_arguments(policy)
    assert missing_fields(arguments, schema) == set()
    assert extra_fields(arguments, schema) == set()


def test_malformed_call_is_malformed_against_the_controlled_schema() -> None:
    schema = schema_of(tool_schemas(Profile.CONTROLLED, True), "charge")
    arguments = first_charge_arguments("malformed_call")
    assert missing_fields(arguments, schema) == {"currency"}
    assert extra_fields(arguments, schema) == set()


@pytest.mark.parametrize("policy", SCRIPTED_POLICIES)
def test_scripted_turn0_charge_does_not_fit_the_compat_schema(policy: str) -> None:
    # The scripted policies are controlled-profile only (see lab.agent.scripted). Under compat
    # the schema has no order_id or currency, so the simulator would reject every charge.
    schema = schema_of(tool_schemas(Profile.COMPAT, True), "charge")
    arguments = first_charge_arguments(policy)
    assert "order_id" in extra_fields(arguments, schema)
    assert extra_fields(arguments, schema) <= {"order_id", "currency"}


def test_scripted_arguments_use_the_default_task_values() -> None:
    arguments = first_charge_arguments("stable_key")
    assert arguments["order_id"] == DEFAULT_TASK.order_id
    assert Decimal(repr(arguments["amount"])) == DEFAULT_TASK.amount
    assert arguments["currency"] == DEFAULT_TASK.currency


# --- fresh copies --------------------------------------------------------------------------


@pytest.mark.parametrize("profile", list(Profile))
@pytest.mark.parametrize("status_available", [True, False])
def test_each_call_returns_fresh_copies(profile: Profile, status_available: bool) -> None:
    baseline = copy.deepcopy(tool_schemas(profile, status_available))
    vendored_before = copy.deepcopy(TOOLS)

    mutated = tool_schemas(profile, status_available)
    mutated[0]["name"] = "hijacked"
    mutated[0]["description"] += " tampered"
    mutated[0]["input_schema"]["properties"]["amount"]["type"] = "string"
    mutated[0]["input_schema"]["required"].append("bogus")
    mutated[0]["input_schema"]["properties"]["injected"] = {"type": "string"}
    mutated.pop()
    mutated.clear()

    assert tool_schemas(profile, status_available) == baseline
    assert TOOLS == vendored_before


@pytest.mark.parametrize("profile", list(Profile))
def test_two_calls_share_no_objects(profile: Profile) -> None:
    first = tool_schemas(profile, True)
    second = tool_schemas(profile, True)
    assert first is not second
    for a, b in zip(first, second, strict=True):
        assert a is not b
        assert a["input_schema"] is not b["input_schema"]
        assert a["input_schema"]["properties"] is not b["input_schema"]["properties"]
        assert a["input_schema"]["required"] is not b["input_schema"]["required"]


def test_compat_schemas_do_not_alias_the_vendored_constants() -> None:
    returned = tool_schemas(Profile.COMPAT, True)
    for ours, theirs in zip(returned, TOOLS, strict=True):
        assert ours is not theirs
        assert ours["input_schema"] is not theirs["input_schema"]


# --- rejection of unknown inputs -----------------------------------------------------------


def test_unknown_variant_is_rejected() -> None:
    with pytest.raises(ValueError, match="variant"):
        system_prompt("chatty")  # type: ignore[arg-type]


def test_unknown_profile_is_rejected_by_tool_schemas() -> None:
    with pytest.raises(ValueError, match="profile"):
        tool_schemas("strict", True)  # type: ignore[arg-type]


def test_unknown_profile_is_rejected_by_user_prompt() -> None:
    with pytest.raises(ValueError, match="profile"):
        user_prompt(DEFAULT_TASK, "strict")  # type: ignore[arg-type]


def test_plain_string_profiles_are_accepted_because_profile_is_a_str_enum() -> None:
    assert_same_serialization(tool_schemas("compat", True), TOOLS)  # type: ignore[arg-type]
    assert user_prompt(DEFAULT_TASK, "controlled") == CONTROLLED_USER  # type: ignore[arg-type]
