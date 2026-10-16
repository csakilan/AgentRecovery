-- Until now the separation between this database and the runtime database rested on table
-- grants. Every role could still open a connection to either database, because Postgres
-- gives PUBLIC the CONNECT privilege on a new database, and was then stopped only by the
-- privileges on the tables inside. A grant is something one careless statement can loosen.
-- Taking CONNECT away makes the boundary hold before any table is reached: a role the
-- ledger does not name cannot open a session here at all.
--
-- Who may connect, and why:
--
--   payment_svc    the agent-facing service and the control API.
--   ledger_reader  the evaluator, which reads the ledger to decide a verdict.
--
-- runtime_svc is not named, so it is refused. The migration runner and the test fixtures
-- connect as the superuser that owns this database, who does not need a grant: revoking
-- from PUBLIC leaves the owner's own entry in the ACL, and a superuser bypasses it anyway.
-- Nothing here can lock that role out.
--
-- REVOKE ... ON DATABASE takes the database name as an identifier and cannot be written as
-- a plain statement in a migration that has to run against any database name (a test run
-- uses a throwaway name), so it runs dynamically against current_database(), which inside
-- a migration is the database it is being applied to. Only CONNECT is revoked; PUBLIC's
-- TEMP privilege is left as it was. Open sessions are not affected, only new connections.
-- Both statements are idempotent.
DO $$
BEGIN
    EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO payment_svc, ledger_reader', current_database()
    );
END
$$;
