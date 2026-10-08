"""Shared request bodies and ledger helpers for simulator and oracle tests."""

from psycopg.rows import dict_row

CHARGE = {"amount": 49.99, "idempotency_key": "k1", "order_id": "1234", "currency": "USD"}
COMPAT_CHARGE = {"amount": 49.99, "idempotency_key": "k1"}


def charges(conn, namespace):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT * FROM charges WHERE namespace = %s ORDER BY id", (namespace,)
        ).fetchall()
