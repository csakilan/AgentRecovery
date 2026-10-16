# Plan 3: Real-model behaviour study Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the existing episode pipeline against one pinned free model and report how it behaves, unaided, under the two recoverable worlds.

**Architecture:** A new `ModelClient` implementation talks to OpenRouter's chat-completions API, converting the versioned tool schemas to the provider's function-calling shape and parsing tool calls back into a `ModelTurn`. It is the one place a real request leaves, so the `:free` allowlist, the cross-process daily budget and a rate-limit pace all sit there. A batch runner drives the existing `behavior_matrix` manifest through the existing `run_spec`, persisting per episode and resumable. A report reads the scored `results` rows and writes `docs/behaviour-findings.md` with Wilson intervals.

**Tech Stack:** Python 3.12, httpx, pydantic v2, psycopg 3, PostgreSQL 16, OpenRouter (`:free` models only), pytest.

## Global Constraints

- Python 3.12 or newer; uv for environments; `uv.lock` committed.
- PostgreSQL 16 on host port 54329. No local `psql` binary.
- Every graph run uses `durability="sync"`, without exception (already enforced by `run_episode`/the runner; do not change it).
- `:free` models only. No paid model ID may ever be sent. The account's $0-per-request on `:free` means the key's $1 paid cap is never touched.
- Zero real model requests from the test suite. Every provider code path must be unreachable from tests, proven by a test (the Plan 2 socket-level guard in `tests/network_guard.py` and `tests/runner/test_no_live_calls.py` stays in force).
- Model output is untrusted input. Tenant, namespace and the effective idempotency key are never taken from it. The key is read only from `OPENROUTER_API_KEY`, never from an argument, a message, or model output.
- The evaluator reads the payment ledger through `ledger_reader` and never reads the runtime database to decide a verdict. Reporting aggregates already-decided verdicts and may read the runtime `results`/`runs` rows.
- Dev-only credentials stay in `.env.example` and `compose.yaml`; the real key lives only in the gitignored `.env`.
- ruff line-length 100; `uv run ruff check .` and `uv run ruff format --check src tests scripts` must pass. Use `datetime.UTC`.
- Commit message bodies end with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## Pinned model (per the spec, 2026-10-16)

Primary `nvidia/nemotron-3-ultra-550b-a55b:free`; fallbacks, used only on transport/rate-limit failure and recorded in the run config: `nvidia/nemotron-3-super-120b-a12b:free`, then `nvidia/nemotron-3.5-super-vl:free`. No conformance pilot.

## What already exists (consume, do not rebuild)

- `src/lab/agent/model.py`: `ModelClient` Protocol (`model_id: str`, `complete(messages, tools) -> ModelTurn`), `ModelTurn(text, tool_calls, raw, input_tokens, output_tokens, malformed)`, `ToolCall(id, name, arguments)`.
- `src/lab/agent/tools.py`: `tool_schemas(profile, status_available) -> list[dict]` returning fresh deep copies in the `{name, description, input_schema}` shape; `TOOL_SCHEMA_VERSION`.
- `src/lab/runtime/budget.py`: `assert_model_allowed(model_id)` (raises `ModelNotAllowed` for non-`:free`), `RequestBudget(conn, daily_limit=900)` with `claim(n=1) -> int` (raises `BudgetExceeded`), `FREE_SUFFIX=":free"`. Connections must be `autocommit=True`.
- `src/lab/protocol/manifest.py`: `behavior_matrix(reps=12, prompts=("neutral",), seed=20261004) -> Manifest` already produces 2 worlds x 2 status x 12 reps = 48 mode-A controlled neutral specs; `Manifest.episodes`, `Manifest.sha256()`.
- `src/lab/runner/episode.py`: `run_spec(spec, model, config, *, manifest_sha256, experiment_name="adhoc", experiment_id=None, clock=...) -> EpisodeResult`; `RunnerConfig.from_env(...)`; `derive_experiment_id(name, cfg)`; `experiment_config(model_id, config, manifest_sha256)`; `EpisodeAlreadyRunError`; `EpisodeResult(... score: EpisodeScore)`.
- `src/lab/oracle/score.py`: `EpisodeScore` with `verdict` (a `Verdict` StrEnum), `claim`, `n_effects`, `n_intended`, `n_wrong`, `truthful_report`, `completed_before_deadline`, `correct_recovery`, `eligible_recoverable`.
- `src/lab/runner/tracing.py`: `force_tracing_off()`.

---

### Task 1: OpenRouter message and schema conversion

**Files:**
- Create: `src/lab/agent/openrouter.py`
- Test: `tests/agent/test_openrouter_convert.py`

**Interfaces:**
- Consumes: `tool_schemas` output shape `{name, description, input_schema}`; graph message dicts (assistant `{"role":"assistant","content":str|None,"tool_calls":[{"id","name","arguments":dict}]}`, tool `{"role":"tool","tool_call_id","name","content":str}`, and plain `{"role":"system"|"user","content":str}`); `ModelTurn`, `ToolCall` from `lab.agent.model`.
- Produces: pure functions `to_openai_tools(tools: list[dict]) -> list[dict]`, `to_openai_messages(messages: list[dict]) -> list[dict]`, `parse_completion(body: dict) -> ModelTurn`. These are the whole conversion surface; the HTTP client in Task 2 calls them.

- [ ] **Step 1: Write the failing test for tool-schema conversion**

```python
# tests/agent/test_openrouter_convert.py
from lab.agent.openrouter import to_openai_tools, to_openai_messages, parse_completion
from lab.agent.tools import tool_schemas
from lab.protocol.task import Profile


def test_tool_schema_is_converted_to_openai_function_shape():
    src = tool_schemas(Profile.CONTROLLED, status_available=True)
    out = to_openai_tools(src)
    assert len(out) == len(src)
    charge = next(t for t in out if t["function"]["name"] == "charge")
    assert charge["type"] == "function"
    # input_schema becomes parameters; name and description carried over verbatim.
    assert charge["function"]["parameters"] == next(
        t["input_schema"] for t in src if t["name"] == "charge"
    )
    assert charge["function"]["description"] == next(
        t["description"] for t in src if t["name"] == "charge"
    )
    # Conversion does not mutate the caller's list.
    assert "input_schema" in src[0] and "function" not in src[0]
```

- [ ] **Step 2: Run it, expect ImportError/fail**

Run: `uv run pytest tests/agent/test_openrouter_convert.py -q`
Expected: FAIL (module/function not defined).

- [ ] **Step 3: Implement the three conversion functions**

In `src/lab/agent/openrouter.py`. `to_openai_tools` maps each `{name, description, input_schema}` to `{"type": "function", "function": {"name", "description", "parameters": input_schema}}` with a deep copy of `input_schema` (never mutate the input). `to_openai_messages` maps graph messages to OpenAI wire shape: system/user pass through; an assistant message's `tool_calls` become `[{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], separators=(",",":"))}}]` with `content` left as given (may be None); a tool message becomes `{"role": "tool", "tool_call_id": ..., "content": ...}` (drop the `name`, which OpenAI tool messages key by id). `parse_completion` reads `body["choices"][0]["message"]`: `text = message.get("content")`; for each `message.get("tool_calls", [])` build a `ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=json.loads(tc["function"]["arguments"]))`; token usage from `body.get("usage", {})` (`prompt_tokens`, `completion_tokens`); `raw=body`; `malformed=False`. If a tool call's `arguments` is not valid JSON, or `choices`/`message` is missing/misshaped, return a `ModelTurn` with `tool_calls=()`, `text` as whatever text is present (or None), `raw=body`, `malformed=True` — never raise.

- [ ] **Step 4: Add message and parse tests**

```python
def test_assistant_and_tool_messages_become_openai_wire_shape():
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "name": "charge", "arguments": {"amount": 49.99}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "charge", "content": "Charge succeeded."},
    ]
    out = to_openai_messages(msgs)
    assert out[0] == {"role": "system", "content": "s"}
    tc = out[2]["tool_calls"][0]
    assert tc == {"id": "c1", "type": "function",
                  "function": {"name": "charge", "arguments": '{"amount":49.99}'}}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "Charge succeeded."}


def test_parse_completion_builds_a_model_turn():
    body = {"choices": [{"message": {"content": "ok", "tool_calls": [
        {"id": "x", "type": "function",
         "function": {"name": "report_done", "arguments": '{"outcome":"succeeded"}'}}]}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3}}
    turn = parse_completion(body)
    assert turn.malformed is False
    assert turn.text == "ok"
    assert turn.tool_calls[0].name == "report_done"
    assert turn.tool_calls[0].arguments == {"outcome": "succeeded"}
    assert turn.input_tokens == 11 and turn.output_tokens == 3


def test_parse_completion_marks_unparseable_arguments_malformed_without_raising():
    body = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "x", "type": "function",
         "function": {"name": "charge", "arguments": "{not json"}}]}}]}
    turn = parse_completion(body)
    assert turn.malformed is True
    assert turn.tool_calls == ()


def test_parse_completion_marks_missing_choices_malformed():
    turn = parse_completion({"error": {"message": "upstream"}})
    assert turn.malformed is True
    assert turn.raw == {"error": {"message": "upstream"}}
```

- [ ] **Step 5: Run to pass; show malformed test can fail**

Run: `uv run pytest tests/agent/test_openrouter_convert.py -q` — expect PASS.
Mutation: make `parse_completion` raise instead of returning `malformed=True` on bad JSON; the two malformed tests go red; revert.

- [ ] **Step 6: ruff and commit**

Run: `uv run ruff check . && uv run ruff format --check src tests scripts`

```bash
git add src/lab/agent/openrouter.py tests/agent/test_openrouter_convert.py
git commit -m "feat(agent): OpenRouter schema and message conversion"
```

---

### Task 2: The OpenRouter HTTP client, hardening and pacing

**Files:**
- Modify: `src/lab/agent/openrouter.py`
- Test: `tests/agent/test_openrouter_client.py`

**Interfaces:**
- Consumes: the Task 1 conversion functions; `assert_model_allowed` from `lab.runtime.budget`; `httpx`.
- Produces: `class OpenRouterClient` implementing `ModelClient`: `__init__(self, model_id: str, *, api_key: str | None = None, http_client: httpx.Client | None = None, base_url: str = "https://openrouter.ai/api/v1", min_interval_s: float = 3.0, max_retries: int = 5, timeout_s: float = 60.0)`; attribute `model_id`; method `complete(messages, tools) -> ModelTurn`. Raises `ModelNotAllowed` at construction for a non-`:free` id; raises `OpenRouterError` (new, subclass of `RuntimeError`) for exhausted transport/HTTP failures. A malformed *response body* is returned as `ModelTurn(malformed=True)`, never raised.

- [ ] **Step 1: Write the failing tests (mocked transport, no network)**

```python
# tests/agent/test_openrouter_client.py
import httpx, pytest
from lab.agent.openrouter import OpenRouterClient, OpenRouterError
from lab.runtime.budget import ModelNotAllowed

FREE = "nvidia/nemotron-3-ultra-550b-a55b:free"


def _client(handler, **kw):
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return OpenRouterClient(FREE, api_key="k", http_client=http, min_interval_s=0.0, **kw)


def test_refuses_a_paid_model_at_construction():
    with pytest.raises(ModelNotAllowed):
        OpenRouterClient("openai/gpt-4o", api_key="k")


def test_sends_bearer_key_model_and_converted_tools_and_parses_reply():
    seen = {}
    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        import json as _j
        seen["body"] = _j.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi",
            "tool_calls": [{"id": "c1", "type": "function",
            "function": {"name": "report_done", "arguments": '{"outcome":"succeeded"}'}}]}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    client = _client(handler)
    from lab.agent.tools import tool_schemas
    from lab.protocol.task import Profile
    turn = client.complete([{"role": "user", "content": "go"}],
                           tool_schemas(Profile.CONTROLLED, True))
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == FREE
    assert seen["body"]["tools"][0]["type"] == "function"
    assert turn.tool_calls[0].name == "report_done"


def test_retries_a_429_then_succeeds():
    calls = {"n": 0}
    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "rate"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    turn = _client(handler, max_retries=3).complete([{"role": "user", "content": "x"}], [])
    assert calls["n"] == 2 and turn.text == "ok"


def test_raises_openrouter_error_after_retries_exhausted():
    def handler(request):
        return httpx.Response(503, json={"error": "down"})
    with pytest.raises(OpenRouterError):
        _client(handler, max_retries=2).complete([{"role": "user", "content": "x"}], [])


def test_a_200_with_a_garbage_body_is_malformed_not_raised():
    def handler(request):
        return httpx.Response(200, json={"unexpected": True})
    turn = _client(handler).complete([{"role": "user", "content": "x"}], [])
    assert turn.malformed is True
```

- [ ] **Step 2: Run, expect fail**

Run: `uv run pytest tests/agent/test_openrouter_client.py -q`
Expected: FAIL (OpenRouterClient/OpenRouterError not defined).

- [ ] **Step 3: Implement the client**

`__init__` calls `assert_model_allowed(model_id)` first. The key is `api_key` if given, else `os.environ["OPENROUTER_API_KEY"]`; if neither, raise `OpenRouterError` (do not read it anywhere else). Build or accept an `httpx.Client` with `timeout=timeout_s`. Keep a `_last_request_monotonic` for pacing. `complete`: enforce the pace (sleep so consecutive requests are at least `min_interval_s` apart, using `time.monotonic`); POST `f"{base_url}/chat/completions"` with headers `{"Authorization": f"Bearer {key}", "Content-Type": "application/json"}` and JSON body `{"model": model_id, "messages": to_openai_messages(messages), "tools": to_openai_tools(tools)}` (omit `tools` if the list is empty). On `httpx.TransportError` or status in `{429, 500, 502, 503, 504}`, retry up to `max_retries` with exponential backoff honouring a `Retry-After` header when present; on exhaustion raise `OpenRouterError` with the last status/text (never the key). On a 2xx, `return parse_completion(response.json())`; if `response.json()` itself raises (non-JSON 2xx), return `ModelTurn(text=None, tool_calls=(), raw={"raw_body": response.text[:2000]}, malformed=True)`. A non-retryable 4xx other than 429 raises `OpenRouterError`.

- [ ] **Step 4: Run to pass; show pacing is real**

Run: `uv run pytest tests/agent/test_openrouter_client.py -q` — expect PASS. Add a test that with `min_interval_s=0.05` two back-to-back `complete` calls take at least 0.05s apart (monkeypatch or measure `time.monotonic`), and a mutation: remove the pace sleep, the pacing test goes red; revert.

- [ ] **Step 5: ruff and commit**

```bash
git add src/lab/agent/openrouter.py tests/agent/test_openrouter_client.py
git commit -m "feat(agent): OpenRouter HTTP client with retry, pacing and malformed handling"
```

---

### Task 3: Budget and allowlist enforced at the client, and the no-live-calls proof

**Files:**
- Modify: `src/lab/agent/openrouter.py`
- Test: `tests/agent/test_openrouter_budget.py`, `tests/runner/test_no_live_calls.py` (extend)

**Interfaces:**
- Consumes: `RequestBudget`, `BudgetExceeded` from `lab.runtime.budget`; the `runtime_db` test fixture (see `tests/conftest.py`).
- Produces: `OpenRouterClient.__init__` gains `budget: RequestBudget | None = None`; when set, `complete` calls `budget.claim(1)` before each HTTP request, so a request that would exceed the daily ceiling raises `BudgetExceeded` before anything is sent.

- [ ] **Step 1: Write the failing budget test (real test runtime DB)**

```python
# tests/agent/test_openrouter_budget.py
import httpx, pytest
from lab.agent.openrouter import OpenRouterClient
from lab.runtime.budget import RequestBudget, BudgetExceeded

FREE = "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_a_request_over_the_daily_ceiling_never_leaves(runtime_db):
    sent = {"n": 0}
    def handler(request):
        sent["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    import psycopg
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        budget = RequestBudget(conn, daily_limit=1)
        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = OpenRouterClient(FREE, api_key="k", http_client=http,
                                  min_interval_s=0.0, budget=budget)
        client.complete([{"role": "user", "content": "x"}], [])   # claims the 1 allowed
        with pytest.raises(BudgetExceeded):
            client.complete([{"role": "user", "content": "x"}], [])
    assert sent["n"] == 1   # the over-budget request was never sent
```

- [ ] **Step 2: Run, expect fail**

Run: `uv run pytest tests/agent/test_openrouter_budget.py -q`
Expected: FAIL (no `budget` parameter / not claimed).

- [ ] **Step 3: Wire the budget**

Add the `budget` parameter; in `complete`, call `self._budget.claim(1)` (when set) immediately before the first HTTP attempt of that call, outside the retry loop (a retry of one logical request does not re-claim). Let `BudgetExceeded` propagate.

- [ ] **Step 4: Extend the no-live-calls proof**

```python
# in tests/runner/test_no_live_calls.py, add:
def test_the_openrouter_client_cannot_reach_the_network_under_the_guard():
    from lab.agent.openrouter import OpenRouterClient, OpenRouterError
    client = OpenRouterClient("nvidia/nemotron-3-ultra-550b-a55b:free",
                              api_key="k", min_interval_s=0.0, max_retries=0)
    with pytest.raises((OpenRouterError, OSError)):
        client.complete([{"role": "user", "content": "x"}], [])
```

(This constructs the real client with its real `httpx.Client`, so the session network guard refuses the connect and the client surfaces it. It proves a stray real client in any later task fails loudly here.)

- [ ] **Step 5: Run both; show the guard test can fail**

Run: `uv run pytest tests/agent/test_openrouter_budget.py tests/runner/test_no_live_calls.py -q` — expect PASS.
Mutation: point the no-live-calls client at `base_url="http://127.0.0.1:1"` (loopback, allowed by the guard) and confirm it still raises `OpenRouterError` (connection refused) rather than reaching a provider; revert to prove the guard, not luck, is what blocks the provider host.

- [ ] **Step 6: ruff and commit**

```bash
git add src/lab/agent/openrouter.py tests/agent/test_openrouter_budget.py tests/runner/test_no_live_calls.py
git commit -m "feat(agent): enforce the daily budget at the OpenRouter client, prove it is unreachable in tests"
```

---

### Task 4: The batch runner over a manifest

**Files:**
- Create: `src/lab/runner/batch.py`
- Modify: `src/lab/runner/__main__.py` (add a `study` subcommand)
- Test: `tests/runner/test_batch.py`

**Interfaces:**
- Consumes: `behavior_matrix`, `Manifest`, `EpisodeSpec` from `lab.protocol.manifest`; `run_spec`, `RunnerConfig`, `EpisodeAlreadyRunError`, `derive_experiment_id`, `experiment_config` from `lab.runner.episode`; a `ModelClient` (scripted in tests, OpenRouter in production).
- Produces: `run_manifest(manifest: Manifest, models: Sequence[ModelClient], config: RunnerConfig, *, experiment_name: str) -> BatchSummary` where `BatchSummary` is a frozen dataclass `(experiment_id, model_id, completed: int, skipped: int, failed: int, switched_to: str | None)`. It runs each spec through `run_spec` with the first model; on an `OpenRouterError` for an episode it advances to the next model in `models` for the rest of the run and records `switched_to`; an episode whose `(experiment_id, episode_id)` already has a completed+scored run is skipped (caught via `EpisodeAlreadyRunError`), which makes the run resumable. One shared `experiment_id` is derived once from the first model's config so a resume re-targets the same experiment.

- [ ] **Step 1: Write the failing test (scripted model, in-process simulator)**

```python
# tests/runner/test_batch.py — uses the shared fixtures in tests/runner/conftest.py
from lab.runner.batch import run_manifest
from lab.protocol.manifest import behavior_matrix
from lab.agent.scripted import ScriptedModel

NAME = "runner-test-behaviour"   # the EXPERIMENT_NAME_PREFIX, so clean_experiments reaps it


def test_a_small_manifest_runs_every_episode_once_and_is_resumable(runner_config):
    manifest = behavior_matrix(reps=1)      # 2 worlds x 2 status x 1 = 4 episodes
    model = ScriptedModel("stable_key")
    first = run_manifest(manifest, [model], runner_config, experiment_name=NAME)
    assert first.completed == 4 and first.skipped == 0 and first.failed == 0
    # Re-running the same manifest skips everything already scored.
    second = run_manifest(manifest, [model], runner_config, experiment_name=NAME)
    assert second.completed == 0 and second.skipped == 4
    assert second.experiment_id == first.experiment_id
```

(`runner_config` is the existing fixture in `tests/runner/conftest.py`: it stands up the in-process public+control simulator and the runtime DB and returns a `RunnerConfig`. The autouse `clean_experiments` fixture there deletes experiments whose name starts with `EXPERIMENT_NAME_PREFIX` (`"runner-test"`), so the experiment name MUST start with that prefix or the rows leak. Do not invent a new fixture.)

- [ ] **Step 2: Run, expect fail**

Run: `uv run pytest tests/runner/test_batch.py -q`
Expected: FAIL (module not defined).

- [ ] **Step 3: Implement `run_manifest` and `BatchSummary`**

Derive the experiment id once from `experiment_name` and `experiment_config(models[0].model_id, config, manifest.sha256())`. Iterate `manifest.episodes`: call `run_spec(spec, current_model, config, manifest_sha256=manifest.sha256(), experiment_name=experiment_name, experiment_id=experiment_id)`. Count a success; on `EpisodeAlreadyRunError` count a skip and continue; on `OpenRouterError` advance `current_model` to the next in `models` (record `switched_to=current_model.model_id`) and retry that one episode once with the new model, counting a fail only if no models remain. Return the `BatchSummary`. Pacing is the client's responsibility, not the batch runner's.

- [ ] **Step 4: Add the `study` CLI subcommand**

In `src/lab/runner/__main__.py`, add `python -m lab.runner study [--reps N] [--seed S] [--experiment-name NAME]`. It calls `force_tracing_off()` (already at import), builds `RunnerConfig.from_env()`, builds the OpenRouter model list (primary + fallbacks from a module constant `STUDY_MODELS` in `batch.py`) each wired to a `RequestBudget` on a runtime connection, builds `behavior_matrix(reps=reps, seed=seed)`, and calls `run_manifest`. Print one JSON line of the `BatchSummary`. Keep `--policy`/`run` exactly as is. Guard the real-model path so it is only reachable through this subcommand, never from `run`.

- [ ] **Step 5: Test the CLI wiring with a stubbed model list**

This test goes in `tests/runner/test_main.py`, which already has the `environ` fixture built from `runner_config`. It stubs both `run_manifest` and the model-builder, so it constructs no real client and writes no rows (the stub returns a summary), which means no experiment-name cleanup concern.

```python
# tests/runner/test_main.py
def test_study_subcommand_uses_run_manifest(monkeypatch, environ):
    import lab.runner.__main__ as m
    from lab.agent.scripted import ScriptedModel
    from lab.runner.batch import BatchSummary
    captured = {}
    def fake_run_manifest(manifest, models, config, *, experiment_name):
        captured["n"] = len(manifest.episodes); captured["name"] = experiment_name
        return BatchSummary("exp-x", models[0].model_id, len(manifest.episodes), 0, 0, None)
    monkeypatch.setattr(m, "run_manifest", fake_run_manifest)
    monkeypatch.setattr(m, "build_study_models", lambda config: [ScriptedModel("stable_key")])
    rc = m.main(["study", "--reps", "1", "--experiment-name", "runner-test-study"], environ)
    assert rc == 0 and captured["n"] == 4 and captured["name"] == "runner-test-study"
```

(Expose `build_study_models(config)` in `__main__` (importing from `batch`) so the test can stub it without constructing a real `OpenRouterClient`; it is the single seam where the real client is built. `main(argv, environ)` takes `environ` as its second positional argument, as the existing `run` tests show.)

- [ ] **Step 6: Run to pass; ruff; commit**

Run: `uv run pytest tests/runner/test_batch.py tests/runner/test_main.py -q && uv run ruff check . && uv run ruff format --check src tests scripts`

```bash
git add src/lab/runner/batch.py src/lab/runner/__main__.py tests/runner/test_batch.py tests/runner/test_main.py
git commit -m "feat(runner): batch-run a manifest, resumable, with model fallback"
```

---

### Task 5: The report generator

**Files:**
- Create: `src/lab/report/__init__.py`, `src/lab/report/behaviour.py`
- Test: `tests/report/test_behaviour.py`

**Interfaces:**
- Consumes: the runtime `results` and `runs` rows (read-only; reporting aggregates already-decided verdicts, it does not decide one); `Verdict` from `lab.oracle.score`.
- Produces: `wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]`; `summarise(rows: Iterable[ResultRow]) -> list[CellSummary]` where a cell is (world, status) and `CellSummary` holds counts per `Verdict`, `n`, the correct-recovery rate over eligible trials, and its Wilson interval; `render_markdown(cells, model_id, experiment_id, manifest_sha256, reps) -> str`; `write_report(experiment_id: str, runtime_url: str, out_dir: Path) -> Path` that reads the rows for one experiment and writes `docs/behaviour-findings.md`, a CSV and a JSONL.

- [ ] **Step 1: Write the failing test for the statistics (no DB)**

```python
# tests/report/test_behaviour.py
from lab.report.behaviour import wilson_interval, summarise, render_markdown, CellSummary


def test_wilson_interval_is_correct_and_bounded():
    lo, hi = wilson_interval(8, 10)
    assert 0.0 <= lo < 0.8 < hi <= 1.0
    assert wilson_interval(0, 0) == (0.0, 0.0)   # no trials -> empty interval, no divide-by-zero


def test_summarise_groups_by_world_and_status_and_counts_verdicts():
    rows = [
        _row("lost_ack", True, "CORRECT", correct_recovery=True, eligible=True),
        _row("lost_ack", True, "DUPLICATE", correct_recovery=False, eligible=True),
        _row("true_fail", False, "MISSED", correct_recovery=False, eligible=True),
    ]
    cells = {(c.world, c.status): c for c in summarise(rows)}
    la = cells[("lost_ack", True)]
    assert la.n == 2 and la.verdicts["CORRECT"] == 1 and la.verdicts["DUPLICATE"] == 1
    assert 0.0 <= la.correct_recovery_rate <= 1.0
```

(Provide a small `_row(...)` helper in the test building the `ResultRow` shape the module defines. Define `ResultRow` as a frozen dataclass in `behaviour.py` with `world, status, verdict, correct_recovery, eligible_recoverable` plus any raw fields the JSONL needs.)

- [ ] **Step 2: Run, expect fail**

Run: `uv run pytest tests/report/test_behaviour.py -q`
Expected: FAIL (module not defined).

- [ ] **Step 3: Implement the statistics and rendering**

`wilson_interval` with the standard formula, returning `(0.0, 0.0)` when `n == 0`. `summarise` groups rows by `(world, status)`, tallies a `Counter` over verdict names, computes correct-recovery rate = (correct_recovery and eligible) count over eligible count (0.0 when no eligible trials) with its Wilson interval. `render_markdown` writes plain prose plus one table per cell: raw counts always shown, the rate with its interval, the model id, the experiment id, the manifest hash, and a sentence that 12 reps is an exploratory sample size. No em dashes. The number is written from the rows, never hard-coded.

- [ ] **Step 4: Implement and test `write_report` against the test runtime DB**

```python
def test_write_report_reads_rows_and_writes_three_files(runtime_db, tmp_path):
    # insert a handful of fake experiments/runs/results rows directly, then:
    from lab.report.behaviour import write_report
    md = write_report("exp-fake", runtime_db["svc"], tmp_path)
    assert md.exists() and md.suffix == ".md"
    assert (tmp_path / "behaviour-findings.csv").exists()
    assert (tmp_path / "behaviour-findings.jsonl").exists()
    assert "exploratory" in md.read_text()
```

(The test seeds `experiments`, `runs` and `results` rows for `exp-fake` itself, so the reader is exercised against real SQL. Reading these runtime rows for a report is allowed; it aggregates verdicts the evaluator already decided.)

- [ ] **Step 5: Run to pass; show a mutation**

Run: `uv run pytest tests/report/test_behaviour.py -q` — expect PASS.
Mutation: make `wilson_interval(0,0)` divide by `n`; the bounded test goes red with `ZeroDivisionError`; revert.

- [ ] **Step 6: ruff and commit**

```bash
git add src/lab/report/ tests/report/test_behaviour.py
git commit -m "feat(report): behaviour-study summary with Wilson intervals"
```

---

### Task 6: The real run (quota spend — gated on explicit user go-ahead)

**Files:**
- Create: `docs/behaviour-findings.md` (written by the report), `docs/behaviour-findings.csv`, `docs/behaviour-findings.jsonl`
- Modify: `docs/decisions.md` (record the no-pilot deviation as a dated decision), `docs/handoff.md` (Plan 3 close)

This task is not written in code; it is the one operational step that spends quota. It runs only after the implementer has reported Tasks 1–5 complete and reviewed, and the user has said to proceed. It is the baseline number in D10.

- [ ] **Step 1: Pre-flight, no quota.** Confirm `.env` has `OPENROUTER_API_KEY`; run `python -m lab.runtime migrate` so the runtime DB is current; bring up the simulator stack the runner points at; re-confirm the pinned model id is still a live `:free` tool-capable endpoint with one free metadata call (`GET /api/v1/models`). State the exact command about to run and that it will send up to 288 real requests.

- [ ] **Step 2: Run the study.** `python -m lab.runner study --reps 12` paced under 20/min. Watch the request counter and the per-episode results land. If the budget or rate limit halts it, the run is resumable (re-run the same command; scored episodes are skipped).

- [ ] **Step 3: Generate the report.** Call `write_report(<experiment_id>, LAB_RUNTIME_URL, Path("docs"))`; read `docs/behaviour-findings.md`.

- [ ] **Step 4: Record and commit.** Add the D-number decision for dropping the pilot, update `docs/handoff.md` with the Plan 3 close and the measured headline, and commit the three report files and the doc updates.

```bash
git add docs/behaviour-findings.* docs/decisions.md docs/handoff.md
git commit -m "docs: behaviour-study results for <model-id>, mode-A baseline"
```

---

## Self-review notes

- Spec coverage: client (Tasks 1–3), budget wiring (Task 3), no-live-calls proof (Task 3), manifest (reused, `behavior_matrix` already exists — no task needed), batch runner (Task 4), report (Task 5), the one quota run and report write-up (Task 6). All spec sections map to a task.
- The `:free` refusal is enforced twice (client `__init__` via `assert_model_allowed`, and the allowlist in budget) — intentional defence in depth, not duplication of logic.
- Fallback-model switching lives in the batch runner (Task 4), pacing in the client (Task 2); each has one home.
- Reporting reads runtime rows, which the evaluator may not do; the plan states why that is allowed (aggregating decided verdicts, not deciding one).
