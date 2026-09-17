"""Region selection: GA probing in configured order + no-candidate failure (AC-BI-005,
AC-BI-008; PLAN.md §5/S5).

The loop tries candidates from `LLM_REGION_CANDIDATES` in order, stops at the first one where
both models are `GenerallyAvailable` at the required SKU, and never probes candidates after the
one it selects. If no candidate qualifies, every candidate *was* tried before the explicit
failure (distinguishing this from S7's quota check, which never tries a second region).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployLlmFixture

ALL_CANDIDATES = ("swedencentral", "francecentral", "westeurope", "germanywestcentral")


def _model_list_calls(fixture: DeployLlmFixture) -> list[str]:
    return [
        line for line in fixture.read_az_log() if line.startswith("cognitiveservices model list")
    ]


def test_selects_first_candidate_with_both_models_generally_available(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()  # GA everywhere by default (conftest baseline)

    deploy_llm_fixture.run_deploy("--yes", expect=0)

    assert _model_list_calls(deploy_llm_fixture) == [
        "cognitiveservices model list --location swedencentral",
    ]


def test_skips_a_candidate_missing_one_model_and_tries_the_next(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    # Embed model not GA in the first candidate -> must be skipped even though chat is GA there.
    deploy_llm_fixture.seed_model_availability("swedencentral", chat_ga=True, embed_ga=False)

    deploy_llm_fixture.run_deploy("--yes", expect=0)

    assert _model_list_calls(deploy_llm_fixture) == [
        "cognitiveservices model list --location swedencentral",
        "cognitiveservices model list --location francecentral",
    ]


def test_stops_probing_once_a_candidate_satisfies_both_models(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    deploy_llm_fixture.seed_model_availability("swedencentral", chat_ga=False, embed_ga=False)
    # francecentral stays GA (conftest baseline) and must satisfy the loop -- westeurope and
    # germanywestcentral must never be probed if the loop truly stops here.

    deploy_llm_fixture.run_deploy("--yes", expect=0)

    assert _model_list_calls(deploy_llm_fixture) == [
        "cognitiveservices model list --location swedencentral",
        "cognitiveservices model list --location francecentral",
    ]


def test_no_candidate_available_fails_explicitly_and_tries_every_region(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    for region in ALL_CANDIDATES:
        deploy_llm_fixture.seed_model_availability(region, chat_ga=False, embed_ga=False)

    run = deploy_llm_fixture.run_deploy("--yes", expect=1)

    assert _model_list_calls(deploy_llm_fixture) == [
        f"cognitiveservices model list --location {region}" for region in ALL_CANDIDATES
    ]
    assert "swedencentral, francecentral, westeurope, germanywestcentral" in run.stderr
