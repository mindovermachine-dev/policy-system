"""Shared fixtures for the release-automation tests (`scripts/release/**`, GH issue #79).

Why this package lives under `ps-service/tests/release/` rather than a root-level `tests/`
(PLAN F-08, precedent `ps-service/tests/test_release_workflow.py`): the root `pyproject.toml`'s
`testpaths` covers only the two workspace members' test dirs and `ps-service/tests` is already
inside basedpyright's strict `include` list. A root-level `tests/` would need `testpaths` and
`include` edits and would be a strict-type-checking blind spot until then. The release scripts
are not `ps-service` source, so this is a documented deviation from L2's "tests mirror `src/`".

Marker policy (DECISIONS X-07): nothing in this package is marked `integration` except
`test_release_rehearsal.py`. The tests are hermetic -- they build temporary git repositories
(`git init -b main` plus a bare `origin.git`), an `--offline` uv mini-workspace (PLAN A-16), and
a fake `gh` selected through `PS_RELEASE_GH`; no network, no services, no GitHub. They follow
`test_release_workflow.py`'s precedent of executing `bash` unmarked, and they deliberately
deviate from L2's "mark `git`-spawning tests `integration`" so that the default suite (and the
CI check added in slice S4b) exercises them on every run.

Prerequisites: `git` and `bash` must be on this machine -- both are devcontainer and CI
prerequisites, so their absence is a failure, not a skip. `uv` is required by the version-sync
tests (slice S7) and is located the same way.

Fixture layout under `tmp_path` (CHANGES X-06 recipe):

    origin.git/         bare, default branch `main`
    work/               `git init -b main`, seeded, `origin` added, `main` pushed, tag `0.11.0`
    bin/gh              fake `gh` (PLAN B.3, CHANGES X-08) -- logs every argv to `PS_RELEASE_GH_LOG`
    shim/git            git shim (CHANGES X-01) -- logs script-issued git argv to `PS_TEST_GIT_LOG`
    summary.md          the file `GITHUB_STEP_SUMMARY` points at
    home/               throwaway `HOME` so the developer's own git config never leaks in

The seeded `work/` tree holds a minimal offline uv workspace (root `pyproject.toml`, two
dependency-free hatchling members `ps-service` and `ps-cli` at `0.11.0`, a `uv.lock`) plus real
copies of `charts/policy-system/Chart.yaml` and `ps-skills/policy-system/.claude-plugin/
plugin.json`, so `sed`/`awk` patterns are exercised on the true file shapes. `charts/policy-
system/values.yaml` is no longer a synced file (issue #80 AC-BI-012 amendment) and is not seeded
here.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_SCRIPTS_DIR = REPO_ROOT / "scripts" / "release"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

SEED_TAG = "0.11.0"
SEED_HEADER = "chore: seed"
MINI_WORKSPACE_MEMBERS = ("ps-service", "ps-cli")
REAL_VERSION_FILES = (
    Path("charts/policy-system/Chart.yaml"),
    Path("ps-skills/policy-system/.claude-plugin/plugin.json"),
)

SEED_IDENTITY: dict[str, str] = {
    "GIT_AUTHOR_NAME": "Seed Author",
    "GIT_AUTHOR_EMAIL": "seed@example.invalid",
    "GIT_COMMITTER_NAME": "Seed Author",
    "GIT_COMMITTER_EMAIL": "seed@example.invalid",
}

SUBPROCESS_TIMEOUT_SECONDS = 60.0
NEWLINE_TOKEN = "\n"

# uv needs an interpreter to resolve `requires-python`; the minimal PATH below may expose a
# stripped-down `/usr/bin/python3`, so pin uv to the interpreter running this test session.
# `UV_OFFLINE` keeps every uv call the scripts make hermetic (PLAN A-16).
UV_ENVIRONMENT: dict[str, str] = {"UV_PYTHON": sys.executable, "UV_OFFLINE": "1"}

# A `git commit`/`gc` may fork a detached background auto-maintenance child. In a container
# whose PID 1 does not reap (e.g. `sleep infinity`), each such orphan becomes an unreapable
# zombie, and a test suite that makes many commits exhausts the PID cgroup. Disabling git's
# auto-maintenance via inherited config env vars (honoured by every git subprocess the fixture
# or the release scripts spawn) removes the source rather than papering over it.
GIT_NO_MAINTENANCE: dict[str, str] = {
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "gc.auto",
    "GIT_CONFIG_VALUE_0": "0",
    "GIT_CONFIG_KEY_1": "maintenance.auto",
    "GIT_CONFIG_VALUE_1": "false",
}


def _require_tool(name: str) -> Path:
    """Locate a prerequisite executable; fail (never skip) when it is missing."""
    located = shutil.which(name)
    assert located is not None, f"`{name}` is a devcontainer/CI prerequisite but is not on PATH"
    return Path(located)


# --------------------------------------------------------------------------------------
# Fake `gh` (PLAN B.3, CHANGES X-08) and git shim (CHANGES X-01)
# --------------------------------------------------------------------------------------

# Dispatches on `$1 $2`. Every invocation is logged first (X-08). `tt semver` mirrors gh-tt's
# real behaviour (highest bare-semver tag over ALL tags, PLAN A-01/A-02); `tt semver bump` runs
# the exact `git tag -a` gh-tt runs (A-03) through `$REAL_GIT` so the git shim never sees it;
# `tt semver note --filename` writes A-07's shape; `release view` exits 1 unless the test has
# created the marker file `$PS_RELEASE_GH_RELEASE_EXISTS`; `release create|edit` only log.
# `PS_RELEASE_GH_SEMVER_OUTPUT`, when set, replaces the `tt semver` answer (failure injection
# for AC-BI-013: a test sets it to `not-a-version`).
FAKE_GH_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$PS_RELEASE_GH_LOG"
git_binary="${REAL_GIT:-git}"

highest_release_tag() {
  "$git_binary" tag --list | grep -E '^[0-9]+\\.[0-9]+\\.[0-9]+$' | sort -V | tail -1
}

next_version() {
  local current="$1" level="$2" major minor patch
  IFS=. read -r major minor patch <<< "$current"
  case "$level" in
    --major) printf '%s.0.0' "$((major + 1))" ;;
    --minor) printf '%s.%s.0' "$major" "$((minor + 1))" ;;
    --patch) printf '%s.%s.%s' "$major" "$minor" "$((patch + 1))" ;;
    *) echo "fake gh: unknown bump level '$level'" >&2; exit 2 ;;
  esac
}

write_note() {
  local from_tag="" to_tag="" filename=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --from) from_tag="$2"; shift 2 ;;
      --to) to_tag="$2"; shift 2 ;;
      --filename) filename="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  {
    printf '## Release Notes for %s\\n\\n' "$to_tag"
    printf '[changes since %s](../../compare/%s..%s)\\n' "$from_tag" "$from_tag" "$to_tag"
    "$git_binary" log --format='%n- **%cd**: %s%n%h %an' "$from_tag..$to_tag"
  } > "$filename"
  printf '%s\\n' "$filename"
}

case "${1:-} ${2:-}" in
  "tt semver")
    case "${3:-}" in
      "")
        if [[ -n "${PS_RELEASE_GH_SEMVER_OUTPUT:-}" ]]; then
          printf '%s\\n' "$PS_RELEASE_GH_SEMVER_OUTPUT"
        else
          highest_release_tag
        fi
        ;;
      bump)
        current="$(highest_release_tag)"
        level="${4:?bump level}"
        next="$(next_version "$current" "$level")"
        "$git_binary" tag -a -m "$next
Bumped ${level#--} from version '$current' to '$next'" "$next"
        ;;
      note) shift 3; write_note "$@" ;;
      *) echo "fake gh: unsupported 'tt semver $3'" >&2; exit 2 ;;
    esac
    ;;
  "release view")
    [[ -n "${PS_RELEASE_GH_RELEASE_EXISTS:-}" && -e "$PS_RELEASE_GH_RELEASE_EXISTS" ]] || exit 1
    ;;
  "release create"|"release edit") ;;
  *) echo "fake gh: unsupported invocation '$*'" >&2; exit 2 ;;
esac
"""

GIT_SHIM_SCRIPT = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$PS_TEST_GIT_LOG"
exec "$REAL_GIT" "$@"
"""


def git_shim(tmp_path: Path, real_git: Path) -> Path:
    """Write the git shim (CHANGES X-01) and return the directory to prepend to `PATH`.

    The shim logs every git argv a *script* issues to `PS_TEST_GIT_LOG` and then execs the real
    git. The fake `gh` calls `$REAL_GIT` directly, so its own git calls bypass the shim.
    """
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "git"
    shim.write_text(GIT_SHIM_SCRIPT, encoding="utf-8")
    shim.chmod(0o755)
    _ = real_git  # the shim resolves it from the environment at run time
    return shim_dir


# --------------------------------------------------------------------------------------
# Script runs and git repositories
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScriptRun:
    """Captured outcome of one script or git invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        """Stdout followed by stderr -- for assertions that do not care which stream."""
        return self.stdout + self.stderr


@dataclass
class GitRepository:
    """A working clone the tests drive with plain git (seed commits, racers)."""

    path: Path
    git: Path
    environment: dict[str, str]

    def run(self, *args: str, expect: int | None = 0) -> ScriptRun:
        """Run `git <args>` inside this repository."""
        completed = subprocess.run(  # noqa: S603 - `git` is a shutil.which-resolved absolute path; args are test literals
            [str(self.git), *args],
            cwd=self.path,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
        run = ScriptRun(completed.returncode, completed.stdout, completed.stderr)
        if expect is not None:
            assert run.returncode == expect, (
                f"git {shlex.join(args)} -> {run.returncode}\n{run.output}"
            )
        return run

    def commit(self, header: str, body: str = "") -> str:
        """Create an empty commit with `header` (and optional `body`); return its SHA."""
        message = header if not body else f"{header}\n\n{body}"
        self.run("commit", "-q", "--allow-empty", "-m", message)
        return self.head()

    def head(self) -> str:
        """Return the SHA of `HEAD`."""
        return self.run("rev-parse", "HEAD").stdout.strip()

    def push(self, *refs: str) -> ScriptRun:
        """Push `main` (or the given refs) to `origin`."""
        return self.run("push", "-q", "origin", *(refs or ("main",)))

    def tags(self) -> list[str]:
        """Return every tag name in this repository."""
        return self.run("tag", "--list").stdout.split()


@dataclass
class ReleaseFixture:
    """The fixture repo pair plus the helpers PLAN B.4 names.

    `work` is the clone the scripts under test run in; `origin` is the bare remote whose
    refs prove what actually landed.
    """

    root: Path
    origin: Path
    work: GitRepository
    summary_path: Path
    fake_gh: Path
    gh_log: Path
    git_log: Path
    home: Path
    bash: Path
    git: Path
    uv: Path | None
    _clone_count: int = field(default=0, init=False)

    def base_environment(self, *, log_git: bool = False) -> dict[str, str]:
        """Minimal environment for a script run: PATH, HOME, summary file, fake gh, shim."""
        path_entries = [str(self.git.parent), str(self.bash.parent), "/usr/bin", "/bin"]
        if self.uv is not None:
            path_entries.insert(0, str(self.uv.parent))
        if log_git:
            path_entries.insert(0, str(git_shim(self.root, self.git)))
        environment: dict[str, str] = {
            "PATH": os.pathsep.join(dict.fromkeys(path_entries)),
            "HOME": str(self.home),
            "GITHUB_STEP_SUMMARY": str(self.summary_path),
            "PS_RELEASE_GH": str(self.fake_gh),
            "PS_RELEASE_GH_LOG": str(self.gh_log),
            "PS_TEST_GIT_LOG": str(self.git_log),
            "REAL_GIT": str(self.git),
            **UV_ENVIRONMENT,
            **GIT_NO_MAINTENANCE,
        }
        return environment

    def run_script(
        self,
        script: str,
        *args: str,
        cwd: Path | None = None,
        expect: int | None = 0,
        log_git: bool = False,
        env_extra: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> ScriptRun:
        """Run `scripts/release/<script>` with list args inside the fixture clone.

        `expect` asserts the exit code (pass `None` to inspect it yourself); `log_git=True`
        prepends the git shim (X-01) so `read_git_log()` shows every git argv the script issued.
        """
        environment = self.base_environment(log_git=log_git)
        environment.update(env_extra or {})
        completed = subprocess.run(  # noqa: S603 - the script path is repo-owned; args are test literals, never shell text
            [str(RELEASE_SCRIPTS_DIR / script), *args],
            cwd=cwd or self.work.path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
            input=stdin,
        )
        run = ScriptRun(completed.returncode, completed.stdout, completed.stderr)
        if expect is not None:
            assert run.returncode == expect, (
                f"{script} {shlex.join(args)} exited {run.returncode}, expected {expect}\n"
                f"--- stdout ---\n{run.stdout}--- stderr ---\n{run.stderr}"
            )
        return run

    def run_bash(
        self,
        snippet: str,
        *,
        cwd: Path,
        env_extra: Mapping[str, str] | None = None,
        expect: int | None = 0,
    ) -> ScriptRun:
        """Execute a workflow `run:` body under bash with the fixture environment.

        `GIT_DIR` points at the fixture clone so `cwd` can be the repo root (where the body's
        relative `scripts/release/...` path resolves) while git reads the fixture history.
        """
        environment = self.base_environment()
        environment["GIT_DIR"] = str(self.work.path / ".git")
        environment.update(env_extra or {})
        completed = subprocess.run(  # noqa: S603 - bash is a shutil.which-resolved absolute path; the snippet is read from the repo's own workflow
            [str(self.bash), "-c", snippet],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
        run = ScriptRun(completed.returncode, completed.stdout, completed.stderr)
        if expect is not None:
            assert run.returncode == expect, (
                f"bash -c {snippet!r} -> {run.returncode}\n{run.output}"
            )
        return run

    def commit(self, header: str, body: str = "") -> str:
        """Commit (empty) in the work clone; returns the new SHA."""
        return self.work.commit(header, body)

    def origin_refs(self) -> dict[str, str]:
        """Map every ref on the bare origin (`refs/heads/main`, `refs/tags/X`) to its SHA."""
        listing = self.work.run("ls-remote", str(self.origin)).stdout
        refs: dict[str, str] = {}
        for line in listing.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/") and not ref.endswith("^{}"):
                refs[ref] = sha
        return refs

    def read_summary(self) -> str:
        """Return what the scripts appended to `GITHUB_STEP_SUMMARY` (empty if nothing)."""
        return self.summary_path.read_text(encoding="utf-8") if self.summary_path.exists() else ""

    def read_gh_log(self) -> list[str]:
        """Every argv line the fake `gh` recorded."""
        return self.gh_log.read_text(encoding="utf-8").splitlines() if self.gh_log.exists() else []

    def read_git_log(self) -> list[str]:
        """Every git argv line the shim recorded (only with `log_git=True`)."""
        return (
            self.git_log.read_text(encoding="utf-8").splitlines() if self.git_log.exists() else []
        )

    def second_clone(self) -> GitRepository:
        """Clone origin's `main` into a racer checkout (valid only after `main` was pushed)."""
        self._clone_count += 1
        racer_path = self.root / f"racer{self._clone_count}"
        self.work.run("clone", "-q", "-b", "main", str(self.origin), str(racer_path))
        return GitRepository(racer_path, self.git, self.work.environment)


def _write_mini_workspace(work_dir: Path) -> None:
    """Seed the dependency-free uv workspace PLAN A-16 describes (members at `SEED_TAG`)."""
    members = ", ".join(f'"{member}"' for member in MINI_WORKSPACE_MEMBERS)
    (work_dir / "pyproject.toml").write_text(
        f"[tool.uv.workspace]\nmembers = [{members}]\n", encoding="utf-8"
    )
    for member in MINI_WORKSPACE_MEMBERS:
        package = member.replace("-", "_")
        package_dir = work_dir / member / "src" / package
        package_dir.mkdir(parents=True)
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
        (work_dir / member / "pyproject.toml").write_text(
            "[project]\n"
            f'name = "{member}"\n'
            f'version = "{SEED_TAG}"\n'
            'requires-python = ">=3.14"\n'
            "dependencies = []\n\n"
            "[build-system]\n"
            'requires = ["hatchling"]\n'
            'build-backend = "hatchling.build"\n\n'
            "[tool.hatch.build.targets.wheel]\n"
            f'packages = ["src/{package}"]\n',
            encoding="utf-8",
        )


def _copy_real_version_files(work_dir: Path) -> None:
    """Copy the real chart and plugin files so sync patterns meet the true shapes."""
    for relative in REAL_VERSION_FILES:
        destination = work_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, destination)


def _lock_mini_workspace(work_dir: Path, uv: Path, environment: Mapping[str, str]) -> None:
    """Create `uv.lock` offline (PLAN A-16) so the seed commit carries a consistent lock."""
    subprocess.run(  # noqa: S603 - `uv` is a shutil.which-resolved absolute path; args are literals
        [str(uv), "lock", "--offline", "--quiet"],
        cwd=work_dir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=True,
    )


@pytest.fixture
def release_fixture(tmp_path: Path) -> ReleaseFixture:
    """Build the bare origin + seeded `main` clone tagged `0.11.0` (CHANGES X-06 recipe)."""
    git = _require_tool("git")
    bash = _require_tool("bash")
    uv_located = shutil.which("uv")
    uv = Path(uv_located) if uv_located is not None else None

    home = tmp_path / "home"
    home.mkdir()
    environment: dict[str, str] = {
        "PATH": os.pathsep.join([str(git.parent), str(bash.parent), "/usr/bin", "/bin"]),
        "HOME": str(home),
        **SEED_IDENTITY,
        **UV_ENVIRONMENT,
        **GIT_NO_MAINTENANCE,
    }
    if uv is not None:
        environment["PATH"] = os.pathsep.join([str(uv.parent), environment["PATH"]])

    origin = tmp_path / "origin.git"
    work_dir = tmp_path / "work"
    bootstrap = GitRepository(tmp_path, git, environment)
    bootstrap.run("init", "-q", "--bare", "-b", "main", str(origin))
    bootstrap.run("init", "-q", "-b", "main", str(work_dir))

    _write_mini_workspace(work_dir)
    _copy_real_version_files(work_dir)
    if uv is not None:
        _lock_mini_workspace(work_dir, uv, environment)

    work = GitRepository(work_dir, git, environment)
    work.run("add", "-A")
    work.run("commit", "-q", "-m", SEED_HEADER)
    work.run("remote", "add", "origin", str(origin))
    work.run("push", "-q", "-u", "origin", "main")
    work.run("tag", "-a", "-m", SEED_TAG, SEED_TAG)
    work.run("push", "-q", "origin", SEED_TAG)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(FAKE_GH_SCRIPT, encoding="utf-8")
    fake_gh.chmod(0o755)

    return ReleaseFixture(
        root=tmp_path,
        origin=origin,
        work=work,
        summary_path=tmp_path / "summary.md",
        fake_gh=fake_gh,
        gh_log=tmp_path / "gh.log",
        git_log=tmp_path / "git.log",
        home=home,
        bash=bash,
        git=git,
        uv=uv,
    )


# --------------------------------------------------------------------------------------
# Workflow files -- structural access shared by the on_ready/on_main/on_semver tests
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowFile:
    """Parsed view of one `.github/workflows/*.yml`, in the `test_release_workflow.py` style.

    Every assertion goes through the parsed structure (`yaml.safe_load`) or through
    `shlex.split` token vectors of `run:` bodies -- never substring matching on raw YAML.
    """

    path: Path
    document: dict[str, object]

    @property
    def jobs(self) -> dict[str, dict[str, object]]:
        """The `jobs:` mapping keyed by job id."""
        jobs = self.document.get("jobs")
        assert isinstance(jobs, dict), f"{self.path.name} has no `jobs:` mapping"
        return cast("dict[str, dict[str, object]]", jobs)

    def job(self, name: str) -> dict[str, object]:
        """One job by id, failing with the actual job list when it is absent."""
        jobs = self.jobs
        assert name in jobs, (
            f"job `{name}` is not defined in {self.path.name}; jobs: {sorted(jobs)}"
        )
        return jobs[name]

    @staticmethod
    def steps(job: dict[str, object]) -> list[dict[str, object]]:
        """A job's `steps:` list."""
        steps = job.get("steps")
        assert isinstance(steps, list), "job has no `steps:` list"
        return cast("list[dict[str, object]]", steps)

    @staticmethod
    def mapping(node: dict[str, object], key: str) -> dict[str, object]:
        """A sub-mapping (`with:`, `env:`, `permissions:`) or an empty mapping when absent."""
        value = node.get(key)
        if value is None:
            return {}
        assert isinstance(value, dict), f"`{key}:` is not a mapping"
        return cast("dict[str, object]", value)

    @staticmethod
    def needs(job: dict[str, object]) -> list[str]:
        """A job's `needs:` as a list, normalising the scalar shorthand."""
        needs = job.get("needs")
        if needs is None:
            return []
        if isinstance(needs, str):
            return [needs]
        assert isinstance(needs, list), "`needs:` is neither a string nor a list"
        return [str(item) for item in cast("list[object]", needs)]

    @staticmethod
    def tokens(run_body: str) -> list[str]:
        """Tokenise a `run:` body, dropping the newline tokens shlex emits for continuations."""
        return [token for token in shlex.split(run_body) if token != NEWLINE_TOKEN]

    @classmethod
    def run_bodies(cls, job: dict[str, object]) -> list[str]:
        """Every `run:` body of a job, in declaration order."""
        return [str(step["run"]) for step in cls.steps(job) if "run" in step]

    @classmethod
    def steps_using(cls, job: dict[str, object], action: str) -> list[dict[str, object]]:
        """Every step whose `uses:` names `action` (any pinned version)."""
        return [
            step for step in cls.steps(job) if str(step.get("uses", "")).startswith(f"{action}@")
        ]

    @classmethod
    def steps_running(cls, job: dict[str, object], token: str) -> list[dict[str, object]]:
        """Every step whose `run:` body contains `token` as a whole shell token."""
        return [step for step in cls.steps(job) if token in cls.tokens(str(step.get("run", "")))]


def load_workflow(file_name: str) -> WorkflowFile:
    """Parse `.github/workflows/<file_name>` (annotating `yaml.safe_load`'s `Any` here)."""
    path = WORKFLOWS_DIR / file_name
    document: dict[str, object] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return WorkflowFile(path=path, document=document)


@pytest.fixture
def repo_root() -> Path:
    """The repository root (the `cwd` a workflow `run:` body assumes)."""
    return REPO_ROOT


@pytest.fixture
def on_ready_workflow() -> WorkflowFile:
    """`.github/workflows/on_ready.yml`, parsed."""
    return load_workflow("on_ready.yml")


@pytest.fixture
def on_main_workflow() -> WorkflowFile:
    """`.github/workflows/on_main.yml`, parsed."""
    return load_workflow("on_main.yml")


@pytest.fixture
def pr_to_ready_workflow() -> WorkflowFile:
    """`.github/workflows/pr-to-ready.yml`, parsed."""
    return load_workflow("pr-to-ready.yml")
