"""Set up the runtime database.

python -m lab.runtime migrate    create the role and database, apply migrations

Mirrors `python -m lab.simulator migrate`: LAB_PG_ADMIN_URL is the superuser connection,
LAB_RUNTIME_DB names the database (default "runtime", as LAB_PAYMENTS_DB defaults to
"payments") and LAB_RUNTIME_SVC_PASSWORD is the password given to the runtime_svc role. The
database is separate from the payments database on purpose, so the evaluator that reads the
ledger cannot reach the agent's records.
"""

from __future__ import annotations

import os
import sys

from lab import db
from lab.runtime import RUNTIME_MIGRATIONS


def _migrate() -> None:
    admin = os.environ["LAB_PG_ADMIN_URL"]
    dbname = os.environ.get("LAB_RUNTIME_DB", "runtime")
    db.ensure_role(admin, "runtime_svc", os.environ["LAB_RUNTIME_SVC_PASSWORD"])
    db.ensure_database(admin, dbname)
    applied = db.apply_migrations(db.with_database(admin, dbname), RUNTIME_MIGRATIONS)
    print(f"applied: {applied or 'nothing new'}")


def main(argv: list[str]) -> None:
    command = argv[1] if len(argv) > 1 else ""
    if command == "migrate":
        _migrate()
        return
    raise SystemExit(f"usage: python -m lab.runtime migrate (got: {command or 'nothing'})")


if __name__ == "__main__":
    main(sys.argv)
