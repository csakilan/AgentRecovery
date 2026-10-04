# Continuation handoff

**Date:** 2026-10-04

## What exists

Planning documents only. There is no application code in this repository, no git
repository yet, no cloud resource, no model run of any kind, and nothing published.
No OpenRouter credit has been bought as far as this session knows. No metric anywhere
in these documents has been measured.

## Files created

| File | Purpose |
|---|---|
| `docs/decisions.md` | D1 to D7, all accepted by the user as of 2026-10-04 |
| `docs/pitch.md` | The sixty-second spoken explanation, with a pre-results ending |
| `docs/superpowers/plans/2026-10-04-roadmap.md` | Scope and acceptance, architecture, contracts, state machine, backlog of eight sub-plans, test and evaluation plan, deployment and teardown, open decisions, risks |
| `docs/superpowers/plans/2026-10-04-plan-1-oracle-and-simulator.md` | Fully detailed, test-first plan for the oracle and payment simulator (11 tasks) |
| `docs/handoff.md` | This file |

## How far Plan 1 has been checked

The code blocks in Plan 1 were extracted into a scratch directory outside the
repository. All 30 extracted files compile. The database-free tests from Tasks 2 to 5
ran and passed (51 tests). The tests for Tasks 6 to 11 need Postgres and have not been
run; they compile but are unverified until Task 6 is executed.

## Facts verified this session

- Local toolchain: Python 3.12.8, uv 0.12.22, Docker 24.0.2, Docker Compose 2.18.1,
  git 2.50.1, Node 22.20.0, no `psql`.
- OpenRouter free models: 20 requests per minute; 50 per day below $10 of purchased
  credit, 1,000 per day at or above it. A negative balance blocks free models too.
- LangGraph Postgres checkpointer: `from langgraph.checkpoint.postgres import
  PostgresSaver`, created with `PostgresSaver.from_conn_string(...)` and `.setup()`.
  Replay semantics after a mid-node crash are not yet verified; Plan 2 tests them.
- RetryLedger at revision `58fdbc0d`: MIT licence, `code/ei_retryledger.py` is 110 lines
  with SHA-256 `b1961cd8...c6cd2a077`. Its two worlds use different first-error text,
  the charge tool has no order or currency field, the legacy score ignores the amount,
  and in the true-failure world only the first charge call fails.

## Still to verify, and when

| Item | Before |
|---|---|
| Whether the GCP $300 is free-trial or promotional credit | Plan 8 |
| Current Cloud SQL, Cloud Run, Scheduler, Artifact Registry prices; Cloud Run job limits | Plan 8 |
| Which OpenRouter `:free` models support tool calling on the day of the pilot | Plan 2 |
| LangGraph replay boundary after a SIGKILL inside a tool node | Plan 2 |
| Published tag for the uv container image and current GitHub Action majors | Plan 1, Task 11 |
| Current OpenTelemetry Python SDK, LangGraph instrumentation options, and whether the GenAI semantic conventions have stabilized | Plan 2 |

## Market research behind D6 (2026-10-04)

All six internship postings cited in the brief were refetched and are live. Counted for
three extras: React or TypeScript, observability, and reusable libraries or platforms.

| Posting | React/TS | Observability | Platform or library |
|---|---|---|---|
| Bot Auto, Intern SWE AI Agents | no | no | yes, "internal platforms", agent frameworks |
| Cresta, SWE Intern | "frontend and/or backend" | yes, "monitoring" of AI behavior | yes, "Platform & Infrastructure" |
| Amazon, SDE Intern Summer 2027 | TypeScript, one of seven languages | yes, "operational excellence through monitoring" | no |
| Datadog, SWE Intern | no | yes, it is the product | yes, "developer platforms" |
| Together AI, SWE Intern Winter 2027 | no | no | yes, open source |
| C3 AI, SWE Intern Summer 2027 | no | no | yes, "foundational components, frameworks, and tools" |

Totals: React 0 of 6, observability 3 of 6, platform or library 5 of 6. None of the six
names OpenTelemetry. Supporting evidence for tracing came from a Summer 2027 AI engineer
intern posting (DV Trading) listing OpenTelemetry as preferred, the OpenTelemetry GenAI
semantic conventions covering model and tool calls, and AI-engineering interview
preparation material on tracing agents (general advice, not interviewer reports).
Cloud Trace is free for the first 2.5 million spans per month.

Not established by this research: that any of these extras changes a hiring outcome. It
is a small, role-fit sample of postings, not a market census.

## Decisions waiting on the user

None. D1 to D7 are all accepted. Reversing D4 is a one-argument change in the manifest
generator (`behavior_matrix(prompts=("neutral", "careful"))`).

## Next implementation task

Plan 1, Task 1: repository scaffold and test database. It starts with `git init`. It
needs the user's go-ahead, because the brief authorizes planning only.

After Plan 1 lands, write Plan 2 (agent loop, scripted model, budget guard, OpenTelemetry
tracing, model pilot) in the same format, starting by verifying the LangGraph, OpenRouter
tool-calling and OpenTelemetry Python APIs on that day.

Plan 1 is unaffected by D4 to D6. The simulator and oracle are the same either way, so
tracing enters in Plan 2 and the second workflow in Plan 4.
