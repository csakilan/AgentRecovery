"""Mode B: bounded same-key retries without a durable payment journal.

The attempt counter is local to one execute call and can reset after replay. Only the
payment backend binds arguments under the key; this executor does not give B C's journal.
"""

import copy
from typing import Any

import httpx

from lab.agent.client import SimulatorClient
from lab.agent.executor import (
    DirectExecutor,
    ExecutionEvidence,
    ToolOutcome,
    render_observation,
    requested_key_of,
)
from lab.agent.model import ToolCall
from lab.execution.clock import BoundaryGuard, Clock, DeadlineBudget
from lab.execution.config import ExecutionConfig
from lab.execution.identity import effective_key
from lab.execution.outcomes import TRANSPORT_UNCERTAIN
from lab.protocol.task import Profile
from lab.protocol.validation import ChargeRefusal, text_is_storable, validate_charge

RULE_DETERMINISTIC_RETRY = "deterministic_retry"


class RetryExecutor:
    def __init__(
        self,
        client: SimulatorClient,
        config: ExecutionConfig,
        budget: DeadlineBudget,
        *,
        clock: Clock,
        guard: BoundaryGuard,
    ) -> None:
        self._client = client.with_bounds(guard, budget.remaining_s)
        self._direct = DirectExecutor(self._client)
        self._config = config
        self._budget = budget
        self._clock = clock
        self._guard = guard
        self._key = effective_key(client.ctx)

    def _check(self) -> None:
        self._guard.check()
        self._budget.check()

    def execute(self, call: ToolCall) -> ToolOutcome:
        snapshot = call.model_copy(update={"arguments": copy.deepcopy(call.arguments)})
        self._check()
        if snapshot.name == "charge":
            if isinstance(validate_charge(Profile.CONTROLLED, snapshot.arguments), ChargeRefusal):
                return self._direct.execute(snapshot)
            args = {**snapshot.arguments, "idempotency_key": self._key}
            attempts = self._config.b_max_attempts
        elif snapshot.name == "get_payment_status":
            key = snapshot.arguments.get("idempotency_key")
            if not isinstance(key, str) or not 1 <= len(key) <= 255 or not text_is_storable(key):
                return self._direct.execute(snapshot)
            args = {"idempotency_key": self._key}
            attempts = 1
        else:
            return self._direct.execute(snapshot)

        for number in range(1, attempts + 1):
            self._check()
            try:
                if snapshot.name == "charge":
                    status, body = self._client.charge(copy.deepcopy(args))
                else:
                    status, body = self._client.status(self._key)
            except httpx.TransportError:
                status, body = None, None
            outcome = self._outcome(snapshot, args, status, body, number)
            if number == attempts or (status is not None and not 500 <= status < 600):
                return outcome
            self._check()
            delay = min(self._config.b_backoff_s[number - 1], self._budget.remaining_s())
            self._clock.sleep(delay)
            self._check()
        raise AssertionError("the configured positive attempt limit must return an outcome")

    def _outcome(
        self,
        call: ToolCall,
        args: dict[str, Any],
        status: int | None,
        body: dict[str, Any] | None,
        attempts: int,
    ) -> ToolOutcome:
        source = (
            "transport_uncertainty"
            if status is None
            else ("charge_response" if call.name == "charge" else "status_response")
        )
        observation = (
            TRANSPORT_UNCERTAIN
            if status is None
            else render_observation(
                RULE_DETERMINISTIC_RETRY, tool=call.name, http_status=status, body=body
            )
        )
        return ToolOutcome(
            requested_args=call.arguments,
            requested_key=requested_key_of(call.arguments),
            effective_args=args,
            effective_key=self._key,
            rule=RULE_DETERMINISTIC_RETRY,
            observation=observation,
            http_status=status,
            evidence=ExecutionEvidence(
                source_kind=source,
                source_http_status=status,
                last_http_tool=call.name,
                request_count=attempts,
            ),
        )
