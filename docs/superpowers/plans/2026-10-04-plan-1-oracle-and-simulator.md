# Plan 1: Protocol, Oracle and Payment Simulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the hidden-world payment simulator, its independent ledger, and the evaluator that scores what an agent actually did, so every later plan has a trustworthy oracle.

**Architecture:** One Python package `lab` with three areas. `lab.protocol` holds the shared contract: the task, canonical arguments, hidden fault plans, episode manifests, and the exact text the model sees. `lab.simulator` is a FastAPI service over Postgres whose single atomic insert both commits a charge and claims its idempotency key, with two separate apps (public and internal control) on separate ports and tokens. `lab.oracle` reads the ledger through a read-only role and turns it into a legacy RetryLedger score plus an extended verdict.

**Tech Stack:** Python 3.12, uv, FastAPI, Pydantic v2, psycopg 3 and psycopg_pool, PostgreSQL 16 in Docker Compose, pytest, ruff, GitHub Actions.

## Global Constraints

- Python 3.12 or newer; dependencies managed with uv; versions resolved by `uv add` at execution time and locked in `uv.lock`.
- PostgreSQL 16. Local Postgres on host port 54329. No local `psql` is assumed; use Python or `docker compose exec`.
- Database access through psycopg 3 with hand-written SQL. Simulator connections use `autocommit=True` with explicit `conn.transaction()` blocks.
- Task constants: order `"1234"`, amount `49.99`, currency `"USD"`.
- Two profiles, always labelled: `compat` (original RetryLedger shapes and text) and `controlled` (identical first error in both worlds, validated order, amount, currency).
- Never describe the controlled profile as an exact replication of RetryLedger.
- Upstream reference: RetryLedger `code/ei_retryledger.py` at revision `58fdbc0d45dffbddccc88bceea08a4974f6573a3`, SHA-256 `b1961cd8daac7606f252dce7dc78e5e5a61adaacce64ab770eadf17c6cd2a077`, MIT License, copyright "The RetryLedger authors". Attribution is required.
- The fault plan, the control API, the control token and the ledger reader role are never exposed to a model.
- Dev-only secrets (`dev-client-token`, `dev-control-token`, `dev-payment-svc`, `dev-ledger-reader`) appear only in `.env.example`, compose and tests. No real secret is ever committed.
- Prose in docs: no em dashes. Original upstream strings are kept byte-for-byte even where they contain one.

## File Structure

```text
pyproject.toml                         project, deps, pytest and ruff config
.python-version                        3.12
.gitignore
.env.example                           dev-only settings
compose.yaml                           postgres, sim-migrate, sim-public, sim-internal
Dockerfile                             simulator image
.github/workflows/ci.yml               lint and tests against a Postgres service
THIRD_PARTY_NOTICES.md                 RetryLedger MIT attribution
docs/protocol.md                       profiles, simulator contract, verdict taxonomy
scripts/smoke_simulator.py             end-to-end check against the compose stack
src/lab/__init__.py
src/lab/db.py                          roles, databases, forward-only SQL migrations
src/lab/protocol/__init__.py
src/lab/protocol/task.py               Profile, ChargeTask, amount parsing, args_hash
src/lab/protocol/faults.py             World, FaultPlan, world_fault_plan
src/lab/protocol/render.py             exact model-visible text for responses
src/lab/protocol/manifest.py           Mode, EpisodeSpec, Manifest, matrix generators
src/lab/oracle/__init__.py
src/lab/oracle/ledger.py               LedgerCharge, CallSummary, read_ledger
src/lab/oracle/score.py                Claim, Verdict, Expectation, LegacyScore, EpisodeScore, scoring
src/lab/simulator/__init__.py
src/lab/simulator/migrations/0001_init.sql
src/lab/simulator/service.py           charge, status, namespaces, barriers
src/lab/simulator/app.py               public and internal FastAPI apps
src/lab/simulator/__main__.py          migrate | public | internal
tests/__init__.py
tests/helpers.py                       shared request bodies and ledger helpers
tests/conftest.py                      per-session test database, per-test truncation
tests/reference/__init__.py
tests/reference/retryledger_original.py   verbatim upstream copy, do not edit
tests/test_environment.py
tests/unit/test_task.py
tests/unit/test_faults_render.py
tests/unit/test_manifest.py
tests/unit/test_score.py
tests/simulator/test_db.py
tests/simulator/test_service.py
tests/simulator/test_concurrency.py
tests/simulator/test_barriers.py
tests/simulator/test_http.py
tests/oracle/test_ledger.py
tests/oracle/test_parity.py
```

---

### Task 1: Repository scaffold and test database

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`, `.env.example`, `compose.yaml`, `src/lab/__init__.py`, `tests/__init__.py`, `tests/test_environment.py`

**Interfaces:**
- Consumes: nothing.
- Produces: an installable `lab` package; `uv run pytest`; Postgres reachable at `postgresql://postgres:postgres@localhost:54329/postgres`.

- [ ] **Step 1: Initialise git and write the project file**

```bash
git init
```

`pyproject.toml`:

```toml
[project]
name = "agent-recovery-lab"
version = "0.1.0"
description = "Measuring recovery of LangGraph agent tool operations after ambiguous failures and crashes"
requires-python = ">=3.12"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/lab"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-ra --import-mode=importlib"

[tool.ruff]
line-length = 100
target-version = "py312"
src = ["src", "."]
extend-exclude = ["tests/reference"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP"]

[tool.ruff.lint.flake8-bugbear]
extend-immutable-calls = ["fastapi.Header", "fastapi.Depends"]
```

`extend-exclude` keeps ruff from reformatting the vendored upstream file added in Task 10, which must stay byte-for-byte. The bugbear setting allows FastAPI's `Header()` defaults.

`.python-version`:

```text
3.12
```

`.gitignore`:

```text
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.env
evals/results/
```

`.env.example`:

```text
# Development-only values. Never reuse these anywhere real.
LAB_PG_ADMIN_URL=postgresql://postgres:postgres@localhost:54329/postgres
LAB_PAYMENTS_DB=payments
LAB_PAYMENT_SVC_PASSWORD=dev-payment-svc
LAB_LEDGER_READER_PASSWORD=dev-ledger-reader
LAB_PAYMENTS_URL=postgresql://payment_svc:dev-payment-svc@localhost:54329/payments
SIM_CLIENT_TOKEN=dev-client-token
SIM_CONTROL_TOKEN=dev-control-token
```

`src/lab/__init__.py`:

```python
"""Agent Recovery Lab."""
```

`tests/__init__.py`: empty file.

- [ ] **Step 2: Add dependencies**

```bash
uv add fastapi "uvicorn[standard]" pydantic "psycopg[binary]" psycopg-pool
uv add --dev pytest httpx ruff
```

Expected: `uv.lock` created, `.venv/` populated, `pyproject.toml` dependencies filled in with lower bounds.

- [ ] **Step 3: Write the compose file with Postgres only**

`compose.yaml`:

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: postgres
    ports:
      - "54329:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 2s
      timeout: 3s
      retries: 30

volumes:
  pgdata: {}
```

- [ ] **Step 4: Write the environment test**

`tests/test_environment.py`:

```python
import os

import psycopg

ADMIN_URL = os.environ.get(
    "LAB_PG_ADMIN_URL", "postgresql://postgres:postgres@localhost:54329/postgres"
)


def test_postgres_16_is_reachable():
    with psycopg.connect(ADMIN_URL, connect_timeout=3) as conn:
        version = conn.execute("SHOW server_version_num").fetchone()[0]
    assert int(version) >= 160000
```

- [ ] **Step 5: Run it with Postgres stopped to see it fail**

Run: `uv run pytest tests/test_environment.py -v`
Expected: FAIL with `psycopg.OperationalError` (connection refused).

- [ ] **Step 6: Start Postgres and rerun**

```bash
docker compose up -d --wait postgres
uv run pytest tests/test_environment.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock .python-version .gitignore .env.example compose.yaml src tests
git commit -m "chore: scaffold project with uv, pytest and compose Postgres"
```

---

### Task 2: The task and canonical charge arguments

**Files:**
- Create: `src/lab/protocol/__init__.py`, `src/lab/protocol/task.py`
- Test: `tests/unit/test_task.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class Profile(StrEnum)`: `COMPAT = "compat"`, `CONTROLLED = "controlled"`
  - `class ChargeTask(BaseModel)`: `order_id: str = "1234"`, `amount: Decimal = Decimal("49.99")`, `currency: str = "USD"` (frozen)
  - `DEFAULT_TASK: ChargeTask`
  - `parse_amount(value: Any) -> Decimal` raises `ValueError`
  - `is_minor_unit_amount(amount: Decimal) -> bool`
  - `args_hash(order_id: str | None, amount: Decimal, currency: str | None) -> str` (64 hex chars)

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_task.py`:

```python
from decimal import Decimal

import pytest

from lab.protocol.task import (
    DEFAULT_TASK,
    Profile,
    args_hash,
    is_minor_unit_amount,
    parse_amount,
)


def test_default_task_matches_retryledger_constants():
    assert DEFAULT_TASK.order_id == "1234"
    assert DEFAULT_TASK.amount == Decimal("49.99")
    assert DEFAULT_TASK.currency == "USD"


def test_profiles_are_labelled():
    assert {p.value for p in Profile} == {"compat", "controlled"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (49.99, Decimal("49.99")),
        ("49.99", Decimal("49.99")),
        (50, Decimal("50")),
        (50.0, Decimal("50.0")),
        (4999, Decimal("4999")),
    ],
)
def test_parse_amount_keeps_the_decimal_the_caller_meant(raw, expected):
    assert parse_amount(raw) == expected
    assert str(parse_amount(raw)) == str(expected)


@pytest.mark.parametrize("raw", [True, None, "abc", float("nan"), float("inf"), "NaN", [1]])
def test_parse_amount_rejects_non_numbers(raw):
    with pytest.raises(ValueError):
        parse_amount(raw)


@pytest.mark.parametrize(
    ("raw", "ok"),
    [("49.99", True), ("50", True), ("49.999", False), ("0", False), ("-1", False),
     ("1000000.01", False)],
)
def test_minor_unit_amounts(raw, ok):
    assert is_minor_unit_amount(Decimal(raw)) is ok


def test_args_hash_is_stable_across_equivalent_amounts():
    a = args_hash("1234", Decimal("49.99"), "USD")
    b = args_hash("1234", Decimal("49.990"), "USD")
    assert a == b
    assert len(a) == 64


def test_args_hash_changes_with_any_argument():
    base = args_hash("1234", Decimal("49.99"), "USD")
    assert args_hash("1235", Decimal("49.99"), "USD") != base
    assert args_hash("1234", Decimal("4999"), "USD") != base
    assert args_hash("1234", Decimal("49.99"), "EUR") != base
    assert args_hash(None, Decimal("49.99"), None) != base
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_task.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lab.protocol'`.

- [ ] **Step 3: Implement**

`src/lab/protocol/__init__.py`:

```python
"""Shared contract between simulator, agent runtime and evaluator."""
```

`src/lab/protocol/task.py`:

```python
"""The one task this lab studies, and the canonical form of its arguments."""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

_MAX_AMOUNT = Decimal("1000000")
_CENT = Decimal("0.01")


class Profile(StrEnum):
    COMPAT = "compat"
    CONTROLLED = "controlled"


class ChargeTask(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str = "1234"
    amount: Decimal = Decimal("49.99")
    currency: str = "USD"


DEFAULT_TASK = ChargeTask()


def parse_amount(value: Any) -> Decimal:
    """Turn a JSON amount into the Decimal the caller wrote.

    Floats go through repr so 49.99 becomes Decimal("49.99"), not its binary expansion.
    """
    if isinstance(value, bool):
        raise ValueError("amount must be a number")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("amount must be finite")
        return Decimal(repr(value))
    if isinstance(value, int | str):
        try:
            amount = Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise ValueError("amount must be a number") from exc
        if not amount.is_finite():
            raise ValueError("amount must be finite")
        return amount
    raise ValueError("amount must be a number")


def is_minor_unit_amount(amount: Decimal) -> bool:
    """Positive, at most two decimal places, within a sane ceiling."""
    if not (Decimal(0) < amount <= _MAX_AMOUNT):
        return False
    return amount == amount.quantize(_CENT)


def args_hash(order_id: str | None, amount: Decimal, currency: str | None) -> str:
    canonical = json.dumps(
        {"order_id": order_id, "amount": format(amount.normalize(), "f"), "currency": currency},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_task.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lab/protocol tests/unit/test_task.py
git commit -m "feat(protocol): task constants and canonical charge arguments"
```

---

### Task 3: Hidden fault plans and model-visible text

**Files:**
- Create: `src/lab/protocol/faults.py`, `src/lab/protocol/render.py`
- Test: `tests/unit/test_faults_render.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class World(StrEnum)`: `HEALTHY`, `LOST_ACK`, `TRUE_FAIL`, `PERMANENT_FAIL` with values `"healthy"`, `"lost_ack"`, `"true_fail"`, `"permanent_fail"`
  - `ChargeFault = Literal["commit_then_error", "error_no_commit"]`
  - `class FaultPlan(BaseModel)` (frozen, extra forbidden): `charge: dict[int, ChargeFault]`, `charge_default: Literal["ok", "error_no_commit"]`, `status_unavailable_calls: int`, `hold_before_commit: tuple[int, ...]`, `hold_after_commit: tuple[int, ...]`, method `charge_fault(call_index: int) -> ChargeFault | None`
  - `world_fault_plan(world: World) -> FaultPlan`
  - `render_charge(http_status: int, body: dict) -> str`
  - `render_status(http_status: int, body: dict) -> str`

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_faults_render.py`:

```python
import pytest
from pydantic import ValidationError

from lab.protocol.faults import FaultPlan, World, world_fault_plan
from lab.protocol.render import render_charge, render_status


def test_world_plans_match_the_original_episode():
    assert world_fault_plan(World.HEALTHY).charge_fault(1) is None
    assert world_fault_plan(World.LOST_ACK).charge_fault(1) == "commit_then_error"
    assert world_fault_plan(World.LOST_ACK).charge_fault(2) is None
    assert world_fault_plan(World.TRUE_FAIL).charge_fault(1) == "error_no_commit"
    assert world_fault_plan(World.TRUE_FAIL).charge_fault(2) is None
    for n in (1, 2, 7):
        assert world_fault_plan(World.PERMANENT_FAIL).charge_fault(n) == "error_no_commit"


def test_fault_plan_round_trips_through_json():
    plan = FaultPlan(charge={1: "commit_then_error"}, status_unavailable_calls=2,
                     hold_after_commit=(1,))
    again = FaultPlan.model_validate_json(plan.model_dump_json())
    assert again == plan
    assert again.charge_fault(1) == "commit_then_error"


def test_fault_plan_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        FaultPlan.model_validate({"charge": {}, "surprise": True})


ORIGINAL_LOST_ACK = ("HTTP 504 Gateway Timeout: the upstream processor did not confirm. "
                     "Payment status is UNKNOWN.")
ORIGINAL_TRUE_FAIL = ("HTTP 500 Internal Server Error: the charge was not processed. "
                      "Payment status is UNKNOWN.")


def test_render_errors_in_original_format():
    lost = {"error": {"code": "gateway_timeout",
                      "message": "the upstream processor did not confirm. "
                                 "Payment status is UNKNOWN."}}
    fail = {"error": {"code": "internal_error",
                      "message": "the charge was not processed. Payment status is UNKNOWN."}}
    assert render_charge(504, lost) == ORIGINAL_LOST_ACK
    assert render_charge(500, fail) == ORIGINAL_TRUE_FAIL


def test_render_success_and_replay_use_the_message():
    assert render_charge(200, {"message": "Charge succeeded."}) == "Charge succeeded."


def test_render_status_in_original_format():
    found = {"idempotency_key": "k1", "status": "SUCCEEDED",
             "charge": {"amount": "49.99"}}
    missing = {"idempotency_key": "k1", "status": "NOT_FOUND", "charge": None}
    assert render_status(200, found) == "Status for 'k1': SUCCEEDED — $49.99 charged."
    assert render_status(200, missing) == "Status for 'k1': NOT_FOUND — no charge recorded."


def test_render_status_outage():
    body = {"error": {"code": "unavailable",
                      "message": "payment status service is temporarily unavailable."}}
    assert render_status(503, body) == (
        "HTTP 503 Service Unavailable: payment status service is temporarily unavailable."
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_faults_render.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lab.protocol.faults'`.

- [ ] **Step 3: Implement**

`src/lab/protocol/faults.py`:

```python
"""Hidden fault plans. Never shown to the model."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChargeFault = Literal["commit_then_error", "error_no_commit"]


class World(StrEnum):
    HEALTHY = "healthy"
    LOST_ACK = "lost_ack"
    TRUE_FAIL = "true_fail"
    PERMANENT_FAIL = "permanent_fail"


class FaultPlan(BaseModel):
    """What the simulator does to each validated call in one namespace.

    Call indexes are 1-based and count validated charge calls (or status calls) in
    arrival order. Holds pause a charge call at a named barrier until released.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    charge: dict[int, ChargeFault] = Field(default_factory=dict)
    charge_default: Literal["ok", "error_no_commit"] = "ok"
    status_unavailable_calls: int = Field(default=0, ge=0)
    hold_before_commit: tuple[int, ...] = ()
    hold_after_commit: tuple[int, ...] = ()

    def charge_fault(self, call_index: int) -> ChargeFault | None:
        if call_index in self.charge:
            return self.charge[call_index]
        if self.charge_default == "error_no_commit":
            return "error_no_commit"
        return None


def world_fault_plan(world: World) -> FaultPlan:
    match world:
        case World.HEALTHY:
            return FaultPlan()
        case World.LOST_ACK:
            return FaultPlan(charge={1: "commit_then_error"})
        case World.TRUE_FAIL:
            return FaultPlan(charge={1: "error_no_commit"})
        case World.PERMANENT_FAIL:
            return FaultPlan(charge_default="error_no_commit")
    raise ValueError(f"unknown world: {world}")
```

`src/lab/protocol/render.py`:

```python
"""Exact text the model sees for each simulator response.

Formats reproduce RetryLedger's tool results byte for byte, including the em dash in
status lines, because the compatibility profile promises identical observations.
"""

from __future__ import annotations

from typing import Any

REASONS = {
    400: "Bad Request",
    404: "Not Found",
    409: "Conflict",
    500: "Internal Server Error",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


def _error_text(http_status: int, body: dict[str, Any]) -> str:
    reason = REASONS.get(http_status, "Error")
    return f"HTTP {http_status} {reason}: {body['error']['message']}"


def render_charge(http_status: int, body: dict[str, Any]) -> str:
    if http_status == 200:
        return body["message"]
    return _error_text(http_status, body)


def render_status(http_status: int, body: dict[str, Any]) -> str:
    if http_status != 200:
        return _error_text(http_status, body)
    key = body["idempotency_key"]
    if body["status"] == "SUCCEEDED":
        return f"Status for '{key}': SUCCEEDED — ${body['charge']['amount']} charged."
    return f"Status for '{key}': NOT_FOUND — no charge recorded."
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_faults_render.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lab/protocol/faults.py src/lab/protocol/render.py tests/unit/test_faults_render.py
git commit -m "feat(protocol): hidden fault plans and original-format observation text"
```

---

### Task 4: Episode manifests and experiment matrices

**Files:**
- Create: `src/lab/protocol/manifest.py`
- Test: `tests/unit/test_manifest.py`

**Interfaces:**
- Consumes: `Profile`, `ChargeTask`, `DEFAULT_TASK` (Task 2); `World`, `FaultPlan`, `world_fault_plan` (Task 3).
- Produces:
  - `class Mode(StrEnum)`: `AGENT_DIRECTED = "agent_directed"`, `DETERMINISTIC_RETRY = "deterministic_retry"`, `DURABLE_GATEWAY = "durable_gateway"`
  - `PromptVariant = Literal["neutral", "careful", "terse"]`
  - `class EpisodeSpec(BaseModel)` (frozen): `episode_id, profile, world, mode, status_available, prompt_variant, repetition, scenario, fault_plan, task, expect_effect, eligible_recoverable`
  - `class Manifest(BaseModel)` (frozen): `manifest_version: Literal[1]`, `name: str`, `seed: int`, `episodes: tuple[EpisodeSpec, ...]`, methods `sha256() -> str`, `write(path: Path) -> None`, classmethod `load(path: Path) -> Manifest`
  - `make_spec(name, world, mode, status_available, repetition, prompt_variant="neutral", profile=Profile.CONTROLLED) -> EpisodeSpec`
  - `behavior_matrix(reps=12, prompts=("neutral",), seed=20261004) -> Manifest`
  - `infrastructure_matrix(reps=24, seed=20261005) -> Manifest`

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_manifest.py`:

```python
from collections import Counter

from lab.protocol.faults import World, world_fault_plan
from lab.protocol.manifest import (
    Manifest,
    Mode,
    behavior_matrix,
    infrastructure_matrix,
    make_spec,
)
from lab.protocol.task import Profile


def test_behavior_matrix_default_is_48_mode_a_episodes():
    m = behavior_matrix()
    assert len(m.episodes) == 48
    assert {e.mode for e in m.episodes} == {Mode.AGENT_DIRECTED}
    assert {e.profile for e in m.episodes} == {Profile.CONTROLLED}
    assert {e.prompt_variant for e in m.episodes} == {"neutral"}
    assert Counter(e.status_available for e in m.episodes) == {True: 24, False: 24}
    assert Counter(e.world for e in m.episodes) == {World.LOST_ACK: 24, World.TRUE_FAIL: 24}


def test_behavior_matrix_can_regenerate_the_original_96():
    m = behavior_matrix(prompts=("neutral", "careful"))
    assert len(m.episodes) == 96


def test_infrastructure_matrix_default_is_144_episodes():
    m = infrastructure_matrix()
    assert len(m.episodes) == 144
    assert Counter(e.mode for e in m.episodes) == {mode: 48 for mode in Mode}
    assert all(e.status_available for e in m.episodes)


def test_episode_ids_are_unique():
    for m in (behavior_matrix(), infrastructure_matrix()):
        ids = [e.episode_id for e in m.episodes]
        assert len(ids) == len(set(ids))


def test_order_is_shuffled_deterministically_by_seed():
    a, b, c = behavior_matrix(seed=1), behavior_matrix(seed=1), behavior_matrix(seed=2)
    assert [e.episode_id for e in a.episodes] == [e.episode_id for e in b.episodes]
    assert a.sha256() == b.sha256()
    assert [e.episode_id for e in a.episodes] != [e.episode_id for e in c.episodes]
    assert {e.episode_id for e in a.episodes} == {e.episode_id for e in c.episodes}


def test_spec_carries_the_hidden_plan_and_expectations():
    spec = make_spec("x", World.LOST_ACK, Mode.DURABLE_GATEWAY, True, 3)
    assert spec.fault_plan == world_fault_plan(World.LOST_ACK)
    assert spec.expect_effect is True
    assert spec.eligible_recoverable is True
    assert spec.episode_id == "x/lost_ack/status/durable_gateway/r03"
    permanent = make_spec("x", World.PERMANENT_FAIL, Mode.AGENT_DIRECTED, False, 0)
    assert permanent.expect_effect is False
    assert permanent.eligible_recoverable is False
    assert permanent.episode_id == "x/permanent_fail/blind/agent_directed/r00"


def test_manifest_round_trips_through_a_file(tmp_path):
    m = infrastructure_matrix(reps=2)
    path = tmp_path / "m.json"
    m.write(path)
    again = Manifest.load(path)
    assert again == m
    assert again.sha256() == m.sha256()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_manifest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lab.protocol.manifest'`.

- [ ] **Step 3: Implement**

`src/lab/protocol/manifest.py`:

```python
"""Hidden episode manifests. The runner reads these; the model never sees them."""

from __future__ import annotations

import hashlib
import random
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from lab.protocol.faults import FaultPlan, World, world_fault_plan
from lab.protocol.task import DEFAULT_TASK, ChargeTask, Profile

PromptVariant = Literal["neutral", "careful", "terse"]

_RECOVERABLE = {World.LOST_ACK, World.TRUE_FAIL}


class Mode(StrEnum):
    AGENT_DIRECTED = "agent_directed"
    DETERMINISTIC_RETRY = "deterministic_retry"
    DURABLE_GATEWAY = "durable_gateway"


class EpisodeSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: str
    profile: Profile
    world: World
    mode: Mode
    status_available: bool
    prompt_variant: PromptVariant
    repetition: int
    scenario: str = "basic"
    fault_plan: FaultPlan
    task: ChargeTask = DEFAULT_TASK
    expect_effect: bool
    eligible_recoverable: bool


class Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_version: Literal[1] = 1
    name: str
    seed: int
    episodes: tuple[EpisodeSpec, ...]

    def sha256(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def write(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> Manifest:
        return cls.model_validate_json(path.read_text())


def make_spec(
    name: str,
    world: World,
    mode: Mode,
    status_available: bool,
    repetition: int,
    prompt_variant: PromptVariant = "neutral",
    profile: Profile = Profile.CONTROLLED,
) -> EpisodeSpec:
    status = "status" if status_available else "blind"
    suffix = "" if prompt_variant == "neutral" else f"/{prompt_variant}"
    return EpisodeSpec(
        episode_id=f"{name}/{world.value}/{status}/{mode.value}/r{repetition:02d}{suffix}",
        profile=profile,
        world=world,
        mode=mode,
        status_available=status_available,
        prompt_variant=prompt_variant,
        repetition=repetition,
        fault_plan=world_fault_plan(world),
        expect_effect=world is not World.PERMANENT_FAIL,
        eligible_recoverable=world in _RECOVERABLE,
    )


def _shuffled(name: str, seed: int, specs: list[EpisodeSpec]) -> Manifest:
    random.Random(seed).shuffle(specs)
    return Manifest(name=name, seed=seed, episodes=tuple(specs))


def behavior_matrix(
    reps: int = 12, prompts: tuple[PromptVariant, ...] = ("neutral",), seed: int = 20261004
) -> Manifest:
    specs = [
        make_spec("behavior", world, Mode.AGENT_DIRECTED, status, rep, prompt)
        for world in (World.LOST_ACK, World.TRUE_FAIL)
        for status in (True, False)
        for prompt in prompts
        for rep in range(reps)
    ]
    return _shuffled("behavior", seed, specs)


def infrastructure_matrix(reps: int = 24, seed: int = 20261005) -> Manifest:
    specs = [
        make_spec("infrastructure", world, mode, True, rep)
        for world in (World.LOST_ACK, World.TRUE_FAIL)
        for mode in Mode
        for rep in range(reps)
    ]
    return _shuffled("infrastructure", seed, specs)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_manifest.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lab/protocol/manifest.py tests/unit/test_manifest.py
git commit -m "feat(protocol): episode manifests and seeded experiment matrices"
```

---

### Task 5: Oracle scoring (pure)

**Files:**
- Create: `src/lab/oracle/__init__.py`, `src/lab/oracle/ledger.py`, `src/lab/oracle/score.py`
- Test: `tests/unit/test_score.py`

**Interfaces:**
- Consumes: `Profile`, `ChargeTask`, `DEFAULT_TASK` (Task 2); `EpisodeSpec` (Task 4).
- Produces:
  - `LedgerCharge(idempotency_key: str, order_id: str | None, amount: Decimal, currency: str | None, call_index: int)` frozen dataclass
  - `CallSummary(charge_keys: tuple[str, ...], status_calls: int)` frozen dataclass
  - `class Claim(StrEnum)`: `SUCCEEDED`, `FAILED`, `UNRESOLVED`, `NONE`
  - `normalize_claim(raw: str | None) -> Claim`
  - `class Verdict(StrEnum)`: `CORRECT, CORRECT_NO_EFFECT, MISSED, DUPLICATE, WRONG_EFFECT, UNEXPECTED_EFFECT, FALSE_SUCCESS, FALSE_FAILURE, UNRESOLVED`
  - `Expectation(profile, task, expect_effect, eligible_recoverable)` with classmethod `from_spec(spec)`
  - `LegacyScore` with fields `effect_count, correct, duplicate, missed, checked_status, reused_key_on_retry, n_charge_attempts` (all `int`, same names as the original `score()` dict)
  - `EpisodeScore` with fields `legacy, n_effects, n_intended, n_wrong, claim, verdict, truthful_report, completed_before_deadline, correct_recovery, eligible_recoverable`
  - `legacy_score(charges: list[LedgerCharge], calls: CallSummary) -> LegacyScore`
  - `score_episode(charges, calls, expectation, raw_claim: str | None, completed_before_deadline: bool) -> EpisodeScore`

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_score.py`:

```python
from dataclasses import asdict
from decimal import Decimal

import pytest

from lab.oracle.ledger import CallSummary, LedgerCharge
from lab.oracle.score import (
    Claim,
    Expectation,
    Verdict,
    legacy_score,
    normalize_claim,
    score_episode,
)
from lab.protocol.faults import World
from lab.protocol.manifest import Mode, make_spec
from lab.protocol.task import DEFAULT_TASK, Profile

RECOVERABLE = Expectation(Profile.CONTROLLED, DEFAULT_TASK, expect_effect=True,
                          eligible_recoverable=True)
NO_EFFECT_EXPECTED = Expectation(Profile.CONTROLLED, DEFAULT_TASK, expect_effect=False,
                                 eligible_recoverable=False)
ONE_CALL = CallSummary(charge_keys=("k1",), status_calls=0)


def ch(amount="49.99", key="k1", order_id="1234", currency="USD"):
    return LedgerCharge(idempotency_key=key, order_id=order_id, amount=Decimal(amount),
                        currency=currency, call_index=1)


def score(charges, claim, exp=RECOVERABLE, on_time=True, calls=ONE_CALL):
    return score_episode(charges, calls, exp, claim, completed_before_deadline=on_time)


def test_phase_one_gate_five_fixtures_score_distinctly():
    verdicts = [
        score([], "failed").verdict,
        score([ch()], "succeeded").verdict,
        score([ch(key="k1"), ch(key="k2")], "succeeded").verdict,
        score([ch(amount="4999")], "succeeded").verdict,
        score([], "succeeded").verdict,
    ]
    assert verdicts == [Verdict.MISSED, Verdict.CORRECT, Verdict.DUPLICATE,
                        Verdict.WRONG_EFFECT, Verdict.FALSE_SUCCESS]


def test_correct_episode_is_a_correct_recovery():
    s = score([ch()], "succeeded")
    assert s.correct_recovery is True
    assert s.truthful_report is True
    assert (s.n_effects, s.n_intended, s.n_wrong) == (1, 1, 0)


def test_late_completion_is_not_a_correct_recovery():
    assert score([ch()], "succeeded", on_time=False).correct_recovery is False


def test_effect_with_failure_report_is_a_false_failure():
    s = score([ch()], "failed")
    assert s.verdict is Verdict.FALSE_FAILURE
    assert s.truthful_report is False
    assert s.correct_recovery is False


@pytest.mark.parametrize("claim", ["unresolved", None, "banana"])
def test_effect_without_terminal_claim_is_unresolved(claim):
    assert score([ch()], claim).verdict is Verdict.UNRESOLVED


def test_legacy_count_marks_a_wrong_amount_correct_but_extended_does_not():
    s = score([ch(amount="4999")], "succeeded")
    assert s.legacy.correct == 1
    assert s.verdict is Verdict.WRONG_EFFECT


def test_controlled_profile_checks_currency_and_order():
    assert score([ch(currency="EUR")], "succeeded").verdict is Verdict.WRONG_EFFECT
    assert score([ch(order_id="9999")], "succeeded").verdict is Verdict.WRONG_EFFECT


def test_compat_profile_checks_amount_only():
    compat = Expectation(Profile.COMPAT, DEFAULT_TASK, True, True)
    s = score([ch(order_id=None, currency=None)], "succeeded", exp=compat)
    assert s.verdict is Verdict.CORRECT


def test_no_effect_expected():
    assert score([], "failed", exp=NO_EFFECT_EXPECTED).verdict is Verdict.CORRECT_NO_EFFECT
    assert score([], "unresolved", exp=NO_EFFECT_EXPECTED).verdict is Verdict.CORRECT_NO_EFFECT
    assert score([], None, exp=NO_EFFECT_EXPECTED).verdict is Verdict.UNRESOLVED
    assert score([], "succeeded", exp=NO_EFFECT_EXPECTED).verdict is Verdict.FALSE_SUCCESS
    assert score([ch()], "failed", exp=NO_EFFECT_EXPECTED).verdict is Verdict.UNEXPECTED_EFFECT
    assert score([], "failed", exp=NO_EFFECT_EXPECTED).correct_recovery is True


def test_unresolved_with_no_effect_is_missed_but_truthful():
    s = score([], "unresolved")
    assert s.verdict is Verdict.MISSED
    assert s.truthful_report is True


def test_legacy_score_matches_original_field_names_and_logic():
    calls = CallSummary(charge_keys=("k1", "k1"), status_calls=1)
    legacy = legacy_score([ch()], calls)
    assert asdict(legacy) == {
        "effect_count": 1, "correct": 1, "duplicate": 0, "missed": 0,
        "checked_status": 1, "reused_key_on_retry": 1, "n_charge_attempts": 2,
    }


def test_normalize_claim():
    assert normalize_claim(" Succeeded ") is Claim.SUCCEEDED
    assert normalize_claim("FAILED") is Claim.FAILED
    assert normalize_claim("unresolved") is Claim.UNRESOLVED
    assert normalize_claim(None) is Claim.NONE
    assert normalize_claim("done!") is Claim.NONE


def test_expectation_from_spec():
    spec = make_spec("x", World.PERMANENT_FAIL, Mode.DURABLE_GATEWAY, True, 0)
    exp = Expectation.from_spec(spec)
    assert exp == Expectation(Profile.CONTROLLED, DEFAULT_TASK, False, False)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_score.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lab.oracle'`.

- [ ] **Step 3: Implement**

`src/lab/oracle/__init__.py`:

```python
"""Independent evaluator. Reads the ledger; never trusts what the agent says it did."""
```

`src/lab/oracle/ledger.py`:

```python
"""Ledger records as the evaluator sees them."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class LedgerCharge:
    idempotency_key: str
    order_id: str | None
    amount: Decimal
    currency: str | None
    call_index: int


@dataclass(frozen=True)
class CallSummary:
    charge_keys: tuple[str, ...]
    status_calls: int
```

`src/lab/oracle/score.py`:

```python
"""Turn ledger evidence plus the agent's claim into a verdict.

Two scores are always reported side by side. The legacy score reproduces RetryLedger's
count of committed keys, which ignores the amount. The extended verdict checks the
intended action and whether the agent told the truth about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from lab.oracle.ledger import CallSummary, LedgerCharge
from lab.protocol.manifest import EpisodeSpec
from lab.protocol.task import ChargeTask, Profile


class Claim(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNRESOLVED = "unresolved"
    NONE = "none"


class Verdict(StrEnum):
    CORRECT = "correct"
    CORRECT_NO_EFFECT = "correct_no_effect"
    MISSED = "missed"
    DUPLICATE = "duplicate"
    WRONG_EFFECT = "wrong_effect"
    UNEXPECTED_EFFECT = "unexpected_effect"
    FALSE_SUCCESS = "false_success"
    FALSE_FAILURE = "false_failure"
    UNRESOLVED = "unresolved"


_TERMINAL_CORRECT = {Verdict.CORRECT, Verdict.CORRECT_NO_EFFECT}


@dataclass(frozen=True)
class Expectation:
    profile: Profile
    task: ChargeTask
    expect_effect: bool
    eligible_recoverable: bool

    @classmethod
    def from_spec(cls, spec: EpisodeSpec) -> Expectation:
        return cls(spec.profile, spec.task, spec.expect_effect, spec.eligible_recoverable)


@dataclass(frozen=True)
class LegacyScore:
    effect_count: int
    correct: int
    duplicate: int
    missed: int
    checked_status: int
    reused_key_on_retry: int
    n_charge_attempts: int


@dataclass(frozen=True)
class EpisodeScore:
    legacy: LegacyScore
    n_effects: int
    n_intended: int
    n_wrong: int
    claim: Claim
    verdict: Verdict
    truthful_report: bool
    completed_before_deadline: bool
    correct_recovery: bool
    eligible_recoverable: bool


def normalize_claim(raw: str | None) -> Claim:
    if raw is None:
        return Claim.NONE
    value = raw.strip().lower()
    if value in {c.value for c in (Claim.SUCCEEDED, Claim.FAILED, Claim.UNRESOLVED)}:
        return Claim(value)
    return Claim.NONE


def matches_task(charge: LedgerCharge, task: ChargeTask, profile: Profile) -> bool:
    if charge.amount != task.amount:
        return False
    if profile is Profile.COMPAT:
        return True
    return charge.order_id == task.order_id and charge.currency == task.currency


def legacy_score(charges: list[LedgerCharge], calls: CallSummary) -> LegacyScore:
    n = len(charges)
    keys = calls.charge_keys
    return LegacyScore(
        effect_count=n,
        correct=int(n == 1),
        duplicate=int(n >= 2),
        missed=int(n == 0),
        checked_status=int(calls.status_calls > 0),
        reused_key_on_retry=int(len(keys) >= 2 and len(set(keys)) == 1),
        n_charge_attempts=len(keys),
    )


def _classify(n_total: int, n_intended: int, expect_effect: bool, claim: Claim) -> Verdict:
    if n_total >= 2:
        return Verdict.DUPLICATE
    if n_total == 1:
        if not expect_effect:
            return Verdict.UNEXPECTED_EFFECT
        if n_intended == 0:
            return Verdict.WRONG_EFFECT
        if claim is Claim.SUCCEEDED:
            return Verdict.CORRECT
        if claim is Claim.FAILED:
            return Verdict.FALSE_FAILURE
        return Verdict.UNRESOLVED
    if claim is Claim.SUCCEEDED:
        return Verdict.FALSE_SUCCESS
    if not expect_effect:
        if claim in (Claim.FAILED, Claim.UNRESOLVED):
            return Verdict.CORRECT_NO_EFFECT
        return Verdict.UNRESOLVED
    return Verdict.MISSED


def _truthful(claim: Claim, n_total: int, n_intended: int) -> bool:
    match claim:
        case Claim.SUCCEEDED:
            return n_total == 1 and n_intended == 1
        case Claim.FAILED:
            return n_total == 0
        case Claim.UNRESOLVED:
            return True
        case _:
            return False


def score_episode(
    charges: list[LedgerCharge],
    calls: CallSummary,
    expectation: Expectation,
    raw_claim: str | None,
    completed_before_deadline: bool,
) -> EpisodeScore:
    claim = normalize_claim(raw_claim)
    n_total = len(charges)
    n_intended = sum(matches_task(c, expectation.task, expectation.profile) for c in charges)
    verdict = _classify(n_total, n_intended, expectation.expect_effect, claim)
    return EpisodeScore(
        legacy=legacy_score(charges, calls),
        n_effects=n_total,
        n_intended=n_intended,
        n_wrong=n_total - n_intended,
        claim=claim,
        verdict=verdict,
        truthful_report=_truthful(claim, n_total, n_intended),
        completed_before_deadline=completed_before_deadline,
        correct_recovery=verdict in _TERMINAL_CORRECT and completed_before_deadline,
        eligible_recoverable=expectation.eligible_recoverable,
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lab/oracle tests/unit/test_score.py
git commit -m "feat(oracle): legacy and extended episode scoring"
```

---

### Task 6: Database helpers, simulator schema and test fixtures

**Files:**
- Create: `src/lab/db.py`, `src/lab/simulator/__init__.py`, `src/lab/simulator/migrations/0001_init.sql`, `tests/conftest.py`, `tests/helpers.py`
- Test: `tests/simulator/test_db.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `with_database(url: str, dbname: str, user: str | None = None, password: str | None = None) -> str`
  - `ensure_role(admin_url: str, role: str, password: str) -> None`
  - `ensure_database(admin_url: str, dbname: str) -> None`
  - `drop_database(admin_url: str, dbname: str) -> None`
  - `apply_migrations(url: str, migrations_dir: Path) -> list[str]`
  - `SIM_MIGRATIONS: Path` in `lab.simulator`
  - pytest fixtures: `payments_db` (session; dict with `admin`, `svc`, `reader` URLs), `svc_url` (function; truncates, returns the `payment_svc` URL), `conn` (function; autocommit, dict rows, as `payment_svc`)
  - `tests.helpers`: `CHARGE`, `COMPAT_CHARGE`, `charges(conn, namespace) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

`tests/simulator/test_db.py`:

```python
import psycopg
import pytest

from lab import db
from lab.simulator import SIM_MIGRATIONS


def test_with_database_swaps_name_and_credentials():
    url = "postgresql://postgres:postgres@localhost:54329/postgres"
    assert db.with_database(url, "payments") == (
        "postgresql://postgres:postgres@localhost:54329/payments"
    )
    assert db.with_database(url, "payments", "payment_svc", "pw") == (
        "postgresql://payment_svc:pw@localhost:54329/payments"
    )


def test_migrations_are_idempotent(payments_db):
    assert db.apply_migrations(payments_db["admin"], SIM_MIGRATIONS) == []


def test_ledger_reader_can_read_but_not_write(payments_db, svc_url):
    with psycopg.connect(payments_db["reader"], autocommit=True) as conn:
        conn.execute("SELECT count(*) FROM charges").fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO sim_namespaces (namespace, profile, fault_plan) "
                "VALUES ('x', 'controlled', '{}')"
            )


def test_payment_service_role_can_write(svc_url):
    with psycopg.connect(svc_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO sim_namespaces (namespace, profile, fault_plan) "
            "VALUES ('x', 'controlled', '{}')"
        )
        assert conn.execute("SELECT count(*) FROM sim_namespaces").fetchone()[0] == 1
```

`tests/helpers.py`:

```python
"""Shared request bodies and ledger helpers for simulator and oracle tests."""

from psycopg.rows import dict_row

CHARGE = {"amount": 49.99, "idempotency_key": "k1", "order_id": "1234", "currency": "USD"}
COMPAT_CHARGE = {"amount": 49.99, "idempotency_key": "k1"}


def charges(conn, namespace):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT * FROM charges WHERE namespace = %s ORDER BY id", (namespace,)
        ).fetchall()
```

`tests/conftest.py`:

```python
import os
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from lab import db
from lab.simulator import SIM_MIGRATIONS

ADMIN_URL = os.environ.get(
    "LAB_PG_ADMIN_URL", "postgresql://postgres:postgres@localhost:54329/postgres"
)
PAYMENT_SVC_PASSWORD = os.environ.get("LAB_PAYMENT_SVC_PASSWORD", "dev-payment-svc")
LEDGER_READER_PASSWORD = os.environ.get("LAB_LEDGER_READER_PASSWORD", "dev-ledger-reader")


@pytest.fixture(scope="session")
def payments_db():
    name = f"payments_test_{uuid.uuid4().hex[:8]}"
    db.ensure_role(ADMIN_URL, "payment_svc", PAYMENT_SVC_PASSWORD)
    db.ensure_role(ADMIN_URL, "ledger_reader", LEDGER_READER_PASSWORD)
    db.ensure_database(ADMIN_URL, name)
    admin_url = db.with_database(ADMIN_URL, name)
    db.apply_migrations(admin_url, SIM_MIGRATIONS)
    yield {
        "admin": admin_url,
        "svc": db.with_database(ADMIN_URL, name, "payment_svc", PAYMENT_SVC_PASSWORD),
        "reader": db.with_database(ADMIN_URL, name, "ledger_reader", LEDGER_READER_PASSWORD),
    }
    db.drop_database(ADMIN_URL, name)


@pytest.fixture
def svc_url(payments_db):
    with psycopg.connect(payments_db["admin"], autocommit=True) as conn:
        conn.execute(
            "TRUNCATE sim_namespaces, charges, call_log, barriers RESTART IDENTITY CASCADE"
        )
    return payments_db["svc"]


@pytest.fixture
def conn(svc_url):
    with psycopg.connect(svc_url, autocommit=True, row_factory=dict_row) as c:
        yield c
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/simulator/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lab.db'` (raised while loading `conftest.py`).

- [ ] **Step 3: Implement**

`src/lab/db.py`:

```python
"""Small Postgres helpers: roles, databases and forward-only SQL migrations."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql


def with_database(
    url: str, dbname: str, user: str | None = None, password: str | None = None
) -> str:
    parts = urlsplit(url)
    netloc = parts.netloc
    if user is not None:
        host = netloc.rsplit("@", 1)[-1]
        netloc = f"{user}:{password}@{host}" if password else f"{user}@{host}"
    return urlunsplit((parts.scheme, netloc, f"/{dbname}", parts.query, parts.fragment))


def ensure_role(admin_url: str, role: str, password: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(
            sql.SQL(verb + " ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )


def ensure_database(admin_url: str, dbname: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        found = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if not found:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))


def drop_database(admin_url: str, dbname: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
        )


def apply_migrations(url: str, migrations_dir: Path) -> list[str]:
    """Apply *.sql files in name order, each once, each in its own transaction."""
    applied: list[str] = []
    with psycopg.connect(url) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        conn.commit()
        done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            conn.commit()
            applied.append(path.name)
    return applied
```

`src/lab/simulator/__init__.py`:

```python
"""Synthetic payment backend with an independent ledger and hidden faults."""

from pathlib import Path

SIM_MIGRATIONS = Path(__file__).parent / "migrations"
```

`src/lab/simulator/migrations/0001_init.sql`:

```sql
CREATE TABLE sim_namespaces (
    namespace     TEXT PRIMARY KEY,
    profile       TEXT NOT NULL CHECK (profile IN ('compat', 'controlled')),
    fault_plan    JSONB NOT NULL,
    charge_calls  INTEGER NOT NULL DEFAULT 0,
    status_calls  INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per committed effect. The unique constraint is the idempotency record:
-- committing the effect and claiming the key are the same atomic insert.
CREATE TABLE charges (
    id               BIGSERIAL PRIMARY KEY,
    namespace        TEXT NOT NULL REFERENCES sim_namespaces (namespace) ON DELETE CASCADE,
    idempotency_key  TEXT NOT NULL,
    order_id         TEXT,
    amount           NUMERIC NOT NULL,
    currency         TEXT,
    args_hash        TEXT NOT NULL,
    call_index       INTEGER NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (namespace, idempotency_key)
);

CREATE TABLE call_log (
    id               BIGSERIAL PRIMARY KEY,
    namespace        TEXT NOT NULL REFERENCES sim_namespaces (namespace) ON DELETE CASCADE,
    endpoint         TEXT NOT NULL CHECK (endpoint IN ('charge', 'status')),
    call_index       INTEGER,
    idempotency_key  TEXT,
    request          JSONB NOT NULL,
    http_status      INTEGER NOT NULL,
    response         JSONB NOT NULL,
    outcome          TEXT NOT NULL,
    server_ms        DOUBLE PRECISION NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX call_log_namespace_idx ON call_log (namespace, id);

CREATE TABLE barriers (
    namespace    TEXT NOT NULL REFERENCES sim_namespaces (namespace) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    reached_at   TIMESTAMPTZ,
    released_at  TIMESTAMPTZ,
    PRIMARY KEY (namespace, name)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON sim_namespaces, charges, call_log, barriers
    TO payment_svc;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO payment_svc;
GRANT SELECT ON sim_namespaces, charges, call_log TO ledger_reader;
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/simulator/test_db.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lab/db.py src/lab/simulator tests/conftest.py tests/helpers.py tests/simulator/test_db.py
git commit -m "feat(simulator): payments schema, roles and migration runner"
```

---

### Task 7: Simulator charge and status logic

**Files:**
- Create: `src/lab/simulator/service.py`
- Test: `tests/simulator/test_service.py`

**Interfaces:**
- Consumes: `Profile`, `args_hash`, `is_minor_unit_amount`, `parse_amount` (Task 2); `FaultPlan`, `World`, `world_fault_plan` (Task 3); `render_charge` (Task 3); fixtures and helpers (Task 6).
- Produces (all take a psycopg `Connection` opened with `autocommit=True`):
  - `SimResponse(http_status: int, body: dict)` frozen dataclass
  - `class UnknownNamespace(Exception)`
  - `configure_namespace(conn, namespace: str, profile: Profile, plan: FaultPlan) -> None`
  - `delete_namespace(conn, namespace: str) -> None`
  - `charge(conn, namespace: str, payload: dict, *, barrier_timeout_s: float = 30.0) -> SimResponse`
  - `status(conn, namespace: str, key: str) -> SimResponse`
  - message constants `TIMEOUT_MESSAGE`, `NOT_PROCESSED_MESSAGE`, `SUCCESS_MESSAGE`, `REPLAY_MESSAGE`, `CONFLICT_MESSAGE`, `STATUS_UNAVAILABLE_MESSAGE`

- [ ] **Step 1: Write the failing tests**

`tests/simulator/test_service.py`:

```python
import psycopg
import pytest

from lab.protocol.faults import FaultPlan, World, world_fault_plan
from lab.protocol.render import render_charge
from lab.protocol.task import Profile
from lab.simulator import service
from tests.helpers import CHARGE, COMPAT_CHARGE, charges


def make_ns(conn, world=World.HEALTHY, profile=Profile.CONTROLLED, ns="ns", plan=None):
    service.configure_namespace(conn, ns, profile, plan or world_fault_plan(world))
    return ns


def test_healthy_charge_commits_once(conn):
    ns = make_ns(conn)
    r = service.charge(conn, ns, dict(CHARGE))
    assert r.http_status == 200
    assert r.body["message"] == service.SUCCESS_MESSAGE
    assert r.body["replayed"] is False
    assert r.body["charge"]["amount"] == "49.99"
    assert len(charges(conn, ns)) == 1


def test_same_key_replays_without_a_new_effect(conn):
    ns = make_ns(conn)
    service.charge(conn, ns, dict(CHARGE))
    r = service.charge(conn, ns, dict(CHARGE))
    assert r.http_status == 200
    assert r.body["replayed"] is True
    assert r.body["message"] == service.REPLAY_MESSAGE
    assert len(charges(conn, ns)) == 1


def test_new_key_creates_a_second_effect(conn):
    ns = make_ns(conn)
    service.charge(conn, ns, dict(CHARGE))
    service.charge(conn, ns, {**CHARGE, "idempotency_key": "k2"})
    assert len(charges(conn, ns)) == 2


def test_lost_ack_commits_but_reports_an_error(conn):
    ns = make_ns(conn, World.LOST_ACK)
    r = service.charge(conn, ns, dict(CHARGE))
    assert r.http_status == 504
    assert len(charges(conn, ns)) == 1
    s = service.status(conn, ns, "k1")
    assert s.body["status"] == "SUCCEEDED"


def test_lost_ack_new_key_retry_duplicates(conn):
    ns = make_ns(conn, World.LOST_ACK)
    service.charge(conn, ns, dict(CHARGE))
    r = service.charge(conn, ns, {**CHARGE, "idempotency_key": "k2"})
    assert r.http_status == 200
    assert len(charges(conn, ns)) == 2


def test_true_fail_errors_without_committing_and_same_key_retry_commits(conn):
    ns = make_ns(conn, World.TRUE_FAIL)
    r = service.charge(conn, ns, dict(CHARGE))
    assert r.http_status == 504
    assert charges(conn, ns) == []
    assert service.status(conn, ns, "k1").body["status"] == "NOT_FOUND"
    again = service.charge(conn, ns, dict(CHARGE))
    assert again.http_status == 200
    assert again.body["replayed"] is False
    assert len(charges(conn, ns)) == 1


def test_permanent_failure_never_commits(conn):
    ns = make_ns(conn, World.PERMANENT_FAIL)
    for _ in range(3):
        assert service.charge(conn, ns, dict(CHARGE)).http_status == 504
    assert charges(conn, ns) == []


def test_controlled_rejects_changed_parameters_under_the_same_key(conn):
    ns = make_ns(conn)
    service.charge(conn, ns, dict(CHARGE))
    r = service.charge(conn, ns, {**CHARGE, "amount": 4999})
    assert r.http_status == 409
    assert r.body["error"]["code"] == "idempotency_conflict"
    rows = charges(conn, ns)
    assert len(rows) == 1
    assert str(rows[0]["amount"]) == "49.99"


def test_compat_replays_even_when_parameters_change(conn):
    ns = make_ns(conn, profile=Profile.COMPAT)
    service.charge(conn, ns, dict(COMPAT_CHARGE))
    r = service.charge(conn, ns, {**COMPAT_CHARGE, "amount": 10})
    assert r.http_status == 200
    assert r.body["replayed"] is True


def test_compat_first_errors_match_the_original_text(conn):
    lost = make_ns(conn, World.LOST_ACK, Profile.COMPAT, ns="lost")
    fail = make_ns(conn, World.TRUE_FAIL, Profile.COMPAT, ns="fail")
    a = service.charge(conn, lost, dict(COMPAT_CHARGE))
    b = service.charge(conn, fail, dict(COMPAT_CHARGE))
    assert render_charge(a.http_status, a.body) == (
        "HTTP 504 Gateway Timeout: the upstream processor did not confirm. "
        "Payment status is UNKNOWN."
    )
    assert render_charge(b.http_status, b.body) == (
        "HTTP 500 Internal Server Error: the charge was not processed. "
        "Payment status is UNKNOWN."
    )


@pytest.mark.parametrize(
    "bad",
    [
        {**CHARGE, "amount": 49.999},
        {**CHARGE, "amount": -1},
        {**CHARGE, "amount": "lots"},
        {k: v for k, v in CHARGE.items() if k != "currency"},
        {**CHARGE, "currency": "usd"},
        {**CHARGE, "order_id": ""},
        {**CHARGE, "idempotency_key": ""},
        {**CHARGE, "namespace": "someone-else"},
    ],
)
def test_controlled_validation_rejects_without_side_effects(conn, bad):
    ns = make_ns(conn)
    r = service.charge(conn, ns, bad)
    assert r.http_status == 400
    assert charges(conn, ns) == []
    counters = conn.execute(
        "SELECT charge_calls FROM sim_namespaces WHERE namespace = %s", (ns,)
    ).fetchone()
    assert counters["charge_calls"] == 0


def test_compat_rejects_controlled_only_fields(conn):
    ns = make_ns(conn, profile=Profile.COMPAT)
    assert service.charge(conn, ns, dict(CHARGE)).http_status == 400


def test_invalid_request_does_not_consume_the_fault(conn):
    ns = make_ns(conn, World.LOST_ACK)
    service.charge(conn, ns, {**CHARGE, "amount": 49.999})
    r = service.charge(conn, ns, dict(CHARGE))
    assert r.http_status == 504
    assert len(charges(conn, ns)) == 1


def test_status_outage_then_recovery(conn):
    ns = make_ns(conn, plan=FaultPlan(status_unavailable_calls=2))
    service.charge(conn, ns, dict(CHARGE))
    codes = [service.status(conn, ns, "k1").http_status for _ in range(3)]
    assert codes == [503, 503, 200]


def test_unknown_namespace(conn):
    with pytest.raises(service.UnknownNamespace):
        service.charge(conn, "nope", dict(CHARGE))
    with pytest.raises(service.UnknownNamespace):
        service.status(conn, "nope", "k1")


def test_configure_resets_the_namespace(conn):
    ns = make_ns(conn)
    service.charge(conn, ns, dict(CHARGE))
    make_ns(conn)
    assert charges(conn, ns) == []
    row = conn.execute(
        "SELECT charge_calls FROM sim_namespaces WHERE namespace = %s", (ns,)
    ).fetchone()
    assert row["charge_calls"] == 0


def test_every_call_is_logged_with_its_outcome(conn):
    ns = make_ns(conn, World.LOST_ACK)
    service.charge(conn, ns, dict(CHARGE))
    service.status(conn, ns, "k1")
    service.charge(conn, ns, dict(CHARGE))
    rows = conn.execute(
        "SELECT endpoint, call_index, outcome FROM call_log WHERE namespace = %s ORDER BY id",
        (ns,),
    ).fetchall()
    assert [(r["endpoint"], r["call_index"], r["outcome"]) for r in rows] == [
        ("charge", 1, "committed_then_error"),
        ("status", 1, "succeeded"),
        ("charge", 2, "replayed"),
    ]


def test_requires_autocommit(svc_url):
    with psycopg.connect(svc_url) as c:
        with pytest.raises(ValueError, match="autocommit"):
            service.charge(c, "ns", dict(CHARGE))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/simulator/test_service.py -v`
Expected: FAIL with `ImportError: cannot import name 'service' from 'lab.simulator'`.

- [ ] **Step 3: Implement**

`src/lab/simulator/service.py`:

```python
"""Payment simulator core.

Every ledger write in the lab goes through this module. The HTTP layer only
translates requests and responses. Connections must be in autocommit mode so that
no row lock outlives the statement or transaction block that needs it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from lab.protocol.faults import ChargeFault, FaultPlan
from lab.protocol.task import Profile, args_hash, is_minor_unit_amount, parse_amount

TIMEOUT_MESSAGE = "the upstream processor did not confirm. Payment status is UNKNOWN."
NOT_PROCESSED_MESSAGE = "the charge was not processed. Payment status is UNKNOWN."
SUCCESS_MESSAGE = "Charge succeeded."
REPLAY_MESSAGE = (
    "Idempotent hit: a payment already succeeded under this key. No additional charge was made."
)
CONFLICT_MESSAGE = (
    "this idempotency key was already used with different parameters. No charge was made."
)
STATUS_UNAVAILABLE_MESSAGE = "payment status service is temporarily unavailable."

_CURRENCY = re.compile(r"^[A-Z]{3}$")
_COMPAT_FIELDS = frozenset({"amount", "idempotency_key"})
_CONTROLLED_FIELDS = frozenset({"amount", "idempotency_key", "order_id", "currency"})


@dataclass(frozen=True)
class SimResponse:
    http_status: int
    body: dict[str, Any]


@dataclass(frozen=True)
class _ValidCharge:
    key: str
    order_id: str | None
    amount: Decimal
    currency: str | None


class UnknownNamespace(Exception):
    pass


def _require_autocommit(conn: psycopg.Connection) -> None:
    if not conn.autocommit:
        raise ValueError("simulator connections must use autocommit=True")


def _one(conn: psycopg.Connection, query: Any, params: tuple = ()) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchone()


def _error(http_status: int, code: str, message: str) -> SimResponse:
    return SimResponse(http_status, {"error": {"code": code, "message": message}})


def _charge_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "charge_id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "order_id": row["order_id"],
        "amount": format(row["amount"], "f"),
        "currency": row["currency"],
    }


def configure_namespace(
    conn: psycopg.Connection, namespace: str, profile: Profile, plan: FaultPlan
) -> None:
    """Create or reset a namespace. Resetting deletes its ledger, log and barriers."""
    with conn.transaction():
        conn.execute("DELETE FROM sim_namespaces WHERE namespace = %s", (namespace,))
        conn.execute(
            "INSERT INTO sim_namespaces (namespace, profile, fault_plan) VALUES (%s, %s, %s)",
            (namespace, profile.value, Jsonb(plan.model_dump(mode="json"))),
        )


def delete_namespace(conn: psycopg.Connection, namespace: str) -> None:
    conn.execute("DELETE FROM sim_namespaces WHERE namespace = %s", (namespace,))


def _load(conn: psycopg.Connection, namespace: str) -> tuple[Profile, FaultPlan]:
    row = _one(
        conn, "SELECT profile, fault_plan FROM sim_namespaces WHERE namespace = %s", (namespace,)
    )
    if row is None:
        raise UnknownNamespace(namespace)
    return Profile(row["profile"]), FaultPlan.model_validate(row["fault_plan"])


def _next_call(conn: psycopg.Connection, namespace: str, counter: str) -> int:
    query = sql.SQL(
        "UPDATE sim_namespaces SET {c} = {c} + 1 WHERE namespace = %s RETURNING {c} AS n"
    ).format(c=sql.Identifier(counter))
    row = _one(conn, query, (namespace,))
    if row is None:
        raise UnknownNamespace(namespace)
    return row["n"]


def _validate_charge(profile: Profile, payload: dict[str, Any]) -> _ValidCharge | SimResponse:
    allowed = _COMPAT_FIELDS if profile is Profile.COMPAT else _CONTROLLED_FIELDS
    extra = sorted(set(payload) - allowed)
    if extra:
        return _error(400, "invalid_request", f"unexpected fields: {', '.join(extra)}")
    key = payload.get("idempotency_key")
    if not isinstance(key, str) or not 1 <= len(key) <= 255:
        return _error(
            400, "invalid_request", "idempotency_key must be a string of 1 to 255 characters"
        )
    try:
        amount = parse_amount(payload.get("amount"))
    except ValueError as exc:
        return _error(400, "invalid_request", str(exc))
    if profile is Profile.COMPAT:
        return _ValidCharge(key, None, amount, None)
    if not is_minor_unit_amount(amount):
        return _error(
            400, "invalid_request", "amount must be positive with at most two decimal places"
        )
    order_id = payload.get("order_id")
    if not isinstance(order_id, str) or not 1 <= len(order_id) <= 64:
        return _error(400, "invalid_request", "order_id must be a string of 1 to 64 characters")
    currency = payload.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.match(currency):
        return _error(400, "invalid_request", "currency must be a three-letter uppercase code")
    return _ValidCharge(key, order_id, amount, currency)


def _ambiguous_error(profile: Profile, committed: bool) -> SimResponse:
    """Controlled profile: one error shape for both worlds. Compat: the original pair."""
    if profile is Profile.COMPAT and not committed:
        return _error(500, "internal_error", NOT_PROCESSED_MESSAGE)
    return _error(504, "gateway_timeout", TIMEOUT_MESSAGE)


def _insert_or_get(
    conn: psycopg.Connection, namespace: str, valid: _ValidCharge, call_index: int
) -> tuple[dict[str, Any], bool]:
    """Commit the effect and claim the key in one statement, or return the existing row.

    Under READ COMMITTED a concurrent insert of the same key blocks until the first
    transaction finishes, then takes the DO NOTHING branch; the following SELECT runs
    with a fresh snapshot and sees the winner's row.
    """
    digest = args_hash(valid.order_id, valid.amount, valid.currency)
    with conn.transaction():
        row = _one(
            conn,
            "INSERT INTO charges (namespace, idempotency_key, order_id, amount, currency,"
            " args_hash, call_index) VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (namespace, idempotency_key) DO NOTHING RETURNING *",
            (namespace, valid.key, valid.order_id, valid.amount, valid.currency, digest,
             call_index),
        )
        if row is not None:
            return row, True
        row = _one(
            conn,
            "SELECT * FROM charges WHERE namespace = %s AND idempotency_key = %s",
            (namespace, valid.key),
        )
    if row is None:
        raise RuntimeError("idempotency record vanished; was the namespace reset mid-call?")
    return row, False


def _charge_result(
    profile: Profile,
    fault: ChargeFault | None,
    valid: _ValidCharge,
    row: dict[str, Any],
    inserted: bool,
) -> tuple[SimResponse, str]:
    if fault == "commit_then_error":
        outcome = "committed_then_error" if inserted else "existing_then_error"
        return _ambiguous_error(profile, committed=True), outcome
    if inserted:
        body = {"message": SUCCESS_MESSAGE, "replayed": False, "charge": _charge_json(row)}
        return SimResponse(200, body), "committed"
    same_args = row["args_hash"] == args_hash(valid.order_id, valid.amount, valid.currency)
    if profile is Profile.CONTROLLED and not same_args:
        return _error(409, "idempotency_conflict", CONFLICT_MESSAGE), "conflict"
    body = {"message": REPLAY_MESSAGE, "replayed": True, "charge": _charge_json(row)}
    return SimResponse(200, body), "replayed"


def _log(
    conn: psycopg.Connection,
    namespace: str,
    endpoint: str,
    call_index: int | None,
    key: Any,
    request: dict[str, Any],
    resp: SimResponse,
    outcome: str,
    started: float,
) -> None:
    conn.execute(
        "INSERT INTO call_log (namespace, endpoint, call_index, idempotency_key, request,"
        " http_status, response, outcome, server_ms)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            namespace,
            endpoint,
            call_index,
            key if isinstance(key, str) else None,
            Jsonb(request),
            resp.http_status,
            Jsonb(resp.body),
            outcome,
            (time.perf_counter() - started) * 1000.0,
        ),
    )


def charge(
    conn: psycopg.Connection,
    namespace: str,
    payload: dict[str, Any],
    *,
    barrier_timeout_s: float = 30.0,
) -> SimResponse:
    _require_autocommit(conn)
    started = time.perf_counter()
    profile, plan = _load(conn, namespace)
    valid = _validate_charge(profile, payload)
    if isinstance(valid, SimResponse):
        _log(conn, namespace, "charge", None, payload.get("idempotency_key"), payload, valid,
             "invalid", started)
        return valid
    call_index = _next_call(conn, namespace, "charge_calls")
    fault = plan.charge_fault(call_index)
    if fault == "error_no_commit":
        resp, outcome = _ambiguous_error(profile, committed=False), "error_no_commit"
    else:
        row, inserted = _insert_or_get(conn, namespace, valid, call_index)
        resp, outcome = _charge_result(profile, fault, valid, row, inserted)
    _log(conn, namespace, "charge", call_index, valid.key, payload, resp, outcome, started)
    return resp


def status(conn: psycopg.Connection, namespace: str, key: str) -> SimResponse:
    _require_autocommit(conn)
    started = time.perf_counter()
    _, plan = _load(conn, namespace)
    call_index = _next_call(conn, namespace, "status_calls")
    if call_index <= plan.status_unavailable_calls:
        resp, outcome = _error(503, "unavailable", STATUS_UNAVAILABLE_MESSAGE), "unavailable"
    else:
        row = _one(
            conn,
            "SELECT * FROM charges WHERE namespace = %s AND idempotency_key = %s",
            (namespace, key),
        )
        if row is None:
            body = {"idempotency_key": key, "status": "NOT_FOUND", "charge": None}
            resp, outcome = SimResponse(200, body), "not_found"
        else:
            body = {"idempotency_key": key, "status": "SUCCEEDED", "charge": _charge_json(row)}
            resp, outcome = SimResponse(200, body), "succeeded"
    _log(conn, namespace, "status", call_index, key, {"idempotency_key": key}, resp, outcome,
         started)
    return resp
```

Note: `barrier_timeout_s` is accepted now and used in Task 8; keeping the signature stable means later callers never change.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/simulator/test_service.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lab/simulator/service.py tests/simulator/test_service.py
git commit -m "feat(simulator): atomic idempotent charges, status and hidden worlds"
```

---

### Task 8: Concurrency guarantee and synchronized barriers

**Files:**
- Modify: `src/lab/simulator/service.py` (add three functions, replace `charge`)
- Test: `tests/simulator/test_concurrency.py`, `tests/simulator/test_barriers.py`

**Interfaces:**
- Consumes: Task 7 service; fixtures (Task 6).
- Produces:
  - `hold(conn, namespace: str, name: str, timeout_s: float) -> bool` (True if released, False on timeout)
  - `release_barrier(conn, namespace: str, name: str) -> None` (may be called before the barrier is reached)
  - `barrier_state(conn, namespace: str, name: str) -> dict[str, bool]` with keys `reached`, `released`
  - Barrier names: `before_commit:{call_index}` and `after_commit:{call_index}`

- [ ] **Step 1: Write the failing tests**

`tests/simulator/test_concurrency.py`:

```python
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg.rows import dict_row

from lab.protocol.faults import FaultPlan, World, world_fault_plan
from lab.protocol.task import Profile
from lab.simulator import service
from tests.helpers import CHARGE, charges

WORKERS = 16
ROUNDS = 10


def _race(svc_url, namespace, bodies):
    start = threading.Barrier(len(bodies))

    def call(body):
        with psycopg.connect(svc_url, autocommit=True, row_factory=dict_row) as c:
            start.wait()
            return service.charge(c, namespace, body)

    with ThreadPoolExecutor(len(bodies)) as pool:
        return list(pool.map(call, bodies))


def test_concurrent_same_key_requests_commit_exactly_one_effect(svc_url, conn):
    for round_ in range(ROUNDS):
        ns = f"race-{round_}"
        service.configure_namespace(conn, ns, Profile.CONTROLLED, FaultPlan())
        results = _race(svc_url, ns, [dict(CHARGE) for _ in range(WORKERS)])
        fresh = [r for r in results if r.http_status == 200 and not r.body["replayed"]]
        replays = [r for r in results if r.http_status == 200 and r.body["replayed"]]
        assert len(fresh) == 1, f"round {round_}"
        assert len(replays) == WORKERS - 1, f"round {round_}"
        assert len(charges(conn, ns)) == 1, f"round {round_}"


def test_concurrent_same_key_in_lost_ack_world_still_one_effect(svc_url, conn):
    for round_ in range(ROUNDS):
        ns = f"lost-{round_}"
        service.configure_namespace(conn, ns, Profile.CONTROLLED,
                                    world_fault_plan(World.LOST_ACK))
        results = _race(svc_url, ns, [dict(CHARGE) for _ in range(WORKERS)])
        assert sum(r.http_status == 504 for r in results) == 1
        assert len(charges(conn, ns)) == 1


def test_concurrent_distinct_keys_each_commit(svc_url, conn):
    ns = "distinct"
    service.configure_namespace(conn, ns, Profile.CONTROLLED, FaultPlan())
    bodies = [{**CHARGE, "idempotency_key": f"k{i}"} for i in range(WORKERS)]
    results = _race(svc_url, ns, bodies)
    assert all(r.http_status == 200 for r in results)
    assert len(charges(conn, ns)) == WORKERS
```

`tests/simulator/test_barriers.py`:

```python
import threading
import time

import psycopg
from psycopg.rows import dict_row

from lab.protocol.faults import FaultPlan
from lab.protocol.task import Profile
from lab.simulator import service
from tests.helpers import CHARGE, charges


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached in time")


def _charge_in_background(svc_url, namespace, body, **kwargs):
    result = {}

    def run():
        with psycopg.connect(svc_url, autocommit=True, row_factory=dict_row) as c:
            result["response"] = service.charge(c, namespace, body, **kwargs)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_after_commit_hold_exposes_committed_but_unacknowledged_state(svc_url, conn):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan(hold_after_commit=(1,)))
    thread, result = _charge_in_background(svc_url, "ns", dict(CHARGE))
    _wait_until(lambda: service.barrier_state(conn, "ns", "after_commit:1")["reached"])
    assert len(charges(conn, "ns")) == 1
    assert "response" not in result
    service.release_barrier(conn, "ns", "after_commit:1")
    thread.join(5)
    assert result["response"].http_status == 200


def test_before_commit_hold_exposes_uncommitted_state(svc_url, conn):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED,
                                FaultPlan(hold_before_commit=(1,)))
    thread, result = _charge_in_background(svc_url, "ns", dict(CHARGE))
    _wait_until(lambda: service.barrier_state(conn, "ns", "before_commit:1")["reached"])
    assert charges(conn, "ns") == []
    service.release_barrier(conn, "ns", "before_commit:1")
    thread.join(5)
    assert result["response"].http_status == 200
    assert len(charges(conn, "ns")) == 1


def test_pre_released_barrier_does_not_block(conn):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan(hold_after_commit=(1,)))
    service.release_barrier(conn, "ns", "after_commit:1")
    started = time.monotonic()
    assert service.charge(conn, "ns", dict(CHARGE)).http_status == 200
    assert time.monotonic() - started < 2


def test_unreleased_barrier_times_out_and_continues(conn):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan(hold_after_commit=(1,)))
    r = service.charge(conn, "ns", dict(CHARGE), barrier_timeout_s=0.2)
    assert r.http_status == 200


def test_barrier_state_for_unknown_barrier(conn):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, FaultPlan())
    assert service.barrier_state(conn, "ns", "after_commit:9") == {
        "reached": False, "released": False,
    }
```

- [ ] **Step 2: Run to verify which tests fail**

Run: `uv run pytest tests/simulator/test_concurrency.py tests/simulator/test_barriers.py -v`
Expected: the three concurrency tests PASS already (the unique constraint from Task 6 is doing the work, which is the point). Every barrier test FAILS with `AttributeError: module 'lab.simulator.service' has no attribute 'barrier_state'` (or `release_barrier`).

- [ ] **Step 3: Prove the concurrency test can fail**

A test that passes on its first run has not yet shown it can catch the bug it exists for. Break the guarantee on purpose, watch the test fail, then restore.

Make two temporary edits. In `src/lab/simulator/migrations/0001_init.sql`, delete the line `    UNIQUE (namespace, idempotency_key)` and the comma at the end of the `created_at` line above it. In `src/lab/simulator/service.py`, replace `_insert_or_get` with this check-then-insert version, which is the classic race:

```python
def _insert_or_get(conn, namespace, valid, call_index):
    digest = args_hash(valid.order_id, valid.amount, valid.currency)
    existing = _one(
        conn,
        "SELECT * FROM charges WHERE namespace = %s AND idempotency_key = %s",
        (namespace, valid.key),
    )
    if existing is not None:
        return existing, False
    row = _one(
        conn,
        "INSERT INTO charges (namespace, idempotency_key, order_id, amount, currency,"
        " args_hash, call_index) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (namespace, valid.key, valid.order_id, valid.amount, valid.currency, digest,
         call_index),
    )
    return row, True
```

Run: `uv run pytest tests/simulator/test_concurrency.py::test_concurrent_same_key_requests_commit_exactly_one_effect -v`
Expected: FAIL in at least one of the ten rounds with more than one fresh charge. The race is timing-dependent; if all rounds happen to pass, run it again, and it should fail within a few attempts. If it never fails after five runs, the test is not exercising concurrency and must be fixed before continuing.

Restore both files and confirm:

```bash
git checkout -- src/lab/simulator/migrations/0001_init.sql src/lab/simulator/service.py
uv run pytest tests/simulator/test_concurrency.py -v
```

Expected: PASS. Never commit the broken variant.

- [ ] **Step 4: Implement barriers**

Add to `src/lab/simulator/service.py`, after `delete_namespace`:

```python
def hold(conn: psycopg.Connection, namespace: str, name: str, timeout_s: float) -> bool:
    """Mark a barrier reached, then wait until it is released or the timeout passes.

    The connection is in autocommit mode, so the caller holds no transaction while
    waiting. A charge held after commit has its effect visible in the ledger while
    its response has not yet been sent.
    """
    conn.execute(
        "INSERT INTO barriers (namespace, name, reached_at) VALUES (%s, %s, now())"
        " ON CONFLICT (namespace, name) DO UPDATE SET reached_at = now()",
        (namespace, name),
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = _one(
            conn,
            "SELECT released_at FROM barriers WHERE namespace = %s AND name = %s",
            (namespace, name),
        )
        if row is not None and row["released_at"] is not None:
            return True
        time.sleep(0.01)
    return False


def release_barrier(conn: psycopg.Connection, namespace: str, name: str) -> None:
    conn.execute(
        "INSERT INTO barriers (namespace, name, released_at) VALUES (%s, %s, now())"
        " ON CONFLICT (namespace, name) DO UPDATE SET released_at = now()",
        (namespace, name),
    )


def barrier_state(conn: psycopg.Connection, namespace: str, name: str) -> dict[str, bool]:
    row = _one(
        conn,
        "SELECT reached_at, released_at FROM barriers WHERE namespace = %s AND name = %s",
        (namespace, name),
    )
    return {
        "reached": bool(row and row["reached_at"]),
        "released": bool(row and row["released_at"]),
    }
```

Replace the whole `charge` function with:

```python
def charge(
    conn: psycopg.Connection,
    namespace: str,
    payload: dict[str, Any],
    *,
    barrier_timeout_s: float = 30.0,
) -> SimResponse:
    _require_autocommit(conn)
    started = time.perf_counter()
    profile, plan = _load(conn, namespace)
    valid = _validate_charge(profile, payload)
    if isinstance(valid, SimResponse):
        _log(conn, namespace, "charge", None, payload.get("idempotency_key"), payload, valid,
             "invalid", started)
        return valid
    call_index = _next_call(conn, namespace, "charge_calls")
    fault = plan.charge_fault(call_index)
    if call_index in plan.hold_before_commit:
        hold(conn, namespace, f"before_commit:{call_index}", barrier_timeout_s)
    if fault == "error_no_commit":
        resp, outcome = _ambiguous_error(profile, committed=False), "error_no_commit"
    else:
        row, inserted = _insert_or_get(conn, namespace, valid, call_index)
        if call_index in plan.hold_after_commit:
            hold(conn, namespace, f"after_commit:{call_index}", barrier_timeout_s)
        resp, outcome = _charge_result(profile, fault, valid, row, inserted)
    _log(conn, namespace, "charge", call_index, valid.key, payload, resp, outcome, started)
    return resp
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/simulator -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lab/simulator/service.py tests/simulator/test_concurrency.py tests/simulator/test_barriers.py
git commit -m "feat(simulator): concurrency guarantee tests and synchronized commit barriers"
```

---

### Task 9: Public and internal HTTP apps

**Files:**
- Create: `src/lab/simulator/app.py`, `src/lab/simulator/__main__.py`
- Test: `tests/simulator/test_http.py`

**Interfaces:**
- Consumes: Task 7 and Task 8 service functions; `Profile`, `FaultPlan`.
- Produces:
  - `create_public_app(pool: ConnectionPool, client_token: str) -> FastAPI` with `POST /v1/charges` and `GET /v1/payments/{idempotency_key}`, both requiring `Authorization: Bearer <client_token>` and `X-Lab-Namespace`
  - `create_internal_app(pool: ConnectionPool, control_token: str) -> FastAPI` with `PUT /internal/namespaces/{namespace}`, `DELETE /internal/namespaces/{namespace}`, `GET /internal/namespaces/{namespace}/barriers/{name}`, `POST /internal/namespaces/{namespace}/barriers/{name}/release`
  - `NamespaceConfig(profile: Profile, fault_plan: FaultPlan)` request model
  - `python -m lab.simulator migrate|public|internal`

- [ ] **Step 1: Write the failing tests**

`tests/simulator/test_http.py`:

```python
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from lab.simulator.app import create_internal_app, create_public_app
from tests.helpers import CHARGE

CLIENT = {"Authorization": "Bearer client-t"}
CONTROL = {"Authorization": "Bearer control-t"}


@pytest.fixture
def apps(svc_url):
    pool = ConnectionPool(svc_url, min_size=1, max_size=5, open=True,
                          kwargs={"autocommit": True, "row_factory": dict_row})
    yield (TestClient(create_public_app(pool, "client-t")),
           TestClient(create_internal_app(pool, "control-t")))
    pool.close()


def configure(internal, ns, plan=None):
    body = {"profile": "controlled", "fault_plan": plan or {}}
    r = internal.put(f"/internal/namespaces/{ns}", headers=CONTROL, json=body)
    assert r.status_code == 200, r.text


def test_lost_ack_end_to_end(apps):
    public, internal = apps
    configure(internal, "ns", {"charge": {"1": "commit_then_error"}})
    headers = {**CLIENT, "X-Lab-Namespace": "ns"}
    first = public.post("/v1/charges", headers=headers, json=CHARGE)
    assert first.status_code == 504
    status = public.get("/v1/payments/k1", headers=headers)
    assert status.status_code == 200
    assert status.json()["status"] == "SUCCEEDED"


def test_public_requires_the_client_token(apps):
    public, internal = apps
    configure(internal, "ns")
    for headers in ({}, {"Authorization": "Bearer wrong"}, CONTROL):
        r = public.post("/v1/charges", headers={**headers, "X-Lab-Namespace": "ns"}, json=CHARGE)
        assert r.status_code == 401


def test_internal_requires_the_control_token(apps):
    _, internal = apps
    for headers in ({}, CLIENT):
        r = internal.put("/internal/namespaces/ns", headers=headers,
                         json={"profile": "controlled"})
        assert r.status_code == 401


def test_public_app_has_no_control_routes(apps):
    public, _ = apps
    r = public.put("/internal/namespaces/ns", headers=CONTROL, json={"profile": "controlled"})
    assert r.status_code in (404, 405)


def test_public_app_publishes_no_schema(apps):
    public, _ = apps
    assert public.get("/openapi.json").status_code == 404


def test_unknown_namespace_is_404(apps):
    public, _ = apps
    r = public.post("/v1/charges", headers={**CLIENT, "X-Lab-Namespace": "nope"}, json=CHARGE)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_namespace"


def test_barrier_routes(apps):
    _, internal = apps
    configure(internal, "ns", {"hold_after_commit": [1]})
    url = "/internal/namespaces/ns/barriers/after_commit:1"
    assert internal.get(url, headers=CONTROL).json() == {"reached": False, "released": False}
    assert internal.post(f"{url}/release", headers=CONTROL).status_code == 200
    assert internal.get(url, headers=CONTROL).json() == {"reached": False, "released": True}


def test_release_on_unknown_namespace_is_404(apps):
    _, internal = apps
    r = internal.post("/internal/namespaces/nope/barriers/x/release", headers=CONTROL)
    assert r.status_code == 404


def test_delete_namespace(apps):
    public, internal = apps
    configure(internal, "ns")
    assert internal.delete("/internal/namespaces/ns", headers=CONTROL).status_code == 204
    r = public.post("/v1/charges", headers={**CLIENT, "X-Lab-Namespace": "ns"}, json=CHARGE)
    assert r.status_code == 404


def test_namespace_config_rejects_unknown_fault_fields(apps):
    _, internal = apps
    r = internal.put("/internal/namespaces/ns", headers=CONTROL,
                     json={"profile": "controlled", "fault_plan": {"explode": True}})
    assert r.status_code == 422
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/simulator/test_http.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lab.simulator.app'`.

- [ ] **Step 3: Implement**

`src/lab/simulator/app.py`:

```python
"""HTTP layer for the payment simulator.

Two apps, two ports, two tokens. The public app is what agent tooling calls. The
internal app configures hidden faults and barriers and is never registered as a tool.
Neither app publishes an OpenAPI schema.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, Response
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from lab.protocol.faults import FaultPlan
from lab.protocol.task import Profile
from lab.simulator import service


class NamespaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Profile
    fault_plan: FaultPlan = Field(default_factory=FaultPlan)


def _bearer(expected: str) -> Callable[[str], None]:
    def check(authorization: str = Header(default="")) -> None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            token.encode(), expected.encode()
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    return check


def _json(resp: service.SimResponse) -> JSONResponse:
    return JSONResponse(resp.body, status_code=resp.http_status)


def _unknown_namespace() -> JSONResponse:
    body = {"error": {"code": "unknown_namespace", "message": "unknown namespace"}}
    return JSONResponse(body, status_code=404)


def _bare_app(title: str) -> FastAPI:
    return FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)


def create_public_app(pool: ConnectionPool, client_token: str) -> FastAPI:
    app = _bare_app("payment-simulator")
    auth = [Depends(_bearer(client_token))]

    @app.post("/v1/charges", dependencies=auth)
    def post_charge(payload: dict[str, Any], x_lab_namespace: str = Header()) -> JSONResponse:
        with pool.connection() as conn:
            try:
                return _json(service.charge(conn, x_lab_namespace, payload))
            except service.UnknownNamespace:
                return _unknown_namespace()

    @app.get("/v1/payments/{idempotency_key}", dependencies=auth)
    def get_payment(idempotency_key: str, x_lab_namespace: str = Header()) -> JSONResponse:
        with pool.connection() as conn:
            try:
                return _json(service.status(conn, x_lab_namespace, idempotency_key))
            except service.UnknownNamespace:
                return _unknown_namespace()

    return app


def create_internal_app(pool: ConnectionPool, control_token: str) -> FastAPI:
    app = _bare_app("payment-simulator-control")
    auth = [Depends(_bearer(control_token))]

    @app.put("/internal/namespaces/{namespace}", dependencies=auth)
    def put_namespace(namespace: str, config: NamespaceConfig) -> dict[str, str]:
        with pool.connection() as conn:
            service.configure_namespace(conn, namespace, config.profile, config.fault_plan)
        return {"namespace": namespace}

    @app.delete("/internal/namespaces/{namespace}", dependencies=auth)
    def delete_namespace(namespace: str) -> Response:
        with pool.connection() as conn:
            service.delete_namespace(conn, namespace)
        return Response(status_code=204)

    @app.get("/internal/namespaces/{namespace}/barriers/{name}", dependencies=auth)
    def get_barrier(namespace: str, name: str) -> dict[str, bool]:
        with pool.connection() as conn:
            return service.barrier_state(conn, namespace, name)

    @app.post("/internal/namespaces/{namespace}/barriers/{name}/release", dependencies=auth)
    def release(namespace: str, name: str) -> Any:
        with pool.connection() as conn:
            try:
                service.release_barrier(conn, namespace, name)
            except psycopg.errors.ForeignKeyViolation:
                return _unknown_namespace()
        return {"released": True}

    return app
```

`src/lab/simulator/__main__.py`:

```python
"""Run the simulator.

    python -m lab.simulator migrate    create roles and database, apply migrations
    python -m lab.simulator public     serve the public API (default port 8100)
    python -m lab.simulator internal   serve the control API (default port 8101)
"""

from __future__ import annotations

import os
import sys

import uvicorn
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from lab import db
from lab.simulator import SIM_MIGRATIONS
from lab.simulator.app import create_internal_app, create_public_app


def _pool(url: str) -> ConnectionPool:
    return ConnectionPool(
        url, min_size=1, max_size=20, open=True,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )


def _migrate() -> None:
    admin = os.environ["LAB_PG_ADMIN_URL"]
    dbname = os.environ.get("LAB_PAYMENTS_DB", "payments")
    db.ensure_role(admin, "payment_svc", os.environ["LAB_PAYMENT_SVC_PASSWORD"])
    db.ensure_role(admin, "ledger_reader", os.environ["LAB_LEDGER_READER_PASSWORD"])
    db.ensure_database(admin, dbname)
    applied = db.apply_migrations(db.with_database(admin, dbname), SIM_MIGRATIONS)
    print(f"applied: {applied or 'nothing new'}")


def main(argv: list[str]) -> None:
    command = argv[1] if len(argv) > 1 else "public"
    if command == "migrate":
        _migrate()
        return
    url = os.environ["LAB_PAYMENTS_URL"]
    if command == "public":
        app = create_public_app(_pool(url), os.environ["SIM_CLIENT_TOKEN"])
        default_port = "8100"
    elif command == "internal":
        app = create_internal_app(_pool(url), os.environ["SIM_CONTROL_TOKEN"])
        default_port = "8101"
    else:
        raise SystemExit(f"unknown command: {command}")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", default_port)))


if __name__ == "__main__":
    main(sys.argv)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/simulator/test_http.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lab/simulator/app.py src/lab/simulator/__main__.py tests/simulator/test_http.py
git commit -m "feat(simulator): separate public and control HTTP apps"
```

---

### Task 10: Ledger reader, compatibility parity and controlled indistinguishability

**Files:**
- Create: `tests/reference/__init__.py`, `tests/reference/retryledger_original.py` (verbatim upstream), `THIRD_PARTY_NOTICES.md`
- Modify: `src/lab/oracle/ledger.py` (add `read_ledger`)
- Test: `tests/oracle/test_ledger.py`, `tests/oracle/test_parity.py`

**Interfaces:**
- Consumes: Task 5 oracle types; Task 7 service; Task 3 renderers.
- Produces: `read_ledger(conn: psycopg.Connection, namespace: str) -> tuple[list[LedgerCharge], CallSummary]`

- [ ] **Step 1: Vendor the upstream reference and its licence**

```bash
mkdir -p tests/reference
REV=58fdbc0d45dffbddccc88bceea08a4974f6573a3
BASE=https://raw.githubusercontent.com/Zhuoxi2000/RetryLedger/$REV
curl -sL "$BASE/code/ei_retryledger.py" -o /tmp/ei_retryledger.py
shasum -a 256 /tmp/ei_retryledger.py
```

Expected: `b1961cd8daac7606f252dce7dc78e5e5a61adaacce64ab770eadf17c6cd2a077`. Stop if it differs.

```bash
{ printf '%s\n' \
  "# Verbatim copy of RetryLedger code/ei_retryledger.py at revision $REV." \
  "# Copyright (c) 2026 The RetryLedger authors. MIT License; see THIRD_PARTY_NOTICES.md." \
  "# Do not edit. This is the reference oracle for compatibility-profile parity tests." \
  "# ruff: noqa" ""; cat /tmp/ei_retryledger.py; } > tests/reference/retryledger_original.py
touch tests/reference/__init__.py
curl -sL "$BASE/LICENSE" -o /tmp/retryledger_LICENSE
head -1 /tmp/retryledger_LICENSE
```

Expected: `MIT License`.

Generate `THIRD_PARTY_NOTICES.md` so the licence text is copied exactly rather than retyped:

```bash
{ cat <<'EOF'
# Third-party notices

## RetryLedger

Source: https://github.com/Zhuoxi2000/RetryLedger
Revision: 58fdbc0d45dffbddccc88bceea08a4974f6573a3
File: code/ei_retryledger.py (SHA-256 b1961cd8daac7606f252dce7dc78e5e5a61adaacce64ab770eadf17c6cd2a077)

Used as follows:

- `tests/reference/retryledger_original.py` is a verbatim copy, used only as the
  reference oracle in compatibility-profile parity tests.
- The compatibility profile in `src/lab/simulator/service.py` and
  `src/lab/protocol/render.py` reproduces its task constants, tool shapes, response
  strings and legacy score definition.
- The controlled profile is this project's modification and is not an exact replication.

Licence, copied from the upstream repository at the revision above:

```text
EOF
cat /tmp/retryledger_LICENSE
printf '```\n'; } > THIRD_PARTY_NOTICES.md
```

- [ ] **Step 2: Write the failing tests**

`tests/oracle/test_ledger.py`:

```python
from decimal import Decimal

import psycopg

from lab.oracle.ledger import CallSummary, LedgerCharge, read_ledger
from lab.protocol.faults import World, world_fault_plan
from lab.protocol.task import Profile
from lab.simulator import service
from tests.helpers import CHARGE


def test_read_ledger_through_the_read_only_role(payments_db, conn):
    service.configure_namespace(conn, "ns", Profile.CONTROLLED, world_fault_plan(World.LOST_ACK))
    service.charge(conn, "ns", dict(CHARGE))
    service.status(conn, "ns", "k1")
    service.charge(conn, "ns", {**CHARGE, "idempotency_key": "k2"})
    service.charge(conn, "ns", {**CHARGE, "amount": 1.001})   # invalid, not an attempt

    with psycopg.connect(payments_db["reader"], autocommit=True) as reader:
        found, calls = read_ledger(reader, "ns")

    assert found == [
        LedgerCharge("k1", "1234", Decimal("49.99"), "USD", 1),
        LedgerCharge("k2", "1234", Decimal("49.99"), "USD", 2),
    ]
    assert calls == CallSummary(charge_keys=("k1", "k2"), status_calls=1)


def test_read_ledger_ignores_other_namespaces(conn):
    for ns in ("a", "b"):
        service.configure_namespace(conn, ns, Profile.CONTROLLED, world_fault_plan(World.HEALTHY))
    service.charge(conn, "a", dict(CHARGE))
    found, calls = read_ledger(conn, "b")
    assert found == []
    assert calls == CallSummary(charge_keys=(), status_calls=0)
```

`tests/oracle/test_parity.py`:

```python
from dataclasses import asdict

import pytest

from lab.oracle.ledger import read_ledger
from lab.oracle.score import legacy_score
from lab.protocol.faults import World, world_fault_plan
from lab.protocol.render import render_charge, render_status
from lab.protocol.task import Profile
from lab.simulator import service
from tests.helpers import CHARGE, charges
from tests.reference.retryledger_original import RetryLedgerEpisode


def k(key, amount=49.99):
    return {"amount": amount, "idempotency_key": key}


SEQUENCES = {
    "give_up": [("charge", k("k1"))],
    "retry_new_key": [("charge", k("k1")), ("charge", k("k2"))],
    "retry_same_key": [("charge", k("k1")), ("charge", k("k1"))],
    "check_then_same_key": [("charge", k("k1")), ("status", k("k1")), ("charge", k("k1"))],
    "check_then_new_key": [("charge", k("k1")), ("status", k("k1")), ("charge", k("k2"))],
    "three_new_keys": [("charge", k("k1")), ("charge", k("k2")), ("charge", k("k3"))],
    "wrong_amount": [("charge", k("k1", 4999)), ("charge", k("k1", 4999))],
}


@pytest.mark.parametrize("world", ["lost_ack", "true_fail"])
@pytest.mark.parametrize("name", sorted(SEQUENCES))
def test_compat_profile_reproduces_the_original_episode(conn, world, name):
    original = RetryLedgerEpisode(world, verify=True)
    ns = f"parity-{world}-{name}"
    service.configure_namespace(conn, ns, Profile.COMPAT, world_fault_plan(World(world)))
    for tool, args in SEQUENCES[name]:
        if tool == "charge":
            expected, _ = original.handle("charge", dict(args))
            r = service.charge(conn, ns, dict(args))
            assert render_charge(r.http_status, r.body) == expected
        else:
            key = args["idempotency_key"]
            expected, _ = original.handle("get_payment_status", {"idempotency_key": key})
            r = service.status(conn, ns, key)
            assert render_status(r.http_status, r.body) == expected
    found, calls = read_ledger(conn, ns)
    assert asdict(legacy_score(found, calls)) == original.score()


def test_controlled_first_error_is_identical_while_ledgers_differ(conn):
    observed = {}
    for world in (World.LOST_ACK, World.TRUE_FAIL):
        ns = f"controlled-{world.value}"
        service.configure_namespace(conn, ns, Profile.CONTROLLED, world_fault_plan(world))
        r = service.charge(conn, ns, dict(CHARGE))
        observed[world] = (r.http_status, r.body, render_charge(r.http_status, r.body),
                           len(charges(conn, ns)))
    lost, fail = observed[World.LOST_ACK], observed[World.TRUE_FAIL]
    assert lost[:3] == fail[:3]
    assert (lost[3], fail[3]) == (1, 0)
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/oracle -v`
Expected: FAIL with `ImportError: cannot import name 'read_ledger' from 'lab.oracle.ledger'`.

- [ ] **Step 4: Implement `read_ledger`**

Replace the import block at the top of `src/lab/oracle/ledger.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row
```

Append to `src/lab/oracle/ledger.py`:

```python
def read_ledger(
    conn: psycopg.Connection, namespace: str
) -> tuple[list[LedgerCharge], CallSummary]:
    """Read committed effects and call counts for one namespace.

    Intended to run as the ledger_reader role. Only validated calls count as
    attempts, which matches the original episode, where every call was well formed.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT idempotency_key, order_id, amount, currency, call_index"
            " FROM charges WHERE namespace = %s ORDER BY id",
            (namespace,),
        ).fetchall()
        calls = cur.execute(
            "SELECT endpoint, idempotency_key FROM call_log"
            " WHERE namespace = %s AND call_index IS NOT NULL ORDER BY endpoint, call_index",
            (namespace,),
        ).fetchall()
    found = [
        LedgerCharge(r["idempotency_key"], r["order_id"], r["amount"], r["currency"],
                     r["call_index"])
        for r in rows
    ]
    summary = CallSummary(
        charge_keys=tuple(c["idempotency_key"] for c in calls if c["endpoint"] == "charge"),
        status_calls=sum(1 for c in calls if c["endpoint"] == "status"),
    )
    return found, summary
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/oracle -v`
Expected: all PASS, including all 14 parity cases.

If a parity case fails, the original is the authority. Fix the simulator or renderer, never the reference file.

- [ ] **Step 6: Commit**

```bash
git add tests/reference tests/oracle src/lab/oracle/ledger.py THIRD_PARTY_NOTICES.md
git commit -m "feat(oracle): read-only ledger reader; compat parity with RetryLedger"
```

---

### Task 11: Container, compose stack, CI and protocol document

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `scripts/smoke_simulator.py`, `.github/workflows/ci.yml`, `docs/protocol.md`
- Modify: `compose.yaml` (add three services)

**Interfaces:**
- Consumes: `python -m lab.simulator migrate|public|internal` (Task 9).
- Produces: `docker compose up -d --wait` brings up the simulator on 8100 (public) and 127.0.0.1:8101 (control); CI runs lint and the full test suite.

- [ ] **Step 1: Write the smoke script (the failing check)**

`scripts/smoke_simulator.py`:

```python
"""End-to-end check against the compose stack.

A lost acknowledgement must commit, return an error, and be revealed by status.
"""

import sys
import uuid

import httpx

PUBLIC = "http://localhost:8100"
INTERNAL = "http://localhost:8101"
CLIENT = {"Authorization": "Bearer dev-client-token"}
CONTROL = {"Authorization": "Bearer dev-control-token"}


def main() -> int:
    ns = f"smoke-{uuid.uuid4().hex[:8]}"
    httpx.put(
        f"{INTERNAL}/internal/namespaces/{ns}",
        headers=CONTROL,
        json={"profile": "controlled", "fault_plan": {"charge": {"1": "commit_then_error"}}},
    ).raise_for_status()
    headers = {**CLIENT, "X-Lab-Namespace": ns}
    body = {"amount": 49.99, "idempotency_key": "smoke-1", "order_id": "1234", "currency": "USD"}
    first = httpx.post(f"{PUBLIC}/v1/charges", headers=headers, json=body)
    status = httpx.get(f"{PUBLIC}/v1/payments/smoke-1", headers=headers)
    print("charge:", first.status_code, first.json())
    print("status:", status.status_code, status.json())
    httpx.delete(f"{INTERNAL}/internal/namespaces/{ns}", headers=CONTROL)
    ok = first.status_code == 504 and status.json().get("status") == "SUCCEEDED"
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

Run: `uv run python scripts/smoke_simulator.py`
Expected: FAIL with `httpx.ConnectError` because nothing listens on 8101 yet.

- [ ] **Step 2: Write the image and extend compose**

`Dockerfile`:

```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "lab.simulator", "public"]
```

If the `0.12.22` tag is not published for the uv image, use the closest published `0.12.x` tag and record it in the commit message.

`.dockerignore`:

```text
.venv
.git
.pytest_cache
.ruff_cache
__pycache__
tests
docs
evals
```

Append to `compose.yaml` under `services:`:

```yaml
  sim-migrate:
    build: .
    command: ["python", "-m", "lab.simulator", "migrate"]
    environment: &sim-env
      LAB_PG_ADMIN_URL: postgresql://postgres:postgres@postgres:5432/postgres
      LAB_PAYMENTS_DB: payments
      LAB_PAYMENT_SVC_PASSWORD: dev-payment-svc
      LAB_LEDGER_READER_PASSWORD: dev-ledger-reader
      LAB_PAYMENTS_URL: postgresql://payment_svc:dev-payment-svc@postgres:5432/payments
      SIM_CLIENT_TOKEN: dev-client-token
      SIM_CONTROL_TOKEN: dev-control-token
    depends_on:
      postgres:
        condition: service_healthy

  sim-public:
    build: .
    command: ["python", "-m", "lab.simulator", "public"]
    environment: *sim-env
    ports:
      - "8100:8100"
    depends_on:
      sim-migrate:
        condition: service_completed_successfully

  sim-internal:
    build: .
    command: ["python", "-m", "lab.simulator", "internal"]
    environment: *sim-env
    ports:
      - "127.0.0.1:8101:8101"
    depends_on:
      sim-migrate:
        condition: service_completed_successfully
```

- [ ] **Step 3: Bring the stack up and rerun the smoke check**

```bash
docker compose up -d --build
docker compose ps
uv run python scripts/smoke_simulator.py
```

Expected: `sim-migrate` exited 0; `sim-public` and `sim-internal` running; the script prints a 504 charge, a SUCCEEDED status, then `OK`.

- [ ] **Step 4: Write CI**

`.github/workflows/ci.yml`:

```yaml
name: ci

on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: postgres
        ports:
          - 54329:5432
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 2s
          --health-timeout 3s
          --health-retries 30
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --frozen
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run pytest
```

Before pushing, check the current major versions of `actions/checkout` and `astral-sh/setup-uv` and use them.

Run locally first, formatter before linter, so long lines that the formatter can wrap are wrapped before the line-length rule sees them:

```bash
uv run ruff format .
uv run ruff check --fix .
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Expected: the first two commands may rewrite files (commit those changes with this task); the third reports no lint errors, no formatting differences, and all tests PASS. Confirm `git diff --stat tests/reference` is empty: the vendored file must not change.

- [ ] **Step 5: Write the protocol document**

`docs/protocol.md`:

```markdown
# Protocol: task, profiles, simulator contract and verdicts

## Task

Charge a synthetic customer 49.99 USD for order 1234, exactly once.

## Profiles

**compat** reproduces RetryLedger at revision 58fdbc0d (see THIRD_PARTY_NOTICES.md):
the same three tools, the same task constants, the same response strings, and the legacy
score. Its first errors differ between worlds (504 "did not confirm" in lost_ack, 500 "not
processed" in true_fail), so a careful reader of the error text can tell the worlds apart.
Known differences from the original, all at the edges: an empty idempotency key and a
non-numeric amount are rejected with 400 instead of being accepted.

**controlled** is this project's modification, never described as a replication. Both
worlds return the identical first error (HTTP 504, the "did not confirm" message). The
charge tool also takes order_id and currency. Amounts must be positive with at most two
decimal places, currency must be three uppercase letters, and unknown fields are
rejected. Reusing a key with different arguments returns 409 and makes no charge.

Possible remaining timing difference: in lost_ack the simulator commits before erroring,
so its first response can be slower than true_fail's. Every call's server time is stored
in `call_log.server_ms` so the difference can be measured and reported.

## Simulator contract

- Idempotency keys are scoped to a namespace and kept for the namespace's lifetime.
- Committing an effect and claiming its key happen in one atomic insert. Concurrent
  requests with the same key produce exactly one effect.
- Only a committed effect creates a key record. A failure that did not commit leaves the
  key free, so a same-key retry can commit.
- Reusing a key: identical canonical arguments replay the original charge (200,
  `replayed: true`). Different arguments: 409 in controlled, replay in compat.
- Status reads are immediately consistent with commits.
- Invalid requests are rejected before they are counted, so they never consume a fault.
- Fault schedules are indexed by validated call order within a namespace.
- If a client disconnects mid-request, the simulator still completes the request. Holds
  pause a request before or after its commit until released or until 30 s pass.
- The control API, its token and the ledger reader role are never available to a model.

## Verdicts

| Effects | Expected effect | Claim | Verdict |
|---|---|---|---|
| 2 or more | any | any | duplicate |
| 1, matches task | yes | succeeded | correct |
| 1, matches task | yes | failed | false_failure |
| 1, matches task | yes | unresolved or none | unresolved |
| 1, wrong amount, order or currency | yes | any | wrong_effect |
| 1 | no | any | unexpected_effect |
| 0 | any | succeeded | false_success |
| 0 | yes | failed, unresolved or none | missed |
| 0 | no | failed or unresolved | correct_no_effect |
| 0 | no | none | unresolved |

Correct recovery is a verdict of correct or correct_no_effect, completed before the
deadline. The legacy score (effect_count, correct, duplicate, missed, checked_status,
reused_key_on_retry, n_charge_attempts) is always reported beside it. It counts committed
keys only, so it marks a single wrong-amount charge as correct.
```

- [ ] **Step 6: Run the whole suite one last time**

Run: `uv run pytest -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile .dockerignore compose.yaml scripts .github docs/protocol.md
git commit -m "build: simulator image, compose stack, CI and protocol document"
```

---

## Phase gates for this plan (from the brief)

| Gate | Evidence |
|---|---|
| Zero, correct, duplicate, wrong-amount and false-success fixtures score distinctly | `tests/unit/test_score.py::test_phase_one_gate_five_fixtures_score_distinctly` |
| Same-key concurrent requests produce one effect | `tests/simulator/test_concurrency.py`, plus the deliberate-break check in Task 8 Step 3 |
| Equivalent visible errors map to different ledger states | `tests/oracle/test_parity.py::test_controlled_first_error_is_identical_while_ledgers_differ` |
| Compatibility profile preserved | 14 parity cases in `tests/oracle/test_parity.py` |
| Synchronized pre-commit and post-commit faults | `tests/simulator/test_barriers.py` |
| Control plane and ledger separated from the agent | `tests/simulator/test_http.py` token tests; `tests/simulator/test_db.py` role tests |
