-- call_log.outcome is the label the oracle and the crash analysis read, but 0001 left it an
-- unconstrained TEXT, so a misspelt or new outcome could be written and reported without
-- anything objecting. profile and endpoint already have CHECKs; this gives outcome one.
--
-- The list is every outcome the simulator can write, taken from service.py:
--
--   claim (_claim_call)       in_flight
--   abort (_abort_call)       server_error
--   invalid request (charge)  invalid
--   charge result             committed, replayed, conflict,
--                             committed_then_error, existing_then_error, error_no_commit
--   status result             succeeded, not_found, unavailable
--
-- A new outcome now needs a migration that extends this list, in the same change as the
-- code that writes it. A write outside the list fails with a check violation, which for a
-- claim is raised before the slot is consumed (the claim is a single statement).
--
-- Existing rows are validated when the constraint is added, so this migration fails, and
-- leaves the schema untouched, if a database already holds a row with another value.
ALTER TABLE call_log ADD CONSTRAINT call_log_outcome_check CHECK (outcome IN (
    'in_flight',
    'server_error',
    'invalid',
    'committed',
    'replayed',
    'conflict',
    'committed_then_error',
    'existing_then_error',
    'error_no_commit',
    'succeeded',
    'not_found',
    'unavailable'
));
