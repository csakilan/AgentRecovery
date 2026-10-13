"""Plan 2 is scoped to scripted models, and this file enforces that scope.

Nothing in the suite may reach a model provider, and no episode state may reach LangSmith.
The enforcement is the session-wide guard in tests/network_guard.py (installed by
tests/conftest.py). These tests show the guard is on, that it refuses the hosts that matter
by name and by IP, that it catches a real HTTP client and not only raw sockets, that it still
lets loopback through, that code which swallows the error still fails its test, and that the
LangSmith switches are off. They also check the source tree itself has no way to name a
provider.
"""

from __future__ import annotations

import ast
import asyncio
import http.server
import os
import socket
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import httpx
import pytest

from lab.runner.tracing import TRACING_VARIABLES, force_tracing_off
from tests.network_guard import PROVIDER_HOSTS, NetworkBlocked, NetworkGuard

REPO = Path(__file__).resolve().parents[2]

# Public addresses, never contacted: the guard refuses before a packet is sent.
PUBLIC_IPS = ("8.8.8.8", "104.18.2.1", "172.217.14.206")


def test_the_guard_is_active_for_the_session(network_guard: NetworkGuard):
    assert network_guard.active
    # The patched methods are the guard's own, not the ones the socket module ships with.
    assert socket.socket.connect.__module__ == "tests.network_guard"
    assert socket.getaddrinfo.__module__ == "tests.network_guard"


@pytest.mark.parametrize("host", PROVIDER_HOSTS)
def test_a_provider_name_is_refused_before_it_is_looked_up(host, network_guard: NetworkGuard):
    with network_guard.expecting() as seen:
        for lookup in (
            lambda: socket.getaddrinfo(host, 443),
            lambda: socket.gethostbyname(host),
            lambda: socket.gethostbyname_ex(host),
            lambda: socket.create_connection((host, 443), timeout=1),
        ):
            with pytest.raises(NetworkBlocked):
                lookup()
    assert len(seen) == 4
    assert {a.target.split(":")[0] for a in seen} == {host}
    assert {a.kind for a in seen} == {"dns"}


@pytest.mark.parametrize("ip", PUBLIC_IPS)
def test_a_public_ip_is_refused_at_connect(ip, network_guard: NetworkGuard):
    with network_guard.expecting() as seen:
        with pytest.raises(NetworkBlocked):
            socket.create_connection((ip, 443), timeout=1)
        sock = socket.socket()
        sock.settimeout(1)
        try:
            with pytest.raises(NetworkBlocked):
                sock.connect((ip, 443))
            with pytest.raises(NetworkBlocked):
                sock.connect_ex((ip, 443))
        finally:
            sock.close()
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(NetworkBlocked):
                udp.sendto(b"x", (ip, 53))
        finally:
            udp.close()
    assert [a.kind for a in seen] == ["connect", "connect", "connect", "sendto"]
    assert all(a.target.startswith(ip) for a in seen)


def test_a_public_ipv6_address_is_refused_too(network_guard: NetworkGuard):
    with network_guard.expecting() as seen:
        sock = socket.socket(socket.AF_INET6)
        try:
            with pytest.raises(NetworkBlocked):
                sock.connect(("2606:4700:4700::1111", 443))
        finally:
            sock.close()
    assert len(seen) == 1


@pytest.mark.parametrize("host", PROVIDER_HOSTS)
def test_a_real_http_client_is_stopped_too(host, network_guard: NetworkGuard):
    url = f"https://{host}/api/v1/models"
    with network_guard.expecting() as seen:
        with pytest.raises(httpx.ConnectError):
            httpx.get(url, timeout=2)

        async def call() -> None:
            async with httpx.AsyncClient(timeout=2) as client:
                await client.get(url)

        with pytest.raises(httpx.ConnectError):
            asyncio.run(call())
    assert len(seen) == 2
    assert {a.target.split(":")[0] for a in seen} == {host}


def test_a_real_http_client_is_stopped_at_a_public_ip(network_guard: NetworkGuard):
    with network_guard.expecting() as seen:
        with pytest.raises(httpx.ConnectError):
            httpx.get("https://104.18.2.1/api/v1/models", timeout=2)
    assert [a.kind for a in seen] == ["connect"]


def test_a_swallowed_refusal_is_still_recorded(network_guard: NetworkGuard):
    with network_guard.expecting() as seen:
        try:
            socket.create_connection(("api.openai.com", 443), timeout=1)
        except OSError:
            pass  # what a retrying client does with a failed connect
    assert [(a.kind, a.target) for a in seen] == [("dns", "api.openai.com")]


def test_a_test_that_swallows_a_refusal_fails_at_teardown(tmp_path: Path):
    """Run the guard's two autouse fixtures in a fresh pytest process, against a test that
    catches the error: it passes its own body and then errors at teardown, naming the host.
    A test that declares the attempt with `expecting()` is left alone."""
    (tmp_path / "conftest.py").write_text(
        "from tests.network_guard import (  # noqa: F401\n"
        "    fail_on_unacknowledged_network_attempts, network_guard,\n"
        ")\n"
    )
    (tmp_path / "test_inner.py").write_text(
        textwrap.dedent(
            """
            import socket

            def test_swallows():
                try:
                    socket.create_connection(("api.openai.com", 443), timeout=1)
                except OSError:
                    pass

            def test_declares(network_guard):
                with network_guard.expecting():
                    try:
                        socket.create_connection(("openrouter.ai", 443), timeout=1)
                    except OSError:
                        pass
            """
        )
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    done = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-q", "-p", "no:cacheprovider",
         "--rootdir", str(tmp_path), "-c", str(tmp_path / "pytest.ini")],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )  # fmt: skip
    assert done.returncode != 0, done.stdout
    assert "2 passed, 1 error" in done.stdout, done.stdout
    assert "api.openai.com" in done.stdout
    assert "ERROR at teardown of test_swallows" in done.stdout


class _Hello(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_loopback_stays_open_for_postgres_and_the_in_process_simulator(
    network_guard: NetworkGuard,
):
    server = http.server.HTTPServer(("127.0.0.1", 0), _Hello)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        for host in ("127.0.0.1", "localhost"):
            assert httpx.get(f"http://{host}:{port}/", timeout=5).text == "hello"
        assert socket.getaddrinfo("localhost", port)
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert network_guard.unacknowledged() == []


def test_a_whole_episode_makes_no_refused_attempt(runner_config, network_guard: NetworkGuard):
    from lab.agent.scripted import ScriptedModel
    from lab.protocol.faults import World
    from lab.protocol.manifest import Mode, make_spec
    from lab.runner.episode import run_spec

    before = len(network_guard.blocked)
    spec = make_spec("no-live", World.LOST_ACK, Mode.AGENT_DIRECTED, True, 0)
    result = run_spec(
        spec,
        ScriptedModel("stable_key"),
        runner_config,
        manifest_sha256="b" * 64,
        experiment_name="runner-test-no-live",
    )
    assert result.score.verdict.value == "correct"
    assert network_guard.blocked[before:] == []


# ---------------------------------------------------------------------------
# LangSmith
# ---------------------------------------------------------------------------


def test_every_tracing_switch_is_off_in_the_test_session():
    assert set(TRACING_VARIABLES) == {
        "LANGSMITH_TRACING",
        "LANGSMITH_TRACING_V2",
        "LANGCHAIN_TRACING",
        "LANGCHAIN_TRACING_V2",
    }
    assert {name: os.environ[name] for name in TRACING_VARIABLES} == dict.fromkeys(
        TRACING_VARIABLES, "false"
    )
    from langsmith.utils import tracing_is_enabled

    assert not tracing_is_enabled()


def test_force_tracing_off_overrides_whatever_the_shell_set(monkeypatch):
    for name in TRACING_VARIABLES:
        monkeypatch.setenv(name, "true")
    force_tracing_off()
    assert {os.environ[name] for name in TRACING_VARIABLES} == {"false"}
    # And into any mapping it is given, without touching os.environ.
    env = dict.fromkeys(TRACING_VARIABLES, "true")
    force_tracing_off(env)
    assert set(env.values()) == {"false"}


def test_importing_the_runner_turns_tracing_off_in_a_fresh_process():
    env = {**os.environ, **dict.fromkeys(TRACING_VARIABLES, "true")}
    code = (
        "import os, lab.runner.episode;"
        "from langsmith.utils import tracing_is_enabled;"
        "print([os.environ[n] for n in sorted(os.environ) if 'TRACING' in n],"
        " tracing_is_enabled())"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "['false', 'false', 'false', 'false'] False"


# ---------------------------------------------------------------------------
# The source tree has no way to reach a provider
# ---------------------------------------------------------------------------

FORBIDDEN_MODULES = {
    "openai",
    "anthropic",
    "openrouter",
    "litellm",
    "langchain_openai",
    "langchain_anthropic",
    "langchain_community",
    "google.generativeai",
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                nodes.add(id(first.value))
    return nodes


# Files that may legitimately name a provider host, and only the host check is waived for
# them (their imports are still scanned). Explicit and minimal on purpose. The replay test
# and worker take their hosts from PROVIDER_HOSTS, so they need no entry.
HOST_STRINGS_ALLOWED = {
    "tests/network_guard.py": "defines PROVIDER_HOSTS, the one list of hosts to refuse",
    "tests/runner/test_no_live_calls.py": "probes the hosts the guard must refuse",
}


def _scan(files: list[Path]) -> list[str]:
    problems: list[str] = []
    for path in files:
        relative = path.relative_to(REPO).as_posix()
        check_hosts = relative not in HOST_STRINGS_ALLOWED
        tree = ast.parse(path.read_text())
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                names = []
            for name in names:
                if any(name == m or name.startswith(m + ".") for m in FORBIDDEN_MODULES):
                    problems.append(f"{relative}: imports {name}")
            if (
                check_hosts
                and isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                for host in PROVIDER_HOSTS + ("smith.langchain.com",):
                    if host in node.value:
                        problems.append(f"{relative}: string mentions {host}")
                        break
    return problems


def test_no_source_file_imports_a_provider_sdk_or_names_a_provider_host():
    files = sorted((REPO / "src" / "lab").rglob("*.py"))
    assert len(files) > 20, "the scan found too few files to mean anything"
    assert _scan(files) == []


def test_no_test_file_or_worker_does_either_except_the_listed_ones():
    """The replay workers (tests/agent/replay_worker.py) build and run the whole graph in a
    child process with a model chosen there, so the scan covers everything under tests/."""
    files = sorted((REPO / "tests").rglob("*.py"))
    assert REPO / "tests" / "agent" / "replay_worker.py" in files
    assert len(files) > 30, "the scan found too few files to mean anything"
    assert _scan(files) == []
    for relative in HOST_STRINGS_ALLOWED:
        assert (REPO / relative).is_file(), f"{relative} is listed but does not exist"


def test_the_scan_catches_what_it_is_for(tmp_path, monkeypatch):
    bad = tmp_path / "worker.py"
    bad.write_text(
        '"""A docstring may name api.openai.com."""\n'
        "import openai\n"
        "from anthropic import Anthropic\n"
        'URL = "https://openrouter.ai/api/v1"\n'
        'F = f"https://api.smith.langchain.com/{1}"\n'
    )
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    assert sorted(_scan([bad])) == [
        "worker.py: imports anthropic",
        "worker.py: imports openai",
        "worker.py: string mentions api.smith.langchain.com",
        "worker.py: string mentions openrouter.ai",
    ]
