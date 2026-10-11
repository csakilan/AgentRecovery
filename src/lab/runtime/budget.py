"""Hard ceilings on paid resources.

Two independent guards. The allowlist refuses any model whose ID does not end
in ":free", because D1 permits no paid usage at all. The counter lives in the
database rather than in a process, so running a second runner cannot defeat it.

The day boundary
----------------
The counter is keyed on the UTC calendar date, so the allowance resets at
00:00:00 UTC and at no other instant. The date is read from the runner's own
clock on every call to `claim` and `used_today`, in UTC whatever the machine's
local zone, and not from the database's clock. Runners that share a budget
should therefore keep their clocks in sync, or one that runs a few minutes
fast will count its first claims of a day against the day before.

A run that is in flight across midnight is not interrupted and not reconciled.
Claims made before midnight stay on the old day: they are not carried into the
new one and not refunded. The first claim after midnight starts a new row with
a full allowance, so a run that was being refused at the limit simply continues,
and one that straddles midnight can spend up to twice the daily limit in the
time around it.

This is an assumption about the provider, not something verified. D11 records
the provider's daily ceiling but not the instant at which its window resets. If
that window is rolling, or is anchored to another zone, then two consecutive
UTC days of the full limit can fall inside one provider day, and the true
ceiling would be exceeded even though this counter never was. Until the reset
instant is confirmed, `daily_limit` is a guarantee about UTC days only.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import psycopg

FREE_SUFFIX = ":free"

# What may precede the suffix: an ID that starts with a letter or digit and continues with
# letters, digits and . _ / - only. That excludes whitespace, control and zero-width
# characters, commas (a list of IDs) and further colons (":free:free") in one rule, and the
# free IDs this project uses all fit it. A provider would reject most of what this refuses
# without charging, so the point is to refuse it here, before it is sent, not to save money.
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


class BudgetExceeded(Exception):
    """The daily request ceiling would be exceeded. Nothing was consumed."""


class ModelNotAllowed(Exception):
    """The model is not a free variant, which D1 forbids."""


def assert_model_allowed(model_id: str) -> None:
    if not model_id.endswith(FREE_SUFFIX):
        raise ModelNotAllowed(f"{model_id!r} is not a :free model")
    name = model_id[: -len(FREE_SUFFIX)]
    if not name:
        raise ModelNotAllowed(f"{model_id!r} has no model name before {FREE_SUFFIX}")
    # fullmatch, not `$`: `$` would let a name ending in a newline through.
    if _MODEL_NAME.fullmatch(name) is None:
        raise ModelNotAllowed(f"{model_id!r} is not a well-formed model ID")


class RequestBudget:
    """A shared daily counter.

    `claim` is all or nothing: a claim that would exceed the limit consumes
    none of it, so a refused claim leaves the budget exactly as it was.
    """

    def __init__(self, conn: psycopg.Connection, daily_limit: int = 900) -> None:
        if not conn.autocommit:
            raise ValueError("budget connections must use autocommit=True")
        self._conn = conn
        self._limit = daily_limit

    @staticmethod
    def _today() -> str:
        # The UTC date, from this process's clock. See "The day boundary" in the module docstring.
        return datetime.now(UTC).date().isoformat()

    def used_today(self) -> int:
        row = self._conn.execute(
            "SELECT requests_made FROM request_budget WHERE day = %s", (self._today(),)
        ).fetchone()
        return 0 if row is None else int(row[0])

    def claim(self, n: int = 1) -> int:
        # A bool is an int in Python, and a float or Decimal would be rounded by the database
        # on its way into the INTEGER column, silently consuming more than was asked for.
        if isinstance(n, bool) or not isinstance(n, int):
            raise ValueError(f"n must be an integer, not {type(n).__name__}")
        if n < 1:
            raise ValueError("n must be at least 1")
        # One statement, two guards. A first claim of the day takes the INSERT path, and the
        # DO UPDATE ... WHERE below never runs there, so the INSERT itself is made
        # conditional: it selects from the claim only `WHERE n <= limit`, so an oversized
        # claim yields no row to insert and nothing to conflict. When a row exists, the claim
        # yields one candidate row, the conflict fires, and the update's own WHERE checks the
        # running total. Either way a refusal returns no row and writes nothing.
        #
        # The claim sits in a MATERIALIZED CTE as an unbounded numeric. Without that, Postgres
        # folds the numeric-to-INTEGER cast for the column at plan time, so a claim beyond the
        # 32-bit range raises NumericValueOutOfRange before the WHERE can refuse it.
        row = self._conn.execute(
            "WITH claim AS MATERIALIZED (SELECT %s::date AS day, %s::numeric AS n)"
            " INSERT INTO request_budget (day, requests_made)"
            " SELECT day, n FROM claim WHERE n <= %s"
            " ON CONFLICT (day) DO UPDATE SET requests_made ="
            "   request_budget.requests_made + EXCLUDED.requests_made,"
            "   updated_at = now()"
            " WHERE request_budget.requests_made + EXCLUDED.requests_made <= %s"
            " RETURNING requests_made",
            (self._today(), n, self._limit, self._limit),
        ).fetchone()
        if row is None:
            raise BudgetExceeded(f"claiming {n} would exceed the daily limit of {self._limit}")
        return int(row[0])
