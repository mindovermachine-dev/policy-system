"""Static and executed assertions on `.github/workflows/on_ready.yml`'s header lint (AC-BI-024).

Structural assertions read the parsed job graph (`needs:`, `permissions:`); wiring assertions
use `shlex` token vectors of the `run:` body (CHANGES X-15); one test executes the body under
bash against the fixture repository so the workflow step and the script are proven together.
GitHub Actions cannot run locally, so the job's *scheduling* is covered statically only.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import ReleaseFixture, WorkflowFile

LINT_JOB = "lint-commit-header"
BUILD_JOB = "build"
MERGE_JOB = "merge-to-trunk"
LINT_SCRIPT_PATH = "scripts/release/lint-commit-header.sh"
CHECKOUT_ACTION = "actions/checkout"
GITHUB_EXPRESSION = re.compile(r"\$\{\{")


def _lint_step(workflow: WorkflowFile) -> dict[str, object]:
    steps = workflow.steps_running(workflow.job(LINT_JOB), LINT_SCRIPT_PATH)
    assert len(steps) == 1, f"expected exactly one step running {LINT_SCRIPT_PATH}, got {steps}"
    return steps[0]


def test_merge_to_trunk_needs_the_header_lint_job(on_ready_workflow: WorkflowFile) -> None:
    needs = on_ready_workflow.needs(on_ready_workflow.job(MERGE_JOB))

    assert set(needs) == {BUILD_JOB, LINT_JOB}, f"`{MERGE_JOB}` needs {needs}"


def test_header_lint_job_declares_contents_read_and_checks_out(
    on_ready_workflow: WorkflowFile,
) -> None:
    lint_job = on_ready_workflow.job(LINT_JOB)

    assert on_ready_workflow.mapping(lint_job, "permissions") == {"contents": "read"}
    assert on_ready_workflow.needs(lint_job) == [], "the lint job must not wait on `build`"
    assert on_ready_workflow.steps_using(lint_job, CHECKOUT_ACTION), "the lint job checks out"


def test_header_lint_step_invokes_lint_commit_header_in_header_mode(
    on_ready_workflow: WorkflowFile,
) -> None:
    tokens = on_ready_workflow.tokens(str(_lint_step(on_ready_workflow)["run"]))

    assert tokens[0] == LINT_SCRIPT_PATH
    assert "--header" in tokens
    assert "--range" not in tokens


def test_no_run_body_in_on_ready_interpolates_a_github_expression(
    on_ready_workflow: WorkflowFile,
) -> None:
    interpolating = [
        (name, body)
        for name, job in on_ready_workflow.jobs.items()
        for body in on_ready_workflow.run_bodies(job)
        if GITHUB_EXPRESSION.search(body)
    ]

    assert not interpolating, f"`run:` bodies interpolating a GitHub expression: {interpolating}"


def test_header_lint_body_executes_against_the_ready_head(
    on_ready_workflow: WorkflowFile, release_fixture: ReleaseFixture, repo_root: Path
) -> None:
    body = str(_lint_step(on_ready_workflow)["run"])

    release_fixture.commit("feat(release): typed ready HEAD - resolves #79")
    release_fixture.run_bash(body, cwd=repo_root, expect=0)

    release_fixture.commit("Untyped ready HEAD - resolves #79")
    failing = release_fixture.run_bash(body, cwd=repo_root, expect=1)

    assert "Untyped ready HEAD - resolves #79" in failing.stdout
    assert "type(scope)!: description" in failing.stdout
