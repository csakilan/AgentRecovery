import httpx
import psycopg
import pytest

from lab.agent.openrouter import OpenRouterClient
from lab.runtime.budget import BudgetExceeded, RequestBudget

FREE = "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_a_request_over_the_daily_ceiling_never_leaves(runtime_db):
    sent = {"n": 0}

    def handler(request):
        sent["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    with psycopg.connect(runtime_db["svc"], autocommit=True) as conn:
        budget = RequestBudget(conn, daily_limit=1)
        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = OpenRouterClient(
            FREE, api_key="k", http_client=http, min_interval_s=0.0, budget=budget
        )
        client.complete([{"role": "user", "content": "x"}], [])  # claims the 1 allowed
        with pytest.raises(BudgetExceeded):
            client.complete([{"role": "user", "content": "x"}], [])
    assert sent["n"] == 1  # the over-budget request was never sent
