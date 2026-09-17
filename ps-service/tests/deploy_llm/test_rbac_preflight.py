"""RBAC preflight for `scripts/deploy-llm.sh` (AC-BI-004; PLAN.md §5/S4).

An evaluator whose signed-in identity has neither `Owner` nor `Contributor` at subscription
scope is stopped before region selection ever runs, with an actionable message: which role is
missing, and an example `az role assignment create` fix command (PLAN.md §0.1 step 5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployLlmFixture


def _model_list_calls(fixture: DeployLlmFixture) -> list[str]:
    return [line for line in fixture.read_az_log() if line.startswith("cognitiveservices model")]


def _role_assignment_calls(fixture: DeployLlmFixture) -> list[str]:
    return [line for line in fixture.read_az_log() if line.startswith("role assignment list")]


def test_missing_owner_and_contributor_fails_before_region_selection(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    deploy_llm_fixture.seed_role_assignments()  # no roles at all

    deploy_llm_fixture.run_deploy("--yes", expect=1)

    assert _model_list_calls(deploy_llm_fixture) == []


def test_reports_which_role_is_missing_and_an_example_fix_command(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(upn="evaluator@example.test")
    deploy_llm_fixture.seed_role_assignments("Reader")

    run = deploy_llm_fixture.run_deploy("--yes", expect=1)

    assert "Owner" in run.stderr
    assert "Contributor" in run.stderr
    assert "evaluator@example.test" in run.stderr
    assert "az role assignment create" in run.stderr


def test_owner_role_present_passes_preflight(deploy_llm_fixture: DeployLlmFixture) -> None:
    deploy_llm_fixture.seed_subscription()
    deploy_llm_fixture.seed_role_assignments("Owner")

    deploy_llm_fixture.run_deploy("--yes", expect=0)

    assert len(_role_assignment_calls(deploy_llm_fixture)) == 1


def test_contributor_role_present_passes_preflight(deploy_llm_fixture: DeployLlmFixture) -> None:
    deploy_llm_fixture.seed_subscription()
    deploy_llm_fixture.seed_role_assignments("Contributor")

    deploy_llm_fixture.run_deploy("--yes", expect=0)

    assert len(_role_assignment_calls(deploy_llm_fixture)) == 1


def test_reader_role_alone_fails_preflight(deploy_llm_fixture: DeployLlmFixture) -> None:
    deploy_llm_fixture.seed_subscription()
    deploy_llm_fixture.seed_role_assignments("Reader")

    deploy_llm_fixture.run_deploy("--yes", expect=1)
