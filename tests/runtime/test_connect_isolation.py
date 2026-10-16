"""Who may open a session on which database.

The payments database and the runtime database are separate so that the evaluator, which
reads the ledger, cannot reach the agent's own records, and the agent side cannot reach the
ledger. Table grants already enforce that. These tests pin the layer underneath: CONNECT is
not granted to PUBLIC, so a role the database does not name is refused before any table is
looked at. A failed login for a wrong password would also raise OperationalError, so every
refusal here is checked to be a privilege refusal, not an authentication one.
"""

import psycopg
import pytest

# (role, database, may connect). Each database admits exactly the roles that use it.
CONNECT_MATRIX = [
    ("payment_svc", "payments", True),
    ("ledger_reader", "payments", True),
    ("runtime_svc", "payments", False),
    ("payment_svc", "runtime", False),
    ("ledger_reader", "runtime", False),
    ("runtime_svc", "runtime", True),
]

# A table each permitted role really uses in its own database, so "can connect" is shown to
# mean a working session and not merely an open socket.
OWN_TABLE = {
    "payment_svc": "charges",
    "ledger_reader": "charges",
    "runtime_svc": "experiments",
}


@pytest.mark.parametrize(("role", "database", "allowed"), CONNECT_MATRIX)
def test_each_role_connects_only_to_the_database_it_uses(
    role, database, allowed, payments_db, runtime_db, role_url
):
    dbname = {"payments": payments_db, "runtime": runtime_db}[database]["name"]
    if allowed:
        with psycopg.connect(role_url(role, dbname), autocommit=True) as conn:
            assert conn.execute(f"SELECT count(*) FROM {OWN_TABLE[role]}").fetchone()[0] >= 0
    else:
        with pytest.raises(psycopg.OperationalError) as refused:
            psycopg.connect(role_url(role, dbname), autocommit=True)
        assert "permission denied for database" in str(refused.value)
        assert "does not have CONNECT privilege" in str(refused.value)


@pytest.mark.parametrize("database", ["payments", "runtime"])
def test_the_admin_role_still_connects_to_both(database, payments_db, runtime_db):
    """Revoking CONNECT from PUBLIC must not lock out the role that runs migrations and the
    test fixtures."""
    url = {"payments": payments_db, "runtime": runtime_db}[database]["admin"]
    with psycopg.connect(url, autocommit=True) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize(
    ("database", "expected"),
    [("payments", {"payment_svc", "ledger_reader"}), ("runtime", {"runtime_svc"})],
)
def test_connect_is_granted_to_named_roles_and_not_to_public(
    database, expected, payments_db, runtime_db
):
    """The catalog view of the same fact: no PUBLIC entry carries CONNECT, and the only
    grantees besides the database's owner are the roles in the matrix."""
    fixture = {"payments": payments_db, "runtime": runtime_db}[database]
    with psycopg.connect(fixture["admin"], autocommit=True) as conn:
        grantees = {
            row[0]
            for row in conn.execute(
                "SELECT coalesce(r.rolname, 'PUBLIC')"
                " FROM pg_database d"
                " CROSS JOIN LATERAL aclexplode(d.datacl) a"
                " LEFT JOIN pg_roles r ON r.oid = a.grantee"
                " WHERE d.datname = %s AND a.privilege_type = 'CONNECT'"
                " AND a.grantee <> d.datdba",
                (fixture["name"],),
            )
        }
    assert grantees == expected
