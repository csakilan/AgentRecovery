"""Ledger records as the evaluator sees them."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row


@dataclass(frozen=True)
class LedgerCharge:
    idempotency_key: str
    order_id: str | None
    amount: Decimal
    currency: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError(f"amount must be a Decimal, got {type(self.amount).__name__}")


@dataclass(frozen=True)
class CallSummary:
    charge_keys: tuple[str, ...]
    status_calls: int


# One statement, not two queries: a statement sees a single snapshot, so the effects and
# the call counts always agree with each other even while a request is in flight. A slot
# is claimed (an in_flight call_log row) before its effect can commit, so a snapshot never
# holds an effect whose attempt is missing. Do not split this into separate reads.
_READ_SQL = """
SELECT 'charge'::text AS kind, idempotency_key, order_id, amount, currency, call_index,
       NULL::text AS endpoint
  FROM charges WHERE namespace = %(ns)s
UNION ALL
SELECT 'call'::text, idempotency_key, NULL::text, NULL::numeric, NULL::text, call_index,
       endpoint
  FROM call_log WHERE namespace = %(ns)s AND call_index IS NOT NULL
ORDER BY call_index
"""


def _require_autocommit(conn: psycopg.Connection) -> None:
    # The same rule the simulator enforces; kept local so the oracle never imports the
    # agent-side service module.
    if not conn.autocommit:
        raise ValueError("ledger connections must use autocommit=True")


def read_ledger(conn: psycopg.Connection, namespace: str) -> tuple[list[LedgerCharge], CallSummary]:
    """Read committed effects and call counts for one namespace.

    Intended to run as the ledger_reader role, and safe to call while requests are in
    flight: the read is a single snapshot. A claimed but unfinished call counts as an
    attempt (its call_log row is in_flight), and its effect appears only once committed.

    Every call_log row with a call_index is an attempt, whatever its outcome (in_flight,
    server_error, committed, error). That matches the original episode, which counts at
    the start of each charge call, so a crashed attempt still counts. Calls rejected by
    validation get no call_index and are not attempts. Results are ordered by call_index,
    the order slots were claimed, never by row id or by when a call finished.

    The connection must be in autocommit mode (ValueError otherwise): a read on a
    non-autocommit connection would silently leave a transaction open.
    """
    _require_autocommit(conn)
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(_READ_SQL, {"ns": namespace}).fetchall()
    found = [
        LedgerCharge(
            r["idempotency_key"],
            r["order_id"],
            Decimal(str(r["amount"])),
            r["currency"],
            r["call_index"],
        )
        for r in rows
        if r["kind"] == "charge"
    ]
    calls = [r for r in rows if r["kind"] == "call"]
    summary = CallSummary(
        charge_keys=tuple(c["idempotency_key"] for c in calls if c["endpoint"] == "charge"),
        status_calls=sum(1 for c in calls if c["endpoint"] == "status"),
    )
    return found, summary
