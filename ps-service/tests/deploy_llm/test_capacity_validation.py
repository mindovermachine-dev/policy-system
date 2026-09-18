"""Capacity-range validation against LLM_REGION's live-reported range (AC-BI-006; superseded by
issue #110 -- see `fail_region_not_viable` in scripts/deploy-llm.sh).

Runs only at the configured `LLM_REGION` -- a capacity outside the range that region's `model
list` response reports for the required SKU is a hard stop showing the actual range. LLM_REGION
itself is never auto-switched to a different region on this failure, but every other
LLM_REGION_CANDIDATES entry is probed for full viability so the error can report which ones would
actually work.
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
    # Config's LLM_CHAT_MODEL_CAPACITY is 300 -- a reported maximum of 200 is below it.
    deploy_llm_fixture.seed_model_availability(
        "swedencentral", chat_ga=True, embed_ga=True, chat_capacity_range=(1, 200)
    )

    run = deploy_llm_fixture.run_deploy("--yes", expect=1)

    assert "LLM_CHAT_MODEL_CAPACITY" in run.stderr
    assert "1-200" in run.stderr


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


def test_capacity_failure_reports_other_viable_regions(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    deploy_llm_fixture.seed_model_availability(
        "swedencentral", chat_ga=True, embed_ga=True, chat_capacity_range=(1, 200)
    )

    run = deploy_llm_fixture.run_deploy("--yes", expect=1)

    # LLM_REGION itself is never auto-switched, but every other candidate (still fully viable
    # per seed_subscription's baseline) is probed to report it as a working alternative.
    assert _model_list_calls(deploy_llm_fixture) == [
        "cognitiveservices model list --location swedencentral",
        "cognitiveservices model list --location francecentral",
        "cognitiveservices model list --location westeurope",
        "cognitiveservices model list --location germanywestcentral",
    ]
    assert (
        "Regions that would work instead: francecentral, westeurope, germanywestcentral"
        in run.stderr
    )
