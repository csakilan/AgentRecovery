import threading
import time
from collections import Counter

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


def test_requires_autocommit(runtime_db):
    """The atomicity argument assumes each claim commits on its own. On a transactional
    connection a claim could be rolled back after the model request had already been sent,
    under-counting real usage, so the budget refuses such a connection outright."""
    with psycopg.connect(runtime_db["svc"]) as c:
        with pytest.raises(ValueError, match="autocommit"):
            RequestBudget(c)
        assert c.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


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


# How long a worker waits at the barrier for the others. A worker that fails before it gets
# there either aborts the barrier, which releases the rest at once, or leaves them to time out
# here. Either way the race fails with a message instead of blocking the whole suite.
BARRIER_TIMEOUT = 10
# The most a whole race may take before its threads are reported as stuck.
RACE_DEADLINE = 30


def _race(url, *, workers, daily_limit, claim_n, sabotage=None):
    """Release `workers` callers at once, each claiming `claim_n` against `daily_limit`.

    Returns the list of "ok" / "refused" verdicts. Anything else is a failure of the test
    machinery or of the database, and it fails here, naming the real error, rather than
    surfacing later as a miscount that reads like a bug in the budget:

    * a worker that raises anything but BudgetExceeded is reported with its exception;
    * a worker that fails before the barrier aborts it, so the others fail at once instead of
      waiting for each other forever (the barrier also has a timeout, for a worker that dies
      without raising);
    * the threads are daemons, so a stuck one cannot keep the interpreter alive, and the join
      is bounded and checked, so a thread that never finished is a failure and not a pass.

    `sabotage(i)` is for the tests of this helper only: it runs in worker `i` just before the
    barrier, and may raise, or return "skip" to make that worker leave without arriving.
    """
    verdicts: list[str] = []
    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(workers)

    def worker(i):
        try:
            with psycopg.connect(url, autocommit=True) as conn:
                budget = RequestBudget(conn, daily_limit=daily_limit)
                if sabotage is not None and sabotage(i) == "skip":
                    return
                barrier.wait(timeout=BARRIER_TIMEOUT)
                try:
                    budget.claim(claim_n)
                    verdict = "ok"
                except BudgetExceeded:
                    verdict = "refused"
            with lock:
                verdicts.append(verdict)
        except threading.BrokenBarrierError:
            with lock:
                errors.append(
                    "BrokenBarrierError: a worker failed before reaching the barrier"
                    f" (or none arrived within {BARRIER_TIMEOUT}s)"
                )
        except Exception as exc:
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")
            barrier.abort()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(workers)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + RACE_DEADLINE
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))

    problems = []
    if errors:
        # The first entry is the original fault; the rest are usually workers it released.
        problems.append("workers raised unexpected errors (first is the original fault):")
        for error, count in Counter(errors).items():
            problems.append(f"  {error}" + (f"  (x{count})" if count > 1 else ""))
    stuck = sum(t.is_alive() for t in threads)
    if stuck:
        problems.append(f"{stuck} of {workers} workers had not finished after {RACE_DEADLINE}s")
    if problems:
        pytest.fail("\n".join(problems), pytrace=False)
    return verdicts


def test_concurrent_first_claims_on_a_fresh_day_respect_the_limit(runtime_db):
    """Eight callers race to create today's row. Each claims 3 against a limit of 7, so
    exactly two may succeed. This exercises the INSERT path and the INSERT-then-conflict
    handover with claims larger than 1, which the test below does not."""
    results = _race(runtime_db["svc"], workers=8, daily_limit=7, claim_n=3)

    assert results.count("ok") == 2
    assert results.count("refused") == 6
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        assert RequestBudget(conn, daily_limit=7).used_today() == 6


def test_two_processes_share_one_ceiling(runtime_db):
    """A per-process counter would be defeated by running two runners, so the
    counter lives in the database."""
    results = _race(runtime_db["svc"], workers=8, daily_limit=5, claim_n=1)

    assert results.count("ok") == 5
    assert results.count("refused") == 3
    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        assert RequestBudget(conn, daily_limit=5).used_today() == 5


def test_one_worker_failing_before_the_barrier_fails_the_race_promptly_and_says_why(runtime_db):
    """The reason the race helper exists: one worker failing before the barrier used to leave
    the other seven blocked for good, hanging the whole suite. It must fail fast, and the
    failure must name the fault rather than leave a miscount."""

    def sabotage(i):
        if i == 0:
            raise RuntimeError("injected failure before the barrier")

    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception) as excinfo:
        _race(runtime_db["svc"], workers=8, daily_limit=5, claim_n=1, sabotage=sabotage)
    elapsed = time.monotonic() - started

    message = str(excinfo.value)
    assert "RuntimeError: injected failure before the barrier" in message
    assert "failed before reaching the barrier" in message
    assert elapsed < BARRIER_TIMEOUT, "an early failure must not wait out the barrier timeout"


def test_a_worker_that_never_arrives_times_out_instead_of_hanging(runtime_db, monkeypatch):
    """A worker can also vanish without raising, so nothing aborts the barrier. The others must
    still give up, via the barrier timeout, and say why."""
    monkeypatch.setitem(globals(), "BARRIER_TIMEOUT", 1)

    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception) as excinfo:
        _race(
            runtime_db["svc"],
            workers=8,
            daily_limit=5,
            claim_n=1,
            sabotage=lambda i: "skip" if i == 0 else None,
        )
    elapsed = time.monotonic() - started

    assert "failed before reaching the barrier" in str(excinfo.value)
    assert elapsed < 10


def test_an_unexpected_worker_error_is_reported_as_itself_not_as_a_miscount(runtime_db):
    """claim(0) raises ValueError in every worker. That must surface as the ValueError, and
    not as `assert 0 == 5`, which would read as a counting bug in the budget."""
    with pytest.raises(pytest.fail.Exception) as excinfo:
        _race(runtime_db["svc"], workers=8, daily_limit=5, claim_n=0)

    assert "ValueError: n must be at least 1" in str(excinfo.value)


def test_a_worker_that_never_finishes_is_a_failure_not_a_silent_pass(runtime_db, monkeypatch):
    """Holding an exclusive lock on the table blocks every claim, so the workers are stuck
    inside `claim`. A bounded join that did not check the threads would let that look like an
    empty, passing race."""
    monkeypatch.setitem(globals(), "RACE_DEADLINE", 1)

    with psycopg.connect(runtime_db["admin"], autocommit=True) as blocker:
        blocker.execute("BEGIN")
        blocker.execute("LOCK TABLE request_budget IN ACCESS EXCLUSIVE MODE")
        try:
            with pytest.raises(pytest.fail.Exception) as excinfo:
                _race(runtime_db["svc"], workers=2, daily_limit=5, claim_n=1)
        finally:
            # Release the workers so they finish and the next test starts clean.
            blocker.execute("ROLLBACK")

    assert "2 of 2 workers had not finished" in str(excinfo.value)
