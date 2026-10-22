import psycopg
import pytest

from lab.execution.types import LeaseLost
from lab.runner.ownership import claim_run, finish_run, renew_run
from tests.runner.test_prepared import prepare


@pytest.mark.parametrize("mode", ["agent_directed", "deterministic_retry", "durable_gateway"])
def test_only_one_live_delivery_and_monotonic_takeover(runner_config, rt, mode):
    prepared = prepare(runner_config)
    rt.execute("UPDATE runs SET mode=%s WHERE id=%s", (mode, prepared.run_id))
    with psycopg.connect(runner_config.runtime_url, autocommit=True) as other:
        first = claim_run(rt, prepared.run_id, "first")
        assert first is not None
        assert claim_run(other, prepared.run_id, "second") is None
        rt.execute(
            "UPDATE runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (first.run_id,),
        )
        replacement = claim_run(other, prepared.run_id, "second")
        assert replacement.version > first.version
        assert renew_run(rt, first) is False
        with pytest.raises(LeaseLost):
            finish_run(
                rt,
                first,
                claimed_outcome="succeeded",
                finished_at=prepared.started_at,
                state="completed",
            )
        assert renew_run(other, replacement)
        finish_run(
            other,
            replacement,
            claimed_outcome="succeeded",
            finished_at=prepared.started_at,
            state="completed",
        )
        assert claim_run(rt, prepared.run_id, "third") is None


def test_pending_setup_is_not_claimable(runner_config, rt):
    prepared = prepare(runner_config)
    rt.execute("UPDATE runs SET setup_state='pending' WHERE id=%s", (prepared.run_id,))
    assert claim_run(rt, prepared.run_id, "worker") is None


def test_heartbeat_reports_failed_renewal():
    from lab.execution.leases import LeaseHeartbeat

    heartbeat = LeaseHeartbeat(lambda: False)
    assert heartbeat.tick_once() is False
    with pytest.raises(LeaseLost):
        heartbeat.check()


def test_heartbeat_renews_while_model_is_blocked(runner_config, rt):
    import threading

    from lab.execution.leases import LeaseHeartbeat

    prepared = prepare(runner_config)
    claim = claim_run(rt, prepared.run_id, "worker")
    entered, release = threading.Event(), threading.Event()

    def model():
        entered.set()
        assert release.wait(5)

    thread = threading.Thread(target=model)
    thread.start()
    try:
        assert entered.wait(5)
        before = rt.execute(
            "SELECT lease_expires_at FROM runs WHERE id=%s", (claim.run_id,)
        ).fetchone()
        with psycopg.connect(runner_config.runtime_url, autocommit=True) as heartbeat_conn:
            heartbeat = LeaseHeartbeat(lambda: renew_run(heartbeat_conn, claim))
            assert heartbeat.tick_once()
            heartbeat.check()
        after = rt.execute(
            "SELECT lease_expires_at FROM runs WHERE id=%s", (claim.run_id,)
        ).fetchone()
        assert after["lease_expires_at"] > before["lease_expires_at"]
    finally:
        release.set()
        thread.join(5)
