"""Static assertions on `.github/workflows/authentik-blueprint-weekly.yml`.

The weekly run applies the chart's Authentik blueprint with the gate forced on, so a change the
trigger list cannot see (a pin, a marker, a dependency) still surfaces within a week as a red
scheduled run. Same `WorkflowFile` discipline as `test_backup_restore_e2e_workflow.py`: parsed
structure and `shlex` token vectors, never substring matching on raw YAML.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from conftest import WorkflowFile

JOB = "blueprint"
CRON = "41 4 * * 1"
EXACT_ACTION_REF = re.compile(r"@v\d+\.\d+\.\d+$")
TIMEOUT_MINUTES = 15
GATE = "./scripts/check-authentik-blueprint.sh"
FORCE_FLAG = "AUTHENTIK_BLUEPRINT_FORCE=1"
PREP_RUNNER = "./.github/actions/prep-runner"


def _triggers(workflow: WorkflowFile) -> dict[str, object]:
    """The `on:` mapping; PyYAML parses the bare key `on` as the boolean `True`."""
    document = cast("dict[object, object]", workflow.document)
    triggers = document.get("on", document.get(True))
    assert isinstance(triggers, dict), "workflow has no `on:` mapping"
    return cast("dict[str, object]", triggers)


def test_runs_weekly_and_on_demand_with_read_only_permissions_and_queued_runs(
    authentik_blueprint_weekly_workflow: WorkflowFile,
) -> None:
    workflow = authentik_blueprint_weekly_workflow
    triggers = _triggers(workflow)

    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": CRON}]
    assert workflow.mapping(workflow.document, "permissions") == {"contents": "read"}
    concurrency = workflow.mapping(workflow.document, "concurrency")
    assert concurrency.get("group") == "authentik-blueprint-weekly"
    assert concurrency.get("cancel-in-progress") is False


def test_job_has_a_timeout_and_a_token_for_the_extension_install(
    authentik_blueprint_weekly_workflow: WorkflowFile,
) -> None:
    workflow = authentik_blueprint_weekly_workflow
    job = workflow.job(JOB)

    assert job["timeout-minutes"] == TIMEOUT_MINUTES
    assert workflow.mapping(job, "env") == {"GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}


def test_checkout_has_full_history_and_exact_pins(
    authentik_blueprint_weekly_workflow: WorkflowFile,
) -> None:
    workflow = authentik_blueprint_weekly_workflow
    job = workflow.job(JOB)

    checkouts = workflow.steps_using(job, "actions/checkout")
    assert len(checkouts) == 1
    assert EXACT_ACTION_REF.search(str(checkouts[0]["uses"]))
    assert workflow.mapping(checkouts[0], "with") == {
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    third_party = [
        str(step["uses"])
        for step in workflow.steps(job)
        if "uses" in step and not str(step["uses"]).startswith("./")
    ]
    assert all(EXACT_ACTION_REF.search(ref) for ref in third_party), third_party


def test_the_gate_is_run_forced_after_the_runner_is_prepared(
    authentik_blueprint_weekly_workflow: WorkflowFile,
) -> None:
    workflow = authentik_blueprint_weekly_workflow
    steps = workflow.steps(workflow.job(JOB))

    prep = [i for i, step in enumerate(steps) if step.get("uses") == PREP_RUNNER]
    gate = [i for i, step in enumerate(steps) if "run" in step]
    assert len(prep) == 1
    assert len(gate) == 1
    assert prep[0] < gate[0]
    assert "with" not in steps[prep[0]], "the prep step must not run a verify script"
    assert workflow.tokens(str(steps[gate[0]]["run"])) == [FORCE_FLAG, GATE]
