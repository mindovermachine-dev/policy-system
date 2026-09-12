"""Static assertions on `.github/workflows/pr-to-ready.yml`'s PR-title lint.

The takt `pr-to-ready` action squashes the PR into `<PR title> - resolves #N` on a `ready/*`
branch, which on_ready.yml's `lint-commit-header` job then rejects when the title is not
conventional. The lint step here fails the PR before that ready branch exists. Same
`WorkflowFile` discipline as `test_on_ready_workflow.py`: parsed structure and `shlex`
token vectors, never substring matching on raw YAML.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import WorkflowFile

DELIVER_JOB = "deliver-pr"
LINT_SCRIPT_PATH = "scripts/release/lint-commit-header.sh"
TAKT_ACTION = "devx-cafe/takt-actions/pr-to-ready"
GITHUB_EXPRESSION = re.compile(r"\$\{\{")


def _lint_step(workflow: WorkflowFile) -> dict[str, object]:
    steps = workflow.steps_running(workflow.job(DELIVER_JOB), LINT_SCRIPT_PATH)
    assert len(steps) == 1, f"expected exactly one step running {LINT_SCRIPT_PATH}, got {steps}"
    return steps[0]


def test_title_lint_runs_before_the_takt_delivery_step(
    pr_to_ready_workflow: WorkflowFile,
) -> None:
    steps = pr_to_ready_workflow.steps(pr_to_ready_workflow.job(DELIVER_JOB))
    lint_index = steps.index(_lint_step(pr_to_ready_workflow))
    takt_steps = pr_to_ready_workflow.steps_using(
        pr_to_ready_workflow.job(DELIVER_JOB), TAKT_ACTION
    )
    assert len(takt_steps) == 1
    assert lint_index < steps.index(takt_steps[0]), "lint the title before anything is pushed"


def test_title_lint_is_gated_like_the_takt_action(pr_to_ready_workflow: WorkflowFile) -> None:
    """Only approved reviews and dispatches deliver; the lint must not fail other review events."""
    condition = str(_lint_step(pr_to_ready_workflow)["if"])
    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "github.event.review.state == 'approved'" in condition


def test_title_lint_invokes_the_script_in_header_mode_with_the_deliver_suffix(
    pr_to_ready_workflow: WorkflowFile,
) -> None:
    step = _lint_step(pr_to_ready_workflow)
    tokens = pr_to_ready_workflow.tokens(str(step["run"]))
    lint_index = tokens.index(LINT_SCRIPT_PATH)
    assert tokens[lint_index + 1] == "--header"
    assert tokens[lint_index + 2] == "$title - resolves #$PR_NUMBER"
    env = pr_to_ready_workflow.mapping(step, "env")
    assert set(env) == {"GH_TOKEN", "PR_NUMBER"}, "PR number and token reach bash via env only"


def test_no_run_body_in_pr_to_ready_interpolates_a_github_expression(
    pr_to_ready_workflow: WorkflowFile,
) -> None:
    offenders = [
        body
        for job in pr_to_ready_workflow.jobs.values()
        for body in pr_to_ready_workflow.run_bodies(job)
        if GITHUB_EXPRESSION.search(body)
    ]
    assert offenders == [], f"`${{{{ }}}}` inside run: bodies is shell injection: {offenders}"
