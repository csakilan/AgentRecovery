"""Exact text the model sees for each simulator response.

Formats reproduce RetryLedger's tool results byte for byte, including the em dash in
status lines, because the compatibility profile promises identical observations.
"""

from __future__ import annotations

from typing import Any

REASONS = {
    400: "Bad Request",
    404: "Not Found",
    409: "Conflict",
    500: "Internal Server Error",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


def reason_phrase(http_status: int) -> str:
    """The reason phrase in "HTTP <status> <reason>: ..." lines ("Error" if not listed).

    200 is "OK". `render_charge` and `render_status` never print a reason for 200, so this
    changes nothing they produce; it is for callers that must describe a 200 they cannot
    render."""
    if http_status == 200:
        return "OK"
    return REASONS.get(http_status, "Error")


def _error_text(http_status: int, body: dict[str, Any]) -> str:
    return f"HTTP {http_status} {reason_phrase(http_status)}: {body['error']['message']}"


def render_charge(http_status: int, body: dict[str, Any]) -> str:
    if http_status == 200:
        return body["message"]
    return _error_text(http_status, body)


def render_status(http_status: int, body: dict[str, Any]) -> str:
    if http_status != 200:
        return _error_text(http_status, body)
    key = body["idempotency_key"]
    if body["status"] == "SUCCEEDED":
        return f"Status for '{key}': SUCCEEDED — ${body['charge']['amount']} charged."
    return f"Status for '{key}': NOT_FOUND — no charge recorded."
