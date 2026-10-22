"""Common run leases for every execution mode; database time is authoritative."""

import math
from datetime import datetime

from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict

from lab.execution.clock import DeadlineBudget
from lab.execution.types import LeaseLost


class RunClaim(BaseModel):
    model_config = ConfigDict(frozen=True)
    run_id: str
    thread_id: str
    owner: str
    version: int
    deadline_at: datetime


def validate_lease(owner: str, lease_s: float) -> None:
    if not owner or not math.isfinite(lease_s) or lease_s <= 0:
        raise ValueError("owner and finite positive lease are required")


def claim_run(conn, run_id: str, owner: str, lease_s: float = 30.0) -> RunClaim | None:
    if not conn.autocommit:
        raise ValueError("run claims require an autocommit connection")
    validate_lease(owner, lease_s)
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "UPDATE runs SET state='running',claim_owner=%s,"
            "lease_expires_at=clock_timestamp()+(%s * interval '1 second'),"
            "version=version+1,delivery_count=delivery_count+1 WHERE id=%s "
            "AND resume_version=1 AND setup_state='ready' "
            "AND state IN ('queued','crashed','running') "
            "AND (claim_owner IS NULL OR lease_expires_at <= clock_timestamp()) "
            "RETURNING id,thread_id,version,deadline_at",
            (owner, lease_s, run_id),
        ).fetchone()
        if row is None:
            return None
        return RunClaim(
            run_id=row["id"],
            thread_id=row["thread_id"],
            owner=owner,
            version=row["version"],
            deadline_at=row["deadline_at"],
        )


def renew_run(conn, claim: RunClaim, lease_s: float = 30.0) -> bool:
    if not conn.autocommit:
        raise ValueError("run renewal requires an autocommit connection")
    validate_lease(claim.owner, lease_s)
    with conn.transaction():
        return (
            conn.execute(
                "UPDATE runs SET lease_expires_at=clock_timestamp()+(%s * interval '1 second') "
                "WHERE id=%s AND claim_owner=%s AND version=%s AND state='running' "
                "AND lease_expires_at > clock_timestamp()",
                (lease_s, claim.run_id, claim.owner, claim.version),
            ).rowcount
            == 1
        )


def check_run_cursor(cur, claim: RunClaim, *, lock: bool = False) -> None:
    row = cur.execute(
        "SELECT id FROM runs WHERE id=%s AND thread_id=%s AND claim_owner=%s "
        "AND version=%s AND state='running' AND lease_expires_at > clock_timestamp()"
        + (" FOR UPDATE" if lock else ""),
        (claim.run_id, claim.thread_id, claim.owner, claim.version),
    ).fetchone()
    if row is None:
        raise LeaseLost("run ownership expired or was replaced")


def assert_run_owner(conn, claim: RunClaim) -> None:
    with conn.cursor() as cur:
        check_run_cursor(cur, claim)


def finish_run(
    conn, claim: RunClaim, *, claimed_outcome: str | None, finished_at: datetime, state: str
) -> None:
    if not conn.autocommit:
        raise ValueError("run finalization requires an autocommit connection")
    if state not in {"completed", "crashed", "abandoned", "score_failed"}:
        raise ValueError("invalid final run state")
    with conn.transaction(), conn.cursor() as cur:
        check_run_cursor(cur, claim, lock=True)
        result = cur.execute(
            "UPDATE runs SET state=%s,claimed_outcome=%s,finished_at=%s,"
            "claim_owner=NULL,lease_expires_at=NULL WHERE id=%s AND claim_owner=%s "
            "AND version=%s AND lease_expires_at > clock_timestamp()",
            (state, claimed_outcome, finished_at, claim.run_id, claim.owner, claim.version),
        )
        if result.rowcount != 1:
            raise LeaseLost("run ownership expired during finalization")


class RunGuard:
    def __init__(self, conn, claim: RunClaim, budget: DeadlineBudget):
        self.conn, self.claim, self.budget = conn, claim, budget

    def check(self) -> None:
        assert_run_owner(self.conn, self.claim)
        self.budget.check()
