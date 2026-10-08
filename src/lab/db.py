"""Small Postgres helpers: roles, databases and forward-only SQL migrations."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql


def with_database(
    url: str, dbname: str, user: str | None = None, password: str | None = None
) -> str:
    parts = urlsplit(url)
    netloc = parts.netloc
    if user is not None:
        host = netloc.rsplit("@", 1)[-1]
        netloc = f"{user}:{password}@{host}" if password else f"{user}@{host}"
    return urlunsplit((parts.scheme, netloc, f"/{dbname}", parts.query, parts.fragment))


def ensure_role(admin_url: str, role: str, password: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(
            sql.SQL(verb + " ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )


def ensure_database(admin_url: str, dbname: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        found = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if not found:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))


def drop_database(admin_url: str, dbname: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
        )


def apply_migrations(url: str, migrations_dir: Path) -> list[str]:
    """Apply *.sql files in name order, each once, each in its own transaction."""
    applied: list[str] = []
    with psycopg.connect(url) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        conn.commit()
        done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            conn.commit()
            applied.append(path.name)
    return applied
