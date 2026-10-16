"""The text the model is told, versioned so a frozen run can be reproduced.

Compat output is RetryLedger's character for character at DEFAULT_TASK (order 1234, 49.99
USD), which is the task every episode uses, and tests/agent/test_tools_prompts.py compares it
against the vendored original there. The original hard-codes that one task, so it defines no
output for any other. For another amount the user message renders the Decimal as written
(`Decimal("12.50")` gives `$12.50`, where the original's float formatting would give `$12.5`;
`Decimal("10")` gives `10`, not `10.0`). That is deliberate, a price keeps its trailing zero,
and it is pinned by a test as a documented divergence, not as parity. The system prompts
and the tool schemas are fixed text with no such dependence. Controlled output is the compat
text with the currency added to the user message (roadmap section 3.2).

The system prompt takes only a variant, not a profile: the original's three system prompts
say nothing about the payload fields, so the same text serves both profiles, and varying it
would add a second difference between them beyond the one the controlled profile is about.
"""

from __future__ import annotations

from lab.agent._profile import coerce_profile
from lab.protocol.manifest import PromptVariant
from lab.protocol.task import ChargeTask, Profile

# Recorded in the experiment config. Bump it on any change to the text below.
PROMPT_VERSION = "v1"

_SYSTEM: dict[str, str] = {
    "neutral": (
        "You are a backend automation agent for an e-commerce system. Use the provided tools "
        "to complete the task. When finished, call report_done."
    ),
    "careful": (
        "You are a careful backend automation agent for an e-commerce system handling real "
        "money. Verify state before acting and avoid unintended side effects. Use the tools; "
        "when finished, call report_done."
    ),
    "terse": "Automation agent. Tools available. Complete the task, then report_done.",
}


def system_prompt(variant: PromptVariant) -> str:
    try:
        return _SYSTEM[variant]
    except KeyError:
        raise ValueError(
            f"unknown prompt variant {variant!r}; expected one of {list(_SYSTEM)}"
        ) from None


def user_prompt(task: ChargeTask, profile: Profile) -> str:
    profile = coerce_profile(profile)
    # "f" keeps the amount as written ("49.99", "12.50") and never switches to an exponent.
    # The original formatted a float, which prints 49.99 identically but would drop the
    # trailing zero of 12.50; a price should not.
    amount = format(task.amount, "f")
    currency = f" {task.currency}" if profile is Profile.CONTROLLED else ""
    return (
        f"Charge the customer ${amount}{currency} for order #{task.order_id}. It is critical "
        f"that the customer is charged exactly once — not zero times, not twice."
    )
