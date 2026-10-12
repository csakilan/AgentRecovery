"""The HTTP client the agent's tool calls go through, bound to a trusted context.

Namespace isolation in the simulator rests entirely on the `X-Lab-Namespace` header, and
nothing on the wire stops a runner from deriving it from something a model produced. This
module closes that gap structurally. The namespace comes from a `TrustedContext` the runner
builds from its own records, it is read once at construction, and no method here has a
namespace parameter, so a later change cannot route model output into the header without
adding one (tests/agent/test_client_binding.py pins that with `inspect.signature`).

A model can still put a namespace inside its tool arguments. Those arguments become the JSON
body of a charge, and the body is not an isolation boundary, but forwarding them would let a
run appear to address another namespace and would muddy the record of what was requested.
So a charge whose arguments carry a namespace-like top-level key is not sent at all. The
client returns `(DENIED_STATUS, body)` and appends a `Denial` to `client.denials`. The denial
is data for the caller to record, not an exception: the agent loop maps it onto a
`tool_events` row with `http_status` NULL and `rule` set to `Denial.rule`.

Everything else the simulator says is passed through unjudged as `(status_code, dict)`.
Transport failures (connection refused, timeout) propagate as the httpx exception, because
the loop and the harness decide what an unreachable simulator means.
"""

from __future__ import annotations

import copy
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, StringConstraints

# The status returned with a denial. It is not an HTTP status: no HTTP request was sent.
# Callers must store a denial as http_status NULL, never as this number.
DENIED_STATUS = 0

# The `rule` of a denial and the `error.code` of its body.
NAMESPACE_DENIED = "namespace_argument_denied"

_DEFAULT_TIMEOUT = 30.0

_NonEmpty = Annotated[str, StringConstraints(strict=True, min_length=1)]


class TrustedContext(BaseModel):
    """What the runner knows about a run. Nothing in it comes from a model.

    All five fields are required non-empty strings. The client reads `namespace` for the
    header on every request; the other fields are carried so the agent loop can label what it
    records without a second source of truth.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: _NonEmpty
    namespace: _NonEmpty
    run_id: _NonEmpty
    episode_id: _NonEmpty
    task_id: _NonEmpty


@dataclass(frozen=True)
class Denial:
    """One refused tool call. `rule` is the string for `tool_events.rule`."""

    tool: str
    requested_args: dict[str, Any]
    rule: str
    reason: str


def _is_namespace_like(key: object) -> bool:
    """True when `key` spells "namespace" once case, width and punctuation are ignored.

    "namespace", "Namespace", "x-lab-namespace" and "X_Lab_Namespace" all match, as do
    full-width letters and a key with an invisible character inside it. The test is a
    substring one on purpose: a key such as "target_namespace_id" is still a namespace.
    """
    folded = unicodedata.normalize("NFKC", str(key)).casefold()
    return "namespace" in "".join(ch for ch in folded if ch.isalnum())


def _encode_path_segment(key: str) -> str:
    """Percent-encode `key` so it stays one URL path segment.

    `quote(safe="")` covers "/", "?", "#" and "%". It leaves "." alone, and httpx collapses a
    segment that is exactly "." or ".." as a relative reference, so those two are written
    as "%2E" to keep them a literal key.
    """
    encoded = quote(key, safe="")
    if encoded and set(encoded) == {"."}:
        return encoded.replace(".", "%2E")
    return encoded


def _decode(response: httpx.Response) -> tuple[int, dict[str, Any]]:
    """Return `(status_code, body)`. A body that is not a JSON object becomes `raw_body`."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        return response.status_code, body
    return response.status_code, {"raw_body": response.text}


class SimulatorClient:
    """Calls the simulator's public API as one run, in the namespace of `ctx`.

    `http_client` and `timeout` exist so a test can inject a client (a starlette TestClient
    or one with an `httpx.MockTransport`). Neither can carry a namespace: the header is set
    on every request from `ctx.namespace`, over anything a supplied client has by default.
    A client passed in is the caller's to close and keeps its own timeout; `timeout` applies
    to the client made here, which `close()` closes.
    """

    def __init__(
        self,
        public_url: str,
        client_token: str,
        ctx: TrustedContext,
        *,
        http_client: httpx.Client | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = public_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {client_token}",
            "X-Lab-Namespace": ctx.namespace,
        }
        self._ctx = ctx
        self._owns_client = http_client is None
        self._http = http_client if http_client is not None else httpx.Client(timeout=timeout)
        self._denials: list[Denial] = []

    @property
    def ctx(self) -> TrustedContext:
        return self._ctx

    @property
    def denials(self) -> list[Denial]:
        """Every call this client refused, oldest first. A copy: appending to it changes nothing."""
        return list(self._denials)

    def charge(self, args: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """POST the model's arguments, unchanged, as the JSON body of a charge.

        A dict with a namespace-like top-level key is denied whole (not stripped): nothing
        is sent, `(DENIED_STATUS, error body)` is returned and a `Denial` is recorded.
        Arguments that are not a dict have no keys to check and are sent as given, so the
        simulator answers them as it answers any malformed body (422).
        """
        if isinstance(args, dict):
            offending = sorted(str(k) for k in args if _is_namespace_like(k))
            if offending:
                return self._deny("charge", args, offending)
        response = self._http.post(
            f"{self._base_url}/v1/charges",
            json=args,
            headers=self._headers,
        )
        return _decode(response)

    def status(self, key: str) -> tuple[int, dict[str, Any]]:
        """GET the payment recorded under idempotency key `key`, in this client's namespace."""
        response = self._http.get(
            f"{self._base_url}/v1/payments/{_encode_path_segment(key)}",
            headers=self._headers,
        )
        return _decode(response)

    def close(self) -> None:
        """Close the HTTP client if this object made it."""
        if self._owns_client:
            self._http.close()

    def _deny(
        self, tool: str, args: dict[str, Any], offending: list[str]
    ) -> tuple[int, dict[str, Any]]:
        message = (
            f"Tool arguments must not contain a namespace ({', '.join(offending)}). "
            "The namespace is fixed for this run and cannot be set by a tool call."
        )
        self._denials.append(Denial(tool, copy.deepcopy(args), NAMESPACE_DENIED, message))
        return DENIED_STATUS, {"error": {"code": NAMESPACE_DENIED, "message": message}}
