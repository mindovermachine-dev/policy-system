"""Soft-deleted AIServices account detection and explicit purge confirmation."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployLlmFixture

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
REGION = "swedencentral"


def _account_name() -> str:
    hash8 = hashlib.sha256(SUBSCRIPTION_ID.encode()).hexdigest()[:8]
    return f"policy-system-llm-{hash8}"


def test_declining_deleted_account_purge_stops_without_mutation(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(SUBSCRIPTION_ID)
    account_name = _account_name()
    deploy_llm_fixture.seed_deleted_account(account_name, location=REGION)

    run = deploy_llm_fixture.run_deploy(stdin="\nn\n", expect=1)

    assert f"Found soft-deleted AIServices account {account_name}" in run.stdout
    assert "Purge declined" in run.stderr
    assert not any(" account purge " in f" {line} " for line in deploy_llm_fixture.read_az_log())
    assert not any(line.endswith(" create") for line in deploy_llm_fixture.read_az_log())


def test_accepting_deleted_account_purge_continues_provisioning(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(SUBSCRIPTION_ID)
    account_name = _account_name()
    deploy_llm_fixture.seed_deleted_account(account_name, location=REGION)

    run = deploy_llm_fixture.run_deploy(stdin="\ny\n")

    assert "Purge it permanently? [y/N]" in run.stdout
    assert (
        f"cognitiveservices account purge --name {account_name} "
        f"--resource-group rg-policy-system --location {REGION}" in deploy_llm_fixture.read_az_log()
    )
    assert "Azure LLM resources provisioned." in run.stdout


def test_yes_flag_authorizes_deleted_account_purge(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(SUBSCRIPTION_ID)
    account_name = _account_name()
    deploy_llm_fixture.seed_deleted_account(account_name, location=REGION)

    run = deploy_llm_fixture.run_deploy("--yes")

    assert "Purge it permanently? [y/N] y" in run.stdout
    assert any(
        "cognitiveservices account purge" in line for line in deploy_llm_fixture.read_az_log()
    )
