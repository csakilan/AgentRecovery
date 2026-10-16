# Plan 3 design: the real-model behaviour study

**Date:** 2026-10-16
**Status:** approved by the user, 2026-10-16

## Purpose

Plan 2 proved the pipeline scores scripted policies correctly and measured what
a crash does to a replayed tool call. Plan 3 runs the pipeline against a real
free model for the first time and records how that model behaves under the two
recoverable worlds, with no infrastructure help beyond mode A. This is the
baseline in the resume framing (D10): the model alone, before any adapter.

## What changes from the roadmap

Two deviations from the roadmap and decisions, made by the user on 2026-10-16,
recorded here rather than applied silently:

- **No conformance pilot.** D2 gated model selection on a 24-episode pilot that
  screened candidates for tool-call conformance. The user dropped it: "you don't
  need to run tests on this, just get the quickest best option." The model is
  pinned directly (below). If the pinned model emits malformed tool calls, the
  oracle already classifies that as its own verdict (a malformed first charge
  scores MISSED), so the study itself surfaces poor conformance instead of a
  separate pilot. This spends 0 pilot requests instead of up to 144.
- **Model pinned, not selected by data.** Primary:
  `nvidia/nemotron-3-ultra-550b-a55b:free` (fetched live from OpenRouter on
  2026-10-16; `:free`, tool-capable, 1M context). Fallbacks, used only if the
  primary will not respond or is rate-limited, and recorded in the run config if
  used: `nvidia/nemotron-3-super-120b-a12b:free`, then
  `nvidia/nemotron-3.5-super-vl:free`.

Unchanged from D4: the behaviour matrix is 2 worlds x 2 status conditions x 12
repetitions = 48 episodes, neutral prompt only, mode A (agent_directed),
controlled profile. At most 288 model requests (6 turns each), inside one
1,000/day quota day.

## Budget and the quota boundary

`:free` models cost $0 per request, so the key's $1 paid-spend cap is never
touched. The account is past the free tier, so the `:free` daily ceiling is
1,000 requests and the rate limit is about 20 requests/minute.

Everything except the one real run is built and tested offline with mocked HTTP
and the scripted model, spending nothing. The single quota spend is the 48-episode
study, run once, paced under the rate limit, after an explicit user go-ahead.
A test must still prove no real provider request is reachable from the suite
(the Plan 2 socket-level guard and the `:free` allowlist both stay in force).

## Architecture

Four units, each with one responsibility and a well-defined interface.

### 1. `src/lab/agent/openrouter.py`: the real model client

Implements the same `ModelClient` interface the scripted model implements
(`complete(messages, tools) -> ModelTurn`), so the graph and runner are
unchanged. Responsibilities:

- Refuse any model ID that does not end in `:free` at construction (defence in
  depth beside `assert_model_allowed`).
- Convert the versioned tool schemas from the kept `{name, description,
  input_schema}` shape into OpenRouter's
  `{"type": "function", "function": {name, description, parameters}}` shape. The
  conversion is the Plan 2 carry-forward item; `TOOL_SCHEMA_VERSION` pins the
  source shape.
- POST to the chat-completions endpoint, parse the assistant message and any
  `tool_calls` back into a `ModelTurn` (text, tool_calls, raw, token usage,
  `malformed=True` when the response is not parseable into the expected shape).
- Read the key only from the environment (`OPENROUTER_API_KEY`), never from an
  argument, a message, or model output.
- Map transport and HTTP errors to a typed error; do not retry a malformed
  response (that is data to record, not a transport fault). Retry only genuine
  transient transport failures and 429s, with backoff, bounded.

### 2. Budget wiring

`assert_model_allowed` (the `:free` allowlist) and `RequestBudget` (the
cross-process daily ceiling, Plan 2) are called at the one place a real request
leaves, so the free-only rule and the daily cap are enforced structurally rather
than by convention. A request that would exceed the budget raises before the
HTTP call.

### 3. `behaviour_matrix(...)` in the manifest module

Generates the 48-episode manifest: worlds {lost_ack, true_fail} x status
{available, blind} x 12 reps, neutral prompt, mode A, controlled profile,
shuffled with a recorded seed. Reversible to the roadmap's 96 by re-adding the
prompt arm, per D4.

### 4. The batch runner

Runs the manifest through the existing `src/lab/runner` episode runner:

- Paces requests under the ~20/min rate limit.
- Writes each episode's `results` row as it completes (the runner already does
  this), so a mid-run stop or rate-limit halt loses no completed episode.
- Resumable: episodes already scored for this experiment are skipped, the rest
  run. Records the model actually used per run, including any fallback switch.

## Report

`docs/behaviour-findings.md` plus machine-readable CSV and JSONL:

- Per-cell (world x status) verdict counts and failure classification.
- Wilson 95% intervals per cell; raw counts always shown.
- The exact model ID and the date, the manifest and config hashes.
- A plain statement that 12 repetitions is an exploratory sample size.
- The number is written only after it is measured (D10).

## Testing

- The client's schema conversion, response parsing (including malformed and
  multi-tool-call responses), `:free` refusal, key sourcing, and budget/allowlist
  enforcement are tested with mocked HTTP (httpx MockTransport or equivalent).
  No real request in any test.
- `behaviour_matrix` is tested for shape, counts, determinism under a seed, and
  that it emits only neutral/mode-A/controlled specs.
- The batch runner is tested end to end with the scripted model against the real
  in-process simulator: pacing, per-episode persistence, resume-skips-completed.
- The no-live-calls guarantee stays proven by the Plan 2 socket-level test; a new
  test asserts the OpenRouter client is unreachable (raises) under the guard.

## Out of scope for Plan 3

Modes B and C, the journal, and the three-mode comparison are Plan 4 and Plan 6.
Plan 3 produces the single-model, mode-A baseline only.
