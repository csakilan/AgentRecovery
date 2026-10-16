-- A run whose episode finished but whose verdict could not be written. Until now the runner
-- had two choices for that, both wrong: leave the run 'completed' with no results row (which
-- looks like a run nobody got round to scoring), or call it 'crashed' (which says the agent
-- did not finish, when it did, and its claim and finish time are on the row). 'score_failed'
-- is the third thing: the agent side is complete and trustworthy, the evaluator side is
-- missing, and `lab.runner.episode.rescore_run` can finish the job from the ledger.
--
-- The column constraint made by 0001 is named runs_state_check by Postgres. Dropping and
-- re-adding it is the only way to widen a CHECK, and nothing else depends on it.
ALTER TABLE runs DROP CONSTRAINT runs_state_check;
ALTER TABLE runs ADD CONSTRAINT runs_state_check CHECK (state IN
    ('queued', 'running', 'completed', 'crashed', 'abandoned', 'score_failed'));
