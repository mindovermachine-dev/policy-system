"""Tests for `scripts/check-authentik-blueprint.sh`, the gate in front of the blueprint check.

The script runs for real inside throwaway git repositories. Only the `uv` executable is replaced,
by a recording shim on PATH, so no container runtime is needed: the shim's log shows whether the
check would have started and its exit status stands in for the check's verdict.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-authentik-blueprint.sh"
TRIGGERS = REPO_ROOT / "scripts" / "authentik-blueprint-triggers.txt"

TRIGGER_FILE = "charts/policy-system/files/authentik-blueprint.yaml"
UNRELATED_FILE = "README.md"
SUBPROCESS_TIMEOUT_SECONDS = 60.0
GIT = shutil.which("git") or "git"


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603  # fixed git argv in a throwaway repository
        [GIT, "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    return completed.stdout.strip()


def _commit(repo: Path, relative: str, content: str) -> str:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, "utf-8")
    _git(repo, "add", "--", relative)
    _git(repo, "commit", "--quiet", "-m", f"change {relative}")
    return _git(repo, "rev-parse", "HEAD")


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "--quiet", "--initial-branch=main")
    _commit(path, UNRELATED_FILE, "base\n")
    return path


def _fake_uv(directory: Path, exit_status: int) -> Path:
    """Install a `uv` shim that records its argv to `uv.log` and exits with `exit_status`."""
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "uv"
    log = directory / "uv.log"
    shim.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit {exit_status}\n', "utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return log


class _Outcome:
    def __init__(self, completed: subprocess.CompletedProcess[str], uv_log: Path) -> None:
        self.returncode = completed.returncode
        self.stdout = completed.stdout
        self.check_invocations = uv_log.read_text("utf-8").splitlines() if uv_log.exists() else []


def _gate(
    repo: Path, tmp_path: Path, *, check_status: int = 0, env: dict[str, str] | None = None
) -> _Outcome:
    shim_dir = tmp_path / "bin"
    uv_log = _fake_uv(shim_dir, check_status)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AUTHENTIK_BLUEPRINT_")
    }
    environment["PATH"] = f"{shim_dir}{os.pathsep}{environment['PATH']}"
    environment.update(env or {})
    completed = subprocess.run(  # noqa: S603  # fixed argv: the script under test
        [str(SCRIPT)],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    return _Outcome(completed, uv_log)


def _repo_with_origin_main(tmp_path: Path) -> Path:
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "switch", "--quiet", "-c", "feature")
    return repo


def test_skips_with_a_reason_and_runs_no_check_when_no_trigger_path_changed(tmp_path: Path) -> None:
    repo = _repo_with_origin_main(tmp_path)
    _commit(repo, "docs/note.md", "unrelated\n")

    outcome = _gate(repo, tmp_path)

    assert outcome.returncode == 0
    assert outcome.check_invocations == []
    assert "authentik-blueprint: skipped - no trigger path changed since " in outcome.stdout
    assert "AUTHENTIK_BLUEPRINT_FORCE=1 forces the run" in outcome.stdout


def test_runs_the_check_and_names_the_changed_file_when_a_trigger_path_changed(
    tmp_path: Path,
) -> None:
    repo = _repo_with_origin_main(tmp_path)
    _commit(repo, "docs/note.md", "unrelated\n")
    _commit(repo, TRIGGER_FILE, "changed\n")

    outcome = _gate(repo, tmp_path)

    assert outcome.returncode == 0
    assert len(outcome.check_invocations) == 1
    assert "pytest -m authentik_blueprint" in outcome.check_invocations[0]
    assert f"authentik-blueprint: running - changed: {TRIGGER_FILE}" in outcome.stdout


def test_a_trigger_change_earlier_in_a_multi_commit_branch_still_runs_the_check(
    tmp_path: Path,
) -> None:
    repo = _repo_with_origin_main(tmp_path)
    _commit(repo, "scripts/lib/authentik-owner.sh", "changed\n")
    _commit(repo, "docs/a.md", "a\n")
    _commit(repo, "docs/b.md", "b\n")

    outcome = _gate(repo, tmp_path)

    assert len(outcome.check_invocations) == 1


def test_the_check_runs_with_durations_and_uncaptured_output_so_phase_timing_reaches_the_log(
    tmp_path: Path,
) -> None:
    repo = _repo_with_origin_main(tmp_path)
    _commit(repo, TRIGGER_FILE, "changed\n")

    outcome = _gate(repo, tmp_path)

    arguments = outcome.check_invocations[0].split()
    assert "--durations=0" in arguments
    assert "-s" in arguments


def test_the_gate_prints_a_start_and_an_end_line_with_utc_timestamps_and_the_status(
    tmp_path: Path,
) -> None:
    repo = _repo_with_origin_main(tmp_path)
    _commit(repo, TRIGGER_FILE, "changed\n")

    outcome = _gate(repo, tmp_path, check_status=3)

    timestamp = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
    lines = outcome.stdout.splitlines()
    started = [
        i
        for i, line in enumerate(lines)
        if re.fullmatch(rf"authentik-blueprint: started {timestamp}", line)
    ]
    ended = [
        i
        for i, line in enumerate(lines)
        if re.fullmatch(rf"authentik-blueprint: finished {timestamp} status 3", line)
    ]
    assert len(started) == 1
    assert len(ended) == 1
    assert started[0] < ended[0]
    assert outcome.returncode == 3


def test_a_failing_check_fails_the_gate(tmp_path: Path) -> None:
    repo = _repo_with_origin_main(tmp_path)
    _commit(repo, TRIGGER_FILE, "changed\n")

    outcome = _gate(repo, tmp_path, check_status=3)

    assert outcome.returncode == 3


def test_runs_when_origin_main_is_missing_and_cannot_be_fetched(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "docs/note.md", "unrelated\n")

    outcome = _gate(repo, tmp_path)

    assert len(outcome.check_invocations) == 1
    assert "authentik-blueprint: running - base unknown" in outcome.stdout


def test_fetches_origin_main_when_missing_and_skips_when_nothing_changed(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    seed = _init_repo(tmp_path / "seed")
    _git(tmp_path, "clone", "--quiet", "--bare", str(seed), str(remote))
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "fetch", "--quiet", "--no-tags", "origin", "main")
    _git(repo, "reset", "--quiet", "--hard", "origin/main")
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    _git(repo, "switch", "--quiet", "-c", "feature")
    _commit(repo, "docs/note.md", "unrelated\n")

    outcome = _gate(repo, tmp_path)

    assert outcome.returncode == 0
    assert outcome.check_invocations == []
    assert "authentik-blueprint: skipped" in outcome.stdout


def test_runs_when_the_comparison_base_cannot_be_found_in_a_shallow_clone(tmp_path: Path) -> None:
    origin = _init_repo(tmp_path / "origin")
    _commit(origin, "docs/one.md", "1\n")
    _commit(origin, "docs/two.md", "2\n")
    repo = tmp_path / "shallow"
    _git(tmp_path, "clone", "--quiet", "--depth=1", f"file://{origin}", str(repo))
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    outcome = _gate(repo, tmp_path)

    assert len(outcome.check_invocations) == 1
    assert "authentik-blueprint: running - base unknown" in outcome.stdout


def test_runs_when_the_force_flag_is_set_even_though_nothing_changed(tmp_path: Path) -> None:
    repo = _repo_with_origin_main(tmp_path)
    _commit(repo, "docs/note.md", "unrelated\n")

    outcome = _gate(repo, tmp_path, env={"AUTHENTIK_BLUEPRINT_FORCE": "1"})

    assert len(outcome.check_invocations) == 1
    assert "authentik-blueprint: running - forced" in outcome.stdout


def test_the_check_reports_the_duration_of_every_test(tmp_path: Path) -> None:
    repo = _repo_with_origin_main(tmp_path)

    outcome = _gate(repo, tmp_path, env={"AUTHENTIK_BLUEPRINT_FORCE": "1"})

    assert outcome.check_invocations == [
        "run pytest -m authentik_blueprint -q -n0 -s --durations=0"
    ]


def test_runs_when_head_equals_origin_main(tmp_path: Path) -> None:
    repo = _repo_with_origin_main(tmp_path)

    outcome = _gate(repo, tmp_path)

    assert len(outcome.check_invocations) == 1
    assert (
        "authentik-blueprint: running - HEAD is origin/main, no comparison base" in outcome.stdout
    )


def test_honours_an_overridden_comparison_ref(tmp_path: Path) -> None:
    repo = _repo_with_origin_main(tmp_path)
    _git(repo, "branch", "other")
    _commit(repo, TRIGGER_FILE, "changed\n")
    _commit(repo, "docs/note.md", "unrelated\n")

    outcome = _gate(repo, tmp_path, env={"AUTHENTIK_BLUEPRINT_BASE_REF": "other"})

    assert f"changed: {TRIGGER_FILE}" in outcome.stdout


def test_trigger_list_names_the_gate_itself() -> None:
    entries = [
        line.split("#", 1)[0].strip()
        for line in TRIGGERS.read_text("utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    ]

    assert "scripts/check-authentik-blueprint.sh" in entries
    assert "scripts/authentik-blueprint-triggers.txt" in entries
    assert TRIGGER_FILE in entries


def _workflow_checkout_step(job: str) -> dict[str, Any]:
    workflow = cast(
        "dict[str, Any]",
        yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "on_ready.yml").read_text("utf-8")),
    )
    steps = cast("list[dict[str, Any]]", workflow["jobs"][job]["steps"])
    return next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout"))


def test_ready_build_job_checks_out_full_history() -> None:
    checkout = _workflow_checkout_step("build")

    assert checkout["with"]["fetch-depth"] == 0


def test_insitu_blueprint_check_calls_the_gate_but_is_not_in_trunk_worthy() -> None:
    config = cast("dict[str, Any]", yaml.safe_load((REPO_ROOT / ".insitu.yml").read_text("utf-8")))
    checks = cast("list[dict[str, Any]]", config["inventory"])
    check = next(c for c in checks if c["id"] == "authentik-blueprint")
    trunk_worthy = next(w for w in config["waves"] if w.get("id") == "trunk-worthy")

    assert check["command"] == "./scripts/check-authentik-blueprint.sh"
    assert "authentik-blueprint" not in trunk_worthy["checks"]


def _workflow(name: str) -> dict[str, Any]:
    text = (REPO_ROOT / ".github" / "workflows" / name).read_text("utf-8")
    return cast("dict[str, Any]", yaml.safe_load(text))


def test_reusable_workflow_runs_the_gate_unforced_with_full_history() -> None:
    workflow = _workflow("authentik-blueprint.yml")
    job = workflow["jobs"]["blueprint"]
    steps = cast("list[dict[str, Any]]", job["steps"])
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    gate = [s for s in steps if s.get("run") == "./scripts/check-authentik-blueprint.sh"]

    triggers = cast("dict[object, object]", workflow).get("on", workflow.get(True))  # pyright: ignore[reportArgumentType]
    assert set(cast("dict[str, object]", triggers)) == {"workflow_call"}
    assert checkout["with"]["fetch-depth"] == 0
    assert len(gate) == 1
    assert "FORCE" not in str(gate[0]) + str(job.get("env"))


def test_wrapup_and_ready_call_the_blueprint_workflow_and_ready_merge_waits_for_it() -> None:
    for name in ("on_dev.yml", "on_ready.yml"):
        job = _workflow(name)["jobs"]["authentik-blueprint"]
        assert job["uses"] == "./.github/workflows/authentik-blueprint.yml", name

    merge = _workflow("on_ready.yml")["jobs"]["merge-to-trunk"]
    assert "authentik-blueprint" in merge["needs"]
