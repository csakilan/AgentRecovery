"""Run one scripted episode from the command line.

    python -m lab.runner run --policy stable_key --world lost_ack
    python -m lab.runner run --policy give_up --manifest m.json --episode "behavior/lost_ack/..."
    python -m lab.runner study [--reps N] [--seed S] [--experiment-name NAME]

`run` has no path to a real model: `--policy` is required and names one of the scripted
policies in `lab.agent.scripted`. The real models are reachable only through `study`, which
runs the behaviour matrix with the first of `lab.runner.batch.STUDY_MODELS` that works (each
model is its own experiment) and prints the chosen model's batch summary.

Without `--manifest` the `run` episode is built with `make_spec`
(controlled profile, `agent_directed`, status tool on unless `--no-status`). With it, the
named episode is read from the manifest file. Connection details come from the environment
of `.env.example` (see `RunnerConfig.from_env`). Set OTEL_EXPORTER_OTLP_ENDPOINT to export
a trace of the episode (see `lab.obs`); it is off otherwise.

Prints one JSON line: the ids, the claim and the verdict the oracle wrote to `results`.
Exit status 0 on a completed episode, 2 on a refusal (bad arguments, an unsupported mode,
missing configuration).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from lab.runner.tracing import force_tracing_off

force_tracing_off()

from lab import obs  # noqa: E402
from lab.agent.scripted import SCRIPTED_POLICIES, ScriptedModel  # noqa: E402
from lab.protocol.faults import World  # noqa: E402
from lab.protocol.manifest import (  # noqa: E402
    EpisodeSpec,
    Manifest,
    Mode,
    behavior_matrix,
    make_spec,
)
from lab.protocol.task import Profile  # noqa: E402
from lab.runner.batch import BatchSummary, build_study_models, run_manifest  # noqa: E402
from lab.runner.episode import (  # noqa: E402
    EpisodeResult,
    RunnerConfig,
    RunnerError,
    run_spec,
)


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m lab.runner", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run one scripted episode")
    run.add_argument("--policy", required=True, choices=SCRIPTED_POLICIES)
    run.add_argument("--world", type=World, choices=list(World), default=World.LOST_ACK)
    run.add_argument("--status", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--rep", type=int, default=0, help="repetition number for make_spec")
    run.add_argument("--manifest", type=Path, help="read the episode from this manifest file")
    run.add_argument("--episode", help="episode id inside --manifest")
    run.add_argument("--max-turns", type=int, help="override RunnerConfig.max_turns")
    run.add_argument("--deadline-s", type=float, help="override RunnerConfig.episode_deadline_s")
    study = commands.add_parser("study", help="run the behaviour matrix against the real models")
    study.add_argument("--reps", type=int, default=12, help="repetitions per world x status cell")
    study.add_argument("--seed", type=int, default=20261004, help="manifest seed")
    study.add_argument("--experiment-name", default="behaviour-study")
    return parser.parse_args(argv)


def _spec_and_manifest(args: argparse.Namespace) -> tuple[EpisodeSpec, str, str]:
    """The spec to run, the manifest hash and the experiment name."""
    if args.manifest is None:
        if args.episode is not None:
            raise RunnerError("--episode needs --manifest")
        spec = make_spec("cli", args.world, Mode.AGENT_DIRECTED, args.status, args.rep)
        manifest = Manifest(name="cli", seed=0, episodes=(spec,))
        return spec, manifest.sha256(), manifest.name
    if args.episode is None:
        raise RunnerError("--manifest needs --episode")
    manifest = Manifest.load(args.manifest)
    by_id = {episode.episode_id: episode for episode in manifest.episodes}
    if args.episode not in by_id:
        raise RunnerError(f"episode {args.episode!r} is not in {args.manifest}")
    return by_id[args.episode], manifest.sha256(), manifest.name


def _summary(result: EpisodeResult) -> dict[str, object]:
    return {
        "experiment_id": result.experiment_id,
        "run_id": result.run_id,
        "episode_id": result.episode_id,
        "namespace": result.namespace,
        "claimed_outcome": result.claimed_outcome,
        "ended_by": result.ended_by,
        "verdict": result.score.verdict.value,
        "correct_recovery": result.score.correct_recovery,
    }


def _study(args: argparse.Namespace, environ: Mapping[str, str] | None) -> BatchSummary:
    """Run the matrix with each study model in turn; the first one that completes is the study.

    A model that completes nothing and failed at least once never worked, so the next one is
    tried (each model is its own experiment, so nothing is mixed). If none works, the last
    summary is returned."""
    if args.reps < 1:
        raise RunnerError(f"--reps must be at least 1, got {args.reps}")
    config = RunnerConfig.from_env(environ)
    manifest = behavior_matrix(reps=args.reps, seed=args.seed)
    models = build_study_models(config)
    if not models:
        raise RunnerError("no study models")
    summary: BatchSummary | None = None
    for model in models:
        summary = run_manifest(manifest, model, config, experiment_name=args.experiment_name)
        if summary.completed > 0 or summary.failed == 0:
            break
    assert summary is not None
    return summary


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = _parse(argv)
    if args.command == "study":
        try:
            obs.configure(environ)
            try:
                summary = _study(args, environ)
            finally:
                obs.flush()
        except RunnerError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(dataclasses.asdict(summary)))
        return 0
    try:
        spec, manifest_sha256, name = _spec_and_manifest(args)
        if spec.profile is not Profile.CONTROLLED:
            # The scripted policies send the controlled profile's argument set (scripted.py).
            raise RunnerError(
                f"scripted policies only run the controlled profile, not {spec.profile}"
            )
        overrides = {}
        if args.max_turns is not None:
            overrides["max_turns"] = args.max_turns
        if args.deadline_s is not None:
            overrides["episode_deadline_s"] = args.deadline_s
        config = RunnerConfig.from_env(environ, **overrides)
        obs.configure(environ)  # tracing only if OTEL_EXPORTER_OTLP_ENDPOINT is set
        try:
            result = run_spec(
                spec,
                ScriptedModel(args.policy),
                config,
                manifest_sha256=manifest_sha256,
                experiment_name=name,
            )
        finally:
            obs.flush()
    except RunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(_summary(result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
