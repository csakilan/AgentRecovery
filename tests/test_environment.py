import os

import psycopg

ADMIN_URL = os.environ.get(
    "LAB_PG_ADMIN_URL", "postgresql://postgres:postgres@localhost:54329/postgres"
)


def test_postgres_16_is_reachable():
    with psycopg.connect(ADMIN_URL, connect_timeout=3) as conn:
        version = conn.execute("SHOW server_version_num").fetchone()[0]
    assert int(version) >= 160000
