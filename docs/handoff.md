# Handoff

**Updated:** 2026-10-05. **For:** the next assistant or developer to pick this up,
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

Verification on 2026-10-05:

- Unit suite: **72 passed** (22 task-argument, 7 fault/render, 20 manifest,
  23 scoring cases).
- Full suite: **72 passed, 1 failed**. The failure is
  `tests/test_environment.py::test_postgres_16_is_reachable`: the local server on
  port 54329 closes the connection unexpectedly. The full suite is not green.
- Python lint and formatting checks pass.
- Nine intentional manifest regressions and four scoring regressions were
  caught by tests. The mutations ran only in child-process memory; no broken
  source file was written.
- Task 5's tests were run before implementation and failed at collection with
  `ModuleNotFoundError: No module named 'lab.oracle'`.

| Task | State |
|---|---|
| 1. Scaffold and environment test | Complete, review clean |
| 2. Task constants and canonical arguments | Complete, review clean |
| 3. Hidden fault plans and observation text | Complete, review clean |
| 4. Episode manifests and matrices | Committed; all four coverage findings addressed |
| 5. Oracle scoring | Implemented, tested and committed |
| 6. Database helpers, simulator schema, fixtures | Not started, needs Postgres |
| 7. Simulator charge and status logic | Not started, needs Postgres |
| 8. Concurrency guarantee and commit barriers | Not started, needs Postgres |
| 9. Public and internal HTTP apps | Not started, needs Postgres |
| 10. Ledger reader and RetryLedger parity | Not started, needs Postgres |
| 11. Container, compose, CI, protocol document | Not started, needs Postgres |

Plans 2 through 8 of the roadmap have not been written in detail yet. Write each
one only after the previous plan lands, and verify the relevant library APIs on
the day rather than trusting a dated snapshot.

## Current stop: Task 5 completed; publishing authorized

The user initially asked to stop after Task 5, run the tests, and leave the
changes for their review before committing. On 2026-10-05, they subsequently
authorized committing and pushing the changes. Task 6 has not been started.
No independent code review of this batch has been performed.

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

The next implementation task is Task 6; the user has not asked to begin it.
It needs working Postgres. The roadmap and detailed plan still contain their
historical planning-only status labels; this file records current progress.

## Blocker one: Docker, and the user owns it

Docker Desktop's container runtime is wedged on this machine. The Postgres
container reports healthy because its healthcheck runs inside Docker's VM, while
the path from the host into that VM is broken.

Evidence gathered: a TCP connection to port 54329 opens instantly on both IPv4
and IPv6 and is then closed with an empty reply, so the proxy accepts and drops
it; `docker exec` and `docker restart` both hang indefinitely; `docker version`
answers in 0.09s. Restarting only the project's container does not work, because
that is one of the hanging commands.

The original diagnosis above was recorded on 2026-10-04. On 2026-10-05, the
connectivity test was rerun from the host and still failed with an unexpectedly
closed connection on both IPv4 and IPv6. Docker Desktop was not restarted.

The remedy is restarting Docker Desktop, which also bounces the user's unrelated
`kind-registry` container. **The user chose to do this themselves.** Do not
restart Docker Desktop without asking them again. Once they confirm:

```bash
docker compose up -d --wait postgres
uv run pytest tests/test_environment.py -v
```

Expect a pass. Tasks 6 to 11 are then unblocked.

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
authorized committing and pushing the coverage changes.

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
did not ask it to.

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
