-- The runtime database holds the agent's own records, and the evaluator must not be able to
-- reach them. Until now that rested on table grants: PUBLIC keeps CONNECT on a new database,
-- so payment_svc and ledger_reader could open a session here and were stopped only by the
-- privileges on each table. Revoking CONNECT from PUBLIC makes the boundary hold before any
-- table is reached, and a later grant on a table cannot undo it.
--
-- Who may connect, and why:
--
--   runtime_svc    the runner and the agent loop, which write every table here.
--
-- payment_svc and ledger_reader are not named, so they are refused. The migration runner
-- and the test fixtures connect as the superuser that owns this database, who does not
-- need a grant: revoking from PUBLIC leaves the owner's own entry in the ACL, and a
-- superuser bypasses it anyway. Nothing here can lock that role out. runtime_svc exists
-- by the time this runs, because 0001 already grants to it.
--
-- REVOKE ... ON DATABASE takes the database name as an identifier, and a test run applies
-- these migrations to a throwaway name, so it runs dynamically against current_database(),
-- which inside a migration is the database it is being applied to. Only CONNECT is
-- revoked; PUBLIC's TEMP privilege is left as it was. Open sessions are not affected, only
-- new connections. Both statements are idempotent.
DO $$
BEGIN
    EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO runtime_svc', current_database());
END
$$;
