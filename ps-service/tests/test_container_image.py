"""Tests that exercise the *built* `ps-service` runtime image (issue #60).

Unlike `test_dockerfile.py`, which asserts the static contract of the build inputs, every
test here needs a real container runtime and a real image: it builds (or is handed) the image
and then interrogates it with the container CLI. That is expensive, so the whole module
carries the `container_image` marker (registered in the root `pyproject.toml`) and the
image-under-test fixture is session-scoped -- at most one build per pytest session.

The image under test comes from `PS_CONTAINER_IMAGE_REF` when it is set. `on_semver.yml`'s
build job sets it to the exact tag its build step produced and its publish job later pushes,
so CI tests the bytes that ship rather than a second, locally rebuilt image. Unset (the
laptop path) means build `ps-service:local-test` from the repository root.

Placement: this module mirrors no source module -- it asserts facts about a repository-root
artefact. It lives at the `ps-service/tests/` root for the same reason `test_dockerfile.py`
does: `testpaths` is `["ps-service/tests", "ps-cli/tests"]` and basedpyright's `include`
covers `ps-service/tests`, so a new root-level `tests/` directory would need both changed and
would still leave a type-checking gap.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.container_image

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Laptop-path tag. Deliberately the only occurrence of a hardcoded image name in this module:
# every other reference flows through the `image_ref` fixture.
_DEFAULT_LOCAL_TAG = "ps-service:local-test"

# `docker` first so CI (which has docker, not podman) needs no environment override; the
# `PS_CONTAINER_CLI` variable wins over both when set.
_CLI_CANDIDATES = ("docker", "podman")
_CLI_OVERRIDE_ENV = "PS_CONTAINER_CLI"
_IMAGE_REF_ENV = "PS_CONTAINER_IMAGE_REF"

_BUILD_TIMEOUT_SECONDS = 1800.0
_RUN_TIMEOUT_SECONDS = 180.0
_INSPECT_TIMEOUT_SECONDS = 60.0

# The dev group (`pyproject.toml:4-11`) minus the two that are not importable modules
# (`pre-commit` installs a `pre_commit` module but is a hook runner, `httpx` is a genuine
# transitive runtime concern). These three are the ones AC-BI-003 names by name.
_DEV_ONLY_MODULES = ("pytest", "ruff", "basedpyright")

# Probes the installed package's own tree plus the two build-context paths that would betray a
# leaked source/fixture copy. Kept as one `python -c` snippet so it is a single container run.
_NO_TEST_SOURCE_PROBE = (
    "import json, pathlib, ps_service; "
    "pkg = pathlib.Path(ps_service.__file__).resolve().parent; "
    "print(json.dumps({"
    "'package_dir': str(pkg), "
    "'test_paths_in_package': sorted(str(p) for p in pkg.rglob('*test*')), "
    "'app_source_tree_exists': pathlib.Path('/app/ps-service').exists(), "
    "'app_test_data_exists': pathlib.Path('/app/test-data').exists()"
    "}))"
)


def _run_container_cli(
    cli: str, args: list[str], *, timeout: float, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run one container-CLI command.

    Single call site for every `podman`/`docker` invocation in this module, so the one `S603`
    suppression below is stated once with its justification rather than repeated at a dozen
    call sites. `cli` is an absolute path resolved by `shutil.which` from a fixed candidate
    list, `args` are module-local literals: no untrusted input reaches the process boundary.
    `shell=True` is never used and an explicit `timeout` is mandatory (L2 coding standard).
    """
    return subprocess.run(  # noqa: S603 - cli is a shutil.which-resolved absolute path, args are module-local literals (see docstring)
        [cli, *args], check=check, capture_output=True, text=True, timeout=timeout
    )


def _resolve_container_cli() -> str:
    """Return an absolute path to the container CLI, skipping the module when none exists.

    Resolved to an absolute path rather than left as a bare name because the CLI may live
    outside a subprocess `PATH` (podman installs under `/opt/podman/bin` on macOS).
    """
    override = os.environ.get(_CLI_OVERRIDE_ENV)
    candidates = (override,) if override else _CLI_CANDIDATES
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved is not None:
            return resolved
    pytest.skip(f"no container CLI available (tried: {', '.join(candidates)})")


@pytest.fixture(scope="session")
def container_cli() -> str:
    """Return the absolute path of the container CLI used by every test in this module."""
    return _resolve_container_cli()


@pytest.fixture(scope="session")
def image_ref(container_cli: str) -> str:
    """Return the image reference under test, building it only when CI has not supplied one.

    `PS_CONTAINER_IMAGE_REF` is set by `on_semver.yml`'s build job to the exact tag its
    `build-push-action` step produced and its publish job later pushes, so CI tests the
    published bytes rather than a second, locally rebuilt image (FLAWS F-02). Unset (the
    laptop path) means build `ps-service:local-test` from the repo root.
    """
    supplied = os.environ.get(_IMAGE_REF_ENV)
    if supplied:
        return supplied

    result = _run_container_cli(
        container_cli,
        ["build", "-t", _DEFAULT_LOCAL_TAG, str(_REPO_ROOT)],
        timeout=_BUILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, (
        f"building {_DEFAULT_LOCAL_TAG} from {_REPO_ROOT} failed "
        f"(exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
    )
    return _DEFAULT_LOCAL_TAG


def _python_in_image(
    cli: str, ref: str, snippet: str, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run `python -c <snippet>` inside a throwaway container built from `ref`."""
    return _run_container_cli(
        cli,
        ["run", "--rm", ref, "python", "-c", snippet],
        timeout=_RUN_TIMEOUT_SECONDS,
        check=check,
    )


def test_image_builds_and_imports_ps_service(container_cli: str, image_ref: str) -> None:
    """The image builds, and its default `python` is the venv interpreter with ps_service in it."""
    result = _python_in_image(
        container_cli,
        image_ref,
        "import ps_service; print(ps_service.__file__)",
        check=False,
    )

    assert result.returncode == 0, (
        f"`import ps_service` failed inside {image_ref} (exit {result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "/app/.venv/" in result.stdout, (
        "ps_service resolved outside the image's virtualenv -- PATH does not prepend "
        f"/app/.venv/bin: {result.stdout!r}"
    )


def _image_environment(cli: str, ref: str) -> list[str]:
    """Return the image's baked-in `Config.Env` entries as `NAME=value` strings."""
    result = _run_container_cli(
        cli,
        ["image", "inspect", "--format", "{{json .Config.Env}}", ref],
        timeout=_INSPECT_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, (
        f"inspecting {ref} failed (exit {result.returncode}):\n{result.stderr}"
    )
    env: list[str] = json.loads(result.stdout)
    return env


def test_container_env_sets_ps_service_host_to_all_interfaces(
    container_cli: str, image_ref: str
) -> None:
    """AC-BI-008: the image's own environment widens the bind, so the published port works.

    The wider bind exists *only* here. `ps_service.config` still defaults to loopback and
    still refuses to widen on a bad value (D-6, guarded by
    `test_dockerfile.py::test_source_default_host_is_still_loopback`).
    """
    env = _image_environment(container_cli, image_ref)

    assert "PS_SERVICE_HOST=0.0.0.0" in env, (
        f"image environment does not bind all interfaces; got: {env}"
    )


def test_container_env_sets_exactly_one_absolute_logging_dir(
    container_cli: str, image_ref: str
) -> None:
    """The image points logging at a writable absolute path, so startup does not abort.

    Without it the container crashes immediately: `logging/facade.py`'s `_find_repo_root`
    walks upward for a `.git` directory and raises when there is none, and an image has none.
    """
    env = _image_environment(container_cli, image_ref)

    logging_dirs = [entry for entry in env if entry.startswith("PS_LOGGING_DIR=")]
    assert len(logging_dirs) == 1, f"expected exactly one PS_LOGGING_DIR entry; got: {env}"
    assert Path(logging_dirs[0].partition("=")[2]).is_absolute(), (
        f"PS_LOGGING_DIR must be an absolute path; got: {logging_dirs[0]!r}"
    )


def test_runtime_image_has_no_dev_dependencies(container_cli: str, image_ref: str) -> None:
    """AC-BI-003, empirically: pytest, ruff and basedpyright are absent from the image."""
    importable = [
        module
        for module in _DEV_ONLY_MODULES
        if _python_in_image(container_cli, image_ref, f"import {module}", check=False).returncode
        == 0
    ]

    assert not importable, f"dev-only dependencies are importable inside {image_ref}: {importable}"


def test_runtime_image_contains_no_test_modules_or_fixtures(
    container_cli: str, image_ref: str
) -> None:
    """AC-BI-003, empirically: no test module, source tree or fixture directory shipped."""
    result = _python_in_image(container_cli, image_ref, _NO_TEST_SOURCE_PROBE, check=False)
    assert result.returncode == 0, (
        f"probing {image_ref} for test sources failed (exit {result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )

    probe: dict[str, object] = json.loads(result.stdout)

    assert probe["test_paths_in_package"] == [], (
        f"installed ps_service package ships test artefacts: {probe['test_paths_in_package']}"
    )
    assert probe["app_source_tree_exists"] is False, (
        "the ps-service source tree reached the runtime image at /app/ps-service; the runtime "
        "stage must copy only the virtualenv (`uv sync --no-editable`)"
    )
    assert probe["app_test_data_exists"] is False, (
        "test fixtures reached the image at /app/test-data"
    )


# --- S6: the smoke test that gates the push (AC-BI-007, AC-BI-008) --------------------------
#
# `on_semver.yml`'s build job runs this module against the image it just built, and only a
# green run lets the publish job push. The four tests below are that gate: A proves the service
# answers through the published port, B' pins `/ready`'s exact answer and the exact reason for
# it, C' proves FalkorDB specifically was healthy, and D is C's negative control.
#
# Why `/ready` is asserted `not_ready` and not `ready` (the F-03 residual, stated once here):
# `app.state.ready` also requires the Cellar/ELI probe, whose endpoint is a hardcoded module
# constant (`ingestion/adapters/cellar_eli/fetch.py`) with no configuration seam. A literal
# `"ready"` would therefore make the release gate depend on a live third-party service. The
# gate instead asserts `/ready` answers 200 `not_ready` for the exactly-known reason, and
# proves FalkorDB specifically healthy -- which is the half of AC-BI-007 this image controls.

_FALKORDB_IMAGE = "falkordb/falkordb:latest"
_SERVICE_CONTAINER_PORT = 8000

# `logging/facade.py`'s `_DEFAULT_LOG_FILENAME` under the Dockerfile's `PS_LOGGING_DIR`. The
# startup entries are written here, not to stdout, so the barrier reads the file.
_LOG_FILE_IN_IMAGE = "/var/log/ps-service/ps-service.jsonl"

_FALKORDB_DEPENDENCY = "falkordb"
_BARRIER_DEPENDENCY = "llm_interface"

# RFC 2606 reserves `.invalid`, so this name cannot resolve on any runner -- the negative
# control's unreachability is guaranteed rather than merely likely.
_UNREACHABLE_FALKORDB_HOST = "falkordb-unreachable.invalid"

_HTTP_OK = 200
_HTTP_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.25
_STARTUP_BARRIER_SECONDS = 30.0
_LIVENESS_DEADLINE_SECONDS = 60.0
_FALKORDB_DEADLINE_SECONDS = 60.0
_PULL_TIMEOUT_SECONDS = 900.0


@dataclass(frozen=True)
class _RunningService:
    """A started `ps-service` container plus the host-side base URL of its published port."""

    name: str
    base_url: str


def _unique(prefix: str) -> str:
    """Return a collision-free container/network name, so parallel runs never clash."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _free_host_port() -> int:
    """Return a currently-free localhost TCP port for `--publish`.

    Bind-then-release rather than a fixed port: a hardcoded port collides with whatever else
    the runner (or the developer's laptop) happens to be running. The gap between release and
    the container's bind is a theoretical race no runner has ever lost in practice, and the
    alternative -- a fixed port -- fails deterministically instead of theoretically.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def _remove_container(cli: str, name: str) -> None:
    """Force-remove a container, tolerating its absence (teardown must never mask a failure)."""
    _run_container_cli(cli, ["rm", "--force", name], timeout=_INSPECT_TIMEOUT_SECONDS, check=False)


def _container_logs(cli: str, name: str) -> str:
    """Return a container's stdout/stderr, for embedding in a failure message."""
    result = _run_container_cli(cli, ["logs", name], timeout=_INSPECT_TIMEOUT_SECONDS, check=False)
    return f"{result.stdout}\n{result.stderr}"


def _read_log_file(cli: str, container: str) -> list[dict[str, object]]:
    """Return the JSONL sink's entries so far, or `[]` while the file does not yet exist."""
    result = _run_container_cli(
        cli,
        ["exec", container, "cat", _LOG_FILE_IN_IMAGE],
        timeout=_INSPECT_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        return []
    entries: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        if line.strip():
            entry: dict[str, object] = json.loads(line)
            entries.append(entry)
    return entries


def _startup_log_entries(
    cli: str, container: str, *, deadline_seconds: float = _STARTUP_BARRIER_SECONDS
) -> list[dict[str, object]]:
    """Return the container's startup log entries once the startup block is provably complete.

    A barrier, not a sleep. `main._check_dependencies_at_startup` probes in the fixed order
    FALKORDB -> LLM_INTERFACE -> CELLAR_ELI, and `llm_interface.connectivity.check_connectivity`
    raises without any network call whenever `PS_LLMINTERFACE_MODEL`/`_EMBED_MODEL` are unset,
    which this fixture guarantees. `logging/emitter.py` is a FIFO `queue.Queue` drained by a
    single writer thread that flushes every line, so once the `llm_interface` warning is in the
    JSONL, a `falkordb` warning -- had one been emitted -- is necessarily already in it. Polling
    for that later entry is what turns "no falkordb warning" from absence-of-evidence into a
    real completion barrier (FLAWS F-05: the `outcome="success"` entry at `main.py:181` is
    emitted *before* the probes and is therefore not a barrier).

    Fails the test on timeout, echoing the container's logs.
    """
    deadline = time.monotonic() + deadline_seconds
    entries: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        entries = _read_log_file(cli, container)
        if any(entry.get("dependency") == _BARRIER_DEPENDENCY for entry in entries):
            return entries
        time.sleep(_POLL_INTERVAL_SECONDS)
    pytest.fail(
        f"the {_BARRIER_DEPENDENCY!r} startup warning never appeared in {_LOG_FILE_IN_IMAGE} "
        f"within {deadline_seconds}s, so the startup probe block cannot be proven complete "
        f"and no absence assertion about {_FALKORDB_DEPENDENCY!r} is sound.\n"
        f"entries seen: {entries}\ncontainer logs:\n{_container_logs(cli, container)}"
    )


def _dependency_warnings(entries: list[dict[str, object]], dependency: str) -> list[object]:
    """Return the startup warning entries naming `dependency`.

    `LogEntry.to_json_line` merges `extra` into the payload's top level, so the key is
    `dependency`, not `extra.dependency`.
    """
    return [
        entry
        for entry in entries
        if entry.get("outcome") == "warning" and entry.get("dependency") == dependency
    ]


def _unhealthy_dependencies(entries: list[dict[str, object]]) -> set[str]:
    """Return the set of dependencies that failed their startup probe."""
    return {
        str(entry["dependency"])
        for entry in entries
        if entry.get("outcome") == "warning" and "dependency" in entry
    }


def _wait_for_falkordb(cli: str, container: str) -> None:
    """Block until FalkorDB answers PING, so the service's one-shot startup probe is not raced."""
    deadline = time.monotonic() + _FALKORDB_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        result = _run_container_cli(
            cli,
            ["exec", container, "redis-cli", "ping"],
            timeout=_INSPECT_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode == 0 and "PONG" in result.stdout:
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    pytest.fail(
        f"{_FALKORDB_IMAGE} did not answer PING within {_FALKORDB_DEADLINE_SECONDS}s:\n"
        f"{_container_logs(cli, container)}"
    )


def _start_service(
    cli: str, image_ref: str, *, network: str, falkordb_host: str, name: str
) -> _RunningService:
    """Start the image under test on `network`, publishing its port on a free localhost port.

    `PS_FALKORDB_HOST` is always passed explicitly rather than relying on the source default
    (`127.0.0.1`), which reaches FalkorDB from no container that has its own network namespace
    -- neither a CI runner's nor the devcontainer's, which sets `PS_FALKORDB_HOST=falkordb`
    for exactly this reason.
    """
    port = _free_host_port()
    result = _run_container_cli(
        cli,
        [
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            network,
            "--publish",
            f"127.0.0.1:{port}:{_SERVICE_CONTAINER_PORT}",
            "--env",
            f"PS_FALKORDB_HOST={falkordb_host}",
            image_ref,
        ],
        timeout=_RUN_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, (
        f"starting {image_ref} as {name} failed (exit {result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    return _RunningService(name=name, base_url=f"http://127.0.0.1:{port}")


def _wait_for_liveness(cli: str, service: _RunningService) -> httpx.Response:
    """Poll `/health` through the published port until it answers 200, then return the response.

    Every request crosses the container boundary from the host, so a success here is itself
    evidence for AC-BI-008 (the image binds every interface, not loopback).
    """
    deadline = time.monotonic() + _LIVENESS_DEADLINE_SECONDS
    last_failure = "no response"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{service.base_url}/health", timeout=_HTTP_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            last_failure = repr(exc)
        else:
            if response.status_code == _HTTP_OK:
                return response
            last_failure = f"HTTP {response.status_code}: {response.text}"
        time.sleep(_POLL_INTERVAL_SECONDS)
    pytest.fail(
        f"{service.base_url}/health never answered {_HTTP_OK} within "
        f"{_LIVENESS_DEADLINE_SECONDS}s (last: {last_failure}):\n"
        f"{_container_logs(cli, service.name)}"
    )


@pytest.fixture(scope="session")
def smoke_network(container_cli: str) -> Iterator[str]:
    """Create the user-defined network that gives the service DNS resolution for FalkorDB."""
    name = _unique("ps-smoke-net")
    result = _run_container_cli(
        container_cli, ["network", "create", name], timeout=_INSPECT_TIMEOUT_SECONDS, check=False
    )
    assert result.returncode == 0, f"creating network {name} failed:\n{result.stderr}"
    try:
        yield name
    finally:
        _run_container_cli(
            container_cli,
            ["network", "rm", name],
            timeout=_INSPECT_TIMEOUT_SECONDS,
            check=False,
        )


@pytest.fixture(scope="session")
def falkordb_hostname(container_cli: str, smoke_network: str) -> Iterator[str]:
    """Run `falkordb/falkordb:latest` on the smoke network and return its resolvable hostname."""
    name = _unique("ps-smoke-falkordb")
    result = _run_container_cli(
        container_cli,
        ["run", "--detach", "--name", name, "--network", smoke_network, _FALKORDB_IMAGE],
        timeout=_PULL_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, (
        f"starting {_FALKORDB_IMAGE} failed (exit {result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    try:
        _wait_for_falkordb(container_cli, name)
        yield name
    finally:
        _remove_container(container_cli, name)


@pytest.fixture(scope="session")
def smoke_service(
    container_cli: str, image_ref: str, smoke_network: str, falkordb_hostname: str
) -> Iterator[_RunningService]:
    """Start the image under test against a reachable FalkorDB and wait until it serves traffic.

    Session-scoped: tests A, B' and C' all interrogate this one startup, and the startup probe
    they assert on happens exactly once per container.
    """
    name = _unique("ps-smoke-service")
    service = _start_service(
        container_cli,
        image_ref,
        network=smoke_network,
        falkordb_host=falkordb_hostname,
        name=name,
    )
    try:
        _wait_for_liveness(container_cli, service)
        yield service
    finally:
        _remove_container(container_cli, name)


def test_health_returns_200_alive_through_the_published_port(
    smoke_service: _RunningService,
) -> None:
    """A (AC-BI-007, AC-BI-008): `/health` answers 200 `alive` from outside the container."""
    response = httpx.get(f"{smoke_service.base_url}/health", timeout=_HTTP_TIMEOUT_SECONDS)

    assert response.status_code == _HTTP_OK, (
        f"/health answered {response.status_code} through the published port: {response.text}"
    )
    assert response.json() == {"status": "alive"}


def test_ready_returns_200_not_ready_while_the_llm_provider_is_unconfigured(
    smoke_service: _RunningService,
) -> None:
    """B' (AC-BI-007): `/ready` answers 200 `not_ready`.

    Deterministic, not merely observed: `connectivity.check_connectivity` raises without a
    network call because neither model variable is set, and `missing_ingestion_config_fields`
    is non-empty for the same reason, so `app.state.ready` cannot be `True`.

    What stops this being a tautology (FLAWS F-04) is the pair of log-derived tests either
    side of it -- `test_startup_probe_reports_the_llm_interface_dependency_unhealthy` fixes
    *why* the answer is `not_ready`, and
    `test_no_falkordb_startup_warning_is_emitted_when_falkordb_is_reachable` proves the
    reason is not FalkorDB. Each is its own test so a failure names which half broke.
    """
    response = httpx.get(f"{smoke_service.base_url}/ready", timeout=_HTTP_TIMEOUT_SECONDS)

    assert response.status_code == _HTTP_OK, (
        f"/ready answered {response.status_code}: {response.text}"
    )
    assert response.json() == {"status": "not_ready"}


def test_startup_probe_reports_the_llm_interface_dependency_unhealthy(
    container_cli: str, smoke_service: _RunningService
) -> None:
    """B' (AC-BI-007): the exactly-known reason `/ready` is `not_ready` is the LLM provider.

    Fails if the wrong image is under test or if the JSONL sink is missing. `cellar_eli` is
    asserted neither present nor absent: its outcome depends on the runner's egress, and
    asserting it would smuggle a third-party dependency back into the release gate.
    """
    unhealthy = _unhealthy_dependencies(_startup_log_entries(container_cli, smoke_service.name))

    assert _BARRIER_DEPENDENCY in unhealthy, (
        f"expected {_BARRIER_DEPENDENCY!r} to fail its startup probe (no model configured); "
        f"unhealthy set was {sorted(unhealthy)}"
    )


def test_no_falkordb_startup_warning_is_emitted_when_falkordb_is_reachable(
    container_cli: str, smoke_service: _RunningService
) -> None:
    """C' (AC-BI-007): FalkorDB specifically was healthy, proven behind a completion barrier.

    `_startup_log_entries` returns only once the `llm_interface` warning -- emitted strictly
    *after* the FalkorDB probe -- is in the JSONL, so "no falkordb entry" means the probe ran
    and succeeded, not that it had not run yet. `test_negative_control_...` below is the proof
    that this absence assertion can actually fail.
    """
    entries = _startup_log_entries(container_cli, smoke_service.name)

    assert _dependency_warnings(entries, _FALKORDB_DEPENDENCY) == [], (
        "the service logged a FalkorDB startup failure while FalkorDB was running and "
        f"reachable by hostname; entries: {entries}"
    )


def test_negative_control_a_falkordb_startup_warning_appears_when_falkordb_is_unreachable(
    container_cli: str, image_ref: str, smoke_network: str
) -> None:
    """D: the negative control -- C's mechanism demonstrably fails when FalkorDB is unreachable.

    Without this, C' is an absence-of-evidence assertion: a barrier that never observed a
    falkordb entry proves nothing unless a falkordb entry is known to be observable. Same
    image, same network, same barrier, one variable changed -- `PS_FALKORDB_HOST` points at an
    RFC 2606 `.invalid` name. Its own container, so the shared healthy stack is untouched.
    """
    name = _unique("ps-smoke-negative")
    service = _start_service(
        container_cli,
        image_ref,
        network=smoke_network,
        falkordb_host=_UNREACHABLE_FALKORDB_HOST,
        name=name,
    )
    try:
        _wait_for_liveness(container_cli, service)
        entries = _startup_log_entries(container_cli, service.name)

        assert _dependency_warnings(entries, _FALKORDB_DEPENDENCY) != [], (
            "no FalkorDB startup warning was logged even though FalkorDB was unreachable -- "
            f"the absence assertion in the C' test has no teeth; entries: {entries}"
        )
        unhealthy = _unhealthy_dependencies(entries)
        assert {_FALKORDB_DEPENDENCY, _BARRIER_DEPENDENCY} <= unhealthy, (
            f"expected both dependencies unhealthy; got {sorted(unhealthy)}"
        )

        response = httpx.get(f"{service.base_url}/ready", timeout=_HTTP_TIMEOUT_SECONDS)
        assert response.status_code == _HTTP_OK
        assert response.json() == {"status": "not_ready"}
    finally:
        _remove_container(container_cli, name)
