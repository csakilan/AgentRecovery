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


def _rows(conn):
    return conn.execute("SELECT day, requests_made, updated_at FROM request_budget").fetchall()


def test_a_first_claim_equal_to_the_limit_succeeds(runtime_conn):
    budget = RequestBudget(runtime_conn, daily_limit=900)
    assert budget.claim(900) == 900
    assert budget.used_today() == 900


def test_a_first_claim_one_over_the_limit_is_refused(runtime_conn):
    """The reported hole: with no row for today the statement takes the INSERT path, which
    used to check nothing, so the first claim of the day could exceed the whole limit."""
    budget = RequestBudget(runtime_conn, daily_limit=900)
    with pytest.raises(BudgetExceeded):
        budget.claim(901)
    assert budget.used_today() == 0


def test_a_first_claim_far_over_the_limit_is_refused(runtime_conn):
    budget = RequestBudget(runtime_conn, daily_limit=900)
    with pytest.raises(BudgetExceeded):
        budget.claim(1000)
    with pytest.raises(BudgetExceeded):
        budget.claim(10**6)
    assert budget.used_today() == 0


def test_a_first_claim_beyond_the_column_range_is_refused_not_an_sql_error(runtime_conn):
    """requests_made is a 32-bit INTEGER. A claim past that range must still be a refusal
    and not a NumericValueOutOfRange from the database."""
    budget = RequestBudget(runtime_conn, daily_limit=900)
    with pytest.raises(BudgetExceeded):
        budget.claim(2**40)
    assert budget.used_today() == 0


def test_a_refused_first_claim_leaves_no_row_behind(runtime_conn):
    budget = RequestBudget(runtime_conn, daily_limit=900)
    with pytest.raises(BudgetExceeded):
        budget.claim(1000)
    assert _rows(runtime_conn) == []
    assert budget.claim(1) == 1, "a refusal must not block later, legitimate claims"


def test_a_refused_claim_leaves_an_existing_row_unchanged(runtime_conn):
    budget = RequestBudget(runtime_conn, daily_limit=10)
    budget.claim(4)
    before = _rows(runtime_conn)
    with pytest.raises(BudgetExceeded):
        budget.claim(7)
    with pytest.raises(BudgetExceeded):
        budget.claim(10**6)
    assert _rows(runtime_conn) == before
    assert budget.used_today() == 4


def test_claiming_one_at_a_time_reaches_the_limit_exactly(runtime_conn):
    budget = RequestBudget(runtime_conn, daily_limit=25)
    assert [budget.claim() for _ in range(25)] == list(range(1, 26))
    with pytest.raises(BudgetExceeded):
        budget.claim()
    assert budget.used_today() == 25


def test_concurrent_first_claims_on_a_fresh_day_respect_the_limit(runtime_db):
    """Eight callers race to create today's row. Each claims 3 against a limit of 7, so
    exactly two may succeed. This exercises the INSERT path and the INSERT-then-conflict
    handover with claims larger than 1, which the test below does not."""
    import threading

    results = []
    barrier = threading.Barrier(8)

    def worker():
        with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
            budget = RequestBudget(conn, daily_limit=7)
            barrier.wait()
            try:
                budget.claim(3)
                results.append("ok")
            except BudgetExceeded:
                results.append("refused")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert results.count("ok") == 2
    assert results.count("refused") == 6
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        assert RequestBudget(conn, daily_limit=7).used_today() == 6


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
