"""A session-wide guard that keeps the test suite off the internet.

Plan 2 is scoped to scripted models: nothing in it may reach a model provider, and no trace
of an episode may reach LangSmith. A stray real client in a later task would otherwise fail
only by being slow or by spending quota. This module makes it fail at once, loudly, in the
test that did it.

What it patches, for the whole session (the `network_guard` fixture in tests/conftest.py):

* `socket.socket.connect`, `connect_ex` and `sendto`: an address that is not loopback (and not
  a unix socket) raises `NetworkBlocked`. This is the layer every Python HTTP client ends at,
  httpx and asyncio included, and it also catches a UDP datagram to a resolver.
* `socket.getaddrinfo`, `gethostbyname` and `gethostbyname_ex`: a name that is not local
  raises `NetworkBlocked`, so a lookup cannot leak a hostname either. Numeric IP literals are
  not DNS and pass through to the connect check. `localhost` and `*.localhost` are local.

Loopback (127.0.0.0/8, ::1, localhost) stays open, because Postgres and the in-process
simulator use it.

Every refusal is recorded in `guard.blocked`. Code that swallows the exception (an HTTP
client turning it into a retry, a bare `except OSError`) therefore still fails the test:
the autouse fixture `fail_on_unacknowledged_network_attempts` checks the record after each
test. A test that attempts a connection on purpose wraps it in `guard.expecting()`.

Limits, stated so nobody over-trusts it: the guard lives inside this Python process. A child
process (subprocess, the replay workers) is not covered, and neither is a C library that
opens its own sockets, such as libpq. The tests in this suite start children only on
loopback.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest

_LOCAL_NAMES = {"localhost", "ip6-localhost", "localhost.localdomain"}


class NetworkBlocked(OSError):
    """An attempt to leave the machine, refused by the test-suite guard."""


@dataclass
class Attempt:
    kind: str  # "connect", "dns" ...
    target: str
    acknowledged: bool = False

    def __str__(self) -> str:
        return f"{self.kind} {self.target}"


def _text(host: Any) -> str:
    # Callers pass str, or bytes already in IDNA form (anyio does); ASCII is what both reduce to.
    if isinstance(host, bytes | bytearray | memoryview):
        return bytes(host).decode("ascii", "replace")
    return str(host)


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def _is_local_name(host: str) -> bool:
    name = host.lower().rstrip(".")
    return name in _LOCAL_NAMES or name.endswith(".localhost")


def is_loopback(host: Any) -> bool:
    """True for loopback addresses and local names. An empty host (bind-style) is local."""
    text = _text(host)
    if text == "":
        return True
    ip = _ip(text)
    if ip is None:
        return _is_local_name(text)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


class NetworkGuard:
    def __init__(self) -> None:
        self.blocked: list[Attempt] = []
        self._lock = threading.Lock()
        self._originals: dict[str, Any] = {}

    @property
    def active(self) -> bool:
        return bool(self._originals)

    # -- recording ---------------------------------------------------------------------

    def _refuse(self, kind: str, target: str) -> NetworkBlocked:
        with self._lock:
            self.blocked.append(Attempt(kind, target))
        return NetworkBlocked(f"test suite network guard: {kind} to {target} is not allowed")

    def unacknowledged(self, since: int = 0) -> list[Attempt]:
        return [a for a in self.blocked[since:] if not a.acknowledged]

    @contextmanager
    def expecting(self) -> Iterator[list[Attempt]]:
        """Mark the refusals made inside the block as expected. Yields the list of them."""
        start = len(self.blocked)
        seen: list[Attempt] = []
        try:
            yield seen
        finally:
            for attempt in self.blocked[start:]:
                attempt.acknowledged = True
                seen.append(attempt)

    # -- patching ----------------------------------------------------------------------

    def install(self) -> None:
        if self.active:
            return
        guard = self
        originals = {
            "connect": socket.socket.connect,
            "connect_ex": socket.socket.connect_ex,
            "sendto": socket.socket.sendto,
            "getaddrinfo": socket.getaddrinfo,
            "gethostbyname": socket.gethostbyname,
            "gethostbyname_ex": socket.gethostbyname_ex,
        }

        def check_address(kind: str, address: Any) -> None:
            # AF_UNIX addresses are paths (str or bytes); AF_INET/INET6 are tuples.
            if isinstance(address, tuple) and address and not is_loopback(address[0]):
                port = address[1] if len(address) > 1 else ""
                raise guard._refuse(kind, f"{_text(address[0])}:{port}")

        def connect(self: socket.socket, address: Any) -> Any:
            check_address("connect", address)
            return originals["connect"](self, address)

        def connect_ex(self: socket.socket, address: Any) -> Any:
            check_address("connect", address)
            return originals["connect_ex"](self, address)

        def sendto(self: socket.socket, data: Any, *args: Any) -> Any:
            if args:
                check_address("sendto", args[-1])
            return originals["sendto"](self, data, *args)

        def check_name(host: Any) -> None:
            if host is None:
                return
            text = _text(host)
            if _ip(text) is not None or is_loopback(text):
                return  # an IP literal is not a lookup; connect() judges where it points
            raise guard._refuse("dns", text)

        def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
            check_name(host)
            return originals["getaddrinfo"](host, *args, **kwargs)

        def gethostbyname(host: Any) -> Any:
            check_name(host)
            return originals["gethostbyname"](host)

        def gethostbyname_ex(host: Any) -> Any:
            check_name(host)
            return originals["gethostbyname_ex"](host)

        self._originals = originals
        socket.socket.connect = connect  # type: ignore[method-assign,assignment]
        socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]
        socket.socket.sendto = sendto  # type: ignore[method-assign,assignment]
        socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
        socket.gethostbyname = gethostbyname  # type: ignore[assignment]
        socket.gethostbyname_ex = gethostbyname_ex  # type: ignore[assignment]

    def uninstall(self) -> None:
        if not self.active:
            return
        socket.socket.connect = self._originals["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = self._originals["connect_ex"]  # type: ignore[method-assign]
        socket.socket.sendto = self._originals["sendto"]  # type: ignore[method-assign]
        socket.getaddrinfo = self._originals["getaddrinfo"]
        socket.gethostbyname = self._originals["gethostbyname"]
        socket.gethostbyname_ex = self._originals["gethostbyname_ex"]
        self._originals = {}


@pytest.fixture(scope="session", autouse=True)
def network_guard() -> Iterator[NetworkGuard]:
    guard = NetworkGuard()
    guard.install()
    try:
        yield guard
    finally:
        guard.uninstall()


@pytest.fixture(autouse=True)
def fail_on_unacknowledged_network_attempts(network_guard: NetworkGuard) -> Iterator[None]:
    """Fail any test during which the guard refused something the test did not expect, even
    if the code under test caught the exception."""
    before = len(network_guard.blocked)
    yield
    leaked = network_guard.unacknowledged(before)
    assert not leaked, f"the test made network attempts the guard refused: {leaked}"
