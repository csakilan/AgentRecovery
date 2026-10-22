"""One bounded unit of durable operation work. Status recovery follows in Task 8."""

from uuid import uuid4

import httpx
import psycopg

from lab.agent.executor import ExecutionEvidence, ToolOutcome, render_observation, requested_key_of
from lab.execution.clock import BoundaryGuard, Clock, DeadlineBudget, DeadlineExceeded
from lab.execution.config import ExecutionConfig
from lab.execution.journal import Journal
from lab.execution.leases import LeaseHeartbeat
from lab.execution.outcomes import RECOVERY_UNRESOLVED, TRANSPORT_UNCERTAIN
from lab.execution.types import TERMINAL_STATES, OperationState, OperationUpdate
from lab.protocol.task import args_hash, parse_amount

RULE_DURABLE_GATEWAY = "durable_gateway"


def matching_charge(body, op) -> bool:
    try:
        charge = body["charge"]
        return (
            charge["idempotency_key"] == op.effective_key
            and args_hash(charge["order_id"], parse_amount(charge["amount"]), charge["currency"])
            == op.args_hash
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


class Reconciler:
    def __init__(
        self,
        journal: Journal,
        client,
        config: ExecutionConfig,
        *,
        clock: Clock,
        guard: BoundaryGuard,
    ):
        self.journal, self.client, self.config = journal, client, config
        self.clock, self.guard = clock, guard

    def step(self, operation_id: str, owner: str):
        op = self.journal.get(operation_id)
        if op.state in TERMINAL_STATES:
            return op
        budget = DeadlineBudget(op.deadline_at, self.clock)
        try:
            self.guard.check()
        except DeadlineExceeded:
            if budget.remaining_s() > 0:
                raise
        claim = self.journal.claim(op.id, owner, op.config.lease_s)
        if claim is None:
            return self.journal.get(op.id)
        op = self.journal.get(op.id)
        if budget.remaining_s() <= 0:
            evidence = ExecutionEvidence(
                operation_id=op.id,
                operation_state="UNRESOLVED",
                attempt_no=op.attempt_count or None,
                fence_version=claim.version,
                source_kind="deadline",
            )
            result = ToolOutcome(
                requested_args=op.requested_args,
                requested_key=requested_key_of(op.requested_args),
                effective_args=None,
                effective_key=None,
                rule=RULE_DURABLE_GATEWAY,
                observation=RECOVERY_UNRESOLVED,
                http_status=None,
                evidence=evidence,
            )
            return self.journal.apply(
                claim,
                OperationUpdate(
                    state=OperationState.UNRESOLVED,
                    evidence={"source_kind": "deadline"},
                    terminal_outcome=result,
                ),
            )
        if op.state not in (OperationState.READY, OperationState.CONFIRMED_NO_EFFECT):
            # Unknown is deliberately left recoverable; Run 4 supplies status/backoff.
            self.journal.release(claim)
            return self.journal.get(op.id)
        if op.attempt_count >= op.config.c_max_attempts:
            self.journal.release(claim)
            return self.journal.get(op.id)
        self.guard.check()
        budget.check()
        attempt = self.journal.begin_attempt(claim, op.config.c_max_attempts)
        # A separate connection renews ownership while HTTP is in progress. No journal
        # transaction, checkpoint transaction or model call shares this connection.
        with psycopg.connect(
            self.journal.conn.info.dsn,
            password=self.journal.conn.info.password,
            autocommit=True,
            connect_timeout=5,
        ) as renewal:
            renewal.execute("SET statement_timeout = '5s'")
            with LeaseHeartbeat(
                lambda: Journal(renewal).renew(claim, op.config.lease_s),
                interval_s=op.config.renew_every_s,
            ) as heartbeat:

                class SendGuard:
                    def check(inner):
                        self.guard.check()
                        heartbeat.check()
                        self.journal.assert_owner(claim)

                client = self.client.with_bounds(SendGuard(), budget.remaining_s)
                try:
                    status, body = client.charge(attempt.effective_args)
                    error_type = None
                except httpx.TransportError as exc:
                    status, body, error_type = None, None, type(exc).__name__
                heartbeat.check()
                state = OperationState.UNKNOWN
                if status is not None and 200 <= status < 300 and matching_charge(body, op):
                    state = OperationState.CONFIRMED_SUCCESS
                elif status is not None and 400 <= status < 500:
                    state = OperationState.TERMINAL_FAILURE
                source = "transport_uncertainty" if status is None else "charge_response"
                observation = (
                    TRANSPORT_UNCERTAIN
                    if status is None
                    else render_observation(
                        RULE_DURABLE_GATEWAY, tool="charge", http_status=status, body=body
                    )
                )
                evidence = ExecutionEvidence(
                    operation_id=op.id,
                    operation_state=state.value,
                    attempt_no=attempt.number,
                    fence_version=claim.version,
                    source_kind=source,
                    source_http_status=status,
                    last_http_tool="charge",
                    request_count=1,
                )
                result = ToolOutcome(
                    requested_args=op.requested_args,
                    requested_key=requested_key_of(op.requested_args),
                    effective_args=attempt.effective_args,
                    effective_key=op.effective_key,
                    rule=RULE_DURABLE_GATEWAY,
                    observation=observation,
                    http_status=status,
                    evidence=evidence,
                )
                details = {
                    "owner": owner,
                    "exchange_id": str(uuid4()),
                    "outcome": result.model_dump(mode="json"),
                    "body": body,
                    "error_type": error_type,
                }
                updated = self.journal.apply(
                    claim,
                    OperationUpdate(
                        state=state,
                        evidence=details,
                        attempt_id=attempt.id,
                        http_status=status,
                        response_body=body,
                        error_type=error_type,
                        terminal_outcome=result if state in TERMINAL_STATES else None,
                    ),
                )
        if state not in TERMINAL_STATES:
            self.journal.release(claim)
        return updated
