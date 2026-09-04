"""Subprocess-based integration tests for the `ps_service` process harness.

Marked `@pytest.mark.integration` (registered in root `pyproject.toml`). These
spawn *real* subprocesses and interact with them over real HTTP/OS
primitives, unlike `test_main.py`'s `TestClient`-based unit tests. See
PLAN_REVIEWED.md §4/§6 (increment 11) for the technique rationale.

The port is configurable via `PS_SERVICE_PORT` (see
`test_ps_service_port_env_override_serves_health_on_overridden_port` below,
which proves it end-to-end via a real subprocess bound to a non-default
port) — the shared-server-fixture design below is retained purely for
efficiency on the default (no-env-override) path, not because the port is
technically unconfigurable. Tests (a) "process starts and stays running" and
(b) "binds localhost-only" both only need "is the server up and reachable"
on the default port, so they share a single class-scoped server
(`running_server`, on `TestSharedServer`) to avoid spinning up a redundant
subprocess for each. Class scope (not module scope) is deliberate: it
guarantees the shared server is torn down — freeing `_PORT` — before tests
(c)/(d) run, which need exclusive use of that same default port for their
own dedicated process lifecycle (one gets SIGTERM'd, the other is expected
to fail to bind). Module scope would keep the shared server alive for the
whole file, colliding with (c)/(d)'s own bind attempts (confirmed
empirically: an early draft using module scope hit "Address already in use"
in test (d) because the module-scoped server from (a)/(b) was still bound).

Two spawn helpers, empirically justified:

- `_spawn_via_documented_command` runs the *literal* `uv run python -m
  ps_service` from `CONTRIBUTING.md`, used only by `running_server` (tests
  (a)/(b)) since AC-BI-004 requires proving that exact documented command
  works. Empirically confirmed (manual `ps`/`lsof` inspection during test
  development): `uv run` does NOT `exec`-replace itself — it forks a real
  child process (`uv`'s pid != the actual `python -m ps_service` pid that
  holds the listening socket). SIGTERM sent to the `uv` wrapper's own pid IS
  forwarded to the child and the child shuts down gracefully (confirmed via
  its "Shutting down / Application shutdown complete" log lines), so
  teardown-only signaling works fine — but the *wrapper's own* `returncode`
  reflects `uv` dying by the same signal (143 = 128+15), not the child's
  actual clean exit code. That makes the wrapper unsuitable for asserting
  `returncode == 0` (test (c)) or for `lsof -p <pid>` (test (b), which
  greps by port instead of by the wrapper's pid for this reason).
- `_spawn_direct` runs `[sys.executable, "-m", "ps_service"]` directly — no
  `uv run` indirection — used by tests (c) and (d), which need the spawned
  pid to be the actual server process so signal/exit-code assertions are
  meaningful. `sys.executable` inside a test process itself started via
  `uv run pytest ...` already resolves to the repo's `.venv` interpreter
  (confirmed empirically), so this still runs the identical `ps_service`
  code — it just skips the wrapper layer that isn't itself under test here.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi.testclient import TestClient

import ps_service.main as main_module
from ps_service.config import ServiceConfig
from ps_service.logging.facade import reset_for_tests, resolve_default_log_path
from ps_service.main import create_app
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.http_transport import MCP_HTTP_MOUNT_PATH
from ps_service.query_engine.cypher_query import (
    _SEED_CHECK_QUERY,  # pyright: ignore[reportPrivateUsage]  # test pins the exact seed-check query text
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    type ReadLines = Callable[[Path], list[dict[str, object]]]

if sys.platform == "win32":  # pragma: no cover - documented platform caveat, not exercised here
    pytest.skip("subprocess signal semantics differ on Windows", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOST = "127.0.0.1"
_PORT = 8000
_OVERRIDE_PORT = (
    8010  # distinct from _PORT so this test can run independently of TestSharedServer's lifecycle
)
_HEALTH_URL = f"http://{_HOST}:{_PORT}/health"
_READY_POLL_TIMEOUT_SECONDS = 5.0
_READY_POLL_INTERVAL_SECONDS = 0.05
_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 10  # the default graceful-shutdown timeout, in seconds
_SHUTDOWN_WAIT_BUFFER_SECONDS = 5
_TERMINATE_WAIT_TIMEOUT_SECONDS = 10


def _build_env(log_dir: Path, extra_env: dict[str, str] | None) -> dict[str, str]:
    """Build a subprocess environment isolated to `log_dir` via `PS_LOGGING_DIR`."""
    env = {**os.environ, "PS_LOGGING_DIR": str(log_dir)}
    if extra_env:
        env.update(extra_env)
    return env


def _spawn_via_documented_command(
    log_dir: Path, *, extra_env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    """Spawn the exact documented local-run command from `CONTRIBUTING.md`:
    `uv run python -m ps_service`.

    Used for AC-BI-003/AC-BI-004, where the point is proving that literal
    command works. See the module docstring for why this wrapper's own
    `returncode`/pid are not suitable for the SIGTERM/port-bind-failure
    tests, which use `_spawn_direct` instead.
    """
    return subprocess.Popen(
        [  # noqa: S607 — `uv` resolved from PATH by design (integration test)
            "uv",
            "run",
            "python",
            "-m",
            "ps_service",
        ],
        cwd=_REPO_ROOT,
        env=_build_env(log_dir, extra_env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _spawn_direct(
    log_dir: Path, *, extra_env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    """Spawn `python -m ps_service` directly via `sys.executable`, without the
    `uv run` wrapper layer.

    Used where the spawned pid must be the actual server process so
    SIGTERM/exit-code assertions are meaningful (see module docstring).
    """
    return subprocess.Popen(
        [sys.executable, "-m", "ps_service"],
        cwd=_REPO_ROOT,
        env=_build_env(log_dir, extra_env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort clean shutdown of a spawned subprocess: SIGTERM, then SIGKILL if needed.

    Ensures no test leaves an orphaned process bound to its listening port,
    even when the test itself fails or raises before reaching its own
    cleanup logic.
    """
    if proc.poll() is not None:
        return  # already exited
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=_TERMINATE_WAIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=_TERMINATE_WAIT_TIMEOUT_SECONDS)


def _try_get_health(url: str = _HEALTH_URL) -> httpx.Response | None:
    """Attempt a single `GET /health` call, returning `None` if the server isn't
    accepting connections yet.

    Factored out of `_wait_until_healthy`'s polling loop so the `try`/`except`
    lives in a single call, not inline inside the loop body (ruff PERF203).
    `url` defaults to `_HEALTH_URL` (the default, no-env-override port) but
    accepts an override so tests spawning a subprocess on a non-default
    `PS_SERVICE_PORT` can poll the right address.
    """
    try:
        return httpx.get(url, timeout=1.0)
    except httpx.HTTPError:
        return None


def _wait_until_healthy(
    deadline_seconds: float = _READY_POLL_TIMEOUT_SECONDS, url: str = _HEALTH_URL
) -> httpx.Response:
    """Poll `GET /health` with a short bounded retry loop until it responds or the deadline is hit.

    Raises `TimeoutError` if the deadline is reached with no successful
    response, so callers get a clear failure rather than a hang. `url`
    defaults to `_HEALTH_URL` but accepts an override for subprocesses
    spawned with a non-default `PS_SERVICE_PORT`.
    """
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        response = _try_get_health(url)
        if response is not None:
            return response
        time.sleep(_READY_POLL_INTERVAL_SECONDS)
    msg = f"ps_service did not become healthy within {deadline_seconds}s"
    raise TimeoutError(msg)


class TestSharedServer:
    """Tests (a) and (b): both only need "is the server up and reachable", so they
    share one process.

    Grouped in a class so `running_server` can be `scope="class"` — shared
    across this class's tests, but torn down (freeing `_PORT`, the default
    port this shared server binds to) before any test outside the class
    runs. See module docstring for why module scope is unsafe here.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def running_server(
        cls, tmp_path_factory: pytest.TempPathFactory
    ) -> Iterator[subprocess.Popen[bytes]]:
        """Class-scoped: one live `ps_service` subprocess shared by this class's tests.

        `@classmethod` per pytest's guidance for class-scoped fixtures
        defined inside a class (avoids `PytestRemovedIn10Warning`: an
        instance-method fixture would run against a throwaway instance whose
        state isn't visible to the actual test-instance methods).
        """
        log_dir = tmp_path_factory.mktemp("ps-service-logs-shared")
        proc = _spawn_via_documented_command(log_dir)
        try:
            _wait_until_healthy()
            yield proc
        finally:
            _terminate(proc)

    @pytest.mark.integration
    def test_documented_run_command_starts_process_and_it_stays_running(
        self, running_server: subprocess.Popen[bytes]
    ) -> None:
        """AC-BI-003 + AC-BI-004: the documented `uv run python -m ps_service`
        command starts a live, staying-up process.

        Polls `/health` over real HTTP until it responds, then asserts the
        subprocess is still alive (not a crash-then-somehow-still-200 fluke).
        """
        response = _wait_until_healthy()

        assert response.status_code == 200
        assert response.json() == {"status": "alive"}
        assert running_server.poll() is None  # still running, not exited

    @pytest.mark.integration
    def test_process_binds_health_and_ready_to_localhost_only(
        self, running_server: subprocess.Popen[bytes]
    ) -> None:
        """AC-BI-002 (behavioral half): the running process's listening socket is
        bound to 127.0.0.1, not 0.0.0.0/*.

        Uses `lsof -n -P -iTCP:<port> -sTCP:LISTEN` (confirmed available on
        this darwin dev machine per PLAN_REVIEWED.md §4). `-n -P` disable
        hostname/port-name resolution — without them `lsof` prints
        `localhost:irdmi` (its resolved name for `127.0.0.1:8000`, `irdmi`
        being port 8000's registered `/etc/services` name) instead of a
        numeric address, which would make the `127.0.0.1`/`0.0.0.0` string
        check unreliable (confirmed empirically). Skips gracefully — rather
        than failing the whole suite — if `lsof` isn't present, since it's
        not guaranteed available on every platform (e.g. Linux CI might need
        `ss` instead).

        Filters by *port*, not by `running_server.pid`: `running_server` is
        spawned via the `uv run` wrapper (see module docstring), whose own
        pid is NOT the pid that actually holds the listening socket (`uv
        run` forks a real child rather than `exec`-replacing itself —
        confirmed empirically). Since `_PORT = 8000` is this class's shared
        server's default (no-env-override) port and no other service in this
        repo's dev setup uses it (per PLAN_REVIEWED.md §3), a port-scoped
        `lsof` query unambiguously identifies the server's socket.
        """
        lsof_path = shutil.which("lsof")
        if lsof_path is None:
            pytest.skip("lsof not available on this system")

        _wait_until_healthy()  # ensure the listening socket is definitely open

        result = subprocess.run(  # noqa: S603 — trusted dev tool (lsof), args are literals
            [lsof_path, "-n", "-P", f"-iTCP:{_PORT}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )

        listen_lines = [line for line in result.stdout.splitlines() if "LISTEN" in line]
        assert listen_lines, f"no LISTEN socket found on port {_PORT}:\n{result.stdout}"
        assert any(f"{_HOST}:{_PORT}" in line for line in listen_lines), (
            f"expected a LISTEN socket on {_HOST}:{_PORT}, got:\n{result.stdout}"
        )
        assert not any("*:" in line or "0.0.0.0" in line for line in listen_lines), (
            f"process is listening on all interfaces, not localhost-only:\n{result.stdout}"
        )


@pytest.mark.integration
def test_sigterm_triggers_clean_bounded_exit(tmp_path: Path) -> None:
    """AC-BI-007 (behavioral half): SIGTERM causes a clean, bounded exit (no hang, no forced kill).

    Spawns its own dedicated subprocess (separate from `running_server`)
    since this test kills it. Accepted limitation (PLAN_REVIEWED.md §7 item
    4): this proves clean bounded exit on SIGTERM, not full in-flight-request
    -drain semantics — `/health`/`/ready` return instantly, so there is
    nothing to keep in-flight during the shutdown window to actually exercise
    `timeout_graceful_shutdown`'s drain behavior.

    Asserts `returncode == -signal.SIGTERM`, NOT `== 0` — confirmed
    empirically by reading uvicorn's `Server.capture_signals()`
    (`uvicorn/server.py`): after the graceful async shutdown sequence
    completes (visible in this repo's manual testing as "Shutting down" /
    "Waiting for application shutdown" / "Application shutdown complete" /
    "Finished server process" log lines), uvicorn restores the *original*
    signal handler and calls `signal.raise_signal(captured_signal)` on
    itself — deliberately re-raising SIGTERM so the OS/parent correctly sees
    "terminated by SIGTERM" rather than a plain `exit(0)`, the standard Unix
    convention for signal-terminated processes. A bounded, non-hanging
    `proc.wait()` plus `returncode == -SIGTERM` (not `-SIGKILL`, which would
    indicate `_terminate`'s kill escalation had to fire) is therefore the
    correct signal of "no hang, no forced kill needed".
    """
    proc = _spawn_direct(tmp_path)
    try:
        _wait_until_healthy()

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=_GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS + _SHUTDOWN_WAIT_BUFFER_SECONDS)
        except subprocess.TimeoutExpired:
            pytest.fail("process did not exit within the graceful-shutdown timeout + buffer")

        assert proc.returncode == -signal.SIGTERM
    finally:
        _terminate(proc)


@pytest.mark.integration
def test_port_bind_failure_exits_nonzero_and_stderr_has_no_secret_content(tmp_path: Path) -> None:
    """AC-BI-010 (subprocess half): a port-bind failure exits nonzero and reports
    it without leaking secrets.

    Occupies the default port (`_PORT`) with a raw socket first, then spawns the
    subprocess pointed at the same port; uvicorn's own bind attempt should
    fail quickly. Per D4 in DECISIONS.md, a resolved file-path appearing in
    the failure traceback is an ACCEPTABLE, non-secret detail (out of
    AC-BI-010's literal "no secrets/credentials" scope) — this test only
    checks for the absence of an actual secret-looking marker, using a
    distinguishing sentinel value injected via env, not for the absence of
    paths generally.
    """
    occupying_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupying_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    secret_sentinel = "sk-test-sentinel-should-never-appear-in-stderr"
    try:
        occupying_socket.bind((_HOST, _PORT))
        occupying_socket.listen(1)

        proc = _spawn_direct(tmp_path, extra_env={"PS_TEST_SECRET_SENTINEL": secret_sentinel})
        try:
            _, stderr = proc.communicate(
                timeout=_READY_POLL_TIMEOUT_SECONDS + _SHUTDOWN_WAIT_BUFFER_SECONDS
            )
        except subprocess.TimeoutExpired:
            _terminate(proc)
            pytest.fail("process did not exit promptly after a port-bind failure")

        assert proc.returncode != 0
        stderr_text = stderr.decode("utf-8", errors="replace")
        assert secret_sentinel not in stderr_text
    finally:
        occupying_socket.close()


@pytest.mark.integration
def test_ps_service_port_env_override_serves_health_on_overridden_port(tmp_path: Path) -> None:
    """AC-BI-003 (subprocess/behavioral proof): PS_SERVICE_PORT actually rebinds
    the real listening port.

    Spawns its own dedicated subprocess with `PS_SERVICE_PORT=_OVERRIDE_PORT`
    (distinct from `_PORT`, the default-path constant used by
    `TestSharedServer`'s shared server and the SIGTERM/bind-failure tests),
    so it can run independently of that fixture's class-scoped lifecycle.
    This is the test that actually proves the issue's motivating pain
    point — the previously-hardcoded port — is fixed: the unit-level tests
    in `test_main.py` only prove `uvicorn.run` was *called* with the right
    `port` kwarg; this proves a real process actually bound there.
    """
    proc = _spawn_direct(tmp_path, extra_env={"PS_SERVICE_PORT": str(_OVERRIDE_PORT)})
    try:
        response = _wait_until_healthy(url=f"http://{_HOST}:{_OVERRIDE_PORT}/health")

        assert response.status_code == 200
        assert response.json() == {"status": "alive"}
    finally:
        _terminate(proc)


@pytest.mark.integration
def test_invalid_ps_service_port_env_exits_nonzero_without_binding(tmp_path: Path) -> None:
    """AC-BI-009 (subprocess/behavioral proof): an invalid PS_SERVICE_PORT fails
    before uvicorn ever binds.

    `main()` calls `load_config()` first; an unparseable `PS_SERVICE_PORT`
    makes `_parse_port` raise `ServiceConfigurationError` synchronously,
    before `create_app`/`uvicorn.run` are ever reached — so the process must
    exit promptly (bounded `communicate` timeout, mirroring
    `test_port_bind_failure_exits_nonzero_and_stderr_has_no_secret_content`'s
    pattern) with a nonzero exit code. Confirmed empirically (manual run
    during test development) that the resulting stderr traceback bottoms out
    at `config.py`'s `_parse_port`/`load_config`, never reaching `main.py`'s
    `create_app`/`uvicorn.run` lines — asserting `PS_SERVICE_PORT` appears in
    stderr is therefore direct evidence uvicorn's bind was never attempted,
    stronger than an absence-of-LISTEN-socket check (which an already-exited
    process would trivially satisfy regardless of cause).
    """
    proc = _spawn_direct(tmp_path, extra_env={"PS_SERVICE_PORT": "not-a-number"})
    try:
        try:
            _, stderr = proc.communicate(
                timeout=_READY_POLL_TIMEOUT_SECONDS + _SHUTDOWN_WAIT_BUFFER_SECONDS
            )
        except subprocess.TimeoutExpired:
            pytest.fail("process did not exit promptly after an invalid PS_SERVICE_PORT")

        assert proc.returncode != 0
        stderr_text = stderr.decode("utf-8", errors="replace")
        assert "PS_SERVICE_PORT" in stderr_text
        assert "ServiceConfigurationError" in stderr_text
    finally:
        _terminate(proc)


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally with scripted values (mirrors
    `mcp_interface/test_cypher_tool.py`'s fake, not imported across files to
    keep this file's test doubles self-contained).
    """

    def __init__(self, *, header: list[list[object]], result_set: list[object]) -> None:
        self.header = header
        self.result_set = result_set


class _FakeGraphHandle:
    """Satisfies `GraphHandle` structurally: `query()` reports itself as
    seeded (D11) for `_SEED_CHECK_QUERY`, and otherwise always returns the
    scripted `_FakeQueryResult`.
    """

    def __init__(self, *, result: _FakeQueryResult) -> None:
        self._result = result

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if q == _SEED_CHECK_QUERY:
            return _FakeQueryResult(header=[[0, "c"]], result_set=[[1]])
        return self._result


class _FakeFalkorDB:
    """Stands in for the eager `falkordb.FalkorDB` client."""

    def __init__(self, handle: _FakeGraphHandle) -> None:
        self._handle = handle

    def select_graph(self, name: str) -> _FakeGraphHandle:
        return self._handle


_JSON_RPC_ACCEPT = "application/json, text/event-stream"


@pytest.mark.integration
def test_mcp_streamable_http_transport_reachable_over_a_real_socket(tmp_path: Path) -> None:
    """AC-BI-001/002 (issue #39, PLAN.md Slice 8): the mounted Streamable HTTP
    transport is reachable over a real TCP socket, not just `TestClient`'s
    virtual transport.

    Spawns the real `ps_service` process the same way
    `test_ps_service_port_env_override_serves_health_on_overridden_port`
    above does (`_spawn_direct`, a real `[sys.executable, "-m", "ps_service"]`
    subprocess bound to `127.0.0.1:<port>`), polls `/ready` (not `/health`
    -- readiness is what proves the app's `lifespan`, including the new
    `async with mcp_asgi_app.router.lifespan_context(...)` this issue added,
    has actually completed and the MCP session manager has started) until
    healthy, then issues a real `httpx.post` -- a genuine OS-level HTTP
    request over a real socket, unlike every other MCP-transport test in
    this suite, which drives the same JSON-RPC shape through `TestClient`'s
    in-process ASGI transport -- to `http://127.0.0.1:<port>/mcp/` with a
    JSON-RPC `initialize` payload matching the exact shape
    `test_main.py`/`test_http_transport.py` already use for MCP `initialize`
    requests. This is the strongest available proof, inside a
    single-machine CI sandbox, that the transport is reachable over a real
    TCP/HTTP stack -- the closest verifiable proxy for AC-BI-001's "a client
    on a different machine" claim, matching the exact proxy this file's own
    AC-BI-004/005 subprocess tests above already rely on.
    """
    port = _OVERRIDE_PORT
    proc = _spawn_direct(tmp_path, extra_env={"PS_SERVICE_PORT": str(port)})
    try:
        ready_response = _wait_until_healthy(url=f"http://{_HOST}:{port}/ready")
        assert ready_response.status_code == 200

        response = httpx.post(
            f"http://{_HOST}:{port}{MCP_HTTP_MOUNT_PATH}/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test-main-integration-client", "version": "0.0.1"},
                },
            },
            headers={"Accept": _JSON_RPC_ACCEPT},
            timeout=5.0,
        )

        assert response.status_code == 200
    finally:
        _terminate(proc)


def test_local_test_bypass_active_on_loopback_starts_and_answers_query_without_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, read_lines: ReadLines
) -> None:
    """AC-BI-005 / AC-BI-008 (issue #67): with the bypass active on a
    loopback-bound instance, the process starts successfully AND answers a
    query with no credential of any kind supplied -- composed in one test,
    not two disconnected unit-level facts (FLAWS.md finding 3 / CHANGES.md
    row 3).

    "Starts successfully" is proven by the `with TestClient(app):` block
    completing without raising: a `LocalTestBypassBindRefusedError` (the
    only thing that could stop a loopback-bound bypass-active instance from
    starting) would surface right there. "Answers a query with no
    credential" is proven by calling the real `mcp_server.cypher` tool
    in-process, straight after startup, passing nothing but the query text
    itself -- no header, token, or credential of any kind exists on this
    call path to omit. Reading the `query_engine` log entry's `principal`
    back out ties this same execution to AC-BI-008: the composition is real,
    not two disconnected facts.

    Dependency connectivity checks (FalkorDB/LLM Interface/Cellar-ELI) are
    stubbed to succeed instantly -- real reachability is out of scope for
    AC-BI-005/AC-BI-008 (covered elsewhere) and `_check_dependencies_at_startup`
    never raises regardless, so stubbing only removes real-network flakiness/
    latency from this test, it does not change what is being proven.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=tmp_path,
        is_local_test_bypass_active=True,
    )

    def stub_connect_from_config(_config: ServiceConfig) -> object:
        return object()

    def stub_check_falkordb_connectivity(db: object, host: str, port: int) -> None:
        return None

    def stub_check_llm_interface_connectivity(_config: ServiceConfig) -> None:
        return None

    def stub_check_cellar_eli_connectivity() -> None:
        return None

    monkeypatch.setattr(main_module, "connect_from_config", stub_connect_from_config)
    monkeypatch.setattr(
        main_module, "check_falkordb_connectivity", stub_check_falkordb_connectivity
    )
    monkeypatch.setattr(
        main_module, "check_llm_interface_connectivity", stub_check_llm_interface_connectivity
    )
    monkeypatch.setattr(
        main_module, "check_cellar_eli_connectivity", stub_check_cellar_eli_connectivity
    )

    fake_graph = _FakeGraphHandle(result=_FakeQueryResult(header=[[0, "id"]], result_set=[["a"]]))

    def stub_mcp_connect_from_config(_config: object) -> _FakeFalkorDB:
        return _FakeFalkorDB(fake_graph)

    monkeypatch.setattr(mcp_server, "connect_from_config", stub_mcp_connect_from_config)

    app = create_app(config)

    # No exception raised during TestClient entry IS the starts-successfully
    # proof (a refused bind would raise here); no credential/header/token of
    # any kind is passed to `cypher` below.
    with TestClient(app):
        result = mcp_server.cypher("MATCH (n) RETURN n.id")

    assert not (isinstance(result, str) and result.startswith("error:"))

    reset_for_tests()  # drain the emitter's queue and join its writer thread before reading
    lines = read_lines(resolve_default_log_path())
    entry = next(
        line
        for line in lines
        if line.get("component") == "query_engine" and line.get("action") == "execute_cypher_query"
    )
    assert entry["principal"] == mcp_server.LOCAL_TEST_PRINCIPAL_ID
