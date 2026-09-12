"""End-to-end rehearsal of the release pipeline against a real clone of this repo (S14,
AC-BI-023, AC-BI-031, "re-runs all").

Everything else under `ps-service/tests/release/` drives the scripts through a synthetic
fixture repo (`conftest.ReleaseFixture`) and a fake `gh` (DECISIONS X-07). This module is the
one exception: it clones THIS repository's real `main` branch into `tmp_path`, adds a synthetic
`feat(release): ... - resolves #79` commit on top of the real tip, and runs `release.sh`
against that clone with the REAL `gh` binary (no `PS_RELEASE_GH` override) -- proving the whole
pipeline end-to-end against real files, real tags and the real `gh-tt` extension, not just the
hermetic fixture's approximation of them.

Why this is `integration` (DECISIONS X-07) and excluded from the default suite:

- `gh tt semver` / `gh tt semver bump` need no GitHub API call (PLAN A-05: pure local `git`
  subprocesses), but they DO need the `gh-tt` extension installed for the invoking user -- a
  devcontainer/CI setup step, not something this suite can seed. `HOME` is therefore a fresh
  `tmp_path` directory carrying only a symlink to the real `~/.local/share/gh` extension store
  (never the real `HOME` wholesale, so the developer's git config/credentials never leak in);
  every commit this test or the release scripts make sets `GIT_AUTHOR_*`/`GIT_COMMITTER_*`
  explicitly, so no git identity is read from config either way.
- `sync-version-files.sh` re-locks `uv.lock` for the two workspace members via plain
  `uv version --package <name> --no-sync <version>` (no `--offline`). Verified by hand before
  writing this test: with `UV_OFFLINE=1` in a fresh clone, that re-lock fails ("basedpyright was
  not found in the cache") because a dev-only transitive dependency isn't warm in every cache:
  the running-workspace's own `.venv`/uv cache doesn't automatically transfer to the tool's
  global uv cache. With the ambient network left enabled (this sandbox has outbound access, and
  so do GitHub-hosted `ubuntu-latest` runners), the re-lock resolves the same 105 packages from
  cache/CDN in well under a second. So this test's own environment deliberately does NOT set
  `UV_OFFLINE=1`, unlike every other module in this package.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_SCRIPTS_DIR = REPO_ROOT / "scripts" / "release"
RELEASE_SCRIPT = RELEASE_SCRIPTS_DIR / "release.sh"

SUBPROCESS_TIMEOUT_SECONDS = 180.0
BARE_SEMVER_REGEX = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

REHEARSAL_COMMIT_HEADER = "feat(release): rehearse lockstep automation - resolves #79"
REHEARSAL_IDENTITY: dict[str, str] = {
    "GIT_AUTHOR_NAME": "S14 Rehearsal",
    "GIT_AUTHOR_EMAIL": "s14-rehearsal@example.invalid",
    "GIT_COMMITTER_NAME": "S14 Rehearsal",
    "GIT_COMMITTER_EMAIL": "s14-rehearsal@example.invalid",
}


def _require_tool(name: str) -> Path:
    """Locate a prerequisite executable; fail (never skip) when it is missing."""
    located = shutil.which(name)
    assert located is not None, f"`{name}` is a devcontainer/CI prerequisite but is not on PATH"
    return Path(located)


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    expect: int | None = 0,
) -> subprocess.CompletedProcess[str]:
    """Run `command`, asserting its exit code against `expect` unless it is `None`."""
    completed = subprocess.run(  # noqa: S603 - every element is a resolved absolute path or a test literal
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    if expect is not None:
        assert completed.returncode == expect, (
            f"{command} -> {completed.returncode}\n"
            f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
        )
    return completed


def _highest_real_release_tag(git: Path) -> str:
    """The highest bare-semver tag in this repository (mirrors `gh tt semver`, PLAN A-01/A-02)."""
    result = _run([str(git), "tag", "--list"], cwd=REPO_ROOT)
    tags = [tag for tag in result.stdout.split() if BARE_SEMVER_REGEX.match(tag)]
    assert tags, f"expected at least one bare-semver tag in {REPO_ROOT}"
    return max(tags, key=lambda tag: tuple(int(part) for part in tag.split(".")))


def _next_minor_version(version: str) -> str:
    """The next minor release after `version` (`X.Y.Z` -> `X.(Y+1).0`)."""
    major, minor, _patch = (int(part) for part in version.split("."))
    return f"{major}.{minor + 1}.0"


def _link_real_gh_extensions(home: Path) -> None:
    """Symlink the real `~/.local/share/gh` (the `gh-tt` extension store) into a throwaway HOME.

    This is the only piece of the real `HOME` this test borrows -- installing `gh-tt` is a
    devcontainer/CI prerequisite (PLAN A-05), not something a test can seed hermetically, but
    every commit identity is set explicitly via env vars, so no git config/credentials from the
    real `HOME` are needed or exposed.
    """
    real_home = os.environ.get("HOME", "")
    real_gh_share = Path(real_home) / ".local" / "share" / "gh"
    assert real_gh_share.is_dir(), (
        f"expected the `gh-tt` extension store at {real_gh_share} "
        "(devcontainer/CI prerequisite: `gh extension install devx-cafe/gh-tt`)"
    )
    share_dir = home / ".local" / "share"
    share_dir.mkdir(parents=True, exist_ok=True)
    (share_dir / "gh").symlink_to(real_gh_share, target_is_directory=True)


def _rehearsal_environment(
    *, home: Path, summary_path: Path, python_executable: str
) -> dict[str, str]:
    """The environment `release.sh` runs under: real `gh`, throwaway `HOME`, no `UV_OFFLINE`.

    Starts from the ambient environment (so `PATH` still resolves the real `git`/`bash`/`uv`/
    `gh`) and overrides only what the rehearsal needs to change: `PS_RELEASE_GH` is removed so
    `${PS_RELEASE_GH:-gh}` falls back to the real binary (A-05); `VIRTUAL_ENV` is removed so it
    does not point at this checkout's own `.venv` from inside the clone; `HOME` is the throwaway
    directory `_link_real_gh_extensions` prepared.
    """
    environment = dict(os.environ)
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("PS_RELEASE_GH", None)
    environment["HOME"] = str(home)
    environment["GITHUB_STEP_SUMMARY"] = str(summary_path)
    environment["UV_PYTHON"] = python_executable
    environment["GH_NO_UPDATE_NOTIFIER"] = "1"
    environment.update(REHEARSAL_IDENTITY)
    return environment


def test_release_rehearsal_on_a_clone_of_this_repo(tmp_path: Path) -> None:
    git = _require_tool("git")
    _require_tool("bash")
    uv = _require_tool("uv")
    _require_tool("gh")

    baseline_tag = _highest_real_release_tag(git)
    expected_release_version = _next_minor_version(baseline_tag)

    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    home = tmp_path / "home"
    home.mkdir()
    _link_real_gh_extensions(home)
    summary_path = tmp_path / "summary.md"

    # Clone THIS repo's real `main` (never the currently checked-out branch) with full tags.
    _run(
        [str(git), "clone", "--quiet", "--bare", "--branch", "main", str(REPO_ROOT), str(origin)],
        cwd=tmp_path,
    )
    _run([str(git), "clone", "--quiet", str(origin), str(work)], cwd=tmp_path)

    commit_environment = {**os.environ, **REHEARSAL_IDENTITY}
    _run(
        [str(git), "commit", "--allow-empty", "-q", "-m", REHEARSAL_COMMIT_HEADER],
        cwd=work,
        env=commit_environment,
    )
    head_before_release = _run([str(git), "rev-parse", "HEAD"], cwd=work).stdout.strip()

    environment = _rehearsal_environment(
        home=home, summary_path=summary_path, python_executable=sys.executable
    )

    _run([str(RELEASE_SCRIPT)], cwd=work, env=environment)

    # A new tag landed, bumping the real baseline's MINOR version (A-31/D-04: only `feat`s and
    # `fix`/`perf`s sit on `main` since the baseline; adding one more `feat` keeps the bump minor).
    tags_after = _run([str(git), "tag", "--list"], cwd=work).stdout.split()
    assert expected_release_version in tags_after, (
        f"expected tag {expected_release_version!r} bumping baseline {baseline_tag!r}, "
        f"got tags {sorted(tags_after)}"
    )

    head_after_release = _run([str(git), "rev-parse", "HEAD"], cwd=work).stdout.strip()
    assert head_after_release != head_before_release, "release.sh must create the release commit"

    tag_commit = _run(
        [str(git), "rev-list", "-n", "1", expected_release_version], cwd=work
    ).stdout.strip()
    assert tag_commit == head_after_release, "the new tag must point at the release commit (HEAD)"

    # All five version-lockstep fields equal the computed release version (AC-BI-012).
    ps_service_pyproject = tomllib.loads(
        (work / "ps-service" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert ps_service_pyproject["project"]["version"] == expected_release_version

    ps_cli_pyproject = tomllib.loads(
        (work / "ps-cli" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert ps_cli_pyproject["project"]["version"] == expected_release_version

    chart = yaml.safe_load(
        (work / "charts" / "policy-system" / "Chart.yaml").read_text(encoding="utf-8")
    )
    assert chart["version"] == expected_release_version
    assert chart["appVersion"] == expected_release_version

    plugin = json.loads(
        (work / "ps-skills" / "policy-system" / ".claude-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    assert plugin["version"] == expected_release_version

    lock = tomllib.loads((work / "uv.lock").read_text(encoding="utf-8"))
    lock_versions = {
        package["name"]: package["version"]
        for package in lock["package"]
        if package["name"] in ("ps-service", "ps-cli")
    }
    assert lock_versions == {
        "ps-service": expected_release_version,
        "ps-cli": expected_release_version,
    }

    # `uv lock --check` is clean in the temp clone (real, network-backed re-lock; see module
    # docstring for why `UV_OFFLINE` is deliberately not set here).
    lock_check = _run([str(uv), "lock", "--check"], cwd=work, env=environment, expect=None)
    assert lock_check.returncode == 0, (
        f"uv lock --check failed after the rehearsal:\n{lock_check.stdout}{lock_check.stderr}"
    )

    # The atomic push actually reached origin, not just the work clone.
    origin_tags = _run(
        [str(git), "--git-dir", str(origin), "tag", "--list"], cwd=tmp_path
    ).stdout.split()
    assert expected_release_version in origin_tags
