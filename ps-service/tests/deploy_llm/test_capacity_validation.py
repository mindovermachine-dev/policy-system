"""Capacity-range validation against the selected region's live-reported range (AC-BI-006;
PLAN.md §5/S6).

Runs only at the region `select_region` already found -- a capacity outside the range that
region's `model list` response reports for the required SKU is a hard stop showing the actual
range; it is not retried against a different candidate region (a bad capacity is an evaluator
config mistake, not a per-region property, PLAN.md §0.1 step 6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployLlmFixture


def _model_list_calls(fixture: DeployLlmFixture) -> list[str]:
    return [
        line for line in fixture.read_az_log() if line.startswith("cognitiveservices model list")
    ]


def test_chat_capacity_above_reported_maximum_fails_showing_actual_range(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    # Config's LLM_CHAT_MODEL_CAPACITY is 1000 -- a reported maximum of 500 is below it.
    deploy_llm_fixture.seed_model_availability(
        "swedencentral", chat_ga=True, embed_ga=True, chat_capacity_range=(1, 500)
    )

    run = deploy_llm_fixture.run_deploy("--yes", expect=1)

    assert "LLM_CHAT_MODEL_CAPACITY" in run.stderr
    assert "1-500" in run.stderr


def test_embed_capacity_below_reported_minimum_fails_showing_actual_range(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    # Config's LLM_EMBED_MODEL_CAPACITY is 350 -- a reported minimum of 400 is above it.
    deploy_llm_fixture.seed_model_availability(
        "swedencentral", chat_ga=True, embed_ga=True, embed_capacity_range=(400, 700)
    )

    run = deploy_llm_fixture.run_deploy("--yes", expect=1)

    assert "LLM_EMBED_MODEL_CAPACITY" in run.stderr
    assert "400-700" in run.stderr


def test_capacity_within_range_passes(deploy_llm_fixture: DeployLlmFixture) -> None:
    deploy_llm_fixture.seed_subscription()  # conftest baseline's ranges comfortably fit 1000/350

    deploy_llm_fixture.run_deploy("--yes", expect=0)


def test_capacity_failure_does_not_try_a_different_region(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    deploy_llm_fixture.seed_model_availability(
        "swedencentral", chat_ga=True, embed_ga=True, chat_capacity_range=(1, 500)
    )

    deploy_llm_fixture.run_deploy("--yes", expect=1)

    assert _model_list_calls(deploy_llm_fixture) == [
        "cognitiveservices model list --location swedencentral",
    ]
