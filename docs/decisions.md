# Decision log

Agent Recovery Lab. One entry per decision that constrains the design.

**Project status, 2026-10-05:** protocol code and pure oracle scoring are
implemented through Task 5. The user authorized committing and pushing the
Task 4 coverage fixes and Task 5 changes. No deployed infrastructure, model runs,
or published artifacts. Nothing in this file records an experimental result.

---

## D1. Spending model is requests per day, not dollars

**Date:** 2026-10-04
**Status:** accepted

The user will not spend out of pocket beyond a single one-time OpenRouter
credit purchase of approximately $10, intended as an unlock of the higher
free-model daily request allowance rather than as a per-token budget. Hosting
stays inside the approximately $300 of GCP credits.

Consequences:

- Evaluation is planned against a daily request ceiling. Under the accepted
  D4 design, the two matrices total 192 episodes, at most 1152 model requests at
  a six-turn cap, so the frozen run is a multi-day batch and must be scheduled as the final
  activity rather than squeezed into a build day.
- Deterministic crash, concurrency and security regressions use scripted model
  behavior and consume no model requests, so CI stays free indefinitely.
- No paid-tier model, no always-on cloud resource, and nothing that continues
  to bill after GCP credits lapse.

**To verify before phase 3:** OpenRouter's current free-tier request allowance
and the credit threshold that unlocks it; whether the free routing's
prompt-logging terms are acceptable (expected yes, data is synthetic, to be
disclosed in the methodology notes); whether the GCP $300 is free-trial credit
on an unupgraded account, which suspends rather than bills, or promotional
credit on an upgraded billing account, which bills on overage. If the latter,
plan hard caps rather than budget alerts, since alerts only notify.

## D2. Free-tier model selection is gated on tool-calling reliability

**Date:** 2026-10-04
**Status:** accepted

D1 restricts the behavior study to free model endpoints, whose tool-calling
conformance varies. A model that emits malformed tool calls fails episodes for
reasons unrelated to recovery reasoning and would silently corrupt the
behavior matrix.

The phase 3 pilot screens candidates for tool-call schema conformance first
and decision interestingness second. Record the exact provider and model ID,
the date, and the observed conformance rate. Pin the model for the frozen run.

## D3. The deployment is private; the showcase is recorded and local

**Date:** 2026-10-04
**Status:** accepted

Supersedes the earlier plan for a publicly reachable experiment console.

Cloud Run services deploy with authentication required and are reached with
the operator's own identity token. There is no anonymous access and no
public demo URL.

Consequences:

- Dropped from scope: visitor-facing auth, per-visitor model quotas, abuse
  rate limiting, token gating for live runs, and the terms of service and
  privacy policy that a public product UI would need. The methodology and
  limitations page in phase 8 is the honest substitute for the latter.
- Teardown becomes the expected end state rather than a loss of the showcase,
  which also extends credit life.
- Deployment evidence comes from Terraform apply output, CI runs and a
  recorded walkthrough instead of a live URL.
- The demo recording is load-bearing. It must show the process kill and the
  recovery with the independent ledger visible on screen.
- Unchanged: the security scenarios in phase 7. The threat model is the agent
  as an untrusted component proposing actions, plus adversarial text arriving
  in tool results, not internet visitors. Cross-namespace requests, scope
  enforcement, schema validation and malicious tool-result text keep full
  weight.
- Deferred, not rejected: a static replay page serving exported run JSON from
  free hosting, if a clickable recruiter-facing link is wanted later. Estimated
  half a day, no backend, no model spend.

## D4. Real-model matrix sizes

**Date:** 2026-10-04
**Status:** accepted by the user, 2026-10-04

This changes the brief's experimental design, so it is recorded explicitly
rather than applied silently.

- Behavior study: drop the prompt-wording arm (neutral versus careful). Keep
  2 worlds × 2 status conditions × 12 repetitions = 48 episodes, at most 288
  model requests. The brief's design was 96 episodes and 576 requests.
- Infrastructure comparison: raise repetitions from 12 to 24 per cell.
  2 worlds × 3 modes × 24 = 144 episodes, at most 864 requests.

Reason: the prompt arm tests a predictable effect (warning a model to be careful
makes it more careful) at a third of a day's request allowance. The freed quota
goes where the resume figure N comes from, and where a likely tie between modes
B and C needs a tighter interval to be believable.

Reversible: `behavior_matrix(prompts=("neutral", "careful"))` regenerates the
original 96.

## D5. Security test scope

**Date:** 2026-10-04
**Status:** accepted by the user, 2026-10-04

Keep at full weight, because they are correctness tests for operation identity:
cross-namespace requests, changed parameters under an existing intent, malicious
tool-result text, the model's lack of access to the control plane and ledger, and
secrets never reaching model observations or logs.

Cut, because the deployment is private and torn down (D3): TLS configuration,
dependency vulnerability scanning, authentication hardening, rate-limit tuning,
secret rotation drills. The locks themselves stay (client and control tokens,
database roles, IAM on GCP); only additional attack testing of them is cut.

Dependency scanning was offered back as a roughly fifteen-minute CI addition and
not taken up.

## D6. Extras under the resume-and-interview value filter

**Date:** 2026-10-04
**Status:** accepted by the user, 2026-10-04 (revised after research the same day)

Filter: keep a feature if it backs a resume figure or carries a likely interview
conversation. Checked against the six internship postings cited in the brief (all
live on 2026-10-04) and broader searches.

- **Console: server-rendered pages, no React.** None of the six postings asks for
  React. Cresta asks for "frontend and/or backend"; Amazon lists TypeScript as one
  acceptable language among seven. Its only jobs are operator inspection and the
  recording.
- **Observability: OpenTelemetry tracing is kept.** This reverses the earlier
  proposal to cut it. Three of six postings mention monitoring (Cresta, Amazon,
  Datadog); none names OpenTelemetry, but a Summer 2027 AI engineer intern posting
  (DV Trading) lists it as preferred, OpenTelemetry has GenAI semantic conventions
  for model and tool calls (under active development), and tracing agents appears
  in AI-engineering interview preparation material. Cost: about half a day to a
  day; Jaeger locally; Cloud Trace on GCP, whose first 2.5 million spans per month
  are free. Structured events in Postgres remain the source of record for scoring
  and the timeline; traces are for debugging and the demo.
- **Second example workflow: kept as an optional final step of Plan 4.** Four of
  six postings describe building platforms, frameworks or tools for other
  engineers. The second workflow (for example, sending a welcome email exactly
  once) is the only evidence that the gateway protects more than payments.
  About half a day if Plan 4 designs for it. First item cut if the schedule
  slips; if cut, the word "reusable" never appears in project materials.

Schedule effect: roughly one to one and a half days added, 12 to 15 working days
in total.

## D7. Tooling and repository layout

**Date:** 2026-10-04
**Status:** accepted

- uv 0.12 for environments and locking, Python 3.12 (both present locally).
- One Python package, `src/lab`, with one entry point per process, instead of the
  brief's separate `apps/*` and `packages/recovery` trees. The process boundaries
  that the crash tests depend on are runtime boundaries, and they stay. Package
  boundaries for a single developer sharing one set of models would add packaging
  work without adding evidence.
- psycopg 3 with hand-written SQL and a small forward-only migration runner
  instead of SQLAlchemy and Alembic. The atomic idempotency insert is the heart of
  the project and should be readable as SQL. Three databases on one instance make
  Alembic's multi-database setup more trouble than it saves.
- Postgres 16 in Docker for local work and tests. No local `psql` is assumed.

## D8. Task 4 coverage and the Task 5 review checkpoint

**Date:** 2026-10-05
**Status:** accepted by the user

The user accepted the proposed batch: address all four Task 4 coverage findings,
implement Task 5, run the tests, and stop for their review before committing.
This resolves the earlier decision to defer or strengthen Task 4's tests.

- Pin both default seeds and the complete default manifest hashes.
- Check prompt suffixes and unique IDs in the original two-prompt matrix.
- Check expected-effect and recovery-eligibility flags for all four worlds.
- Check that both experiment models reject mutation and unknown fields.
- Keep Task 4's production behavior unchanged.
- Implement Task 5's planned scoring contract, with regression checks for
  mixed effect counts and propagation of recovery-denominator eligibility.

The batch ends after Task 5. Tasks 6 onward are not authorized by this batch.
The changes were initially left uncommitted at the user's review checkpoint.
The user subsequently authorized committing and pushing the batch on
2026-10-05. Work remains stopped after Task 5.

## D9. The simulator's call-counter ordering is Task 8's problem

**Date:** 2026-10-08
**Status:** accepted by the user

Task 7's review found that `charge()` increments the per-namespace call counter
and commits it before doing the work and before writing the call log. Any
exception between the increment and the log silently consumes a fault-schedule
slot while leaving no record that an attempt happened. That matters because the
attempt count feeds a published metric, the legacy score's `n_charge_attempts`
and `reused_key_on_retry`.

Fixing it requires restructuring `charge()`, whose structure the Task 7 brief
prescribes, so it was escalated rather than changed silently.

The user chose to fold it into Task 8 rather than fix it first or accept it.

Reason: Task 8 adds the synchronized barrier that pauses a request in exactly
that window, so Task 8 must decide the ordering regardless. Doing it there is
one change instead of two, and Task 8 can write a test that demonstrates the
problem, which is awkward before barriers exist.

**Binding requirement on Task 8.** Task 8 must resolve the ordering so that a
consumed fault-schedule slot and the call-log record cannot disagree, and must
carry a test that injects a failure between the increment and the log. The
barrier's placement relative to the counter increment is part of that decision.
Task 8 is not complete while this is open.

## D10. The evaluation is framed as baseline and treatment, not a three-way survey

**Date:** 2026-10-11
**Status:** accepted by the user

The user asked for a result of the shape: measure a metric, understand the
system, change it, measure again, and report how far the metric moved. The
earlier design produced a side-by-side comparison of three modes, which reads
as a survey and makes a weaker claim from the same data.

**Baseline:** the model on its own, which the 48-episode behaviour study already
measures (mode A, `agent_directed`).

**Treatment:** the adapters, which are the engineered change.

**Primary metric: duplicate charges.** When the model cannot check payment
status and receives an ambiguous error, some fraction of the time it retries
under a fresh idempotency key and charges twice. A stable key cannot do this,
because the backend's atomic idempotency record absorbs the retry. So the
expected finding is a real rate falling to zero with a one-sentence mechanism.
Chosen as primary because it is near-certain to hold and is intuitive to a
non-specialist reader.

**Secondary metric: false success.** The worst verdict in the taxonomy: the agent
reports success when nothing committed, so a human downstream believes it. If
the model alone does this at any rate and the gateway drives it to zero by
requiring evidence before claiming success, the prevented failure is the
expensive one.

**Explicitly not the headline:** deterministic retry versus durable gateway. Both
use a stable key, so on effect correctness they will probably tie. The gateway's
real advantage is truthful reporting when status evidence is unavailable, which
is a subtler second finding, not a lead.

**Binding constraints on this framing.**

- The metric and the comparison are pre-registered before the frozen run. The
  number is written only after it is measured. Choosing a target figure and then
  finding data to match it is the one failure mode this project exists to avoid,
  and it would not survive an interviewer's probing.
- A tie, or a baseline that turns out already strong, is published as measured.
- The denominator is stated with every rate, and the Wilson interval with it.
  Twenty-four repetitions per cell is exploratory, and the report says so.

**Two before-and-after results already measured, usable now.**

1. The D9 call-accounting fix took the legacy attempt count on a crash episode
   from 1 to 3, and key reuse from 0 to 1, bringing both into exact agreement
   with the reference implementation. Before the fix the count was silently
   wrong on precisely the crash episodes the project studies.
2. The Task 7 review found the test suite caught 8 of 18 injected faults in the
   simulator. After the fix waves, every injected fault is caught. That is a
   measurable statement about test effectiveness rather than about the system.

## D11. Library facts verified for Plan 2, and what they change

**Date:** 2026-10-11
**Status:** accepted; supersedes the roadmap's dated snapshot

Verified today rather than trusted from the roadmap, as the handoff requires.

**OpenRouter free-tier limits, from the official docs:** 20 requests per minute
at every level; 50 per day below the credit threshold and **1,000 per day at or
above it**; the threshold is 10 credits all-time purchased, and the higher limit
starts one credit early at 9 to absorb rounding. A third-party source claimed
200 per day, which is wrong. **D1's arithmetic stands**, so the roughly 1,600
planned requests remain about two days of allowance rather than eight.

**Free tool-capable models exist and are discoverable, not guesswork.**
`GET https://openrouter.ai/api/v1/models` returned 458 models, of which 15 end
in `:free` and **14 declare `tools` in `supported_parameters`**. The pilot must
query this live and filter on that field rather than hardcoding a list from a
blog, because free availability changes. Today's candidates with the largest
context windows include `thinkingmachines/inkling:free`,
`nvidia/nemotron-3-ultra-550b-a55b:free`, `google/gemma-4-31b-it:free` and
`poolside/laguna-s-2.1:free`. Declaring `tools` is not the same as emitting
well-formed calls under pressure, which is what D2's conformance pilot measures.

**Versions:** `langgraph` 1.2.14 (2026-10-06) and
`langgraph-checkpoint-postgres` 3.1.2 (2026-08-07), both requiring Python 3.10
or newer. We are on 3.12.

**LangGraph has a durability setting, which the roadmap did not account for.**
Graph execution takes `durability="exit" | "async" | "sync"`. `exit` saves only
when execution ends, so intermediate state is lost on a mid-run crash. `async`
saves while the next step runs, with a small risk that a checkpoint is missing
after a crash. `sync` writes every checkpoint before the next step begins.

Consequence, and it matters for fairness: **all three modes must run with the
same durability setting, and that setting must be `sync`.** Giving the durable
gateway `sync` while leaving the baselines on `exit` would hand mode C a win
that came from switching off the baselines' checkpointing, which is exactly the
rigged comparison the brief forbids. Record the setting in the experiment
config, beside the model ID and the prompt version.

**The boundary this project studies is undocumented upstream.** The LangGraph
documentation states that when a node fails in a super-step, the writes of
sibling nodes that succeeded are kept and those nodes do not re-run. It does
**not** state what happens to a node that completed but whose write had not been
committed when the process died. That is precisely the commit-then-crash
boundary the lab exists to measure.

Two consequences. Plan 2 must determine it empirically rather than cite it, with
a test that kills a worker inside a tool node and observes which nodes re-run.
And the finding is worth reporting in the writeup: the replay behaviour at the
boundary where an external effect has committed but the graph has not recorded
it is not specified by the framework, which is why a durable journal outside the
graph is needed at all.

**Observability, revised.** The GenAI semantic conventions have moved to a
separate repository whose releases section is empty and whose schema URL reads
"TODO", so they are in flux. Use the attribute names where convenient, but do
not depend on them and do not claim conformance to them. Structured events in
Postgres remain the source of record for scoring; traces are for debugging and
the demo, as D6 already says.

## D12. No conformance pilot; the behaviour baseline ran on the reachable fallback model

**Date:** 2026-10-16
**Status:** accepted by the user, 2026-10-16

Two choices about the Plan 3 behaviour study, recorded rather than applied silently.

First, the D2 conformance pilot was dropped at the user's instruction: pick a
free tool-capable model directly rather than screen candidates with 24 pilot
episodes. The risk D2 guarded against (a model that emits malformed tool calls
corrupting the matrix) is still covered, because the oracle scores a malformed
first charge as its own verdict, so poor conformance shows up in the study
itself instead of a separate pilot. This spent 0 pilot requests.

Second, the pinned primary `nvidia/nemotron-3-ultra-550b-a55b:free` was listed
as live in the models metadata but did not respond to a chat-completions request
at run time: a direct call returned nothing at both 30 and 120 seconds, and all
48 of its episodes crashed with no model call recorded. The runner's ordered
fallback then ran the whole 48-episode matrix on the first fallback,
`nvidia/nemotron-3-super-120b-a12b:free`, which completed cleanly. The behaviour
baseline is therefore attributed to the 120B model, not Ultra. The study spent
nothing, because both models are free. If Ultra's free endpoint becomes usable
later, the study can be re-run against it as its own experiment; the 120B result
stands as the recorded baseline until then.

The baseline result (mode A, the model alone, controlled profile, 48 episodes):
45 of 48 verdicts CORRECT and 3 WRONG_EFFECT, with correct recovery in 44 of 48
eligible trials. Per-condition Wilson intervals are in docs/behaviour-findings.md.
Twelve repetitions per cell is an exploratory size, stated in the report.
