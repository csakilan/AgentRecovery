import json
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from lab.oracle.score import Verdict
from lab.report.behaviour import (
    CellSummary,
    ResultRow,
    render_markdown,
    summarise,
    wilson_interval,
    write_report,
)


def _row(world, status, verdict, *, correct_recovery, eligible, repetition=0):
    return ResultRow(
        run_id=f"run-{uuid.uuid4().hex[:8]}",
        episode_id=f"t/{world}/{'status' if status else 'blind'}/agent_directed/r{repetition:02d}",
        world=world,
        status=status,
        mode="agent_directed",
        repetition=repetition,
        verdict=verdict,
        correct_recovery=correct_recovery,
        eligible_recoverable=eligible,
    )


def test_wilson_interval_is_correct_and_bounded():
    lo, hi = wilson_interval(8, 10)
    assert 0.0 <= lo < 0.8 < hi <= 1.0
    assert wilson_interval(0, 0) == (0.0, 0.0)  # no trials -> empty interval, no divide-by-zero


def test_wilson_interval_matches_the_published_formula():
    # 8 of 10 at z = 1.96: centre 0.7 / 1.0384 ..., the textbook value is about (0.490, 0.943).
    lo, hi = wilson_interval(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-3)
    assert hi == pytest.approx(0.9433, abs=1e-3)
    assert wilson_interval(0, 5)[0] == 0.0
    assert wilson_interval(5, 5)[1] == 1.0


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
    assert la.correct_recovery_rate == 0.5
    assert (la.recovered, la.eligible) == (1, 2)
    assert la.interval == wilson_interval(1, 2)
    assert len(cells) == 2


def test_rate_counts_only_eligible_trials():
    rows = [
        _row("a", True, "CORRECT", correct_recovery=True, eligible=True),
        _row("a", True, "CORRECT", correct_recovery=True, eligible=False),  # ignored by the rate
        _row("a", True, "MISSED", correct_recovery=False, eligible=True),
    ]
    (cell,) = summarise(rows)
    assert cell.n == 3
    assert (cell.recovered, cell.eligible) == (1, 2)
    assert cell.correct_recovery_rate == 0.5


def test_a_cell_with_no_eligible_trial_has_rate_zero_and_empty_interval():
    (cell,) = summarise([_row("a", False, "MISSED", correct_recovery=False, eligible=False)])
    assert cell.eligible == 0
    assert cell.correct_recovery_rate == 0.0
    assert cell.interval == (0.0, 0.0)


def _cells() -> list[CellSummary]:
    rows = [
        _row("lost_ack", True, "CORRECT", correct_recovery=True, eligible=True),
        _row("lost_ack", True, "DUPLICATE", correct_recovery=False, eligible=True),
        _row("lost_ack", True, "DUPLICATE", correct_recovery=False, eligible=True),
        _row("perm_fail", False, "CORRECT_NO_EFFECT", correct_recovery=False, eligible=False),
    ]
    return summarise(rows)


def test_markdown_carries_provenance_raw_counts_and_the_exploratory_sentence():
    text = render_markdown(
        _cells(),
        model_id="vendor/model-x",
        experiment_id="exp-77",
        manifest_sha256="ab12cd",
        reps=7,
    )
    for needed in (
        "vendor/model-x",
        "exp-77",
        "ab12cd",
        "exploratory",
        "between 1 and 3 scored trials",
    ):
        assert needed in text
    assert "—" not in text and "–" not in text  # no em or en dashes
    # Raw counts are written from the cells: lost_ack has 3 trials, 2 of them DUPLICATE.
    assert "| DUPLICATE | 2 |" in text
    assert "| CORRECT | 1 |" in text
    assert "1 of 3" in text  # recovered of eligible
    lo, hi = wilson_interval(1, 3)
    assert f"{lo:.3f}" in text and f"{hi:.3f}" in text
    # perm_fail had no eligible trial, so it reports no rate rather than a made-up zero.
    assert "No trial in this cell was eligible" in text


def test_markdown_lists_every_verdict_even_at_zero_and_follows_the_data():
    text = render_markdown(_cells(), model_id="m", experiment_id="e", manifest_sha256="h", reps=3)
    for verdict in Verdict:
        assert f"| {verdict.name} |" in text
    other = summarise([_row("lost_ack", True, "MISSED", correct_recovery=False, eligible=True)] * 4)
    changed = render_markdown(other, model_id="m", experiment_id="e", manifest_sha256="h", reps=3)
    assert "| MISSED | 4 |" in changed and "| DUPLICATE | 2 |" not in changed


def test_markdown_reports_runs_that_have_no_verdict():
    text = render_markdown(
        _cells(),
        model_id="m",
        experiment_id="e",
        manifest_sha256="h",
        reps=3,
        unscored={"crashed": 2, "score_failed": 1},
    )
    assert "2 crashed" in text and "1 score_failed" in text


def _seed(admin_free_url, experiment_id, trials, *, model_id="vendor/model-x", sha="deadbeef"):
    """Insert an experiment with runs. Each trial is (episode_id, state, result or None), where
    a result is (verdict_value, correct_recovery, eligible_recoverable)."""
    with psycopg.connect(admin_free_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO experiments (id, name, manifest_sha256, config_json)"
            " VALUES (%s, %s, %s, %s)",
            (experiment_id, "t", sha, Jsonb({"model_id": model_id})),
        )
        for i, (episode_id, state, result) in enumerate(trials):
            run_id = f"{experiment_id}-run{i}"
            conn.execute(
                "INSERT INTO runs"
                " (id, experiment_id, episode_id, namespace, mode, thread_id, state)"
                " VALUES (%s, %s, %s, %s, 'agent_directed', %s, %s)",
                (run_id, experiment_id, episode_id, f"ns-{run_id}", f"th-{run_id}", state),
            )
            if result is not None:
                verdict, recovered, eligible = result
                conn.execute(
                    "INSERT INTO results (run_id, evaluator_version, verdict, correct_recovery,"
                    " score_json) VALUES (%s, 'v', %s, %s, %s)",
                    (run_id, verdict, recovered, Jsonb({"eligible_recoverable": eligible})),
                )


def test_write_report_reads_rows_and_writes_three_files(runtime_db, tmp_path):
    exp = f"exp-fake-{uuid.uuid4().hex[:6]}"
    other = f"exp-other-{uuid.uuid4().hex[:6]}"
    url = runtime_db["svc"]
    try:
        _seed(
            url,
            exp,
            [
                ("t/lost_ack/status/agent_directed/r00", "completed", ("correct", True, True)),
                ("t/lost_ack/status/agent_directed/r01", "completed", ("duplicate", False, True)),
                ("t/true_fail/blind/agent_directed/r00", "completed", ("missed", False, True)),
                ("t/true_fail/blind/agent_directed/r01", "crashed", None),
                (
                    "t/lost_ack/blind/agent_directed/r02/pressured",
                    "completed",
                    ("correct", True, True),
                ),
            ],
        )
        # Another experiment's results must not leak into this report.
        _seed(
            url,
            other,
            [("t/lost_ack/status/agent_directed/r00", "completed", ("false_success", False, True))],
        )
        md = write_report(exp, url, tmp_path)
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DELETE FROM experiments WHERE id = ANY(%s)", ([exp, other],))

    assert md.exists() and md.suffix == ".md"
    csv_path = tmp_path / "behaviour-findings.csv"
    jsonl_path = tmp_path / "behaviour-findings.jsonl"
    assert csv_path.exists() and jsonl_path.exists()
    text = md.read_text()
    assert "exploratory" in text
    assert exp in text and "vendor/model-x" in text and "deadbeef" in text
    assert "FALSE_SUCCESS" in text and "| FALSE_SUCCESS | 0 |" in text  # listed, with zero count
    assert "1 crashed" in text
    # Cells hold 2, 1 and 1 trials; the text must say so, not a pooled repetition count (3).
    assert "between 1 and 2 scored trials" in text and "3 times" not in text
    lines = [json.loads(line) for line in jsonl_path.read_text().splitlines()]
    assert len(lines) == 4  # the crashed run has no verdict, so no trial line
    assert {line["verdict"] for line in lines} == {"CORRECT", "DUPLICATE", "MISSED"}
    first = next(line for line in lines if line["episode_id"].endswith("r00") and line["status"])
    assert first["world"] == "lost_ack" and first["eligible_recoverable"] is True
    header, *body = csv_path.read_text().splitlines()
    assert header.startswith("world,status,n,")
    assert len(body) == 3  # (lost_ack, status), (lost_ack, blind), (true_fail, blind)


def test_write_report_rejects_an_unknown_experiment(runtime_db, tmp_path):
    with pytest.raises(LookupError):
        write_report("no-such-experiment", runtime_db["svc"], tmp_path)


def test_sample_size_wording_follows_the_cells_when_they_are_unequal():
    rows = [
        _row("a", True, "CORRECT", correct_recovery=True, eligible=True, repetition=i)
        for i in range(5)
    ] + [_row("b", True, "MISSED", correct_recovery=False, eligible=True, repetition=0)] * 2
    text = render_markdown(summarise(rows), "m", "e", "h", reps=5)
    assert "between 2 and 5 scored trials" in text
    assert "run 5 times" not in text and "run 7 times" not in text


def test_sample_size_wording_states_one_value_when_cells_are_equal():
    rows = [_row(w, True, "CORRECT", correct_recovery=True, eligible=True) for w in "ab" * 4]
    text = render_markdown(summarise(rows), "m", "e", "h", reps=99)
    assert "Each condition holds 4 scored trials" in text and "99" not in text


def test_an_empty_report_says_there_is_no_data_instead_of_describing_it():
    text = render_markdown([], "m", "e", "h", unscored={"crashed": 1})
    assert "No scored episodes yet" in text
    assert "Each condition" not in text and "intervals" not in text
    assert "1 crashed" in text
