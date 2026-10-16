"""Run a whole manifest through the episode runner with one model: resumable.

The model id is part of an experiment's config, so each model is its own experiment and one
batch never mixes models. `run_manifest` derives the experiment id once, from the model, so a
second invocation over the same manifest re-targets the same experiment:

- an episode that is already completed and scored is counted as skipped;
- an episode left `queued`, `running` or `crashed` by an earlier invocation has that leftover
  run cleared and is run again;
- an episode that fails now (the client gave up, or the runner refused) is counted as failed,
  its crashed row stays, and the next invocation retries it.

Pacing is the client's job, not this module's. `build_study_models` is the one place a real
OpenRouter client is built.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import psycopg

from lab.agent.model import ModelClient
from lab.agent.openrouter import OpenRouterClient, OpenRouterError
from lab.protocol.manifest import Manifest
from lab.runner.episode import (
    EpisodeAlreadyRunError,
    RunnerConfig,
    RunnerError,
    clear_incomplete_run,
    derive_experiment_id,
    experiment_config,
    run_is_complete,
    run_spec,
)
from lab.runtime.budget import RequestBudget

STUDY_MODELS = (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3.5-super-vl:free",
)


@dataclass(frozen=True)
class BatchSummary:
    experiment_id: str
    model_id: str
    completed: int
    skipped: int
    failed: int


def run_manifest(
    manifest: Manifest,
    model: ModelClient,
    config: RunnerConfig,
    *,
    experiment_name: str,
) -> BatchSummary:
    manifest_sha256 = manifest.sha256()
    cfg = experiment_config(model.model_id, config, manifest_sha256)
    experiment_id = derive_experiment_id(experiment_name, cfg)

    completed = skipped = failed = 0
    for spec in manifest.episodes:
        # run_spec opens its own connection; this one only decides skip or clear.
        with psycopg.connect(config.runtime_url, autocommit=True) as rt:
            if run_is_complete(rt, experiment_id, spec.episode_id):
                skipped += 1
                continue
            clear_incomplete_run(rt, experiment_id, spec.episode_id)
        try:
            run_spec(
                spec,
                model,
                config,
                manifest_sha256=manifest_sha256,
                experiment_name=experiment_name,
                experiment_id=experiment_id,
            )
        except EpisodeAlreadyRunError:
            # A run the clear step deliberately kept (score_failed): rescore_run's job.
            skipped += 1
        except (OpenRouterError, RunnerError):
            failed += 1
        else:
            completed += 1
    return BatchSummary(
        experiment_id=experiment_id,
        model_id=model.model_id,
        completed=completed,
        skipped=skipped,
        failed=failed,
    )


def build_study_models(config: RunnerConfig) -> list[ModelClient]:
    """The study's OpenRouter clients, primary first, each counted against the daily budget.

    The one seam where a real client is built, so a test can replace it. The budget connection
    stays open for the life of the process (the CLI is short-lived)."""
    conn = psycopg.connect(config.runtime_url, autocommit=True)
    try:
        budget = RequestBudget(conn)
        return [OpenRouterClient(model_id, budget=budget) for model_id in STUDY_MODELS]
    except BaseException:
        with contextlib.suppress(Exception):
            conn.close()
        raise
