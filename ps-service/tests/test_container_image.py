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

import base64
import contextlib
import json
import os
import shutil
import subprocess
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from ps_test_support.required_startup_env import REQUIRED_STARTUP_ENV

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

pytestmark = pytest.mark.container_image

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Laptop-path tag. Deliberately the only occurrence of a hardcoded image name in this module:
# every other reference flows through the `image_ref` fixture.
_DEFAULT_LOCAL_TAG = "ps-service:local-test"

# The packaged `catalog.json` the Dockerfile's allow-listed build context (`ps-service/src`)
# carries into the image -- the source of truth `GET /catalog` must serve from inside it.
_PACKAGED_CATALOG_JSON = _REPO_ROOT / "ps-service/src/ps_service/api/curated_content/catalog.json"

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
# answers, B' pins `/ready`'s exact answer and the exact reason for it, C' proves FalkorDB
# specifically was healthy, and D is C's negative control.
#
# Issue #58 made `create_app` fail closed without OIDC config (AC-BI-002), so every container
# this module starts now runs with the local-test bypass (issue #67) -- and the bypass is itself
# refused on a non-loopback bind (`main._refuse_non_loopback_bypass_bind`), which the image's own
# baked-in default (`PS_SERVICE_HOST=0.0.0.0`, AC-BI-008) is. So these containers now bind
# loopback and every test reaches them via `docker exec` (`_get_from_container`), inside their
# own network namespace, instead of through a published port. AC-BI-008's env-baking half is
# still proven live by `test_container_env_sets_ps_service_host_to_all_interfaces`; its
# published-port-reachability half has no live coverage left in this module.
#
# Why `/ready` is asserted `not_ready` and not `ready` (the F-03 residual, stated once here):
# `app.state.ready` also requires the Cellar/ELI probe, whose endpoint is a hardcoded module
# constant (`ingestion/adapters/cellar_eli/fetch.py`) with no configuration seam. A literal
# `"ready"` would therefore make the release gate depend on a live third-party service. The
# gate instead asserts `/ready` answers 503 `not_ready` for the exactly-known reason, and
# proves FalkorDB specifically healthy -- which is the half of AC-BI-007 this image controls.

_FALKORDB_IMAGE = "falkordb/falkordb:latest"
_SERVICE_CONTAINER_PORT = 8000

# `logging/facade.py`'s `_DEFAULT_LOG_FILENAME` under the Dockerfile's `PS_LOGGING_DIR`. The
# startup entries are written here, not to stdout, so the barrier reads the file.
_LOG_FILE_IN_IMAGE = "/var/log/ps-service/ps-service.jsonl"

_FALKORDB_DEPENDENCY = "falkordb"
_BARRIER_DEPENDENCY = "llm_interface"

# Issue #133: PS state Postgres is unconditionally probed and never no-ops when unconfigured
# (unlike Passkey Signing Postgres), so it always shows unhealthy in the smoke-test container,
# which never sets `PS_STATE_POSTGRES_HOST`. Since issue #130 it also gates `/ready`.
_STATE_POSTGRES_DEPENDENCY = "state_postgres"

# Issue #130: `GET /catalog` reads the catalog-source override from the PS state Postgres and
# fails closed without one, so the catalog smoke test runs against a real Postgres sidecar.
_POSTGRES_IMAGE = "postgres:16-alpine"
# The sidecar is provisioned like production: the bootstrap superuser is `postgres_admin` (the
# chart's `psPostgres.admin.user`), and the real init script creates the unprivileged `ps_state`
# role and database, so no `POSTGRES_DB` is set (the script's `CREATE DATABASE ps_state OWNER
# ps_state` would collide with one). The three dicts keep the admin credential, the application
# credential and the init script's extra inputs apart.
_STATE_INIT_SCRIPT = _REPO_ROOT / "charts/policy-system/files/ps-postgres-init.sh"
_ADMIN_PASSWORD_IN_SMOKE = "ps-smoke-admin-password"
_STATE_ADMIN_ENV = {
    "POSTGRES_USER": "postgres_admin",
    "POSTGRES_PASSWORD": _ADMIN_PASSWORD_IN_SMOKE,
}
_STATE_APP_CREDS = {
    "user": "ps_state",
    "password": "ps-smoke-state-password",
    "database": "ps_state",
}
_STATE_INIT_ENV = {
    "PS_STATE_POSTGRES_PASSWORD": _STATE_APP_CREDS["password"],
    "PS_PASSKEYSIGNING_POSTGRES_PASSWORD": "ps-smoke-signing-password",
}
_STATE_POSTGRES_DEADLINE_SECONDS = 90.0

# RFC 2606 reserves `.invalid`, so this name cannot resolve on any runner -- the negative
# control's unreachability is guaranteed rather than merely likely.
_UNREACHABLE_FALKORDB_HOST = "falkordb-unreachable.invalid"

_HTTP_OK = 200
_HTTP_SERVICE_UNAVAILABLE = 503
_HTTP_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.25
_STARTUP_BARRIER_SECONDS = 30.0
_LIVENESS_DEADLINE_SECONDS = 60.0
_FALKORDB_DEADLINE_SECONDS = 60.0
_PULL_TIMEOUT_SECONDS = 900.0


@dataclass(frozen=True)
class _RunningService:
    """A started `ps-service` container, bound to loopback inside its own network namespace."""

    name: str


def _unique(prefix: str) -> str:
    """Return a collision-free container/network name, so parallel runs never clash."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


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


def _wait_for_state_postgres(cli: str, container: str) -> None:
    """Block until the sidecar Postgres accepts TCP connections, so the service can migrate.

    Checks `127.0.0.1` (TCP), not the default unix socket: the official image starts a
    socket-only temporary server while it runs its init scripts, so a socket check can pass
    before the real server is up -- and the service treats a failed startup migration as fatal.
    """
    deadline = time.monotonic() + _STATE_POSTGRES_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        result = _run_container_cli(
            cli,
            [
                "exec",
                container,
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "ps_state",
                "-d",
                "ps_state",
            ],
            timeout=_INSPECT_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    pytest.fail(
        f"{_POSTGRES_IMAGE} did not accept connections within "
        f"{_STATE_POSTGRES_DEADLINE_SECONDS}s:\n"
        f"{_container_logs(cli, container)}"
    )


def _get_from_container(
    cli: str, container: str, path: str, *, port: int = _SERVICE_CONTAINER_PORT
) -> httpx.Response:
    """GET `path` from inside `container`'s own network namespace via `docker exec`.

    These containers bind loopback only (local-test bypass, AC-BI-002), so the host cannot
    reach them through a published port -- this execs `python` inside the container and issues
    the request from its own network namespace instead, via the image's own `httpx` install
    (a genuine runtime dependency, not a dev-only one -- see `_DEV_ONLY_MODULES`). A non-zero
    exit (typically `ConnectionRefusedError` while the service has not started listening yet)
    surfaces as `httpx.ConnectError`, matching the exception type a direct `httpx.get` would
    have raised, so every poll-loop call site needs no change in kind.
    """
    snippet = (
        "import base64, httpx; "
        f"r = httpx.get('http://127.0.0.1:{port}{path}', timeout={_HTTP_TIMEOUT_SECONDS}); "
        "print(r.status_code); "
        "print(base64.b64encode(r.content).decode())"
    )
    result = _run_container_cli(
        cli,
        ["exec", container, "python", "-c", snippet],
        timeout=_INSPECT_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise httpx.ConnectError(result.stderr.strip() or "docker exec probe failed")
    status_line, body_line = result.stdout.splitlines()[:2]
    return httpx.Response(status_code=int(status_line), content=base64.b64decode(body_line))


def _env_flags(env: dict[str, str]) -> list[str]:
    """Expand an env dict into repeated `docker run --env KEY=VALUE` arguments."""
    flags: list[str] = []
    for key, value in env.items():
        flags.extend(["--env", f"{key}={value}"])
    return flags


def _start_service(
    cli: str, image_ref: str, *, network: str, falkordb_host: str, name: str
) -> _RunningService:
    """Start the image under test on `network`, bound to loopback with the local-test bypass.

    `PS_SERVICE_LOCAL_TEST_BYPASS=true` is needed because these tests carry no OIDC credentials
    (issue #67) and `create_app` now fails closed without one (issue #58, AC-BI-002). The bypass
    is itself refused on a non-loopback bind (`main._refuse_non_loopback_bypass_bind`) -- and the
    image's own baked-in default is `PS_SERVICE_HOST=0.0.0.0` (AC-BI-008) -- so `PS_SERVICE_HOST`
    is pinned to loopback here explicitly. That means no `--publish`ed port can reach this
    container from the host; every test against it goes through `_get_from_container` instead.

    `PS_FALKORDB_HOST` is always passed explicitly rather than relying on the source default
    (`127.0.0.1`), which reaches FalkorDB from no container that has its own network namespace
    -- neither a CI runner's nor the devcontainer's, which sets `PS_FALKORDB_HOST=falkordb`
    for exactly this reason.

    `REQUIRED_STARTUP_ENV` (issue #148) supplies every env var `create_app` needs unconditionally,
    regardless of the local-test bypass (currently `PS_AUTHENTIK_API_TOKEN`/`PS_AUTHENTIK_BASE_URL`,
    issue #140, AC-BI-003) -- without them the container never reaches a listening `/health` at
    all. It is the same dict `test_startup_env_parity.py` proves is sufficient, hermetically and
    without Docker, so a future addition to it can never again go unnoticed here.
    """
    result = _run_container_cli(
        cli,
        [
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            network,
            "--env",
            f"PS_FALKORDB_HOST={falkordb_host}",
            "--env",
            "PS_SERVICE_HOST=127.0.0.1",
            "--env",
            "PS_SERVICE_LOCAL_TEST_BYPASS=true",
            *_env_flags(REQUIRED_STARTUP_ENV),
            image_ref,
        ],
        timeout=_RUN_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, (
        f"starting {image_ref} as {name} failed (exit {result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    return _RunningService(name=name)


def _wait_for_liveness(cli: str, service: _RunningService) -> httpx.Response:
    """Poll `/health` inside the container until it answers 200, then return the response."""
    deadline = time.monotonic() + _LIVENESS_DEADLINE_SECONDS
    last_failure = "no response"
    while time.monotonic() < deadline:
        try:
            response = _get_from_container(cli, service.name, "/health")
        except httpx.HTTPError as exc:
            last_failure = repr(exc)
        else:
            if response.status_code == _HTTP_OK:
                return response
            last_failure = f"HTTP {response.status_code}: {response.text}"
        time.sleep(_POLL_INTERVAL_SECONDS)
    pytest.fail(
        f"{service.name}'s /health never answered {_HTTP_OK} within "
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


def test_health_returns_200_alive(container_cli: str, smoke_service: _RunningService) -> None:
    """A (AC-BI-007): `/health` answers 200 `alive`.

    AC-BI-002 (built-image half): `version` must match `ps-service/pyproject.toml`'s own
    declared `[project] version` -- the real, non-editable wheel install baked into the image
    (see CHANGES.md C-02) carries hatchling-stamped metadata equal to that source-tree value.
    """
    response = _get_from_container(container_cli, smoke_service.name, "/health")

    assert response.status_code == _HTTP_OK, (
        f"/health answered {response.status_code}: {response.text}"
    )
    with (_REPO_ROOT / "ps-service/pyproject.toml").open("rb") as pyproject_file:
        expected_version = tomllib.load(pyproject_file)["project"]["version"]
    assert response.json()["status"] == "alive"
    assert response.json()["version"] == expected_version


def test_ready_returns_503_not_ready_while_the_llm_provider_is_unconfigured(
    container_cli: str, smoke_service: _RunningService
) -> None:
    """B' (AC-BI-007): `/ready` answers 503 `not_ready`.

    Deterministic, not merely observed: `connectivity.check_connectivity` raises without a
    network call because neither model variable is set, and `missing_ingestion_config_fields`
    is non-empty for the same reason, so `app.state.ready` cannot be `True`.

    What stops this being a tautology (FLAWS F-04) is the pair of log-derived tests either
    side of it -- `test_startup_probe_reports_the_llm_interface_dependency_unhealthy` fixes
    *why* the answer is `not_ready`, and
    `test_no_falkordb_startup_warning_is_emitted_when_falkordb_is_reachable` proves the
    reason is not FalkorDB. Each is its own test so a failure names which half broke.
    """
    response = _get_from_container(container_cli, smoke_service.name, "/ready")

    assert response.status_code == _HTTP_SERVICE_UNAVAILABLE, (
        f"/ready answered {response.status_code}: {response.text}"
    )
    assert response.json() == {
        "status": "not_ready",
        "unhealthy_dependencies": [_BARRIER_DEPENDENCY, _STATE_POSTGRES_DEPENDENCY],
    }


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


def _wait_for_catalog(cli: str, service: _RunningService) -> httpx.Response:
    """Poll `/catalog` until it answers 200, then return it.

    New Slice 6.8 (CHANGES.md MA3): `GET /catalog` needs no FalkorDB/LLM
    dependency (AC-BI-011), so this polls the same way `_wait_for_liveness`
    polls `/health`, just against `/catalog` instead.
    """
    deadline = time.monotonic() + _LIVENESS_DEADLINE_SECONDS
    last_failure = "no response"
    while time.monotonic() < deadline:
        try:
            response = _get_from_container(cli, service.name, "/catalog")
        except httpx.HTTPError as exc:
            last_failure = repr(exc)
        else:
            if response.status_code == _HTTP_OK:
                return response
            last_failure = f"HTTP {response.status_code}: {response.text}"
        time.sleep(_POLL_INTERVAL_SECONDS)
    pytest.fail(
        f"{service.name}'s /catalog never answered {_HTTP_OK} within "
        f"{_LIVENESS_DEADLINE_SECONDS}s (last: {last_failure}):\n"
        f"{_container_logs(cli, service.name)}"
    )


def _init_script_volume_flag(cli: str) -> str:
    """Return the `--volume` value mounting the real init script read-only into the sidecar.

    The file is mode 0644, so the postgres entrypoint sources it (`. file`) instead of executing
    it; nothing here depends on the exec bit. Podman needs `z` to relabel the bind mount.
    """
    options = "ro,z" if "podman" in Path(cli).name else "ro"
    target = "/docker-entrypoint-initdb.d/ps-postgres-init.sh"
    return f"{_STATE_INIT_SCRIPT.resolve()}:{target}:{options}"


@contextlib.contextmanager
def _state_postgres_sidecar(cli: str, network: str) -> Generator[str]:
    """Run a production-shaped PS state Postgres sidecar and yield its resolvable hostname."""
    name = _unique("ps-smoke-state-pg")
    result = _run_container_cli(
        cli,
        [
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            network,
            *_env_flags({**_STATE_ADMIN_ENV, **_STATE_INIT_ENV}),
            "--volume",
            _init_script_volume_flag(cli),
            _POSTGRES_IMAGE,
        ],
        timeout=_PULL_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, (
        f"starting {_POSTGRES_IMAGE} failed (exit {result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    try:
        _wait_for_state_postgres(cli, name)
        yield name
    finally:
        _remove_container(cli, name)


def _state_target_env(state_hostname: str) -> dict[str, str]:
    """Return the `PS_STATE_POSTGRES_*` application-role target the CLI and service share."""
    return {
        "PS_STATE_POSTGRES_HOST": state_hostname,
        "PS_STATE_POSTGRES_PORT": "5432",
        "PS_STATE_POSTGRES_DATABASE": _STATE_APP_CREDS["database"],
        "PS_STATE_POSTGRES_USER": _STATE_APP_CREDS["user"],
    }


def _run_provision_cli(
    cli: str, image_ref: str, network: str, state_hostname: str
) -> subprocess.CompletedProcess[str]:
    """Run the provisioning CLI from the image under test, as the Helm Job does.

    The admin credential reaches this one-shot container only; the service container started
    later never receives it.
    """
    env = {
        **_state_target_env(state_hostname),
        "PS_STATE_ADMIN_POSTGRES_USER": _STATE_ADMIN_ENV["POSTGRES_USER"],
        "PS_STATE_ADMIN_POSTGRES_PASSWORD": _STATE_ADMIN_ENV["POSTGRES_PASSWORD"],
    }
    return _run_container_cli(
        cli,
        [
            "run",
            "--rm",
            "--network",
            network,
            *_env_flags(env),
            image_ref,
            "python",
            "-m",
            "ps_service.graph_gateway.provision",
        ],
        timeout=_RUN_TIMEOUT_SECONDS,
        check=False,
    )


def _start_state_postgres_service(
    cli: str, image_ref: str, network: str, state_hostname: str, name: str
) -> None:
    """Start the image under test with no FalkorDB, holding only the `ps_state` credential.

    Bound to loopback with the local-test bypass, same reasoning as `_start_service` (issue #58,
    AC-BI-002); also needs `REQUIRED_STARTUP_ENV` for the same reason (issue #148).
    """
    env = {
        "PS_SERVICE_HOST": "127.0.0.1",
        "PS_SERVICE_LOCAL_TEST_BYPASS": "true",
        **_state_target_env(state_hostname),
        "PS_STATE_POSTGRES_PASSWORD": _STATE_APP_CREDS["password"],
        **REQUIRED_STARTUP_ENV,
    }
    result = _run_container_cli(
        cli,
        ["run", "--detach", "--name", name, "--network", network, *_env_flags(env), image_ref],
        timeout=_RUN_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, (
        f"starting {image_ref} as {name} failed (exit {result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )


def _wait_for_exit_logs(cli: str, name: str) -> str:
    """Return a container's logs once it has exited, failing if it keeps running."""
    deadline = time.monotonic() + _LIVENESS_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        state = _run_container_cli(
            cli,
            ["inspect", "--format", "{{.State.Running}}", name],
            timeout=_INSPECT_TIMEOUT_SECONDS,
            check=False,
        )
        if state.stdout.strip() == "false":
            return _container_logs(cli, name)
        time.sleep(_POLL_INTERVAL_SECONDS)
    pytest.fail(f"{name} kept running although its state database is not provisioned")


@pytest.fixture(scope="module")
def state_postgres_hostname(container_cli: str, smoke_network: str) -> Iterator[str]:
    """Run a production-shaped PS state Postgres sidecar and return its resolvable hostname.

    Real init script, separate admin role, unprivileged `ps_state` (see the env dicts above).
    The database is empty: `provisioned_state_postgres` runs the CLI on it.
    """
    with _state_postgres_sidecar(container_cli, smoke_network) as hostname:
        yield hostname


@pytest.fixture(scope="module")
def provisioned_state_postgres(
    container_cli: str, image_ref: str, smoke_network: str, state_postgres_hostname: str
) -> str:
    """Provision the sidecar with the CLI from the image under test, then return its hostname.

    This is the operator flow the Helm Job performs: the service refuses to start until it ran
    (issue #205), and the CLI needs only the admin credential, never the service's.
    """
    result = _run_provision_cli(container_cli, image_ref, smoke_network, state_postgres_hostname)
    assert result.returncode == 0, (
        f"provisioning {state_postgres_hostname} failed (exit {result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}"
    )
    return state_postgres_hostname


@pytest.fixture(scope="module")
def catalog_only_service(
    container_cli: str, image_ref: str, smoke_network: str, provisioned_state_postgres: str
) -> Iterator[_RunningService]:
    """Start the image under test with no FalkorDB at all, against a provisioned state Postgres.

    New Slice 6.8: `GET /catalog` (AC-BI-011) is provably FalkorDB/LLM-free,
    so this deliberately does *not* reuse `smoke_service` (which wires a
    FalkorDB container) -- the route answers with real content with no graph
    database running at all. Since issue #130 the catalog-source override lives in the PS state
    Postgres and a failed read fails closed, so this container is given a real Postgres sidecar.
    Since issue #205 the service holds `ps_state` credentials only and fails closed until the
    provisioning CLI ran, which `provisioned_state_postgres` does first.
    """
    name = _unique("ps-smoke-catalog")
    _start_state_postgres_service(
        container_cli, image_ref, smoke_network, provisioned_state_postgres, name
    )
    service = _RunningService(name=name)
    try:
        _wait_for_liveness(container_cli, service)
        yield service
    finally:
        _remove_container(container_cli, name)


def test_get_catalog_serves_real_packaged_content_from_inside_the_built_image(
    container_cli: str, catalog_only_service: _RunningService
) -> None:
    """New Slice 6.8 (CHANGES.md MA3): `GET /catalog` serves real, non-empty content
    from *inside a built container image* -- not a `tmp_path` fixture.

    This is the exact proof FLAWS.md's MA3 verdict required: `catalog.json`'s
    packaged copy lives at `ps_service/api/curated_content/catalog.json`,
    already inside the Dockerfile's allow-listed build context
    (`ps-service/src`) -- unlike the repo-root `curated-content/` tree, which
    `.dockerignore`'s deny-all-then-allow-list excludes entirely. Without
    MA3's fix, this route would 500/serve an empty listing inside a real
    deployed image even though every fast, in-process test (`tests/api/
    test_routes_catalog.py`) would still pass, since those never touch a
    built image at all.
    """
    response = _wait_for_catalog(container_cli, catalog_only_service)

    body = response.json()
    assert body["instruments"], "GET /catalog returned an empty listing from inside the image"
    instrument_ids = {item["instrument_id"] for item in body["instruments"]}
    # The expectation is the packaged copy itself, not a hardcoded id list: every curation
    # run rewrites `catalog.json` (see `curated_content/__init__.py`), so a literal here would
    # go stale on the next export while the image stayed correct. Comparing against the file
    # the Dockerfile packages is the actual MA3 proof -- the image serves *these* bytes.
    packaged_ids = {
        item["instrument_id"]
        for item in json.loads(_PACKAGED_CATALOG_JSON.read_text(encoding="utf-8"))
    }
    assert packaged_ids, "packaged catalog.json is empty -- nothing to prove against"
    assert instrument_ids == packaged_ids


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

        response = _get_from_container(container_cli, service.name, "/ready")
        assert response.status_code == _HTTP_SERVICE_UNAVAILABLE
        assert response.json() == {
            "status": "not_ready",
            "unhealthy_dependencies": [
                _FALKORDB_DEPENDENCY,
                _BARRIER_DEPENDENCY,
                _STATE_POSTGRES_DEPENDENCY,
            ],
        }
    finally:
        _remove_container(container_cli, name)


_GRAPH_GATEWAY_MIGRATION = "0001_graph_mutation_log.sql"
_LIST_GRAPH_GATEWAY_MIGRATIONS = (
    "import importlib.resources as r; "
    "print(*sorted(p.name for p in r.files('ps_service.graph_gateway.migrations').iterdir() "
    "if p.name.endswith('.sql')))"
)


def test_runtime_image_ships_graph_gateway_migrations_and_provision_cli_entry(
    container_cli: str, image_ref: str
) -> None:
    """Issue #205 AC-BI-011/012: the image the chart's provisioning Job runs carries both parts.

    The Job runs `python -m ps_service.graph_gateway.provision` from this image, so the module
    must be runnable (`--help` prints usage and never connects) and the migration file it applies
    must be packaged.
    """
    usage = _run_container_cli(
        container_cli,
        ["run", "--rm", image_ref, "python", "-m", "ps_service.graph_gateway.provision", "--help"],
        timeout=_RUN_TIMEOUT_SECONDS,
        check=False,
    )
    migrations = _python_in_image(
        container_cli, image_ref, _LIST_GRAPH_GATEWAY_MIGRATIONS, check=False
    )

    assert usage.returncode == 0, f"provision --help failed in {image_ref}:\n{usage.stderr}"
    assert "PS_STATE_ADMIN_POSTGRES_USER" in usage.stdout
    assert migrations.returncode == 0, f"listing migrations failed:\n{migrations.stderr}"
    assert _GRAPH_GATEWAY_MIGRATION in migrations.stdout.split()


def _psql_in_sidecar(cli: str, sidecar: str, statement: str, *, database: str = "postgres") -> str:
    """Run one SQL statement as the sidecar's admin role and return its unaligned output."""
    result = _run_container_cli(
        cli,
        [
            "exec",
            sidecar,
            "psql",
            "--username",
            _STATE_ADMIN_ENV["POSTGRES_USER"],
            "--dbname",
            database,
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--command",
            statement,
        ],
        timeout=_INSPECT_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, f"psql failed in {sidecar}:\n{result.stderr}"
    return result.stdout.strip()


def test_state_postgres_sidecar_matches_production_roles(
    container_cli: str, state_postgres_hostname: str
) -> None:
    """The sidecar is built by the real init script, so the smoke test cannot pass on a lax one.

    A superuser `ps_state` (the old fixture) would make the startup verifier's ownership check
    meaningless, so the application role must be an unprivileged login that owns only its own
    database, and the graph owner role must be NOLOGIN.
    """
    roles = _psql_in_sidecar(
        container_cli,
        state_postgres_hostname,
        "SELECT r.rolsuper, r.rolcanlogin, "
        "(SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'postgres'), "
        "(SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'ps_state'), "
        "(SELECT NOT rolcanlogin FROM pg_roles WHERE rolname = 'ps_state_graph_owner') "
        "FROM pg_roles r WHERE r.rolname = 'ps_state'",
    )

    assert roles == "f|t|postgres_admin|ps_state|t"


def test_provision_cli_in_built_image_provisions_empty_state_postgres_and_is_idempotent(
    container_cli: str, image_ref: str, smoke_network: str
) -> None:
    """AC-FR-001/002/006: the CLI shipped in the image takes an empty ps_state to ready, twice.

    Uses its own sidecar: the shared one is already provisioned by `provisioned_state_postgres`,
    so the first run would be a no-op there.
    """
    with _state_postgres_sidecar(container_cli, smoke_network) as sidecar:
        first = _run_provision_cli(container_cli, image_ref, smoke_network, sidecar)
        second = _run_provision_cli(container_cli, image_ref, smoke_network, sidecar)
        tables = _psql_in_sidecar(
            container_cli,
            sidecar,
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'graph_log'",
            database=_STATE_APP_CREDS["database"],
        )

    assert first.returncode == 0, f"first provision failed:\n{first.stdout}\n{first.stderr}"
    assert "0001_graph_mutation_log.sql" in first.stdout
    assert second.returncode == 0, f"second provision failed:\n{second.stdout}\n{second.stderr}"
    assert "none (already up to date)" in second.stdout
    assert tables == "5"
    assert _ADMIN_PASSWORD_IN_SMOKE not in first.stdout + first.stderr


def test_service_fails_closed_in_image_on_unprovisioned_state_postgres(
    container_cli: str, image_ref: str, smoke_network: str
) -> None:
    """Negative control for AC-FR-006: the service never comes up without the CLI step.

    Same sidecar and service wiring as `catalog_only_service`, minus the provisioning run. The
    process exits at startup with the verifier's fixed reason, so a green catalog test cannot be
    explained by a verifier that quietly stopped checking.
    """
    with _state_postgres_sidecar(container_cli, smoke_network) as sidecar:
        name = _unique("ps-smoke-unprovisioned")
        _start_state_postgres_service(container_cli, image_ref, smoke_network, sidecar, name)
        try:
            logs = _wait_for_exit_logs(container_cli, name)
        finally:
            _remove_container(container_cli, name)

    assert "migration_not_recorded" in logs
    assert _ADMIN_PASSWORD_IN_SMOKE not in logs
