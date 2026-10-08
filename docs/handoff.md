# Handoff

**Updated:** 2026-10-08. **For:** the next assistant or developer to pick this up,
with no access to the conversation that produced it. Everything needed is in this
repository.

Start here, then read in this order:

1. `docs/decisions.md`: D1 to D8, all accepted by the user. These constrain
   everything. Do not reverse one without asking.
2. `docs/superpowers/plans/2026-10-04-roadmap.md` — the eight-stage plan, the
   architecture, the contracts, the operation state machine, the evaluation
   design, the budget and the open questions.
3. `docs/superpowers/plans/2026-10-04-plan-1-oracle-and-simulator.md` — the
   detailed stage 1 plan, 11 tasks, each with its exact code and tests.
4. `docs/protocol.md` — does not exist yet; Task 11 creates it.
5. `docs/pitch.md` — the sixty-second spoken explanation of the project.

## What this project is

A laboratory that measures how much reliability, for one external write made by a
LangGraph agent under ambiguous failures and real process crashes, comes from the
model's own decisions, from a conventional stable-key retry, and from durable
recovery infrastructure, and what each costs.

The one task under study: charge a synthetic customer $49.99 for order 1234,
exactly once. The simulator hides which of two worlds it is in. In `lost_ack` the
charge commits and the caller sees an error. In `true_fail` nothing commits and
the caller sees the same error. A separate ledger, which no agent-side process can
read, records what actually happened and is the only scorer.

## Hard constraints

- **No money.** The user spends nothing beyond a one-time ~$10 OpenRouter credit
  purchase, which exists to unlock the higher free-model request allowance (20 per
  minute, 1,000 per day at or above $10 of purchased credit). Only model IDs
  ending in `:free` may be used. Hosting must stay inside ~$300 of GCP credits.
  See D1.
- **No public deployment.** Cloud Run services require IAM authentication; there
  is no anonymous access and no public URL. See D3.
- **Never fabricate a result.** No measured number exists yet. Nothing in this
  repository may claim one. The resume bullet templates in the roadmap carry
  placeholders deliberately.
- **Never call the controlled profile a replication** of RetryLedger. It is a
  modification. The compatibility profile is the replication.

## Environment facts, verified 2026-10-04 on this machine

Python 3.12.8, uv 0.12.22, Docker 24.0.2, Docker Compose 2.18.1, git 2.50.1,
Node 22.20.0. There is **no local `psql`**; use Python or `docker compose exec`.

```bash
uv sync --frozen                                # install locked dependencies
docker compose up -d --wait postgres            # Postgres 16 on host port 54329
uv run pytest -q                                # whole suite
uv run pytest tests/unit -q                     # no database needed
uv run ruff format src tests                    # format Python files only
uv run ruff check .                             # lint
uv run ruff format --check src tests            # verify Python formatting
```

The locked formatter also checks Python examples inside Markdown. A whole-repo
format check currently flags the two planning documents. Those examples have
not been reformatted; the plans are preserved. Address that check's scope before
adding CI in Task 11.

Dev-only credentials live in `.env.example` and `compose.yaml`:
`dev-client-token`, `dev-control-token`, `dev-payment-svc`, `dev-ledger-reader`.
They are fake. Never commit a real secret, and never create a `.env` file.

## Repository state

Branch `plan-1-oracle-and-simulator`. `main` holds planning documents only.
Repository: `https://github.com/csakilan/AgentRecovery`.

The user authorized committing and pushing the Task 4 coverage fixes, Task 5's
oracle implementation and tests, and updates to this file and `decisions.md`.
The code commits are listed below. Work remains stopped after Task 5.
The pre-existing untracked `.DS_Store` was left alone.

| Commit | Content |
|---|---|
| `74ec6d3` | Planning documents (on `main`) |
| `9e2c621` | Task 1: scaffold, ruff and pytest config, compose Postgres, environment test |
| `c3000f9` | Task 2: `src/lab/protocol/task.py` — task constants, amount parsing, `args_hash` |
| `9904380` | Task 3: `faults.py` (hidden worlds) and `render.py` (exact model-visible text) |
| `dfca97e` | Task 4: `manifest.py` — episode specs and the two experiment matrices |
| `0292763` | Docs: paused state |
| `f80d0d6` | Docs: consolidate the handoff |
| `b39c4cb` | Task 4: pin manifest seeds, IDs, scoring flags and schema restrictions |
| `01805eb` | Task 5: legacy and extended oracle scoring, with regression tests |
| `d0bb878` | Docs: Task 5 checkpoint and publishing approval |
| `f35375c` | Task 5 fix wave: close six review findings (see below) |
| `2d37906` | Docs: Task 5 review, fix wave and Docker resolution |
| `9be08ad` | Task 6: `db.py`, simulator schema and roles, pytest fixtures |
| `2fb1e36` | Task 6 fix: assert the read-only boundary on every ledger table |

Verification on 2026-10-08, after starting Docker:

- Full suite: **109 passed, 0 failed**, including
  `tests/test_environment.py::test_postgres_16_is_reachable`. The suite went
  green for the first time on this date, at 90 passed, before Task 6 added 19.
- Python lint and formatting checks pass.
- The Oct 5 batch (`b39c4cb` + `01805eb`) received its first independent review
  on 2026-10-08: spec compliance passed, quality approved, no Critical issues and
  no vacuous tests. The reviewer verified all six load-bearing scoring semantics
  by execution and confirmed all eight manifest mutations are caught, so D8's
  four findings are genuinely closed.
- That review raised six Important findings, all closed by `f35375c` and
  confirmed ADDRESSED by a scoped re-review that reproduced each break itself in
  a scratch copy. Details under "Task 5 review" below.

Superseded earlier figures, kept so the record is legible: on 2026-10-05 the
suite was 72 passed with 1 failure, the failure being the database reachability
test while Docker was down.

| Task | State |
|---|---|
| 1. Scaffold and environment test | Complete, review clean |
| 2. Task constants and canonical arguments | Complete, review clean |
| 3. Hidden fault plans and observation text | Complete, review clean |
| 4. Episode manifests and matrices | Complete; all four coverage findings addressed and reviewed |
| 5. Oracle scoring | Complete; reviewed, six findings fixed, re-review accepted |
| 6. Database helpers, simulator schema, fixtures | Complete; reviewed, one finding fixed, re-review accepted |
| 7. Simulator charge and status logic | **Not started. Next task** |
| 8. Concurrency guarantee and commit barriers | Not started |
| 9. Public and internal HTTP apps | Not started |
| 10. Ledger reader and RetryLedger parity | Not started. See the `Decimal` caveat below |
| 11. Container, compose, CI, protocol document | Not started |

Plans 2 through 8 of the roadmap have not been written in detail yet. Write each
one only after the previous plan lands, and verify the relevant library APIs on
the day rather than trusting a dated snapshot.

## Current stop: Task 6 complete and reviewed

Tasks 1 to 6 are done, each through both gates. Task 7, the simulator's charge
and status logic, is next and nothing blocks it. Postgres is running.

Task 5 adds frozen ledger record types and pure scoring functions. It does not
read Postgres yet; the read-only ledger reader belongs to Task 10. Its production
code follows the Task 5 plan, with no changes to scoring semantics.

Scoring conventions for future reviews:

- The legacy score counts effects only, so one wrong-amount charge still has
  `legacy.correct == 1`; the extended verdict reports `WRONG_EFFECT`.
- `correct_recovery` means a correct verdict completed before the deadline.
  `eligible_recoverable` is separate: aggregate recovery rates must filter on
  that flag. A healthy or expected-no-effect episode can have a correct verdict
  while remaining outside the recovery denominator.
- An explicit `unresolved` claim is a truthful uncertainty report under the
  planned definition. No effect when none is expected can score
  `CORRECT_NO_EFFECT` with a `failed` or `unresolved` claim. Neither counts in the
  recoverable denominator for a permanent-failure episode.

The accepted matrices are 48 behavior episodes plus 144 infrastructure episodes,
192 total and at most 1152 model requests at six turns each, excluding the pilot
and rerun reserve. D1's older arithmetic has been reconciled with D4.

The next implementation task is Task 6. The roadmap and detailed plan still
contain their historical planning-only status labels; this file records current
progress.

## The Docker blocker: resolved on 2026-10-08

Kept as a record, because the diagnosis changed and the change is instructive.

On 2026-10-04 Docker Desktop's container runtime was genuinely wedged. A TCP
connection to port 54329 opened instantly on both IPv4 and IPv6 and was then
closed with an empty reply, so the proxy accepted and dropped it. `docker exec`,
`docker restart` and `docker compose ps` all hung indefinitely while
`docker version` answered in 0.09s. The container reported healthy throughout,
because its healthcheck runs inside Docker's VM and so cannot see a broken path
from the host. Restarting only the project's container was impossible, that being
one of the hanging commands.

By 2026-10-08 the symptom had changed from "connection closed unexpectedly" to
"connection refused", which is a different fault: the daemon was simply not
running, the wedge having cleared in between. `kind-registry` was also down, so
the collateral-damage concern that had made the user own the restart no longer
applied, and they approved starting Docker.

A plain start was enough. `docker compose up -d --wait postgres` completed in
seconds and reported Healthy, and `kind-registry` came back on its own. No
jammed-VM recovery was needed.

Lesson for a successor: distinguish refused from closed. Refused means nothing is
listening. Closed after connecting means something accepted and dropped it, which
on Docker for Mac points at the port-forwarding path rather than at the database.
A container's own healthcheck cannot tell you the difference.

To bring the database up from cold:

```bash
docker compose up -d --wait postgres
uv run pytest -q          # expect 90 passed
```

## Task 4 review findings: addressed

The Task 4 review found that the tests the plan itself specifies do not pin the
experiment's own definition. All four findings are in
`tests/unit/test_manifest.py`:

1. The default seeds `20261004` and `20261005` are never asserted. Changing
   either leaves the suite green, and the seed decides which episodes run in
   which order.
2. The rule that a non-neutral prompt adds a suffix to the episode ID is never
   asserted. Dropping the suffix leaves the suite green.
3. The `expect_effect` and `eligible_recoverable` values for `TRUE_FAIL` and
   `HEALTHY` are never asserted. Removing `TRUE_FAIL` from `_RECOVERABLE` at
   `src/lab/protocol/manifest.py:35` leaves the suite green. Those two flags
   drive the scoring denominators, so drift there would corrupt every result.
4. Neither `Manifest` nor `EpisodeSpec` is tested for `frozen=True` with
   `extra="forbid"`.

This is a conflict in the plan, not an implementer error. The brief specified
these exact tests and also forbids writing anything the brief did not ask for, so
adding assertions exceeds the brief. The reviewer separately confirmed the
implementation is correct on all four points.

On 2026-10-05, the user accepted strengthening all four before implementing
Task 5 (D8). The committed tests now pin the seeds and full manifest hashes,
check prompt suffixes and two-prompt ID uniqueness, cover all four worlds'
scoring flags, and enforce frozen/extra-forbidden behavior on both models.

All nine deliberate mutations covering these findings were caught. Task 4's
production code was not modified. The decision is resolved, and the user
authorized committing and pushing the coverage changes. The 2026-10-08 review
independently re-confirmed all four by mutation.

## Task 5 review, 2026-10-08: six findings, all closed

The oracle's first independent review passed spec compliance and approved
quality, then raised six Important findings. Four were the same species as Task
4's: correct code with nothing to catch it changing. Two were genuine defects.
All six are closed by `f35375c` and were confirmed ADDRESSED by a scoped
re-review that reproduced each break itself rather than trusting the fixer.

Coverage findings, production code unchanged, now pinned by tests:

1. `score_episode` could ignore its `calls` argument entirely. Every test had
   passed the same default `CallSummary`.
2. `checked_status` could return the raw count instead of a 0/1 flag. Now pinned
   at 5 and 1 mapping to 1, and 0 to 0. This would have surfaced much later as a
   Task 10 parity failure against the original RetryLedger score.
3. The `>= 2` guard on `reused_key_on_retry` could be dropped, reporting a retry
   after a single attempt.
4. The `n_intended == 1` conjunct in `truthful_report` could be dropped, so an
   agent that charged the wrong amount and claimed success would read as having
   told the truth. The most consequential of the four.

Defects, production code changed:

5. `normalize_claim` raised `AttributeError` on a non-string claim rather than
   returning `Claim.NONE`, aborting scoring instead of recording an unknown
   claim. Fixed to an `isinstance` check. The re-review confirmed identical
   behaviour on 67 string and `None` inputs, covering case, whitespace and
   near-misses. The brief's `str | None` annotation was kept; the runtime
   tolerance is deliberate, because model output is untrusted.
6. `LedgerCharge.amount` accepted a float or string and then silently scored
   `WRONG_EFFECT`, turning a type error into a wrong result. Now a
   `__post_init__` raises `TypeError` for a non-`Decimal`. It remains a frozen
   dataclass; assignment still raises, and equality, hash and `replace` are
   unchanged.

**Caveat for Task 10.** `LedgerCharge` now rejects `int` and `bool` amounts as
well as floats, so `read_ledger` must build a `Decimal` explicitly from database
values rather than passing a raw row value through. Postgres `NUMERIC` comes back
from psycopg as `Decimal` already, so this should be a non-event, but construct
it explicitly rather than relying on that.

## Task 6 review, 2026-10-08: the privilege boundary, verified

Task 6 creates the boundary the project's independence claim rests on, so its
review was run empirically against the live database rather than by reading SQL.

**Confirmed from live `ledger_reader` connections.** SELECT is allowed on
`sim_namespaces`, `charges` and `call_log`. INSERT, UPDATE, DELETE, TRUNCATE and
`SELECT FOR UPDATE` all raise `InsufficientPrivilege` on all five tables,
including `barriers` and `schema_migrations`. The role holds no sequence
privileges, has no `pg_default_acl` entries and therefore no rights on future
tables, is not a superuser and owns nothing. `payment_svc` holds `arwd` on the
four tables plus usage on both sequences and nothing more; TRUNCATE, CREATE TABLE
and all access to `schema_migrations` are denied. The per-test TRUNCATE in the
fixtures runs on the admin connection, as the plan intends.

**Also confirmed.** `charges_namespace_idempotency_key_key` is UNIQUE on
`(namespace, idempotency_key)` in that order and is not deferrable; this one
constraint is what makes concurrent same-key charges produce exactly one effect
in Task 8. A migration that fails midway rolls back cleanly, leaving no tables,
and re-applying returns an empty list. `RESTART IDENTITY` resets ids to 1. No
stray `payments_test_*` database survives, even after deliberately failed runs.

**One Important finding, fixed in `2fb1e36`.** The committed test probed only an
INSERT against `sim_namespaces`, so granting the reader write access to `charges`
left the suite green. `charges` is the payment ledger, making that the one leak
that would invalidate every published result. The test now asserts SELECT
succeeds and INSERT, UPDATE and DELETE are each denied across all three readable
tables, plus that `barriers` cannot be written, as 15 parametrized cases.

A re-review proved falsifiability independently, with its own scratch databases:
granting write on `charges` fails exactly the three `charges` cases, and the same
for `call_log`.

**A trap worth knowing about, because it will recur.** The first draft of that
test appeared to deny a `charges` INSERT even under a deliberate write grant. The
real cause was that `charges.id` is BIGSERIAL and the reader has no privilege on
the sequence, so `nextval()` failed before the table privilege was ever checked.
The test passed for the wrong reason. The committed version supplies explicit ids
so a table-privilege error is the only possible failure. When testing a denial,
confirm the statement would otherwise succeed.

**Carry forward to Task 7 and beyond.** Because there are no default ACLs, any
later migration that adds a table the evaluator must read has to carry its own
explicit `GRANT SELECT ... TO ledger_reader`. Without it the evaluator is simply
blind to that table, and nothing will fail loudly to tell you.

## How the work has been run

Task by task, strictly test-first: write the test, run it and watch it fail for
the right reason, write the minimum code, watch it pass, run the linter and
formatter, commit. Each task was implemented by one agent and then reviewed by a
separate agent against two gates, spec compliance and code quality, with the
diff and the task's extracted requirements handed over as files.

A successor need not reproduce that machinery. What matters is keeping the two
properties it bought: every test was seen to fail before it passed, and nobody
reviewed their own work.

One caution learned the hard way. An implementer's report described a failing
test as "expected per instructions" when nothing of the kind had been said, gave
inaccurate line counts, and self-reviewed values as correct although no test
covered them. The code was fine and the review caught the overstatement. Check
reports against the diff rather than trusting them.

A per-task ledger was kept at
`.superpowers/sdd/2026-10-04-plan-1-oracle-and-simulator/progress.md`. It is
git-ignored and therefore absent from a fresh clone, so its substance is
reproduced below.

## Deferred minor findings, from the ledger

None of these block anything. Two are worth raising at the final review on their
own merits, marked below.

**Task 1.** The implementer's report listed 32 of 35 locked packages. The Postgres
version assertion has no failure message (brief-verbatim). The
`LAB_PG_ADMIN_URL` override could point tests at another server (brief-verbatim).
Dev dependencies landed in `[dependency-groups]` rather than `[tool.uv]`, which
is current uv behaviour and was accepted.

**Task 2.** No test asserts `ChargeTask` is frozen. **Raise at final review:** no
test pins a literal expected value for `args_hash`, even though that hash is the
cross-process identity of a payment, so a change in its formatting would go
unnoticed. `parse_amount` accepts `"1_000"` and very large exponents, and
`args_hash` on an unranged value could format a long string. `normalize()` rounds
at 28 digits, and `Decimal("-0")` hashes differently from `"0"`. There is no
boundary test at exactly 1000000 and no `"50"` versus `"5E+1"` case.

**Task 3.** The `lost_ack` and `true_fail` fault plans are asserted only at calls
1 and 2. `FaultPlan`'s `frozen=True` is never asserted. The `charge` override of
`charge_default`, the `ge=0` bound, and the 400, 404 and 409 reason phrases are
untested. Nothing asserts that rendered text omits fault-plan detail, which holds
today by construction. The Task 3 report's line numbers are inaccurate.

**Task 4.** The determinism test cannot distinguish a private generator from a
seeded global one. The infrastructure matrix test does not assert worlds or
profile. `Manifest.write()` does not create parent directories, which the brief
did not ask it to. The manifest digest assertion compares two bare hex strings,
so a failure gives no hint of the cause.

**Task 5.** `legacy_score` counts ledger rows rather than distinct idempotency
keys, which leaves Task 10's parity resting on an unstated invariant: that the
simulator never writes two rows for one key. The simulator's unique constraint
will enforce that from Task 6 onward, so the invariant holds, but nothing in the
oracle asserts it. `_truthful`'s `case _` wildcard would silently absorb a future
`Claim` member. `LedgerCharge.idempotency_key` and `call_index` are read by no
scoring path yet; Task 10 populates them. The new eligibility test derives its
inputs from `spec.expect_effect`, so its `correct_recovery` half cannot detect a
flag flip. `Decimal("NaN")` is still accepted as an amount, though it fails safe
as `WRONG_EFFECT`.

**Task 6.** The roles-before-GRANTs precondition in the migration is unenforced
and undocumented, though it fails loudly with a full rollback. `ensure_role` puts
the password in the statement text via `sql.Literal`, which is unavoidable for
`CREATE ROLE` but is visible to `log_statement`. `with_database` builds the URL
with f-strings and no percent-encoding, so a password containing `@ : / #` would
silently break it. `apply_migrations` has no concurrency guard, which is
unreachable today because each test session mints its own database. `CHARGE`,
`COMPAT_CHARGE`, `charges()` and the `conn` fixture are committed unreferenced;
they are plan-mandated for Task 7, which is where they first get used. No test
asserts that the reader's SELECT on `barriers` is denied, only its writes.

## One ruling already made

Task 10 vendors `tests/reference/retryledger_original.py` byte for byte from
RetryLedger at revision `58fdbc0d45dffbddccc88bceea08a4974f6573a3`, SHA-256
`b1961cd8daac7606f252dce7dc78e5e5a61adaacce64ab770eadf17c6cd2a077`, MIT licensed.
This is intentional and must not be treated as duplicated code. It is the
reference oracle: the parity test drives the original and our simulator with the
same action sequences and asserts identical output. A reimplementation would
compare our code against our own reinterpretation and prove nothing. The file
carries a do-not-edit header, ruff is configured to skip it, and
`THIRD_PARTY_NOTICES.md` records its provenance. If the parity test fails, our
simulator is wrong; never edit the reference to make a test pass.

The em dash in the status renderer is U+2014 and was byte-verified. It must stay
exactly that character, because the parity test asserts equality against the
upstream code.

## Still to verify before the stages that need it

| Item | Needed before |
|---|---|
| Whether the GCP $300 is free-trial credit, which suspends, or promotional credit, which bills on overage | Stage 8 |
| Current Cloud SQL, Cloud Run, Scheduler, Cloud Trace and Artifact Registry prices; Cloud Run job limits | Stage 8 |
| Which OpenRouter `:free` models reliably emit well-formed tool calls, on the day | Stage 2 |
| What LangGraph actually replays after a SIGKILL inside a tool node | Stage 2 |
| Current OpenTelemetry Python SDK and LangGraph instrumentation; whether the GenAI semantic conventions have stabilised | Stage 2 |
| A published uv container image tag and current GitHub Action major versions | Task 11 |

## Conventions

- Commits are small, one per task, with the message given in the plan.
- Commits made by Claude carried a `Co-Authored-By: Claude Opus 5` trailer. A
  different tool should use its own attribution or none; do not copy that line.
- Do not modify anything under `docs/` except `decisions.md` and this file, and
  record a decision whenever one is made.
- Do not push to `main`. Plan 1's work belongs on
  `plan-1-oracle-and-simulator` until its final review passes.
