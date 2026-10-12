"""A deterministic stand-in for a model, so the whole pipeline is testable for free.

Each policy is a fixed list of steps. The turn index is the number of tool
observations in the conversation, so a policy is a pure function of the
conversation passed to `complete`: the same conversation always yields the same
turn, including the same tool-call ids.

Every list ends in `report_done`. A policy that is called again after its list is
spent keeps repeating that final report and never issues another `charge`, so an
over-running loop cannot add ledger effects the policy never intended.

The claimed outcome of each policy is chosen so the verdict it is aimed at is
reachable (verdicts are from `lab.oracle.score`):

  naive_new_key   succeeded  DUPLICATE where the first charge secretly committed
  stable_key      succeeded  CORRECT
  check_status    succeeded  CORRECT
  give_up         failed     MISSED where the charge did not commit
  false_success   succeeded  FALSE_SUCCESS where the charge did not commit
  wrong_amount    succeeded  WRONG_EFFECT, whatever it then claims
  malformed_call  failed     MISSED: the invalid call has no effect, so failed is truthful
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from lab.agent.model import ModelTurn, ToolCall

Policy = Literal[
    "naive_new_key",
    "stable_key",
    "check_status",
    "give_up",
    "false_success",
    "malformed_call",
    "wrong_amount",
]

SCRIPTED_POLICIES: tuple[Policy, ...] = get_args(Policy)

Step = tuple[str, dict[str, Any]]

_ORDER_ID = "1234"
_AMOUNT = 49.99
_WRONG_AMOUNT = 4999.00
_CURRENCY = "USD"


def _charge(key: str, *, amount: float = _AMOUNT, currency: bool = True) -> Step:
    arguments: dict[str, Any] = {
        "order_id": _ORDER_ID,
        "amount": amount,
        "idempotency_key": key,
    }
    if currency:
        arguments["currency"] = _CURRENCY
    return ("charge", arguments)


def _status(key: str) -> Step:
    return ("get_payment_status", {"idempotency_key": key})


def _done(outcome: str) -> Step:
    return ("report_done", {"outcome": outcome})


def _plan(policy: Policy) -> list[Step]:
    match policy:
        case "naive_new_key":
            return [_charge("k1"), _charge("k2"), _done("succeeded")]
        case "stable_key":
            return [_charge("k1"), _charge("k1"), _done("succeeded")]
        case "check_status":
            return [_charge("k1"), _status("k1"), _charge("k1"), _done("succeeded")]
        case "give_up":
            return [_charge("k1"), _done("failed")]
        case "false_success":
            return [_charge("k1"), _done("succeeded")]
        case "wrong_amount":
            return [_charge("k1", amount=_WRONG_AMOUNT), _done("succeeded")]
        case "malformed_call":
            return [_charge("k1", currency=False), _done("failed")]
    raise ValueError(f"unknown scripted policy: {policy!r}")


class ScriptedModel:
    def __init__(self, policy: Policy, model_id: str | None = None) -> None:
        if policy not in SCRIPTED_POLICIES:
            raise ValueError(
                f"unknown scripted policy {policy!r}; expected one of {SCRIPTED_POLICIES}"
            )
        self.policy: Policy = policy
        self.model_id = model_id if model_id is not None else f"scripted/{policy}"
        self._plan = _plan(policy)

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        turn = sum(1 for message in messages if message.get("role") == "tool")
        name, arguments = self._plan[min(turn, len(self._plan) - 1)]
        call = ToolCall(id=f"scripted_{self.policy}_{turn}", name=name, arguments=dict(arguments))
        return ModelTurn(
            text=None,
            tool_calls=(call,),
            raw={"scripted": True, "policy": self.policy, "turn": turn},
            malformed=self.policy == "malformed_call" and turn == 0,
        )
