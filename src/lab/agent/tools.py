"""The tools the model may call, for each profile.

The shape is the original's: a list of `{name, description, input_schema}`. It is not any
provider's function-calling format. The real model client (Plan 3) must convert each entry
(for OpenRouter, to `{"type": "function", "function": {name, description, parameters}}`);
that conversion belongs there, so these schemas stay comparable to the vendored original.

Compat is RetryLedger's `TOOLS` verbatim (compared in tests/agent/test_tools_prompts.py).
Controlled is roadmap section 3.2: it adds `order_id` and `currency` to `charge`, gives
`report_done` an `unresolved` outcome, and sets `additionalProperties: false` on every tool.
"""

from __future__ import annotations

import copy
from typing import Any

from lab.protocol.task import Profile

# Recorded in the experiment config. Bump it on any change to the schemas below.
TOOL_SCHEMA_VERSION = "v1"

_STATUS_TOOL = "get_payment_status"

_CHARGE_DESCRIPTION = (
    "Charge the customer's card. 'idempotency_key' is a client-chosen unique string for this "
    "payment; reusing the SAME key on a retry is guaranteed to charge at most once, while a new "
    "key creates a separate charge."
)
_STATUS_DESCRIPTION = (
    "Return the authoritative status of a payment by its idempotency_key (SUCCEEDED or NOT_FOUND)."
)
_DONE_DESCRIPTION = "Report the final outcome and end the task."

_COMPAT: list[dict[str, Any]] = [
    {
        "name": "charge",
        "description": _CHARGE_DESCRIPTION,
        "input_schema": {
            "type": "object",
            "properties": {"amount": {"type": "number"}, "idempotency_key": {"type": "string"}},
            "required": ["amount", "idempotency_key"],
        },
    },
    {
        "name": _STATUS_TOOL,
        "description": _STATUS_DESCRIPTION,
        "input_schema": {
            "type": "object",
            "properties": {"idempotency_key": {"type": "string"}},
            "required": ["idempotency_key"],
        },
    },
    {
        "name": "report_done",
        "description": _DONE_DESCRIPTION,
        "input_schema": {
            "type": "object",
            "properties": {"outcome": {"type": "string", "description": "succeeded | failed"}},
            "required": ["outcome"],
        },
    },
]

_CONTROLLED: list[dict[str, Any]] = [
    {
        "name": "charge",
        "description": _CHARGE_DESCRIPTION,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "order_id": {"type": "string"},
                "amount": {"type": "number"},
                "currency": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["order_id", "amount", "currency", "idempotency_key"],
        },
    },
    {
        "name": _STATUS_TOOL,
        "description": _STATUS_DESCRIPTION,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"idempotency_key": {"type": "string"}},
            "required": ["idempotency_key"],
        },
    },
    {
        "name": "report_done",
        "description": _DONE_DESCRIPTION,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "outcome": {"type": "string", "enum": ["succeeded", "failed", "unresolved"]}
            },
            "required": ["outcome"],
        },
    },
]


def tool_schemas(profile: Profile, status_available: bool) -> list[dict[str, Any]]:
    """The tool list for a profile, as a fresh deep copy the caller may mutate freely.

    `status_available=False` drops `get_payment_status`, as the original does when `verify`
    is off. That is the behaviour study's experimental variable.
    """
    try:
        profile = Profile(profile)
    except ValueError:
        raise ValueError(
            f"unknown profile {profile!r}; expected one of {[p.value for p in Profile]}"
        ) from None
    source = _CONTROLLED if profile is Profile.CONTROLLED else _COMPAT
    return [
        copy.deepcopy(tool) for tool in source if status_available or tool["name"] != _STATUS_TOOL
    ]
