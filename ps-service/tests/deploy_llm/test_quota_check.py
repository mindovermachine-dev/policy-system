"""Quota hard stop at the selected region (AC-BI-007; PLAN.md §5/S7).

Runs once, at the region `select_region` already selected: `az cognitiveservices usage list
--location <region>`. Insufficient quota for either model is a hard stop with a quota-increase
message, and -- unlike S5's model-availability loop -- is never retried against another region
(PLAN.md §0.1 step 7).
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


def test_quota_failure_does_not_try_a_different_region(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription()
    deploy_llm_fixture.seed_usage("swedencentral", chat=(950, 1000), embed=(0, 10_000))

    deploy_llm_fixture.run_deploy("--yes", expect=1)

    assert _usage_list_calls(deploy_llm_fixture) == [
        "cognitiveservices usage list --location swedencentral",
    ]


def test_sufficient_quota_at_both_models_passes(deploy_llm_fixture: DeployLlmFixture) -> None:
    deploy_llm_fixture.seed_subscription()  # conftest baseline's ample quota fits 1000/350

    deploy_llm_fixture.run_deploy("--yes", expect=0)
