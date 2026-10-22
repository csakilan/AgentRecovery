"""Optional worker hooks stop provider work without changing logical quota accounting."""

from datetime import timedelta

import httpx
import pytest

from lab.agent import openrouter
from lab.agent.openrouter import OpenRouterClient
from lab.execution.clock import DeadlineBudget, DeadlineExceeded, NoopGuard
from lab.execution.types import LeaseLost
from tests.execution.helpers import FakeClock


class Budget:
    def __init__(self):
        self.claims = 0

    def claim(self, n):
        self.claims += n


@pytest.mark.parametrize("reason", ["deadline", "ownership"])
def test_deadline_and_ownership_checked_before_retry(monkeypatch, reason):
    clock = FakeClock()
    budget = Budget()
    deadline = DeadlineBudget(clock.now() + timedelta(seconds=0.5), clock)
    sent = []

    class Guard:
        def check(self):
            if reason == "ownership" and sent:
                raise LeaseLost("replaced")

    def send(request):
        sent.append(request)
        assert max(request.extensions["timeout"].values()) <= 0.5
        return httpx.Response(503, json={})

    monkeypatch.setattr(openrouter, "_sleep", clock.sleep)
    monkeypatch.setattr(openrouter.time, "monotonic", clock.monotonic)
    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        model = OpenRouterClient(
            "example:free",
            api_key="test",
            http_client=http,
            min_interval_s=0,
            budget=budget,
            guard=Guard(),
            remaining_s=deadline.remaining_s,
        )
        with pytest.raises(DeadlineExceeded if reason == "deadline" else LeaseLost):
            model.complete([], [])
    assert len(sent) == budget.claims == 1


def test_initial_pacing_expiry_claims_no_second_quota(monkeypatch):
    clock = FakeClock()
    deadline = DeadlineBudget(clock.now() + timedelta(seconds=0.5), clock)
    budget = Budget()
    sent = []

    def send(request):
        sent.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    monkeypatch.setattr(openrouter, "_sleep", clock.sleep)
    monkeypatch.setattr(openrouter.time, "monotonic", clock.monotonic)
    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        model = OpenRouterClient(
            "example:free",
            api_key="test",
            http_client=http,
            budget=budget,
            guard=NoopGuard(),
            remaining_s=deadline.remaining_s,
        )
        model.complete([], [])
        with pytest.raises(DeadlineExceeded):
            model.complete([], [])
    assert len(sent) == budget.claims == 1
