import psycopg
import pytest

from lab.runtime.budget import (
    BudgetExceeded,
    ModelNotAllowed,
    RequestBudget,
    assert_model_allowed,
)


@pytest.mark.parametrize(
    "model_id",
    [
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "thinkingmachines/inkling:free",
    ],
)
def test_free_models_are_allowed(model_id):
    assert_model_allowed(model_id)


@pytest.mark.parametrize(
    "model_id",
    [
        "openai/gpt-5",
        "anthropic/claude-opus-5",
        "google/gemma-4-31b-it",
        "google/gemma-4-31b-it:nitro",
        "free:google/gemma-4-31b-it",
        ":free",
        "",
    ],
)
def test_everything_else_is_refused(model_id):
    """D1 allows no paid usage, so the allowlist is a code-level guard rather
    than a configuration value someone can mistype."""
    with pytest.raises(ModelNotAllowed):
        assert_model_allowed(model_id)


def test_claim_counts_up_and_reports(runtime_conn):
    budget = RequestBudget(runtime_conn, daily_limit=5)
    assert budget.used_today() == 0
    assert budget.claim() == 1
    assert budget.claim(3) == 4
    assert budget.used_today() == 4


def test_claim_refuses_to_exceed_the_limit(runtime_conn):
    budget = RequestBudget(runtime_conn, daily_limit=3)
    budget.claim(3)
    with pytest.raises(BudgetExceeded):
        budget.claim()
    assert budget.used_today() == 3, "a refused claim must not consume budget"


def test_a_partial_claim_is_all_or_nothing(runtime_conn):
    budget = RequestBudget(runtime_conn, daily_limit=5)
    budget.claim(4)
    with pytest.raises(BudgetExceeded):
        budget.claim(2)
    assert budget.used_today() == 4


def test_two_processes_share_one_ceiling(runtime_db):
    """A per-process counter would be defeated by running two runners, so the
    counter lives in the database."""
    import threading

    results = []
    barrier = threading.Barrier(8)

    def worker():
        with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
            budget = RequestBudget(conn, daily_limit=5)
            barrier.wait()
            try:
                budget.claim()
                results.append("ok")
            except BudgetExceeded:
                results.append("refused")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert results.count("ok") == 5
    assert results.count("refused") == 3
