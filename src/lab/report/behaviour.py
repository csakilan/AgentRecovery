"""The behaviour-study report: per-condition verdict counts with Wilson intervals.

This module reads the runtime database (`runs`, `results`, `experiments`) and that is
allowed. The rule that the evaluator must not read the runtime database binds the evaluator,
the code that decides a verdict from the ledger and the claim. A report only adds up verdicts
that were already decided and stored, so nothing it reads can change one. Nothing here
imports the evaluator's ledger reader or touches the ledger database.

A cell is one (world, status) pair, where status says whether the status tool was available.
Cells pool every mode and prompt variant present in the experiment, which is right for the
single-mode behaviour study and would hide a mode difference in a mixed experiment.

Every number in the rendered text is computed from the cells it is given.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psycopg

from lab.oracle.score import Verdict

REPORT_STEM = "behaviour-findings"
_EPISODE_ID = re.compile(
    r"^(?P<name>.+?)/(?P<world>[^/]+)/(?P<status>status|blind)/(?P<mode>[^/]+)"
    r"/r(?P<repetition>\d+)(?:/(?P<prompt>[^/]+))?$"
)


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval for a proportion; (0.0, 0.0) when there are no trials."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    # The bounds are exactly 0 and 1 at the extremes; floating point lands a hair off.
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == n else min(1.0, centre + half)
    return (low, high)


@dataclass(frozen=True)
class ResultRow:
    """One scored trial: the stored verdict plus the condition parsed from its episode id."""

    world: str
    status: bool  # True when the status tool was available
    verdict: str  # a Verdict member name, for example "DUPLICATE"
    correct_recovery: bool
    eligible_recoverable: bool
    run_id: str = ""
    episode_id: str = ""
    mode: str = ""
    repetition: int = 0


@dataclass(frozen=True)
class CellSummary:
    world: str
    status: bool
    n: int
    verdicts: Counter[str]
    eligible: int  # trials that could be recovered at all
    recovered: int  # eligible trials that were correct recoveries
    correct_recovery_rate: float  # recovered / eligible, 0.0 when nothing was eligible
    interval: tuple[float, float] = field(default=(0.0, 0.0))  # Wilson, on recovered / eligible


def summarise(rows: Iterable[ResultRow]) -> list[CellSummary]:
    """Group trials by (world, status), in world then status-first order."""
    grouped: dict[tuple[str, bool], list[ResultRow]] = {}
    for row in rows:
        grouped.setdefault((row.world, row.status), []).append(row)
    cells = []
    for (world, status), members in sorted(
        grouped.items(), key=lambda kv: (kv[0][0], not kv[0][1])
    ):
        eligible = sum(1 for r in members if r.eligible_recoverable)
        recovered = sum(1 for r in members if r.eligible_recoverable and r.correct_recovery)
        cells.append(
            CellSummary(
                world=world,
                status=status,
                n=len(members),
                verdicts=Counter(r.verdict for r in members),
                eligible=eligible,
                recovered=recovered,
                correct_recovery_rate=recovered / eligible if eligible else 0.0,
                interval=wilson_interval(recovered, eligible),
            )
        )
    return cells


def _status_label(status: bool) -> str:
    return "status tool available" if status else "status tool unavailable"


def render_markdown(
    cells: Iterable[CellSummary],
    model_id: str,
    experiment_id: str,
    manifest_sha256: str,
    reps: int,
    unscored: Mapping[str, int] | None = None,
) -> str:
    """Plain-prose findings: one table of raw verdict counts per cell, then its rate."""
    cells = list(cells)
    total = sum(c.n for c in cells)
    lines = [
        "# Behaviour findings",
        "",
        f"Model: {model_id}. Experiment: {experiment_id}. Manifest SHA-256: {manifest_sha256}.",
        "",
        f"This report covers {total} scored trials in {len(cells)} conditions. "
        f"Each condition was run {reps} times, which is an exploratory sample size. "
        "The intervals below are wide at this size, so the rates show a direction and are "
        "too imprecise to rank conditions that sit close together.",
        "",
        "A trial counts toward the correct-recovery rate only when it was eligible for recovery, "
        "meaning a fault that a careful agent could recover from. "
        "The interval is a 95 percent Wilson score interval on that rate. "
        "The raw verdict counts are given for every condition, including the ones at zero.",
        "",
    ]
    if unscored:
        parts = ", ".join(f"{count} {state}" for state, count in sorted(unscored.items()))
        lines += [
            f"Runs with no verdict are left out of every count above and below: {parts}.",
            "",
        ]
    names = [v.name for v in Verdict]
    for cell in cells:
        lines += [
            f"## {cell.world}, {_status_label(cell.status)}",
            "",
            "| Verdict | Count |",
            "| --- | --- |",
        ]
        extras = sorted(set(cell.verdicts) - set(names))
        lines += [f"| {name} | {cell.verdicts.get(name, 0)} |" for name in names + extras]
        lines += ["", f"Trials: {cell.n}."]
        if cell.eligible == 0:
            lines.append("No trial in this cell was eligible for recovery, so no rate is reported.")
        else:
            low, high = cell.interval
            lines.append(
                f"Correct recovery: {cell.recovered} of {cell.eligible} eligible trials, "
                f"a rate of {cell.correct_recovery_rate:.3f} "
                f"(95 percent Wilson interval {low:.3f} to {high:.3f})."
            )
        lines.append("")
    return "\n".join(lines)


def _parse_episode(episode_id: str) -> re.Match[str]:
    match = _EPISODE_ID.match(episode_id)
    if match is None:
        raise ValueError(f"episode id {episode_id!r} does not follow the make_spec layout")
    return match


def _read_experiment(
    conn: psycopg.Connection[Any], experiment_id: str
) -> tuple[str, str, list[ResultRow], dict[str, int]]:
    """Return (model_id, manifest_sha256, scored rows, run count by state for unscored runs)."""
    header = conn.execute(
        "SELECT manifest_sha256, config_json FROM experiments WHERE id = %s", (experiment_id,)
    ).fetchone()
    if header is None:
        raise LookupError(f"no experiment {experiment_id!r} in the runtime database")
    manifest_sha256, config = header
    records = conn.execute(
        "SELECT r.id, r.episode_id, r.state, s.verdict, s.correct_recovery, s.score_json"
        " FROM runs r LEFT JOIN results s ON s.run_id = r.id"
        " WHERE r.experiment_id = %s ORDER BY r.episode_id, r.id",
        (experiment_id,),
    ).fetchall()
    rows: list[ResultRow] = []
    unscored: Counter[str] = Counter()
    for run_id, episode_id, state, verdict, correct_recovery, score in records:
        if verdict is None:
            unscored[state] += 1
            continue
        episode = _parse_episode(episode_id)
        rows.append(
            ResultRow(
                world=episode["world"],
                status=episode["status"] == "status",
                verdict=Verdict(verdict).name,
                correct_recovery=correct_recovery,
                eligible_recoverable=score["eligible_recoverable"],
                run_id=run_id,
                episode_id=episode_id,
                mode=episode["mode"],
                repetition=int(episode["repetition"]),
            )
        )
    return config.get("model_id", "unknown"), manifest_sha256, rows, dict(unscored)


def _write_csv(path: Path, cells: list[CellSummary]) -> None:
    names = [v.name for v in Verdict]
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["world", "status", "n", *names, "eligible", "recovered", "rate", "ci_low", "ci_high"]
        )
        for c in cells:
            writer.writerow(
                [
                    c.world,
                    "status" if c.status else "blind",
                    c.n,
                    *(c.verdicts.get(name, 0) for name in names),
                    c.eligible,
                    c.recovered,
                    f"{c.correct_recovery_rate:.6f}",
                    f"{c.interval[0]:.6f}",
                    f"{c.interval[1]:.6f}",
                ]
            )


def write_report(experiment_id: str, runtime_url: str, out_dir: Path) -> Path:
    """Write the markdown, CSV and JSONL for one experiment and return the markdown path."""
    with psycopg.connect(runtime_url, autocommit=True) as conn:
        model_id, manifest_sha256, rows, unscored = _read_experiment(conn, experiment_id)
    cells = summarise(rows)
    reps = len({r.repetition for r in rows})
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = out_dir / f"{REPORT_STEM}.md"
    markdown.write_text(
        render_markdown(cells, model_id, experiment_id, manifest_sha256, reps, unscored)
    )
    _write_csv(out_dir / f"{REPORT_STEM}.csv", cells)
    with (out_dir / f"{REPORT_STEM}.jsonl").open("w") as handle:
        for row in rows:
            handle.write(json.dumps(asdict(row), sort_keys=True) + "\n")
    return markdown
