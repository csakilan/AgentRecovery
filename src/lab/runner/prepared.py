"""Supervisor-only, create-once preparation. Workers load only the safe projection."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from lab.agent.client import TrustedContext
from lab.agent.model import ModelClient
from lab.agent.openrouter import OpenRouterClient
from lab.agent.prompts import system_prompt, user_prompt
from lab.agent.scripted import ScriptedModel
from lab.execution.clock import Clock
from lab.protocol.evidence import pack_evidence, unpack_evidence
from lab.protocol.manifest import EpisodeSpec, Mode
from lab.protocol.task import Profile
from lab.runner.types import ModelDescriptor, PreparedRun, WorkerInput

if TYPE_CHECKING:
    from lab.runner.episode import RunnerConfig


class LegacyResumeUnsupported(ValueError):
    pass


def describe_model(model: ModelClient) -> ModelDescriptor:
    # Subclasses and custom transports can change behavior that the descriptor cannot save.
    if type(model) is ScriptedModel:
        return ModelDescriptor(kind="scripted", model_id=model.model_id, policy=model.policy)
    if type(model) is OpenRouterClient and model.reconstructible:
        return ModelDescriptor(
            kind="openrouter", model_id=model.model_id, parameters=model.parameters
        )
    return ModelDescriptor(kind="injected", model_id=model.model_id)


def load_prepared(conn, run_id: str) -> PreparedRun:
    # Explicit projection: never deserialize episode_spec_json in worker-facing code.
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "SELECT id,experiment_id,episode_id,namespace,thread_id,mode,"
            "worker_input_json,initial_messages_json,started_at,deadline_at,"
            "setup_state,resume_version FROM runs WHERE id=%s",
            (run_id,),
        ).fetchone()
    if row is None:
        raise KeyError(run_id)
    if row["resume_version"] != 1:
        raise LegacyResumeUnsupported("legacy run has no saved resume input; use a new experiment")
    return PreparedRun(
        run_id=row["id"],
        worker_input=WorkerInput.model_validate(row["worker_input_json"]),
        initial_messages=unpack_evidence(row["initial_messages_json"]),
        **{
            k: row[k]
            for k in (
                "experiment_id",
                "episode_id",
                "namespace",
                "thread_id",
                "mode",
                "started_at",
                "deadline_at",
                "setup_state",
            )
        },
    )


def prepare_run(
    spec: EpisodeSpec,
    model: ModelDescriptor,
    config: RunnerConfig,
    *,
    manifest_sha256: str,
    experiment_name: str = "adhoc",
    experiment_id: str | None = None,
    clock: Clock,
) -> PreparedRun:
    # Frozen Pydantic models still contain mutable dictionaries. Revalidate a fresh
    # snapshot before saving anything, even if the caller supplied a model instance.
    model = ModelDescriptor.model_validate(model.model_dump(mode="json"))
    from lab.runner.episode import (
        NamespaceConfigError,
        derive_experiment_id,
        ensure_experiment,
        experiment_config,
        new_namespace,
    )

    if spec.mode != Mode.AGENT_DIRECTED and spec.profile != Profile.CONTROLLED:
        raise ValueError("B/C require the controlled profile")
    cfg = experiment_config(model.model_id, config, manifest_sha256, model_descriptor=model)
    eid = experiment_id or derive_experiment_id(experiment_name, cfg)
    with psycopg.connect(config.runtime_url, autocommit=True, row_factory=dict_row) as conn:
        ensure_experiment(conn, eid, experiment_name, manifest_sha256, cfg)
        token = uuid.uuid4().hex[:20]
        run_id = "run-" + token
        namespace = new_namespace(eid, spec.episode_id, token)
        ctx = TrustedContext(
            tenant=config.tenant,
            namespace=namespace,
            run_id=run_id,
            episode_id=spec.episode_id,
            task_id=f"charge-order-{spec.task.order_id}",
        )
        worker = WorkerInput(
            ctx=ctx,
            profile=spec.profile,
            task=spec.task,
            prompt_variant=spec.prompt_variant,
            status_available=spec.status_available,
            model=model,
            max_turns=config.max_turns,
            http_timeout_s=config.http_timeout_s,
            execution=config.execution,
        )
        messages = [
            {"role": "system", "content": system_prompt(spec.prompt_variant)},
            {"role": "user", "content": user_prompt(spec.task, spec.profile)},
        ]
        started = clock.now()
        if started.tzinfo is None:
            raise ValueError("clock must return timezone-aware timestamps")
        conn.execute(
            "INSERT INTO runs(id,experiment_id,episode_id,namespace,mode,thread_id,state,"
            "started_at,deadline_at,resume_version,episode_spec_json,worker_input_json,"
            "execution_config_json,initial_messages_json,model_descriptor_json,setup_state) "
            "VALUES(%s,%s,%s,%s,%s,%s,'queued',%s,%s,1,%s,%s,%s,%s,%s,'pending') "
            "ON CONFLICT(experiment_id,episode_id) DO NOTHING",
            (
                run_id,
                eid,
                spec.episode_id,
                namespace,
                spec.mode.value,
                "thread-" + token,
                started,
                started + timedelta(seconds=config.episode_deadline_s),
                Jsonb(spec.model_dump(mode="json")),
                Jsonb(worker.model_dump(mode="json")),
                Jsonb(config.execution.model_dump(mode="json")),
                Jsonb(pack_evidence(messages)),
                Jsonb(model.model_dump(mode="json")),
            ),
        )
        row = conn.execute(
            "SELECT id,episode_spec_json,model_descriptor_json FROM runs "
            "WHERE experiment_id=%s AND episode_id=%s",
            (eid, spec.episode_id),
        ).fetchone()
        if row["episode_spec_json"] != spec.model_dump(mode="json") or row[
            "model_descriptor_json"
        ] != model.model_dump(mode="json"):
            raise ValueError("saved preparation input differs")
        prepared = load_prepared(conn, row["id"])
        if prepared.setup_state == "ready":
            return prepared
        response = httpx.put(
            config.sim_control_url.rstrip("/")
            + "/internal/namespaces/"
            + quote(prepared.namespace, safe=""),
            headers={"Authorization": "Bearer " + config.control_token, "If-None-Match": "*"},
            json={
                "profile": spec.profile.value,
                "fault_plan": spec.fault_plan.model_dump(mode="json"),
            },
            timeout=config.http_timeout_s,
        )
        if response.status_code != 200:
            raise NamespaceConfigError(
                f"create-only namespace setup returned HTTP {response.status_code}"
            )
        conn.execute(
            "UPDATE runs SET setup_state='ready' WHERE id=%s AND setup_state='pending'",
            (prepared.run_id,),
        )
        return load_prepared(conn, prepared.run_id)
