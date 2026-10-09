"""Run the simulator.

python -m lab.simulator migrate    create roles and database, apply migrations
python -m lab.simulator public     serve the public API (default 0.0.0.0:8100)
python -m lab.simulator internal   serve the control API (default 127.0.0.1:8101)

HOST and PORT override the bind address. The control API defaults to loopback so that a
missing network protection fails closed; set HOST=0.0.0.0 where the platform needs it.
"""

from __future__ import annotations

import os
import sys

import uvicorn
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from lab import db
from lab.simulator import SIM_MIGRATIONS
from lab.simulator.app import create_internal_app, create_public_app


def _pool(url: str) -> ConnectionPool:
    return ConnectionPool(
        url,
        min_size=1,
        max_size=20,
        open=True,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )


def _migrate() -> None:
    admin = os.environ["LAB_PG_ADMIN_URL"]
    dbname = os.environ.get("LAB_PAYMENTS_DB", "payments")
    db.ensure_role(admin, "payment_svc", os.environ["LAB_PAYMENT_SVC_PASSWORD"])
    db.ensure_role(admin, "ledger_reader", os.environ["LAB_LEDGER_READER_PASSWORD"])
    db.ensure_database(admin, dbname)
    applied = db.apply_migrations(db.with_database(admin, dbname), SIM_MIGRATIONS)
    print(f"applied: {applied or 'nothing new'}")


def main(argv: list[str]) -> None:
    command = argv[1] if len(argv) > 1 else "public"
    if command == "migrate":
        _migrate()
        return
    url = os.environ["LAB_PAYMENTS_URL"]
    if command == "public":
        app = create_public_app(_pool(url), os.environ["SIM_CLIENT_TOKEN"])
        default_host, default_port = "0.0.0.0", "8100"
    elif command == "internal":
        app = create_internal_app(_pool(url), os.environ["SIM_CONTROL_TOKEN"])
        default_host, default_port = "127.0.0.1", "8101"
    else:
        raise SystemExit(f"unknown command: {command}")
    host = os.environ.get("HOST", default_host)
    uvicorn.run(app, host=host, port=int(os.environ.get("PORT", default_port)))


if __name__ == "__main__":
    main(sys.argv)
