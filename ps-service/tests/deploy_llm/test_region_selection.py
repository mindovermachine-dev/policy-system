"""Region targeting: LLM_REGION used as configured, never auto-switched (AC-BI-005/AC-BI-008
superseded by issue #110 -- see `verify_target_region`/`fail_region_not_viable` in
scripts/deploy-llm.sh).

The script checks only the configured `LLM_REGION` for both models being `GenerallyAvailable` at
the required SKU; it is never replaced by a different region. When that check fails, every other
`LLM_REGION_CANDIDATES` entry is probed for full viability (GA + capacity range + quota) purely to
report which ones would actually work -- the script still hard-stops.
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


def test_uses_configured_region_directly(deploy_llm_fixture: DeployLlmFixture) -> None:
    deploy_llm_fixture.seed_subscription()  # GA everywhere by default (conftest baseline)

    deploy_llm_fixture.run_deploy("--yes", expect=0)

    # Only LLM_REGION (swedencentral) is ever checked when it already satisfies both models --
    # no candidate probing happens on the success path.
    assert _model_list_calls(deploy_llm_fixture) == [
        "cognitiveservices model list --location swedencentral",
    ]


def test_region_not_generally_available_fails_and_reports_working_alternatives(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    # Embed model not GA at LLM_REGION -> hard stop, LLM_REGION is not swapped for a candidate.
    deploy_llm_fixture.seed_model_availability("swedencentral", chat_ga=True, embed_ga=False)

    run = deploy_llm_fixture.run_deploy("--yes", expect=1)

    assert (
        "Region swedencentral does not have both gpt-5.4-mini (GlobalStandard) and "
        "text-embedding-3-large (DataZoneStandard) Generally Available." in run.stderr
    )
    assert (
        "Regions that would work instead: francecentral, westeurope, germanywestcentral"
        in run.stderr
    )
    # Every other candidate (still fully viable per seed_subscription's baseline) is probed for
    # the report, in LLM_REGION_CANDIDATES order.
    assert _model_list_calls(deploy_llm_fixture) == [
        "cognitiveservices model list --location swedencentral",
        "cognitiveservices model list --location francecentral",
        "cognitiveservices model list --location westeurope",
        "cognitiveservices model list --location germanywestcentral",
    ]


def test_reports_only_the_alternatives_that_are_actually_viable(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    deploy_llm_fixture.seed_model_availability("swedencentral", chat_ga=False, embed_ga=False)
    # westeurope is also broken -- it must be excluded from the "would work instead" list even
    # though it's still probed.
    deploy_llm_fixture.seed_model_availability("westeurope", chat_ga=True, embed_ga=False)
    # francecentral/germanywestcentral stay GA (conftest baseline) and must be reported.

    run = deploy_llm_fixture.run_deploy("--yes", expect=1)

    assert "Regions that would work instead: francecentral, germanywestcentral" in run.stderr


def test_no_candidate_available_fails_explicitly_and_probes_every_region(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    for region in ALL_CANDIDATES:
        deploy_llm_fixture.seed_model_availability(region, chat_ga=False, embed_ga=False)

    run = deploy_llm_fixture.run_deploy("--yes", expect=1)

    assert _model_list_calls(deploy_llm_fixture) == [
        f"cognitiveservices model list --location {region}" for region in ALL_CANDIDATES
    ]
    assert "No other candidate region in LLM_REGION_CANDIDATES currently works either." in (
        run.stderr
    )
