"""Shared fixtures for the `install.sh` release tests (GH issue #81, Slice 2).

Per `.orchestrator/tracker/issue-81-ps-cli-release-asset/CHANGES.md` X-01, this package does
**not** build a `PLAN.md`-style `http.server`-based `MockGitHub` and does **not** add any
env-var override (`PS_CLI_GITHUB_API_BASE`) to production `install.sh` -- a permanent,
silently-honoured override would let an attacker redirect the API base `install.sh` trusts,
undermining the checksum story `install.sh` exists to provide. Instead, every later
`install.sh` slice (3-9) shims `curl` itself on `PATH`, the same way
`ps-service/tests/release/conftest.py:115-180` shims `gh` for the release-automation
tests: a fake executable, logging every invocation, placed in a fixture-owned directory
prepended to `PATH` ahead of the real tool. `install.sh` never learns it is being faked --
only `PATH` changes.

Two pieces of infrastructure live here, both consumed by Slices 3-9 (this slice proves only
that they work, via `test_conftest_mock_github.py`):

- `wheel_builder` (session-scoped): builds a **real** `ps-cli` wheel via `uv build`, for any
  version string a test asks for, from a scratch copy of `ps-cli/` with `pyproject.toml`'s
  `version` field patched before the build (PLAN.md §3) -- hatchling reads the wheel version
  from that field at build time, so this is the only way to get a wheel stamped `9.9.9`
  without actually tagging a release. Each version is built at most once per test session
  (`WheelBuilder` caches by version).
- `fake_curl`: a `bin/curl` fake executable (CHANGES.md §1b) that answers exactly the three
  call shapes `install.sh` makes -- a metadata fetch (`curl -fsSL <url>`, no `-o`) and a file
  download (`curl -fsSL -o <dest> <url>`) -- by inspecting `$@`, never a real HTTP server.
  `FakeCurl.set_latest`/`.set_tag` register a release's JSON body and downloadable files;
  `FakeCurl.log` is the parsed invocation log, used by later slices to prove a code path was
  (or was not) reached.

Also provided: `fake_uv`, a no-op logging fake `uv` (same recipe) for Slice 6's negative-path
test -- CHANGES.md §1a notes it may end up unneeded there (checksum failure blocks before any
`uv` call under the CHANGES.md design), but is built here regardless so Slice 6's implementer
has it as a belt-and-suspenders check without having to invent the plumbing.

Marker policy: unmarked (not `integration`), matching `ps-service/tests/release/conftest.py`'s
own precedent for hermetic subprocess-based tests -- `uv`/`curl`/`sha256sum` are devcontainer
prerequisites and every fake executable here talks to nothing but the local filesystem.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

REPO_ROOT = Path(__file__).resolve().parents[3]
PS_CLI_DIR = REPO_ROOT / "ps-cli"

# Real GitHub asset download URL shape: `.../releases/download/<version>/<asset name>`. The
# version appears as its own path segment immediately before the filename -- used by both the
# JSON bodies this fixture writes and the fake `curl` script's dispatch, so a test that
# configures two different releases (e.g. Slice 4's `set_latest` + `set_tag`) never collides on
# the shared `SHA256SUMS` asset name.
DOWNLOAD_BASE_URL = "https://github.com/mindovermachine-dev/policy-system/releases/download"

SHORT_SUBPROCESS_TIMEOUT_SECONDS = 30.0
WHEEL_BUILD_TIMEOUT_SECONDS = 120.0

FAKE_CURL_LOG_ENV = "PS_CLI_FAKE_CURL_LOG"
FAKE_CURL_RESPONSES_ENV = "PS_CLI_FAKE_CURL_RESPONSES_DIR"
FAKE_UV_LOG_ENV = "PS_CLI_FAKE_UV_LOG"


def _require_tool(name: str) -> Path:
    """Locate a prerequisite executable; fail (never skip) when it is missing."""
    located = shutil.which(name)
    assert located is not None, f"`{name}` is a devcontainer/CI prerequisite but is not on PATH"
    return Path(located)


# --------------------------------------------------------------------------------------
# SHA-256: real `sha256sum`/`shasum -a 256` subprocess, never hand-computed (PLAN.md §1)
# --------------------------------------------------------------------------------------


def _sha256_command() -> list[str]:
    """The `sha256sum`/`shasum -a 256` command prefix, whichever this machine has."""
    sha256sum = shutil.which("sha256sum")
    if sha256sum is not None:
        return [sha256sum]
    return [str(_require_tool("shasum")), "-a", "256"]


def _sha256_hexdigest(path: Path) -> str:
    """The real SHA-256 hex digest of `path`, via a `sha256sum`/`shasum -a 256` subprocess."""
    completed = subprocess.run(  # noqa: S603 - command is shutil.which-resolved; args are literals/test paths
        [*_sha256_command(), str(path)],
        capture_output=True,
        text=True,
        timeout=SHORT_SUBPROCESS_TIMEOUT_SECONDS,
        check=True,
    )
    return completed.stdout.split()[0]


@pytest.fixture
def sha256_hexdigest() -> Callable[[Path], str]:
    """Fixture form of the real SHA-256 digest helper -- pytest's importlib import mode gives
    every `ps-cli/tests/release/*.py` module its own `conftest` identity, so a plain function
    cannot be `from conftest import ...`ed at runtime (only under `TYPE_CHECKING`, for types).
    Requesting this fixture by name is how a test reaches it instead.
    """
    return _sha256_hexdigest


def _sha256sums_line(wheel_path: Path) -> bytes:
    """The GNU-coreutils-format `SHA256SUMS` line for `wheel_path` (two spaces, name verbatim)."""
    completed = subprocess.run(  # noqa: S603 - command is shutil.which-resolved; cwd pins the relative filename
        [*_sha256_command(), wheel_path.name],
        cwd=wheel_path.parent,
        capture_output=True,
        text=True,
        timeout=SHORT_SUBPROCESS_TIMEOUT_SECONDS,
        check=True,
    )
    return completed.stdout.encode("utf-8")


# --------------------------------------------------------------------------------------
# Real wheel builder (PLAN.md §3): scratch copy of ps-cli/, version patched, `uv build`
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WheelArtifact:
    """One real, built `ps-cli` wheel plus its real `SHA256SUMS` line."""

    version: str
    wheel_filename: str
    wheel_bytes: bytes
    sha256sums_bytes: bytes


_VERSION_LINE = re.compile(r'^version = "[^"]+"$', re.MULTILINE)


def _patch_version(pyproject_path: Path, version: str) -> None:
    """Rewrite `pyproject_path`'s top-level `version = "..."` line to `version`."""
    original = pyproject_path.read_text(encoding="utf-8")
    patched, replacements = _VERSION_LINE.subn(f'version = "{version}"', original, count=1)
    assert replacements == 1, (
        f'expected exactly one `version = "..."` line in {pyproject_path}, replaced {replacements}'
    )
    pyproject_path.write_text(patched, encoding="utf-8")


def _build_ps_cli_wheel(build_root: Path, version: str) -> WheelArtifact:
    """Build a real `ps-cli` wheel stamped `version` from a scratch copy of `ps-cli/`."""
    scratch = build_root / f"ps-cli-{version}"
    shutil.copytree(
        PS_CLI_DIR,
        scratch,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", ".venv", "dist"),
    )
    _patch_version(scratch / "pyproject.toml", version)

    dist_dir = scratch / "dist"
    uv = _require_tool("uv")
    completed = subprocess.run(  # noqa: S603 - `uv` is shutil.which-resolved; args are literals
        [str(uv), "build", "--out-dir", str(dist_dir)],
        cwd=scratch,
        capture_output=True,
        text=True,
        timeout=WHEEL_BUILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, (
        f"uv build failed for ps-cli {version}:\n--- stdout ---\n{completed.stdout}"
        f"\n--- stderr ---\n{completed.stderr}"
    )

    wheel_filename = f"ps_cli-{version}-py3-none-any.whl"
    wheel_path = dist_dir / wheel_filename
    assert wheel_path.is_file(), (
        f"expected {wheel_filename} in {dist_dir}, found "
        f"{sorted(entry.name for entry in dist_dir.iterdir())}"
    )

    return WheelArtifact(
        version=version,
        wheel_filename=wheel_filename,
        wheel_bytes=wheel_path.read_bytes(),
        sha256sums_bytes=_sha256sums_line(wheel_path),
    )


@dataclass
class WheelBuilder:
    """Builds real `ps-cli` wheels on demand, caching one build per version for the session."""

    _build_root: Path
    _cache: dict[str, WheelArtifact] = field(default_factory=dict)

    def build(self, version: str) -> WheelArtifact:
        """Return the wheel artifact for `version`, building it once and reusing it after."""
        cached = self._cache.get(version)
        if cached is not None:
            return cached
        artifact = _build_ps_cli_wheel(self._build_root, version)
        self._cache[version] = artifact
        return artifact


@pytest.fixture(scope="session")
def wheel_builder(tmp_path_factory: pytest.TempPathFactory) -> WheelBuilder:
    """Session-scoped factory for real `ps-cli` wheels, parameterizable by version (PLAN.md §3)."""
    return WheelBuilder(_build_root=tmp_path_factory.mktemp("ps-cli-wheel-builds"))


# --------------------------------------------------------------------------------------
# Fake `curl` (CHANGES.md §1, replacing PLAN.md's `MockGitHub`/`http.server` design)
# --------------------------------------------------------------------------------------

# Dispatch (CHANGES.md §1b): every invocation is logged first. Scan `$@` for `-o`:
#   * absent  -> a metadata fetch; the last argument is the URL. `/releases/latest` and
#     `/releases/tags/<version>` each select a canned JSON body.
#   * present -> a file download; the argument after `-o` is `<dest>`, the last argument is
#     `<url>`. The URL's final two path segments are `<version>/<filename>`; `SHA256SUMS` or a
#     `.whl` filename selects the canned bytes to copy to `<dest>`. (CHANGES.md §1b describes
#     selecting only by the `.whl`/`SHA256SUMS` suffix; this also keys on the URL's version
#     segment so two releases configured in the same test -- e.g. Slice 4's `set_latest` +
#     `set_tag` -- never collide on the shared `SHA256SUMS` asset name.)
# No configured response -> a clear stderr message and a non-zero exit, never silent empty
# output (a silently-empty response would mask a genuinely-broken `install.sh` invocation as a
# passing test).
FAKE_CURL_SCRIPT = f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "${FAKE_CURL_LOG_ENV}"

responses="${{{FAKE_CURL_RESPONSES_ENV}}}"
args=("$@")
last_index=$(( ${{#args[@]}} - 1 ))
url="${{args[$last_index]}}"

output_index=-1
for i in "${{!args[@]}}"; do
  if [[ "${{args[$i]}}" == "-o" ]]; then
    output_index="$i"
    break
  fi
done

if [[ "$output_index" -eq -1 ]]; then
  case "$url" in
    */releases/latest)
      body="$responses/latest.json"
      ;;
    */releases/tags/*)
      version="${{url##*/releases/tags/}}"
      body="$responses/tags/$version.json"
      ;;
    *)
      echo "fake curl: no metadata route configured for GET $url" >&2
      exit 22
      ;;
  esac
  if [[ ! -f "$body" ]]; then
    echo "fake curl: no metadata route configured for GET $url" >&2
    exit 22
  fi
  cat "$body"
else
  dest="${{args[$((output_index + 1))]}}"
  version="$(basename "$(dirname "$url")")"
  filename="$(basename "$url")"
  case "$filename" in
    SHA256SUMS) source_file="$responses/downloads/$version/SHA256SUMS" ;;
    *.whl) source_file="$responses/downloads/$version/$filename" ;;
    *)
      echo "fake curl: no download route configured for GET $url" >&2
      exit 22
      ;;
  esac
  if [[ ! -f "$source_file" ]]; then
    echo "fake curl: no download route configured for GET $url (expected $source_file)" >&2
    exit 22
  fi
  cp "$source_file" "$dest"
fi
"""


@dataclass
class FakeCurl:
    """A fake `curl` on `PATH` (CHANGES.md §1b) plus the response table it serves from."""

    bin_dir: Path
    responses_dir: Path
    log_path: Path

    @property
    def environment(self) -> dict[str, str]:
        """Env vars the fake `curl` script needs, in addition to `PATH`."""
        return {
            FAKE_CURL_LOG_ENV: str(self.log_path),
            FAKE_CURL_RESPONSES_ENV: str(self.responses_dir),
        }

    @property
    def log(self) -> list[list[str]]:
        """Every `curl` invocation's argv, parsed into arg-vectors, in call order."""
        if not self.log_path.exists():
            return []
        return [
            shlex.split(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def set_latest(self, version: str, wheel_bytes: bytes, sha256sums_bytes: bytes) -> None:
        """Serve `version` as the `/releases/latest` release."""
        json_path = self.responses_dir / "latest.json"
        self._write_release(json_path, version, wheel_bytes, sha256sums_bytes)

    def set_tag(self, version: str, wheel_bytes: bytes, sha256sums_bytes: bytes) -> None:
        """Serve `version` at `/releases/tags/<version>`."""
        json_path = self.responses_dir / "tags" / f"{version}.json"
        self._write_release(json_path, version, wheel_bytes, sha256sums_bytes)

    def _write_release(
        self, json_path: Path, version: str, wheel_bytes: bytes, sha256sums_bytes: bytes
    ) -> None:
        """Write the downloadable bytes and the release JSON body for `version`."""
        wheel_filename = f"ps_cli-{version}-py3-none-any.whl"
        download_dir = self.responses_dir / "downloads" / version
        download_dir.mkdir(parents=True, exist_ok=True)
        (download_dir / wheel_filename).write_bytes(wheel_bytes)
        (download_dir / "SHA256SUMS").write_bytes(sha256sums_bytes)

        body = {
            "tag_name": version,
            "assets": [
                {
                    "name": wheel_filename,
                    "browser_download_url": f"{DOWNLOAD_BASE_URL}/{version}/{wheel_filename}",
                },
                {
                    "name": "SHA256SUMS",
                    "browser_download_url": f"{DOWNLOAD_BASE_URL}/{version}/SHA256SUMS",
                },
            ],
        }
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(body), encoding="utf-8")


@pytest.fixture
def fake_curl(tmp_path: Path) -> FakeCurl:
    """A fresh fake `curl` (CHANGES.md §1b), unconfigured -- tests call `.set_latest`/`.set_tag`."""
    bin_dir = tmp_path / "fake-curl-bin"
    bin_dir.mkdir()
    script = bin_dir / "curl"
    script.write_text(FAKE_CURL_SCRIPT, encoding="utf-8")
    script.chmod(0o755)

    responses_dir = tmp_path / "fake-curl-responses"
    responses_dir.mkdir()

    log_path = tmp_path / "fake-curl.log"
    return FakeCurl(bin_dir=bin_dir, responses_dir=responses_dir, log_path=log_path)


# --------------------------------------------------------------------------------------
# Fake `uv` (belt-and-suspenders helper for Slice 6's negative-path test)
# --------------------------------------------------------------------------------------

# A pure logger, not a functional `uv` replacement: Slice 6's checksum-mismatch test only
# needs to prove `uv` was never invoked (CHANGES.md §1a: checksum verification blocks before
# `install.sh` ever reaches the `uv tool install` line under this design), so there is nothing
# for a real invocation to do.
FAKE_UV_SCRIPT = f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_UV_LOG_ENV}"
"""


@dataclass
class FakeUv:
    """A no-op, logging fake `uv` on `PATH` -- proves `uv` was (or was not) invoked."""

    bin_dir: Path
    log_path: Path

    @property
    def environment(self) -> dict[str, str]:
        """Env vars the fake `uv` script needs, in addition to `PATH`."""
        return {FAKE_UV_LOG_ENV: str(self.log_path)}

    @property
    def log(self) -> list[list[str]]:
        """Every `uv` invocation's argv, parsed into arg-vectors, in call order."""
        if not self.log_path.exists():
            return []
        return [
            shlex.split(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line
        ]


@pytest.fixture
def fake_uv(tmp_path: Path) -> FakeUv:
    """A fresh no-op fake `uv` (belt-and-suspenders helper; see module docstring)."""
    bin_dir = tmp_path / "fake-uv-bin"
    bin_dir.mkdir()
    script = bin_dir / "uv"
    script.write_text(FAKE_UV_SCRIPT, encoding="utf-8")
    script.chmod(0o755)
    return FakeUv(bin_dir=bin_dir, log_path=tmp_path / "fake-uv.log")
