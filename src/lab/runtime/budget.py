"""Hard ceilings on paid resources.

Two independent guards. The allowlist refuses any model whose ID does not end
in ":free", because D1 permits no paid usage at all. The counter lives in the
database rather than in a process, so running a second runner cannot defeat it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg

FREE_SUFFIX = ":free"


class BudgetExceeded(Exception):
    """The daily request ceiling would be exceeded. Nothing was consumed."""


class ModelNotAllowed(Exception):
    """The model is not a free variant, which D1 forbids."""


def assert_model_allowed(model_id: str) -> None:
    if not model_id.endswith(FREE_SUFFIX):
        raise ModelNotAllowed(f"{model_id!r} is not a :free model")
    if len(model_id) <= len(FREE_SUFFIX):
        raise ModelNotAllowed(f"{model_id!r} has no model name before {FREE_SUFFIX}")


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
        return datetime.now(UTC).date().isoformat()

    def used_today(self) -> int:
        row = self._conn.execute(
            "SELECT requests_made FROM request_budget WHERE day = %s", (self._today(),)
        ).fetchone()
        return 0 if row is None else int(row[0])

    def claim(self, n: int = 1) -> int:
        if n < 1:
            raise ValueError("n must be at least 1")
        row = self._conn.execute(
            "INSERT INTO request_budget (day, requests_made) VALUES (%s, %s)"
            " ON CONFLICT (day) DO UPDATE SET requests_made ="
            "   request_budget.requests_made + EXCLUDED.requests_made,"
            "   updated_at = now()"
            " WHERE request_budget.requests_made + EXCLUDED.requests_made <= %s"
            " RETURNING requests_made",
            (self._today(), n, self._limit),
        ).fetchone()
        if row is None:
            raise BudgetExceeded(f"claiming {n} would exceed the daily limit of {self._limit}")
        return int(row[0])
