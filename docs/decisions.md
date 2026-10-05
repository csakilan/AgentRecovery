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
