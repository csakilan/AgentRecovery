"""Short, service-role transactions for durable payment intent."""

from datetime import datetime
from typing import Any
from uuid import uuid4

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from lab.agent.client import TrustedContext
from lab.agent.executor import ToolOutcome
from lab.execution.clock import DeadlineExceeded
from lab.execution.config import ExecutionConfig
from lab.execution.identity import effective_key, operation_id
from lab.execution.types import (
    TERMINAL_STATES,
    Attempt,
    IntentConflict,
    LeaseLost,
    Operation,
    OperationClaim,
    OperationUpdate,
)
from lab.protocol.evidence import pack_evidence, unpack_evidence
from lab.protocol.task import args_hash, parse_amount
from lab.protocol.validation import ValidCharge


class Journal:
    def __init__(self, conn: Connection[Any]):
        if not conn.autocommit:
            raise ValueError("journal requires an autocommit connection")
        self.conn = conn

    def claim(self, operation_id: str, owner: str, lease_s: float) -> OperationClaim | None:
        from lab.runner.ownership import validate_lease

        validate_lease(owner, lease_s)
        with self.conn.transaction(), self.conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "UPDATE operations SET claim_owner=%s,version=version+1,"
                "lease_expires_at=clock_timestamp()+(%s * interval '1 second'),"
                "state=CASE WHEN state='IN_FLIGHT' THEN 'UNKNOWN' ELSE state END "
                "WHERE id=%s AND state IN ('READY','IN_FLIGHT','UNKNOWN','CONFIRMED_NO_EFFECT') "
                "AND (claim_owner IS NULL OR lease_expires_at <= clock_timestamp()) "
                "RETURNING version,state",
                (owner, lease_s, operation_id),
            ).fetchone()
            if row is None:
                return None
            self._event(cur, operation_id, "claimed", row["version"], {"state": row["state"]})
            return OperationClaim(operation_id=operation_id, owner=owner, version=row["version"])

    def renew(self, claim: OperationClaim, lease_s: float) -> bool:
        from lab.runner.ownership import validate_lease

        validate_lease(claim.owner, lease_s)
        with self.conn.transaction():
            return (
                self.conn.execute(
                    "UPDATE operations SET lease_expires_at=clock_timestamp()+(%s * "
                    "interval '1 second') "
                    "WHERE id=%s AND claim_owner=%s AND version=%s AND lease_expires_at > "
                    "clock_timestamp()",
                    (lease_s, claim.operation_id, claim.owner, claim.version),
                ).rowcount
                == 1
            )

    @staticmethod
    def _owned(cur, claim: OperationClaim):
        row = cur.execute(
            "SELECT *,clock_timestamp() AS db_now FROM operations WHERE id=%s AND claim_owner=%s "
            "AND version=%s AND lease_expires_at > clock_timestamp() FOR UPDATE",
            (claim.operation_id, claim.owner, claim.version),
        ).fetchone()
        if row is None:
            raise LeaseLost("operation ownership expired or was replaced")
        return row

    def assert_owner(self, claim: OperationClaim) -> None:
        with self.conn.transaction(), self.conn.cursor(row_factory=dict_row) as cur:
            self._owned(cur, claim)

    def release(self, claim: OperationClaim) -> None:
        with self.conn.transaction(), self.conn.cursor(row_factory=dict_row) as cur:
            self._owned(cur, claim)
            cur.execute(
                "UPDATE operations SET claim_owner=NULL,lease_expires_at=NULL WHERE id=%s",
                (claim.operation_id,),
            )

    def begin_attempt(self, claim: OperationClaim, max_attempts: int) -> Attempt:
        with self.conn.transaction(), self.conn.cursor(row_factory=dict_row) as cur:
            row = self._owned(cur, claim)
            if row["deadline_at"] <= row["db_now"]:
                raise DeadlineExceeded("operation deadline passed before send")
            limit = min(
                max_attempts, ExecutionConfig.model_validate(row["config_json"]).c_max_attempts
            )
            if (
                row["state"] not in ("READY", "CONFIRMED_NO_EFFECT")
                or row["attempt_count"] >= limit
            ):
                raise ValueError("operation cannot begin another attempt")
            number = row["attempt_count"] + 1
            aid = str(uuid4())
            cur.execute(
                "INSERT INTO attempts(id,operation_id,attempt_no,fence_version,owner) "
                "VALUES(%s,%s,%s,%s,%s)",
                (aid, claim.operation_id, number, claim.version, claim.owner),
            )
            cur.execute(
                "UPDATE operations SET "
                "state='IN_FLIGHT',attempt_count=%s,next_check_at=NULL WHERE id=%s",
                (number, claim.operation_id),
            )
            self._event(
                cur,
                claim.operation_id,
                "attempt_started",
                claim.version,
                {"attempt_id": aid, "attempt_no": number},
            )
            args = unpack_evidence(row["requested_args_json"])
            effective = {
                "idempotency_key": row["effective_key"],
                "order_id": args["order_id"],
                "amount": format(parse_amount(args["amount"]).normalize(), "f"),
                "currency": args["currency"],
            }
            return Attempt(
                id=aid, number=number, fence_version=claim.version, effective_args=effective
            )

    def apply(self, claim: OperationClaim, update: OperationUpdate) -> Operation:
        terminal = update.state in TERMINAL_STATES
        if terminal != (update.terminal_outcome is not None):
            raise ValueError("terminal state requires exactly one terminal outcome")
        allowed = {
            "READY": {"UNRESOLVED"},
            "IN_FLIGHT": {"UNKNOWN", "CONFIRMED_SUCCESS", "TERMINAL_FAILURE", "UNRESOLVED"},
            "UNKNOWN": {
                "UNKNOWN",
                "CONFIRMED_NO_EFFECT",
                "CONFIRMED_SUCCESS",
                "TERMINAL_FAILURE",
                "UNRESOLVED",
            },
            "CONFIRMED_NO_EFFECT": {"UNKNOWN", "UNRESOLVED"},
        }
        with self.conn.transaction(), self.conn.cursor(row_factory=dict_row) as cur:
            row = self._owned(cur, claim)
            if update.state.value not in allowed.get(row["state"], set()):
                raise ValueError("invalid operation transition")
            if update.attempt_id is not None:
                result = cur.execute(
                    "UPDATE attempts SET "
                    "finished_at=clock_timestamp(),http_status=%s,response_json=%s,error_type=%s "
                    "WHERE id=%s AND operation_id=%s AND fence_version=%s AND owner=%s "
                    "AND finished_at IS NULL",
                    (
                        update.http_status,
                        Jsonb(pack_evidence(update.response_body))
                        if update.response_body is not None
                        else None,
                        update.error_type,
                        update.attempt_id,
                        claim.operation_id,
                        claim.version,
                        claim.owner,
                    ),
                )
                if result.rowcount != 1:
                    raise LeaseLost("attempt does not belong to this active claim")
            cur.execute(
                "UPDATE operations SET state=%s,last_evidence_json=%s,next_check_at=%s,"
                "status_check_count=COALESCE(%s,status_check_count),terminal_outcome_json=%s,"
                "finished_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END,"
                "claim_owner=CASE WHEN %s THEN NULL ELSE claim_owner END,"
                "lease_expires_at=CASE WHEN %s THEN NULL ELSE lease_expires_at END "
                "WHERE id=%s AND lease_expires_at > clock_timestamp()",
                (
                    update.state.value,
                    Jsonb(pack_evidence(update.evidence)),
                    update.next_check_at,
                    update.status_check_count,
                    Jsonb(pack_evidence(update.terminal_outcome.model_dump(mode="json")))
                    if terminal
                    else None,
                    terminal,
                    terminal,
                    terminal,
                    claim.operation_id,
                ),
            )
            if cur.rowcount != 1:
                raise LeaseLost("operation expired during update")
            self._event(cur, claim.operation_id, update.state.value, claim.version, update.evidence)
            if terminal:
                cur.execute(
                    "INSERT INTO outbox(id,operation_id,run_id) VALUES(%s,%s,%s)",
                    ("terminal-" + claim.operation_id, claim.operation_id, row["run_id"]),
                )
        return self.get(claim.operation_id)

    def register(
        self,
        ctx: TrustedContext,
        charge: ValidCharge,
        requested_args: dict[str, Any],
        deadline_at: datetime,
        *,
        config: ExecutionConfig,
    ) -> Operation:
        if not isinstance(charge, ValidCharge):
            raise ValueError("only validated charge intent may be registered")
        if deadline_at.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")
        oid = str(operation_id(ctx))
        digest = args_hash(charge.order_id, charge.amount, charge.currency)
        conflict = False
        with self.conn.transaction(), self.conn.cursor(row_factory=dict_row) as cur:
            parent = cur.execute(
                "SELECT namespace,episode_id,execution_config_json,deadline_at "
                "FROM runs WHERE id=%s FOR SHARE",
                (ctx.run_id,),
            ).fetchone()
            if (
                parent is None
                or parent["namespace"] != ctx.namespace
                or parent["episode_id"] != ctx.episode_id
            ):
                raise ValueError("trusted context does not match saved run")
            if (
                parent["execution_config_json"] is not None
                and ExecutionConfig.model_validate(parent["execution_config_json"]) != config
            ):
                raise ValueError("operation configuration differs from saved run")
            if parent["deadline_at"] is not None:
                deadline_at = min(deadline_at, parent["deadline_at"])
            inserted = cur.execute(
                "INSERT INTO operations(id,run_id,tenant,namespace,task_id,effective_key,"
                "requested_args_json,args_hash,config_json,state,deadline_at,next_check_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'READY',"
                "LEAST(%s,clock_timestamp()+(%s * interval '1 second')),clock_timestamp()) "
                "ON CONFLICT DO NOTHING RETURNING id",
                (
                    oid,
                    ctx.run_id,
                    ctx.tenant,
                    ctx.namespace,
                    ctx.task_id,
                    effective_key(ctx),
                    Jsonb(pack_evidence(requested_args)),
                    digest,
                    Jsonb(config.model_dump(mode="json")),
                    deadline_at,
                    config.operation_timeout_s,
                ),
            ).fetchone()
            row = cur.execute("SELECT * FROM operations WHERE id=%s FOR UPDATE", (oid,)).fetchone()
            if row is None:
                raise ValueError("operation identity collision")
            if row["config_json"] != config.model_dump(mode="json"):
                raise ValueError("operation configuration is immutable")
            conflict = row["args_hash"] != digest
            if inserted or conflict:
                self._event(
                    cur,
                    oid,
                    "intent_conflict" if conflict else "registered",
                    row["version"],
                    {"requested_args": requested_args} if conflict else {},
                )
        if conflict:
            raise IntentConflict(oid)
        return self.get(oid)

    @staticmethod
    def _event(cur, oid, event_type, version, details):
        cur.execute(
            "INSERT INTO recovery_events(operation_id,event_type,fence_version,details_json) "
            "VALUES(%s,%s,%s,%s)",
            (oid, event_type, version, Jsonb(pack_evidence(details))),
        )

    def get(self, operation_id: str) -> Operation:
        with self.conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT o.*,r.episode_id FROM operations o JOIN runs r ON r.id=o.run_id "
                "WHERE o.id=%s",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return Operation(
            id=row["id"],
            ctx=TrustedContext(
                tenant=row["tenant"],
                namespace=row["namespace"],
                run_id=row["run_id"],
                episode_id=row["episode_id"],
                task_id=row["task_id"],
            ),
            requested_args=unpack_evidence(row["requested_args_json"]),
            args_hash=row["args_hash"],
            effective_key=row["effective_key"],
            config=ExecutionConfig.model_validate(row["config_json"]),
            **{
                k: row[k]
                for k in (
                    "state",
                    "attempt_count",
                    "version",
                    "claim_owner",
                    "lease_expires_at",
                    "next_check_at",
                    "status_check_count",
                    "deadline_at",
                    "created_at",
                    "finished_at",
                )
            },
            last_evidence=unpack_evidence(row["last_evidence_json"])
            if row["last_evidence_json"] is not None
            else None,
            terminal_outcome=ToolOutcome.model_validate(
                unpack_evidence(row["terminal_outcome_json"])
            )
            if row["terminal_outcome_json"] is not None
            else None,
        )
