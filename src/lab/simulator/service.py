"""Payment simulator core.

Every ledger write in the lab goes through this module. The HTTP layer only
translates requests and responses. Connections must be in autocommit mode so that
no row lock outlives the statement or transaction block that needs it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from lab.protocol.faults import ChargeFault, FaultPlan
from lab.protocol.task import Profile, args_hash
from lab.protocol.validation import ChargeRefusal, validate_charge
from lab.protocol.validation import ValidCharge as _ValidCharge
from lab.protocol.validation import text_is_storable as _storable

logger = logging.getLogger(__name__)

TIMEOUT_MESSAGE = "the upstream processor did not confirm. Payment status is UNKNOWN."
NOT_PROCESSED_MESSAGE = "the charge was not processed. Payment status is UNKNOWN."
SUCCESS_MESSAGE = "Charge succeeded."
REPLAY_MESSAGE = (
    "Idempotent hit: a payment already succeeded under this key. No additional charge was made."
)
CONFLICT_MESSAGE = (
    "this idempotency key was already used with different parameters. No charge was made."
)
STATUS_UNAVAILABLE_MESSAGE = "payment status service is temporarily unavailable."
SERVER_ERROR_MESSAGE = "the simulator failed while handling this call."

# Every call_log outcome this module writes must be listed in the CHECK constraint of
# migration 0003_call_log_outcome_check.sql. A new outcome needs a new migration extending it,
# and a path in tests/simulator/test_call_accounting.py that writes it.
#
# Call-log outcome of a call whose slot is claimed but whose result is not yet recorded.
IN_FLIGHT = "in_flight"
# Call-log outcome of a call that raised after its slot was claimed.
SERVER_ERROR = "server_error"


def _clean_str(value: str) -> str:
    """The same string made safe to store as evidence: NUL and surrogates become visible text."""
    return value.replace("\x00", "\\u0000").encode("utf-8", "backslashreplace").decode("utf-8")


@dataclass(frozen=True)
class SimResponse:
    http_status: int
    body: dict[str, Any]


class UnknownNamespace(Exception):
    pass


def _require_autocommit(conn: psycopg.Connection) -> None:
    if not conn.autocommit:
        raise ValueError("simulator connections must use autocommit=True")


def _one(conn: psycopg.Connection, query: Any, params: tuple = ()) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchone()


def _error(http_status: int, code: str, message: str) -> SimResponse:
    return SimResponse(http_status, {"error": {"code": code, "message": message}})


def _charge_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "charge_id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "order_id": row["order_id"],
        "amount": format(row["amount"], "f"),
        "currency": row["currency"],
    }


def configure_namespace(
    conn: psycopg.Connection, namespace: str, profile: Profile, plan: FaultPlan
) -> None:
    """Create or reset a namespace. Resetting deletes its ledger, log and barriers."""
    with conn.transaction():
        conn.execute("DELETE FROM sim_namespaces WHERE namespace = %s", (namespace,))
        conn.execute(
            "INSERT INTO sim_namespaces (namespace, profile, fault_plan) VALUES (%s, %s, %s)",
            (namespace, profile.value, Jsonb(plan.model_dump(mode="json"))),
        )


class NamespaceConfigConflict(ValueError):
    """Create-only setup cannot replace an existing namespace configuration."""


def configure_namespace_once(
    conn: psycopg.Connection, namespace: str, profile: Profile, plan: FaultPlan
) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO sim_namespaces(namespace,profile,fault_plan) "
            "VALUES(%s,%s,%s) ON CONFLICT(namespace) DO NOTHING",
            (namespace, profile.value, Jsonb(plan.model_dump(mode="json"))),
        )
        row = _one(
            conn,
            "SELECT profile,fault_plan FROM sim_namespaces WHERE namespace=%s FOR SHARE",
            (namespace,),
        )
        if row["profile"] != profile.value or row["fault_plan"] != plan.model_dump(mode="json"):
            raise NamespaceConfigConflict(namespace)


def delete_namespace(conn: psycopg.Connection, namespace: str) -> None:
    conn.execute("DELETE FROM sim_namespaces WHERE namespace = %s", (namespace,))


def hold(conn: psycopg.Connection, namespace: str, name: str, timeout_s: float) -> bool:
    """Mark a barrier reached, then wait until it is released or the timeout passes.

    The connection is in autocommit mode, so the caller holds no transaction while
    waiting. A charge held after commit has its effect visible in the ledger while
    its response has not yet been sent.
    """
    conn.execute(
        "INSERT INTO barriers (namespace, name, reached_at) VALUES (%s, %s, now())"
        " ON CONFLICT (namespace, name) DO UPDATE SET reached_at = now()",
        (namespace, name),
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = _one(
            conn,
            "SELECT released_at FROM barriers WHERE namespace = %s AND name = %s",
            (namespace, name),
        )
        if row is not None and row["released_at"] is not None:
            return True
        time.sleep(0.01)
    return False


def release_barrier(conn: psycopg.Connection, namespace: str, name: str) -> None:
    conn.execute(
        "INSERT INTO barriers (namespace, name, released_at) VALUES (%s, %s, now())"
        " ON CONFLICT (namespace, name) DO UPDATE SET released_at = now()",
        (namespace, name),
    )


def barrier_state(conn: psycopg.Connection, namespace: str, name: str) -> dict[str, bool]:
    row = _one(
        conn,
        "SELECT reached_at, released_at FROM barriers WHERE namespace = %s AND name = %s",
        (namespace, name),
    )
    return {
        "reached": bool(row and row["reached_at"]),
        "released": bool(row and row["released_at"]),
    }


def _load(conn: psycopg.Connection, namespace: str) -> tuple[Profile, FaultPlan]:
    row = _one(
        conn, "SELECT profile, fault_plan FROM sim_namespaces WHERE namespace = %s", (namespace,)
    )
    if row is None:
        raise UnknownNamespace(namespace)
    return Profile(row["profile"]), FaultPlan.model_validate(row["fault_plan"])


def _claim_call(
    conn: psycopg.Connection,
    namespace: str,
    endpoint: str,
    counter: str,
    key: Any,
    request: dict[str, Any],
) -> tuple[int, int]:
    """Consume the next fault-schedule slot and record the attempt, in one statement.

    The counter increment and an in-flight call-log row are a single atomic statement,
    so a slot can never be consumed without a record of the attempt, whatever happens
    afterwards (an exception, or a worker killed inside a barrier). No lock is taken
    and no transaction stays open. Returns (call_index, call_log id); the row is
    finalised by _finish_call once the result is known.
    """
    query = sql.SQL(
        "WITH claimed AS ("
        " UPDATE sim_namespaces SET {c} = {c} + 1 WHERE namespace = %s RETURNING {c} AS n)"
        " INSERT INTO call_log (namespace, endpoint, call_index, idempotency_key, request,"
        " http_status, response, outcome, server_ms)"
        " SELECT %s::text, %s::text, n, %s::text, %s, 0, '{{}}'::jsonb, %s::text, 0"
        " FROM claimed RETURNING id, call_index"
    ).format(c=sql.Identifier(counter))
    row = _one(
        conn,
        query,
        (
            namespace,
            namespace,
            endpoint,
            _clean_str(key) if isinstance(key, str) else None,
            Jsonb(request, dumps=_dumps_untrusted),
            IN_FLIGHT,
        ),
    )
    if row is None:
        raise UnknownNamespace(namespace)
    return row["call_index"], row["id"]


def _finish_call(
    conn: psycopg.Connection, log_id: int, resp: SimResponse, outcome: str, started: float
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE call_log SET http_status = %s, response = %s, outcome = %s, server_ms = %s"
            " WHERE id = %s",
            (
                resp.http_status,
                Jsonb(resp.body, dumps=_dumps_untrusted),
                outcome,
                (time.perf_counter() - started) * 1000.0,
                log_id,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError("call log row vanished; was the namespace reset mid-call?")


def _abort_call(conn: psycopg.Connection, log_id: int, started: float) -> None:
    """Best effort: turn an in-flight row into a server_error record, never mask the cause."""
    resp = _error(500, "internal_error", SERVER_ERROR_MESSAGE)
    with contextlib.suppress(psycopg.Error, RuntimeError):
        _finish_call(conn, log_id, resp, SERVER_ERROR, started)


def _validate_charge(profile: Profile, payload: dict[str, Any]) -> _ValidCharge | SimResponse:
    result = validate_charge(profile, payload)
    if isinstance(result, ChargeRefusal):
        return SimResponse(result.http_status, result.body)
    return result


def _ambiguous_error(profile: Profile, committed: bool) -> SimResponse:
    """Controlled profile: one error shape for both worlds. Compat: the original pair."""
    if profile is Profile.COMPAT and not committed:
        return _error(500, "internal_error", NOT_PROCESSED_MESSAGE)
    return _error(504, "gateway_timeout", TIMEOUT_MESSAGE)


def _insert_or_get(
    conn: psycopg.Connection, namespace: str, valid: _ValidCharge, call_index: int
) -> tuple[dict[str, Any], bool]:
    """Commit the effect and claim the key in one statement, or return the existing row.

    Under READ COMMITTED a concurrent insert of the same key blocks until the first
    transaction finishes, then takes the DO NOTHING branch; the following SELECT runs
    with a fresh snapshot and sees the winner's row.
    """
    digest = args_hash(valid.order_id, valid.amount, valid.currency)
    with conn.transaction():
        row = _one(
            conn,
            "INSERT INTO charges (namespace, idempotency_key, order_id, amount, currency,"
            " args_hash, call_index) VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (namespace, idempotency_key) DO NOTHING RETURNING *",
            (
                namespace,
                valid.key,
                valid.order_id,
                valid.amount,
                valid.currency,
                digest,
                call_index,
            ),
        )
        if row is not None:
            return row, True
        row = _one(
            conn,
            "SELECT * FROM charges WHERE namespace = %s AND idempotency_key = %s",
            (namespace, valid.key),
        )
    if row is None:
        raise RuntimeError("idempotency record vanished; was the namespace reset mid-call?")
    return row, False


def _charge_result(
    profile: Profile,
    fault: ChargeFault | None,
    valid: _ValidCharge,
    row: dict[str, Any],
    inserted: bool,
) -> tuple[SimResponse, str]:
    if fault == "commit_then_error":
        outcome = "committed_then_error" if inserted else "existing_then_error"
        return _ambiguous_error(profile, committed=True), outcome
    if inserted:
        body = {"message": SUCCESS_MESSAGE, "replayed": False, "charge": _charge_json(row)}
        return SimResponse(200, body), "committed"
    same_args = row["args_hash"] == args_hash(valid.order_id, valid.amount, valid.currency)
    if profile is Profile.CONTROLLED and not same_args:
        return _error(409, "idempotency_conflict", CONFLICT_MESSAGE), "conflict"
    body = {"message": REPLAY_MESSAGE, "replayed": True, "charge": _charge_json(row)}
    return SimResponse(200, body), "replayed"


def _sanitize(value: Any) -> Any:
    """Make an untrusted payload fit in jsonb without losing what was sent.

    Python's json.loads accepts the literals NaN, Infinity and -Infinity, so an agent can
    send them over HTTP, but they are not valid JSON and Postgres refuses to store them;
    they are replaced with their names. Strings are made storable by _clean_str.
    """
    if isinstance(value, str):
        return _clean_str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, dict):
        return {_sanitize(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_sanitize(v) for v in value]
    return value


def _dumps_untrusted(value: Any) -> str:
    return json.dumps(_sanitize(value), allow_nan=False, default=str)


def _log(
    conn: psycopg.Connection,
    namespace: str,
    endpoint: str,
    call_index: int | None,
    key: Any,
    request: dict[str, Any],
    resp: SimResponse,
    outcome: str,
    started: float,
) -> None:
    conn.execute(
        "INSERT INTO call_log (namespace, endpoint, call_index, idempotency_key, request,"
        " http_status, response, outcome, server_ms)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            namespace,
            endpoint,
            call_index,
            _clean_str(key) if isinstance(key, str) else None,
            Jsonb(request, dumps=_dumps_untrusted),
            resp.http_status,
            Jsonb(resp.body, dumps=_dumps_untrusted),
            outcome,
            (time.perf_counter() - started) * 1000.0,
        ),
    )


def charge(
    conn: psycopg.Connection,
    namespace: str,
    payload: dict[str, Any],
    *,
    barrier_timeout_s: float = 30.0,
) -> SimResponse:
    _require_autocommit(conn)
    started = time.perf_counter()
    profile, plan = _load(conn, namespace)
    valid = _validate_charge(profile, payload)
    if isinstance(valid, SimResponse):
        # The payload is untrusted and this is the one path that stores it verbatim, so the
        # log write can fail on input the validation just rejected (a NUL character in a
        # string, say). The contract promises the 400, so a lost log row must never turn
        # it into a 500.
        try:
            _log(
                conn,
                namespace,
                "charge",
                None,
                payload.get("idempotency_key"),
                payload,
                valid,
                "invalid",
                started,
            )
        except (psycopg.Error, TypeError, ValueError):
            logger.error("could not log an invalid charge request in %s", namespace, exc_info=True)
        return valid
    call_index, log_id = _claim_call(conn, namespace, "charge", "charge_calls", valid.key, payload)
    try:
        fault = plan.charge_fault(call_index)
        if call_index in plan.hold_before_commit:
            hold(conn, namespace, f"before_commit:{call_index}", barrier_timeout_s)
        if fault == "error_no_commit":
            resp, outcome = _ambiguous_error(profile, committed=False), "error_no_commit"
        else:
            row, inserted = _insert_or_get(conn, namespace, valid, call_index)
            if call_index in plan.hold_after_commit:
                hold(conn, namespace, f"after_commit:{call_index}", barrier_timeout_s)
            resp, outcome = _charge_result(profile, fault, valid, row, inserted)
    except Exception:
        _abort_call(conn, log_id, started)
        raise
    _finish_call(conn, log_id, resp, outcome, started)
    return resp


def status(conn: psycopg.Connection, namespace: str, key: str) -> SimResponse:
    _require_autocommit(conn)
    started = time.perf_counter()
    _, plan = _load(conn, namespace)
    call_index, log_id = _claim_call(
        conn, namespace, "status", "status_calls", key, {"idempotency_key": key}
    )
    try:
        if call_index <= plan.status_unavailable_calls:
            resp, outcome = _error(503, "unavailable", STATUS_UNAVAILABLE_MESSAGE), "unavailable"
        else:
            # A key Postgres cannot store was never committed (charge rejects it), so it is
            # not found, and the lookup itself would fail on it.
            row = (
                _one(
                    conn,
                    "SELECT * FROM charges WHERE namespace = %s AND idempotency_key = %s",
                    (namespace, key),
                )
                if _storable(key)
                else None
            )
            if row is None:
                body = {"idempotency_key": key, "status": "NOT_FOUND", "charge": None}
                resp, outcome = SimResponse(200, body), "not_found"
            else:
                body = {"idempotency_key": key, "status": "SUCCEEDED", "charge": _charge_json(row)}
                resp, outcome = SimResponse(200, body), "succeeded"
    except Exception:
        _abort_call(conn, log_id, started)
        raise
    _finish_call(conn, log_id, resp, outcome, started)
    return resp
