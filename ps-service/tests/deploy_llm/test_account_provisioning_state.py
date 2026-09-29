"""ensure_account must not treat an existing-but-not-yet-provisioned AIServices account as ready
just because `az cognitiveservices account show` succeeds.

A run interrupted between `account create` returning and Azure actually finishing provisioning
(Ctrl-C, or the script dying mid-wait) leaves exactly this shape behind: `show` succeeds, but
`properties.provisioningState` is still "Creating". Before this fix, a rerun's ensure_account
treated that as fully ready and walked straight into ensure_deployment, which fails confusingly
against an account that is not actually usable yet. wait_for_account_ready (scripts/deploy-
llm.sh) now polls provisioningState to a terminal value first, hard-failing clearly on both a
provisioning timeout and a "Failed" account rather than silently pressing on.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployLlmFixture

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
RESOURCE_GROUP = "rg-policy-system"
DEFAULT_REGION = "swedencentral"
CHAT_MODEL_NAME = "gpt-5.4-mini"
EMBED_MODEL_NAME = "text-embedding-3-large"


def _hash8(subscription_id: str) -> str:
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _account_name(subscription_id: str = SUBSCRIPTION_ID) -> str:
    return f"policy-system-llm-{_hash8(subscription_id)}"


def _account_show_calls(fixture: DeployLlmFixture, account_name: str) -> list[str]:
    return [
        line
        for line in fixture.read_az_log()
        # "show " (trailing space), not "show-deleted" -- account_show_calls asserts only the
        # plain `account show` polling ensure_account/wait_for_account_ready do.
        if line.startswith("cognitiveservices account show ") and f"--name {account_name}" in line
    ]


def _deployment_create_calls(fixture: DeployLlmFixture) -> list[str]:
    return [
        line
        for line in fixture.read_az_log()
        if line.startswith("cognitiveservices account deployment create")
    ]


def test_rerun_waits_out_a_still_provisioning_account_then_proceeds(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    account_name = _account_name()
    deploy_llm_fixture.seed_existing_resource_group(RESOURCE_GROUP, location=DEFAULT_REGION)
    deploy_llm_fixture.seed_existing_account(
        account_name, provisioning_state="Creating", ready_after_polls=1
    )

    run = deploy_llm_fixture.run_deploy("--yes", expect=0)

    # Two `show` calls: ensure_account's own check (returns "Creating"), then one poll inside
    # wait_for_account_ready (returns "Succeeded") -- proves the wait actually happened rather
    # than the first "show succeeded" being treated as "ready".
    assert len(_account_show_calls(deploy_llm_fixture, account_name)) == 2
    assert len(_deployment_create_calls(deploy_llm_fixture)) == 2
    assert account_name in run.stdout


def test_permanently_failed_account_hard_stops_before_deploying(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    account_name = _account_name()
    deploy_llm_fixture.seed_existing_resource_group(RESOURCE_GROUP, location=DEFAULT_REGION)
    deploy_llm_fixture.seed_existing_account(account_name, provisioning_state="Failed")

    run = deploy_llm_fixture.run_deploy("--yes", expect=1)

    assert account_name in run.stderr
    assert "Failed" in run.stderr
    assert _deployment_create_calls(deploy_llm_fixture) == []


def test_a_still_provisioning_account_past_the_timeout_hard_stops_with_a_clear_message(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    account_name = _account_name()
    deploy_llm_fixture.seed_existing_resource_group(RESOURCE_GROUP, location=DEFAULT_REGION)
    deploy_llm_fixture.seed_existing_account(account_name, provisioning_state="Creating")

    run = deploy_llm_fixture.run_deploy(
        "--yes", expect=1, env={"DEPLOY_LLM_ACCOUNT_READY_TIMEOUT_SECONDS": "0"}
    )

    assert account_name in run.stderr
    assert "unusually long" in run.stderr
    # The timeout is checked before the loop ever sleeps/re-polls, so ensure_account's own
    # initial `show` is the only one -- no wasted poll before giving up.
    assert len(_account_show_calls(deploy_llm_fixture, account_name)) == 1
    assert _deployment_create_calls(deploy_llm_fixture) == []
