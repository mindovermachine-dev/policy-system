"""Quota hard stop at LLM_REGION (AC-BI-007; superseded by issue #110 -- see
`fail_region_not_viable` in scripts/deploy-llm.sh).

Runs once, at the configured `LLM_REGION`: `az cognitiveservices usage list --location <region>`.
Insufficient quota for either model is a hard stop with a quota-increase message. LLM_REGION is
never auto-switched on this failure, but every other LLM_REGION_CANDIDATES entry is probed for
full viability so the error can report which ones would actually work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployLlmFixture


def _usage_list_calls(fixture: DeployLlmFixture) -> list[str]:
    return [
        line for line in fixture.read_az_log() if line.startswith("cognitiveservices usage list")
    ]


def test_insufficient_chat_quota_fails_with_quota_increase_message(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    # Config's LLM_CHAT_MODEL_CAPACITY is 1000 -- only 50 remaining (1000 limit, 950 in use).
    deploy_llm_fixture.seed_usage("swedencentral", chat=(950, 1000), embed=(0, 10_000))

    run = deploy_llm_fixture.run_deploy("--yes", expect=1)

    assert "LLM_CHAT_MODEL_CAPACITY" in run.stderr
    assert "quota" in run.stderr.lower()


def test_insufficient_embed_quota_fails_with_quota_increase_message(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    # Config's LLM_EMBED_MODEL_CAPACITY is 350 -- only 20 remaining (700 limit, 680 in use).
    deploy_llm_fixture.seed_usage("swedencentral", chat=(0, 10_000), embed=(680, 700))

    run = deploy_llm_fixture.run_deploy("--yes", expect=1)

    assert "LLM_EMBED_MODEL_CAPACITY" in run.stderr
    assert "quota" in run.stderr.lower()


def test_quota_failure_reports_other_viable_regions(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    deploy_llm_fixture.seed_usage("swedencentral", chat=(950, 1000), embed=(0, 10_000))

    run = deploy_llm_fixture.run_deploy("--yes", expect=1)

    # LLM_REGION itself is never auto-switched, but every other candidate (still fully viable
    # per seed_subscription's baseline) is probed to report it as a working alternative.
    assert _usage_list_calls(deploy_llm_fixture) == [
        "cognitiveservices usage list --location swedencentral",
        "cognitiveservices usage list --location francecentral",
        "cognitiveservices usage list --location westeurope",
        "cognitiveservices usage list --location germanywestcentral",
    ]
    assert (
        "Regions that would work instead: francecentral, westeurope, germanywestcentral"
        in run.stderr
    )


def test_sufficient_quota_at_both_models_passes(deploy_llm_fixture: DeployLlmFixture) -> None:
    deploy_llm_fixture.seed_subscription()  # conftest baseline's ample quota fits 1000/350

    deploy_llm_fixture.run_deploy("--yes", expect=0)
