-- payment_svc is the role the agent-facing service connects as, so whatever it may write is
-- reachable by anything that compromises that service. 0001 gave it every verb on every
-- table, which let that process rewrite the evidence the scorer reads. Narrow it to what
-- the code actually does, so scorer independence rests on Postgres refusing and not on
-- service.py behaving.
--
--   charges      INSERT + SELECT. A committed effect is never updated or deleted.
--   call_log     INSERT + SELECT, and UPDATE of the four result columns only. _claim_call
--                inserts an in_flight row and _finish_call / _abort_call fill in the result,
--                so the row's identity (namespace, endpoint, call_index, key, request) is
--                fixed once written.
--   barriers     INSERT + SELECT, and UPDATE of the two timestamps (the upsert in hold and
--                release_barrier).
--   sim_namespaces  INSERT + SELECT, UPDATE of the two call counters only (_claim_call), and
--                DELETE, which is the one delete kept: configure_namespace and
--                delete_namespace reset or remove a namespace this way. The profile and
--                fault plan are never updated in place.
--
-- No DELETE is granted on charges, call_log or barriers. Their rows still go when a
-- namespace is deleted, through ON DELETE CASCADE: Postgres runs the cascade with the
-- privileges of the table owner, not of the role deleting the parent row. That means the
-- namespace DELETE can still remove a namespace's evidence. It cannot be dropped, because
-- the control API needs it, and it is a limit of this migration: closing it needs the
-- control plane to connect as a different role from the agent-facing service.
REVOKE ALL ON sim_namespaces, charges, call_log, barriers FROM payment_svc;

GRANT SELECT, INSERT, DELETE ON sim_namespaces TO payment_svc;
GRANT UPDATE (charge_calls, status_calls) ON sim_namespaces TO payment_svc;

GRANT SELECT, INSERT ON charges TO payment_svc;

GRANT SELECT, INSERT ON call_log TO payment_svc;
GRANT UPDATE (http_status, response, outcome, server_ms) ON call_log TO payment_svc;

GRANT SELECT, INSERT ON barriers TO payment_svc;
GRANT UPDATE (reached_at, released_at) ON barriers TO payment_svc;
