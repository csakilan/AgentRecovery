from datetime import timedelta

import pytest
from pydantic import ValidationError

from tests.execution.helpers import FakeClock


def test_defaults_are_serializable_and_finite():
    from lab.execution.config import ExecutionConfig

    settings = ExecutionConfig()
    assert settings.model_dump(mode="json") == {
        "execution_protocol_version": 1,
        "resume_protocol_version": 1,
        "b_max_attempts": 3,
        "b_backoff_s": [1.0, 2.0],
        "c_max_attempts": 3,
        "operation_timeout_s": 120.0,
        "lease_s": 30.0,
        "renew_every_s": 10.0,
        "status_backoff_s": [1.0, 2.0, 4.0, 8.0],
        "tick_s": 2.0,
    }
    assert ExecutionConfig.model_validate_json(settings.model_dump_json()) == settings
    with pytest.raises(ValidationError):
        settings.b_max_attempts = 8


@pytest.mark.parametrize(
    "values",
    [
        {"execution_protocol_version": 2},
        {"resume_protocol_version": 0},
        {"execution_protocol_version": True},
        {"unknown_setting": 1},
        {"b_max_attempts": 0},
        {"b_max_attempts": True},
        {"c_max_attempts": -1},
        {"b_max_attempts": 4},
        {"b_backoff_s": [1, 0]},
        {"b_backoff_s": [1, float("nan")]},
        {"status_backoff_s": []},
        {"status_backoff_s": [float("inf")]},
        {"lease_s": 0},
        {"operation_timeout_s": float("inf")},
        {"operation_timeout_s": float("nan")},
        {"renew_every_s": 30},
        {"renew_every_s": -1},
        {"tick_s": 0},
    ],
)
def test_invalid_recovery_settings_cannot_start_a_run(values):
    from lab.execution.config import ExecutionConfig

    with pytest.raises(ValidationError):
        ExecutionConfig(**values)


def test_one_send_configuration_needs_no_retry_wait():
    from lab.execution.config import ExecutionConfig

    assert ExecutionConfig(b_max_attempts=1, b_backoff_s=()).b_max_attempts == 1


def test_deadline_is_exclusive_and_never_has_negative_remaining_time():
    from lab.execution.clock import DeadlineBudget, DeadlineExceeded

    clock = FakeClock()
    budget = DeadlineBudget(clock.now() + timedelta(seconds=3), clock)
    assert budget.remaining_s() == 3
    budget.check()
    clock.advance(3)
    with pytest.raises(DeadlineExceeded):
        budget.check()
    assert budget.remaining_s() == 0
    clock.advance(2)
    assert budget.remaining_s() == 0


def test_naive_deadlines_are_refused():
    from lab.execution.clock import DeadlineBudget

    clock = FakeClock()
    with pytest.raises(ValueError):
        DeadlineBudget(clock.now().replace(tzinfo=None), clock)


def test_existing_runner_still_allows_a_zero_episode_deadline():
    from lab.runner.episode import RunnerConfig

    config = RunnerConfig(
        sim_public_url="http://sim",
        sim_control_url="http://control",
        client_token="c",
        control_token="t",
        runtime_url="runtime",
        ledger_url="ledger",
        episode_deadline_s=0,
    )
    assert config.episode_deadline_s == 0
