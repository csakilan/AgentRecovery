# Resume here

**Paused:** 2026-10-04, at the user's request, part-way through Plan 1.

Read this with `docs/superpowers/plans/2026-10-04-roadmap.md` (the eight-stage
plan), `docs/superpowers/plans/2026-10-04-plan-1-oracle-and-simulator.md` (the
detailed stage 1 plan, 11 tasks) and `docs/decisions.md` (D1 to D7, all accepted).

The authoritative task-by-task record is the SDD ledger at
`.superpowers/sdd/2026-10-04-plan-1-oracle-and-simulator/progress.md`. It is
git-ignored, so it lives only on this machine. Trust it and `git log` over
anyone's recollection.

## Where the work stands

Branch `plan-1-oracle-and-simulator`, four commits ahead of `main`. `main` holds
planning documents only.

| Task | State |
|---|---|
| 1. Scaffold, compose Postgres, environment test | Complete, review clean (`9e2c621`) |
| 2. Task constants and canonical charge arguments | Complete, review clean (`c3000f9`) |
| 3. Hidden fault plans and observation text | Complete, review clean (`9904380`) |
| 4. Episode manifests and experiment matrices | Code committed (`dfca97e`); spec passed, **quality not approved**, fix loop not started |
| 5. Oracle scoring | Not started. Needs no database, so it can run while Docker is down |
| 6 to 11 | Not started. All need Postgres |

36 unit tests pass. `tests/test_environment.py` fails for the environment reason
below, not for a code reason.

Pushed through `9904380`. Commit `dfca97e` is local only, deliberately, because
its review has not settled.

## Blocker one: Docker

Docker Desktop's container runtime is wedged on this machine. The Postgres
container reports healthy because its healthcheck runs inside Docker's VM, while
the path from the host into that VM is broken. Confirmed: a TCP connection to
54329 opens and is then closed with an empty reply; `docker exec` and
`docker restart` both hang indefinitely; `docker version` answers in 0.09s.

Restarting only the project container does not work, because that command is one
of the ones that hangs. The fix is restarting Docker Desktop, which also bounces
the user's unrelated `kind-registry` container.

**The user chose to restart Docker Desktop themselves.** Do not restart it
without asking again. After they confirm it is restarted:

```bash
docker compose up -d --wait postgres
uv run pytest tests/test_environment.py -v
```

Expect the test to pass. Then Task 6 onwards is unblocked.

## Blocker two: an open decision on test depth

The Task 4 reviewer found that the tests the plan itself specifies are too weak
to detect a silent change in the experiment's own definition. All four findings
are in `tests/unit/test_manifest.py`:

1. The default seeds `20261004` and `20261005` are never asserted. Changing
   either leaves the suite green, and the seed determines which episodes run in
   which order.
2. The rule that a non-neutral prompt adds a suffix to the episode ID is never
   asserted. Dropping the suffix leaves the suite green.
3. The `expect_effect` and `eligible_recoverable` values for `TRUE_FAIL` and
   `HEALTHY` are never asserted. Removing `TRUE_FAIL` from `_RECOVERABLE` at
   `src/lab/protocol/manifest.py:35` leaves the suite green. These two flags
   drive the scoring denominators, so a drift here would corrupt every result.
4. Neither `Manifest` nor `EpisodeSpec` is tested for being frozen with
   `extra="forbid"`.

This is a genuine conflict rather than an implementer error. The plan's brief
specified these exact tests and also forbids writing anything the brief did not
ask for, so adding assertions exceeds the brief. The reviewer separately
confirmed the implementation itself is correct on all four points.

The three options put to the user were: strengthen all four now; keep the plan
verbatim and defer all four to the final whole-branch review; or strengthen only
the seeds and flags, which are the two that would corrupt results. **The user
paused instead of choosing.** This decision is still open.

Whatever is chosen, the same weakness probably recurs in later tasks, because the
plan's tests were written to specify behaviour rather than to resist tampering. A
general version of the question: should each task's tests pin the project's
constants, or is pinning them a final-review concern?

## Smaller items already logged

The ledger carries about fifteen deferred minor findings from tasks 1 to 4.
One is worth raising at the final review on its own merits: no test pins a
literal expected value for `args_hash`, even though that hash is the
cross-process identity of a payment, so a change to its formatting would go
unnoticed.

The ledger also records that the Task 4 implementer's report overstated its
evidence: it described the failing Postgres test as expected when nobody had said
so, reported inaccurate line counts, and self-reviewed the seed defaults as
correct although no test covers them. The code was correct. Reports from
implementers should be checked against the diff rather than trusted.

## The immediate next action

1. Resolve the test-depth decision with the user.
2. Either start Task 4's fix loop or mark Task 4 complete with the findings
   parked, according to that decision.
3. Run Task 5, which needs no database.
4. Wait for the user to restart Docker, then continue from Task 6.
