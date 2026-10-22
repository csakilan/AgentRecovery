"""Mode C keeps the first valid intent and reuses durable terminal evidence."""

import copy
from uuid import uuid4

from lab.agent.executor import DirectExecutor, ExecutionEvidence, ToolOutcome, requested_key_of
from lab.execution.identity import operation_id
from lab.execution.outcomes import INTENT_CONFLICT, RECOVERY_UNRESOLVED
from lab.execution.retry import RetryExecutor
from lab.execution.types import TERMINAL_STATES, IntentConflict
from lab.protocol.task import Profile
from lab.protocol.validation import ChargeRefusal, validate_charge


class GatewayExecutor:
    def __init__(self, client, journal, reconciler, config, budget, *, clock, guard):
        self.client, self.journal, self.reconciler = client, journal, reconciler
        self.config, self.budget, self.clock, self.guard = config, budget, clock, guard
        self.direct = DirectExecutor(client.with_bounds(guard, budget.remaining_s))
        self.status = RetryExecutor(client, config, budget, clock=clock, guard=guard)

    def execute(self, call):
        snapshot = call.model_copy(update={"arguments": copy.deepcopy(call.arguments)})
        self.guard.check()
        self.budget.check()
        if snapshot.name == "get_payment_status":
            return self.status.execute(snapshot).model_copy(update={"rule": "durable_gateway"})
        if snapshot.name != "charge":
            return self.direct.execute(snapshot)
        charge = validate_charge(Profile.CONTROLLED, snapshot.arguments)
        if isinstance(charge, ChargeRefusal):
            return self.direct.execute(snapshot)
        try:
            op = self.journal.register(
                self.client.ctx,
                charge,
                snapshot.arguments,
                self.budget.deadline_at,
                config=self.config,
            )
        except IntentConflict:
            op = self.journal.get(str(operation_id(self.client.ctx)))
            return ToolOutcome(
                requested_args=snapshot.arguments,
                requested_key=requested_key_of(snapshot.arguments),
                effective_args=None,
                effective_key=None,
                http_status=None,
                rule="intent_conflict",
                observation=INTENT_CONFLICT,
                evidence=ExecutionEvidence(
                    operation_id=op.id,
                    operation_state=op.state.value,
                    fence_version=op.version,
                    source_kind="argument_conflict",
                ),
            )
        owner = "foreground-" + str(uuid4())
        last = None
        seen = set()
        count = 0
        while op.state not in TERMINAL_STATES:
            op = self.reconciler.step(op.id, owner)
            details = op.last_evidence or {}
            if details.get("owner") == owner and details.get("exchange_id") not in seen:
                seen.add(details["exchange_id"])
                last = ToolOutcome.model_validate(details["outcome"])
                count += 1
            if op.state in TERMINAL_STATES:
                break
            self.guard.check()
            remaining = min(
                self.budget.remaining_s(),
                max(0, (op.deadline_at - self.clock.now()).total_seconds()),
            )
            if remaining <= 0:
                # A still-live owner can finish its already-sent request later.
                # Foreground expiry is not permission to steal or overwrite it.
                return ToolOutcome(
                    requested_args=snapshot.arguments,
                    requested_key=requested_key_of(snapshot.arguments),
                    effective_args=last.effective_args if last else None,
                    effective_key=last.effective_key if last else None,
                    http_status=last.http_status if last else None,
                    rule="durable_gateway",
                    observation=RECOVERY_UNRESOLVED,
                    evidence=ExecutionEvidence(
                        operation_id=op.id,
                        operation_state=op.state.value,
                        attempt_no=op.attempt_count or None,
                        fence_version=op.version,
                        source_kind="deadline",
                        last_http_tool=last.evidence.last_http_tool if last else None,
                        request_count=count,
                    ),
                )
            self.clock.sleep(min(op.config.tick_s, remaining))
        terminal = op.terminal_outcome
        assert terminal is not None
        evidence = terminal.evidence.model_copy(
            update={
                "cached": count == 0,
                "request_count": count,
                "last_http_tool": last.evidence.last_http_tool
                if last
                else terminal.evidence.last_http_tool,
            }
        )
        return terminal.model_copy(
            update={
                "requested_args": snapshot.arguments,
                "requested_key": requested_key_of(snapshot.arguments),
                "effective_args": last.effective_args if last else None,
                "effective_key": last.effective_key if last else None,
                "http_status": last.http_status if last else None,
                "evidence": evidence,
            }
        )
