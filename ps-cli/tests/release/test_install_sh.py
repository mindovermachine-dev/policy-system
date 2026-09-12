"""Red-then-green tests for the rewritten `install.sh` (GH issue #81, Slices 3-6).

Exercises `install.sh` as a real `bash` subprocess against the `fake_curl`/`fake_uv`/
`wheel_builder` fixtures built in Slice 2 (`ps-cli/tests/release/conftest.py`), per
CHANGES.md X-01: no `PS_CLI_GITHUB_API_BASE` env-var seam is added to production
`install.sh` -- the fake `curl` prepended onto `PATH` is the only test-only surface, and
`install.sh` itself never learns it is being faked (only `PATH` changes).

Each positive-path test sandboxes the real `uv tool install` this script runs via
`UV_TOOL_DIR`/`UV_TOOL_BIN_DIR` pointed at fresh `tmp_path` subdirectories (PLAN.md §1,
confirmed working), then proves the installed version by invoking
`<UV_TOOL_BIN_DIR>/ps-cli --version` as its own sandboxed subprocess -- the "back to the CLI
surface" half of each vertical slice, not just "the script exited 0".

Marker policy: unmarked (not `integration`), matching `conftest.py`'s own documented
precedent -- every subprocess here (`bash`, `curl`, `uv`, `sha256sum`/`shasum`) talks only to
the local filesystem, aside from the real `uv tool install`'s own dependency resolution
against the package index the devcontainer already depends on for `uv build`/`uv sync` to
work at all.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from conftest import FakeCurl, FakeUv, WheelBuilder

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "ps-cli" / "install.sh"

INSTALL_TIMEOUT_SECONDS = 180.0
VERSION_CHECK_TIMEOUT_SECONDS = 30.0
# Slice 7's `PS_CLI_REF` path does a real `git clone` + real `uv` dependency resolution against
# this repo's real `main` branch over the network -- generously longer than the local, hermetic
# fake-`curl` runs above, which never leave 127.0.0.1-free loopback-less local processes.
GIT_PLUS_INSTALL_TIMEOUT_SECONDS = 300.0


def _run_install_sh(
    *,
    tmp_path: Path,
    path_dirs: list[Path],
    extra_env: dict[str, str],
    include_inherited_path: bool = True,
    timeout: float = INSTALL_TIMEOUT_SECONDS,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run `install.sh` as a real `bash` subprocess, sandboxed into fresh `tmp_path` dirs.

    `include_inherited_path` prepends `path_dirs` onto the real, inherited `PATH` by default
    (every Slice 3-7/9 scenario). Slice 8's no-`gh`-on-`PATH` proof passes `False` instead: the
    inherited `PATH` on this devcontainer resolves `gh` from the same directory as `bash` and
    the GNU coreutils `install.sh` needs, so appending it would silently re-admit `gh` no matter
    what `path_dirs` excludes.

    `UV_TOOL_BIN_DIR` is always included in the assembled `PATH` (first, so a freshly installed
    `ps-cli` always wins over any other same-named executable already on `PATH`) regardless of
    `include_inherited_path` -- on a real machine this directory is expected to already be on
    `PATH` (typically `~/.local/bin`, via `uv tool update-shell`) for `uv tool install` to be
    useful at all; `install.sh`'s own final `ps-cli --version` line relies on that. The directory
    need not exist yet when the subprocess starts -- `uv tool install` creates and populates it
    partway through the same run, and `PATH` is only searched when `ps-cli --version` is reached.

    Returns the completed process and the `UV_TOOL_BIN_DIR` a successful run installs into.
    """
    tool_dir = tmp_path / "uv-tool-dir"
    tool_bin_dir = tmp_path / "uv-tool-bin-dir"
    path_prefix = os.pathsep.join([str(tool_bin_dir), *(str(directory) for directory in path_dirs)])
    path_value = (
        os.pathsep.join([path_prefix, os.environ.get("PATH", "")])
        if include_inherited_path
        else path_prefix
    )
    environment = {
        **os.environ,
        "PATH": path_value,
        "UV_TOOL_DIR": str(tool_dir),
        "UV_TOOL_BIN_DIR": str(tool_bin_dir),
        **extra_env,
    }
    completed = subprocess.run(  # noqa: S603 - args are literals; bash + a fixed script path
        ["bash", str(INSTALL_SH)],  # noqa: S607 - intentional: PATH search must find test PATH's bash
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return completed, tool_bin_dir


def _installed_version(
    tool_bin_dir: Path, *, path_dirs: list[Path]
) -> subprocess.CompletedProcess[str]:
    """Invoke `<tool_bin_dir>/ps-cli --version` as its own sandboxed subprocess."""
    path_prefix = os.pathsep.join(str(directory) for directory in path_dirs)
    environment = {
        **os.environ,
        "PATH": os.pathsep.join([path_prefix, os.environ.get("PATH", "")]),
    }
    return subprocess.run(  # noqa: S603 - args are literals; ps-cli path is the sandboxed tool_bin_dir
        [str(tool_bin_dir / "ps-cli"), "--version"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=VERSION_CHECK_TIMEOUT_SECONDS,
        check=False,
    )


def test_install_with_no_overrides_installs_latest_and_prints_its_version(
    tmp_path: Path, fake_curl: FakeCurl, wheel_builder: WheelBuilder
) -> None:
    artifact = wheel_builder.build("9.9.9")
    fake_curl.set_latest("9.9.9", artifact.wheel_bytes, artifact.sha256sums_bytes)

    completed, tool_bin_dir = _run_install_sh(
        tmp_path=tmp_path,
        path_dirs=[fake_curl.bin_dir],
        extra_env=fake_curl.environment,
    )
    assert completed.returncode == 0, completed.stderr

    version = _installed_version(tool_bin_dir, path_dirs=[fake_curl.bin_dir])
    assert version.returncode == 0, version.stderr
    assert "9.9.9" in version.stdout


def test_install_with_ps_cli_version_installs_that_release_not_latest(
    tmp_path: Path, fake_curl: FakeCurl, wheel_builder: WheelBuilder
) -> None:
    latest_artifact = wheel_builder.build("9.9.9")
    pinned_artifact = wheel_builder.build("1.2.3")
    fake_curl.set_latest("9.9.9", latest_artifact.wheel_bytes, latest_artifact.sha256sums_bytes)
    fake_curl.set_tag("1.2.3", pinned_artifact.wheel_bytes, pinned_artifact.sha256sums_bytes)

    completed, tool_bin_dir = _run_install_sh(
        tmp_path=tmp_path,
        path_dirs=[fake_curl.bin_dir],
        extra_env={**fake_curl.environment, "PS_CLI_VERSION": "1.2.3"},
    )
    assert completed.returncode == 0, completed.stderr

    version = _installed_version(tool_bin_dir, path_dirs=[fake_curl.bin_dir])
    assert version.returncode == 0, version.stderr
    assert "1.2.3" in version.stdout
    assert "9.9.9" not in version.stdout


def test_invalid_ps_cli_version_format_fails_before_any_network_call(
    tmp_path: Path, fake_curl: FakeCurl
) -> None:
    completed, _tool_bin_dir = _run_install_sh(
        tmp_path=tmp_path,
        path_dirs=[fake_curl.bin_dir],
        extra_env={**fake_curl.environment, "PS_CLI_VERSION": "not-a-version"},
    )
    assert completed.returncode == 1
    assert "X.Y.Z" in completed.stderr
    assert fake_curl.log == []


def _flip_one_hex_character(sha256sums_bytes: bytes) -> bytes:
    """Corrupt a real `SHA256SUMS` line's hash by flipping its first hex character."""
    text = sha256sums_bytes.decode("utf-8")
    first_character = text[0]
    flipped_character = "0" if first_character != "0" else "1"
    return (flipped_character + text[1:]).encode("utf-8")


def test_checksum_mismatch_aborts_and_never_invokes_uv(
    tmp_path: Path, fake_curl: FakeCurl, fake_uv: FakeUv, wheel_builder: WheelBuilder
) -> None:
    artifact = wheel_builder.build("9.9.9")
    corrupted_sha256sums = _flip_one_hex_character(artifact.sha256sums_bytes)
    fake_curl.set_latest("9.9.9", artifact.wheel_bytes, corrupted_sha256sums)

    completed, _tool_bin_dir = _run_install_sh(
        tmp_path=tmp_path,
        path_dirs=[fake_curl.bin_dir, fake_uv.bin_dir],
        extra_env={**fake_curl.environment, **fake_uv.environment},
    )
    assert completed.returncode != 0
    assert artifact.wheel_filename in completed.stderr
    assert fake_uv.log == []


@pytest.mark.integration
def test_ps_cli_ref_installs_via_git_plus_url_skipping_release_resolution(
    tmp_path: Path, fake_curl: FakeCurl
) -> None:
    """AC-BI-007: `PS_CLI_REF` installs via `git+...@<ref>#subdirectory=ps-cli`, never
    entering the release-resolution/`curl` machinery -- proven by asserting `fake_curl.log`
    stays empty, not just that the process happens to exit 0.

    Does real dependency resolution against this repo's real `main` branch over the network
    (`git+https://github.com/.../policy-system@main#subdirectory=ps-cli`) -- marked
    `integration` per L2's `ps-cli` Testing Patterns rule, matching
    `ps-service/tests/release/test_release_rehearsal.py`'s precedent for the one test in its
    module that clones the real repo instead of a hermetic fixture.
    """
    # `fake_curl` is deliberately left unconfigured (no `.set_latest`/`.set_tag`): if
    # `install.sh` ever called `curl` in this path, the fake would have no response to serve
    # and would exit non-zero itself (see `FAKE_CURL_SCRIPT`'s "no ... route configured"
    # branches), so a clean exit here is itself part of the proof, on top of the log-emptiness
    # assertion below.
    completed, tool_bin_dir = _run_install_sh(
        tmp_path=tmp_path,
        path_dirs=[fake_curl.bin_dir],
        extra_env={**fake_curl.environment, "PS_CLI_REF": "main"},
        timeout=GIT_PLUS_INSTALL_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0, completed.stderr
    assert fake_curl.log == []

    version = _installed_version(tool_bin_dir, path_dirs=[fake_curl.bin_dir])
    assert version.returncode == 0, version.stderr


_GH_FREE_REQUIRED_TOOLS = (
    "bash",
    "sha256sum",
    "grep",
    "sed",
    "awk",
    "mktemp",
    "basename",
    "dirname",
    "head",
    "rm",
    "cat",
    "cp",
    "uv",
)


def _build_gh_free_bin_dir(tmp_path: Path) -> Path:
    """A private bin dir symlinking exactly the executables `install.sh` (plus the fake `curl`
    test double's own coreutils use, e.g. `cat`) shells out to, never `gh` (AC-BI-008;
    CHANGES.md X-04 -- no existing precedent to imitate, written fresh).

    Locates each tool via `shutil.which` on the real machine. On this devcontainer `gh` is
    apt-installed into the *same* directory (`/usr/bin`) as `bash` and the GNU coreutils
    `install.sh` needs (confirmed empirically: `command -v bash sha256sum grep gh` all resolve
    under `/usr/bin` here) -- so assembling `PATH` from those tools' raw parent directories
    would silently re-admit `gh` no matter how "minimal" the directory list looks. Symlinking
    each required executable by name into a fresh, curated directory is the construction that
    both supplies every tool `install.sh` calls and genuinely excludes `gh`; the caller still
    positively re-checks this via `shutil.which("gh", path=...)` rather than trusting the
    construction blindly.
    """
    bin_dir = tmp_path / "gh-free-bin"
    bin_dir.mkdir()
    for tool_name in _GH_FREE_REQUIRED_TOOLS:
        resolved = shutil.which(tool_name)
        assert resolved is not None, (
            f"`{tool_name}` is a devcontainer/CI prerequisite but is not on PATH"
        )
        (bin_dir / tool_name).symlink_to(resolved)
    return bin_dir


def test_install_succeeds_with_no_gh_on_path(
    tmp_path: Path, fake_curl: FakeCurl, wheel_builder: WheelBuilder
) -> None:
    """AC-BI-008: `install.sh` succeeds with only `curl`, `sha256sum`/`shasum`, and `uv` (plus
    the POSIX utilities it shells out to) on `PATH` -- `gh` is never required and, here, is
    positively confirmed absent from the assembled `PATH` rather than merely assumed absent.
    """
    artifact = wheel_builder.build("9.9.9")
    fake_curl.set_latest("9.9.9", artifact.wheel_bytes, artifact.sha256sums_bytes)

    gh_free_bin_dir = _build_gh_free_bin_dir(tmp_path)
    path_dirs = [fake_curl.bin_dir, gh_free_bin_dir]

    assembled_path = os.pathsep.join(str(directory) for directory in path_dirs)
    assert shutil.which("gh", path=assembled_path) is None, (
        "PATH minimization failed to exclude `gh` from the assembled PATH -- "
        "AC-BI-008 cannot be proven by this test as constructed"
    )

    completed, tool_bin_dir = _run_install_sh(
        tmp_path=tmp_path,
        path_dirs=path_dirs,
        extra_env=fake_curl.environment,
        include_inherited_path=False,
    )
    assert completed.returncode == 0, completed.stderr

    version = _installed_version(tool_bin_dir, path_dirs=[fake_curl.bin_dir])
    assert version.returncode == 0, version.stderr
    assert "9.9.9" in version.stdout


def test_rerunning_install_upgrades_to_the_newer_release(
    tmp_path: Path, fake_curl: FakeCurl, wheel_builder: WheelBuilder
) -> None:
    """AC-BI-009: re-running `install.sh` against the same sandboxed `UV_TOOL_DIR`/
    `UV_TOOL_BIN_DIR` (simulating the same machine) lands the newer of two releases.

    Empirically confirmed (see IMPL_SLICE_4.md) that plain `uv tool install <local-wheel-path>`
    already uninstalls the previous version and installs the new one when the two differ --
    `install.sh` downloads each run's wheel to a fresh `mktemp -d` path with the release's own
    version baked into its filename, so no `--reinstall` flag was needed in `install.sh` itself.
    """
    first_artifact = wheel_builder.build("1.0.0")
    fake_curl.set_latest("1.0.0", first_artifact.wheel_bytes, first_artifact.sha256sums_bytes)

    first_completed, tool_bin_dir = _run_install_sh(
        tmp_path=tmp_path,
        path_dirs=[fake_curl.bin_dir],
        extra_env=fake_curl.environment,
    )
    assert first_completed.returncode == 0, first_completed.stderr
    first_version = _installed_version(tool_bin_dir, path_dirs=[fake_curl.bin_dir])
    assert first_version.returncode == 0, first_version.stderr
    assert "1.0.0" in first_version.stdout

    second_artifact = wheel_builder.build("2.0.0")
    fake_curl.set_latest("2.0.0", second_artifact.wheel_bytes, second_artifact.sha256sums_bytes)

    second_completed, tool_bin_dir_again = _run_install_sh(
        tmp_path=tmp_path,
        path_dirs=[fake_curl.bin_dir],
        extra_env=fake_curl.environment,
    )
    assert second_completed.returncode == 0, second_completed.stderr
    assert tool_bin_dir_again == tool_bin_dir

    second_version = _installed_version(tool_bin_dir, path_dirs=[fake_curl.bin_dir])
    assert second_version.returncode == 0, second_version.stderr
    assert "2.0.0" in second_version.stdout
    assert "1.0.0" not in second_version.stdout
