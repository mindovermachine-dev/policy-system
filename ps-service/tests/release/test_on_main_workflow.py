"""Static and executed assertions on `.github/workflows/on_main.yml`'s header lint (AC-BI-025).

The lint job must fail *visibly* on a direct push with a non-conventional header and must be
independent of the release path (no job may `needs:` it, so a lint failure never blocks a
release -- AC-BI-025 says the `release` job still runs). Range mode receives
`github.event.before..github.sha` through `env:` (CHANGES X-09) and needs the full history
(`fetch-depth: 0`) to walk the range. One test executes the body against the fixture repo.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import ReleaseFixture, WorkflowFile

LINT_JOB = "lint-commit-header"
LINT_SCRIPT_PATH = "scripts/release/lint-commit-header.sh"
CHECKOUT_ACTION = "actions/checkout"
BEFORE_EXPRESSION = "${{ github.event.before }}"
AFTER_EXPRESSION = "${{ github.sha }}"
GITHUB_EXPRESSION = re.compile(r"\$\{\{")

RELEASE_JOB = "release"
MERGE_TO_TRUNK_JOB = "merge-to-trunk"
RELEASE_SCRIPT_PATH = "scripts/release/release.sh"
GH_TT_EXTENSION = "devx-cafe/gh-tt"
GH_TT_PIN = "dc445201"
MAIN_REF_EXPRESSION = "github.ref == 'refs/heads/main'"
CHORE_RELEASE_PREFIX = "chore(release):"
READY_PUSHER_EXPRESSION = "${{ secrets.READY_PUSHER }}"
SECRET_CHECK_STEP_NAME = "Verify READY_PUSHER secret"


def _lint_step(workflow: WorkflowFile) -> dict[str, object]:
    steps = workflow.steps_running(workflow.job(LINT_JOB), LINT_SCRIPT_PATH)
    assert len(steps) == 1, f"expected exactly one step running {LINT_SCRIPT_PATH}, got {steps}"
    return steps[0]


def _secret_check_step(job: dict[str, object]) -> dict[str, object]:
    steps = cast("list[dict[str, object]]", job["steps"])
    matches = [step for step in steps if step.get("name") == SECRET_CHECK_STEP_NAME]
    assert len(matches) == 1, f"expected exactly one '{SECRET_CHECK_STEP_NAME}' step, got {matches}"
    return matches[0]


def test_lint_job_exists_and_declares_contents_read(on_main_workflow: WorkflowFile) -> None:
    lint_job = on_main_workflow.job(LINT_JOB)

    assert on_main_workflow.mapping(lint_job, "permissions") == {"contents": "read"}
    assert on_main_workflow.needs(lint_job) == [], "the lint job waits on nothing"
    dependants = [
        name
        for name, job in on_main_workflow.jobs.items()
        if LINT_JOB in on_main_workflow.needs(job)
    ]
    assert dependants == [], f"jobs gated on the lint (release must not be): {dependants}"


def test_lint_job_checks_out_full_history_for_range_mode(on_main_workflow: WorkflowFile) -> None:
    checkouts = on_main_workflow.steps_using(on_main_workflow.job(LINT_JOB), CHECKOUT_ACTION)

    assert len(checkouts) == 1, f"expected one checkout step, got {checkouts}"
    assert on_main_workflow.mapping(checkouts[0], "with").get("fetch-depth") == 0


def test_lint_step_invokes_lint_commit_header_in_range_mode(
    on_main_workflow: WorkflowFile,
) -> None:
    step = _lint_step(on_main_workflow)
    tokens = on_main_workflow.tokens(str(step["run"]))

    assert tokens[0] == LINT_SCRIPT_PATH
    assert "--range" in tokens
    assert "--header" not in tokens
    assert on_main_workflow.mapping(step, "env") == {
        "BEFORE": BEFORE_EXPRESSION,
        "AFTER": AFTER_EXPRESSION,
    }


def test_no_run_body_in_on_main_interpolates_a_github_expression(
    on_main_workflow: WorkflowFile,
) -> None:
    interpolating = [
        (name, body)
        for name, job in on_main_workflow.jobs.items()
        for body in on_main_workflow.run_bodies(job)
        if GITHUB_EXPRESSION.search(body)
    ]

    assert not interpolating, f"`run:` bodies interpolating a GitHub expression: {interpolating}"


def test_lint_body_executes_the_pushed_range_and_names_the_offender(
    on_main_workflow: WorkflowFile, release_fixture: ReleaseFixture, repo_root: Path
) -> None:
    body = str(_lint_step(on_main_workflow)["run"])
    before = release_fixture.work.head()
    offender = release_fixture.commit("Direct push without a type")
    after = release_fixture.commit("fix(release): typed follow-up")

    failing = release_fixture.run_bash(
        body, cwd=repo_root, env_extra={"BEFORE": before, "AFTER": after}, expect=1
    )

    assert f"{offender} Direct push without a type" in failing.stdout
    assert "type(scope)!: description" in failing.stdout


def test_lint_body_falls_back_to_head_on_workflow_dispatch(
    on_main_workflow: WorkflowFile, release_fixture: ReleaseFixture, repo_root: Path
) -> None:
    body = str(_lint_step(on_main_workflow)["run"])
    release_fixture.commit("older untyped commit")
    head = release_fixture.commit("docs(release): typed HEAD")

    release_fixture.run_bash(body, cwd=repo_root, env_extra={"BEFORE": "", "AFTER": head})


# --------------------------------------------------------------------------------------
# `release` job (S5/S6, AC-BI-001..004, 017 layer 1)
# --------------------------------------------------------------------------------------


def test_release_job_runs_only_on_main_and_skips_release_commits(
    on_main_workflow: WorkflowFile,
) -> None:
    condition = str(on_main_workflow.job(RELEASE_JOB).get("if", ""))

    assert MAIN_REF_EXPRESSION in condition
    assert "startsWith" in condition
    assert CHORE_RELEASE_PREFIX in condition


def test_release_job_permissions_are_contents_read_only(on_main_workflow: WorkflowFile) -> None:
    release_job = on_main_workflow.job(RELEASE_JOB)

    assert on_main_workflow.mapping(release_job, "permissions") == {"contents": "read"}


def test_release_job_secret_check_is_identical_to_on_ready(
    on_main_workflow: WorkflowFile, on_ready_workflow: WorkflowFile
) -> None:
    release_step = _secret_check_step(on_main_workflow.job(RELEASE_JOB))
    trunk_step = _secret_check_step(on_ready_workflow.job(MERGE_TO_TRUNK_JOB))

    assert release_step["run"] == trunk_step["run"]
    assert release_step.get("shell") == trunk_step.get("shell")
    assert on_main_workflow.mapping(release_step, "env") == on_ready_workflow.mapping(
        trunk_step, "env"
    )


def test_release_job_checks_out_main_tip_with_full_tags_and_ready_pusher(
    on_main_workflow: WorkflowFile,
) -> None:
    checkouts = on_main_workflow.steps_using(on_main_workflow.job(RELEASE_JOB), CHECKOUT_ACTION)

    assert len(checkouts) == 1, f"expected one checkout step, got {checkouts}"
    with_block = on_main_workflow.mapping(checkouts[0], "with")
    assert with_block.get("ref") == "main"
    assert with_block.get("fetch-depth") == 0
    assert with_block.get("token") == READY_PUSHER_EXPRESSION


def test_release_job_installs_gh_tt(on_main_workflow: WorkflowFile) -> None:
    steps = on_main_workflow.steps_running(on_main_workflow.job(RELEASE_JOB), GH_TT_EXTENSION)

    assert len(steps) == 1, f"expected one gh-tt install step, got {steps}"
    tokens = on_main_workflow.tokens(str(steps[0]["run"]))
    assert GH_TT_EXTENSION in tokens
    assert "--pin" in tokens
    assert GH_TT_PIN in tokens


def test_stage_concurrency_group_is_unchanged(on_main_workflow: WorkflowFile) -> None:
    assert on_main_workflow.document.get("concurrency") == {
        "group": "stage",
        "cancel-in-progress": False,
    }


def test_no_run_body_interpolates_a_github_expression(on_main_workflow: WorkflowFile) -> None:
    release_job = on_main_workflow.job(RELEASE_JOB)
    interpolating = [
        body for body in on_main_workflow.run_bodies(release_job) if GITHUB_EXPRESSION.search(body)
    ]

    assert not interpolating, (
        f"release job `run:` bodies interpolating a GitHub expression: {interpolating}"
    )


def test_release_step_invokes_release_sh(on_main_workflow: WorkflowFile) -> None:
    steps = on_main_workflow.steps_running(on_main_workflow.job(RELEASE_JOB), RELEASE_SCRIPT_PATH)

    assert len(steps) == 1, f"expected exactly one step running {RELEASE_SCRIPT_PATH}, got {steps}"
    tokens = on_main_workflow.tokens(str(steps[0]["run"]))
    assert tokens == [RELEASE_SCRIPT_PATH]
