import json
import time

import httpx
import pytest

from lab.agent import openrouter
from lab.agent.openrouter import OpenRouterClient, OpenRouterError
from lab.agent.tools import tool_schemas
from lab.protocol.task import Profile
from lab.runtime.budget import ModelNotAllowed

FREE = "nvidia/nemotron-3-ultra-550b-a55b:free"
USER_X = [{"role": "user", "content": "x"}]


@pytest.fixture
def slept(monkeypatch):
    """Record backoff and pacing sleeps instead of waiting for them."""
    seconds: list[float] = []
    monkeypatch.setattr(openrouter, "_sleep", seconds.append)
    return seconds


def _client(handler, **kw):
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return OpenRouterClient(FREE, api_key="k", http_client=http, min_interval_s=0.0, **kw)


def test_refuses_a_paid_model_at_construction():
    with pytest.raises(ModelNotAllowed):
        OpenRouterClient("openai/gpt-4o", api_key="k")


def test_sends_bearer_key_model_and_converted_tools_and_parses_reply():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "hi",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "report_done",
                                        "arguments": '{"outcome":"succeeded"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    client = _client(handler)
    turn = client.complete(
        [{"role": "user", "content": "go"}], tool_schemas(Profile.CONTROLLED, True)
    )
    assert seen["auth"] == "Bearer k"
    assert seen["url"].endswith("/chat/completions")
    assert seen["body"]["model"] == FREE
    assert seen["body"]["tools"][0]["type"] == "function"
    assert turn.tool_calls[0].name == "report_done"
    assert client.model_id == FREE


def test_posts_to_the_configured_base_url():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _client(handler, base_url="http://127.0.0.1:9/v1/").complete(USER_X, [])
    assert seen["url"] == "http://127.0.0.1:9/v1/chat/completions"


def test_tools_are_omitted_from_the_body_when_the_list_is_empty():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _client(handler).complete(USER_X, [])
    assert "tools" not in seen["body"]


def test_retries_a_429_then_succeeds(slept):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "rate"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    turn = _client(handler, max_retries=3).complete(USER_X, [])
    assert calls["n"] == 2 and turn.text == "ok"
    assert slept == [0.0]


def test_retry_after_header_sets_the_wait(slept):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "7"}, json={"error": "rate"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _client(handler, max_retries=3).complete(USER_X, [])
    assert slept == [7.0]


def test_backoff_grows_exponentially_without_a_retry_after_header(slept):
    def handler(request):
        return httpx.Response(503, json={"error": "down"})

    with pytest.raises(OpenRouterError):
        _client(handler, max_retries=3).complete(USER_X, [])
    assert slept == [1.0, 2.0, 4.0]


def test_raises_openrouter_error_after_retries_exhausted(slept):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, json={"error": "down"})

    with pytest.raises(OpenRouterError, match="503"):
        _client(handler, max_retries=2).complete(USER_X, [])
    assert calls["n"] == 3


def test_a_transport_error_is_retried_then_succeeds(slept):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    turn = _client(handler, max_retries=2).complete(USER_X, [])
    assert calls["n"] == 2 and turn.text == "ok"


def test_a_transport_error_that_never_clears_raises(slept):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(OpenRouterError):
        _client(handler, max_retries=1).complete(USER_X, [])


def test_a_non_retryable_4xx_raises_at_once_without_leaking_the_key(slept):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, text="bad credentials")

    with pytest.raises(OpenRouterError, match="401") as info:
        _client(handler, max_retries=3).complete(USER_X, [])
    assert calls["n"] == 1
    assert "Bearer" not in str(info.value)


def test_a_200_with_a_garbage_body_is_malformed_not_raised():
    def handler(request):
        return httpx.Response(200, json={"unexpected": True})

    turn = _client(handler).complete(USER_X, [])
    assert turn.malformed is True


def test_a_200_that_is_not_json_is_malformed_with_the_raw_body():
    def handler(request):
        return httpx.Response(200, text="<html>gateway hiccup</html>" + "z" * 5000)

    turn = _client(handler).complete(USER_X, [])
    assert turn.malformed is True
    assert turn.tool_calls == ()
    assert turn.raw["raw_body"].startswith("<html>gateway hiccup</html>")
    assert len(turn.raw["raw_body"]) == 2000


def test_a_200_with_a_json_nesting_bomb_is_malformed_not_raised():
    def handler(request):
        return httpx.Response(200, text="[" * 200_000)

    turn = _client(handler).complete(USER_X, [])
    assert turn.malformed is True


def test_pacing_keeps_consecutive_requests_min_interval_apart():
    stamps: list[float] = []

    def handler(request):
        stamps.append(time.monotonic())
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenRouterClient(
        FREE,
        api_key="k",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        min_interval_s=0.05,
    )
    client.complete(USER_X, [])
    client.complete(USER_X, [])
    assert stamps[1] - stamps[0] >= 0.05


def test_the_key_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    OpenRouterClient(FREE, http_client=http, min_interval_s=0.0).complete(USER_X, [])
    assert seen["auth"] == "Bearer from-env"


def test_no_key_anywhere_is_an_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(OpenRouterError):
        OpenRouterClient(FREE)
