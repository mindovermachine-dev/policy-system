"""Idempotent rerun against the same subscription/config makes zero changes (AC-BI-011;
PLAN.md §5/S9).

No new production logic was expected to be needed here if S1-S8 already implement "check
existence/value before acting" throughout (§0.1 step 8/9, §0.5) -- this slice's job is to *prove*
that end-to-end property, which is exactly what AC-BI-011 asks for. State is seeded directly via
the `seed_existing_*` fixture helpers (PLAN.md §2.4) rather than by running the script twice, so
these tests also prove idempotency does not depend on this exact script having created the state.
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
DEFAULT_KEY1 = "FAKE-KEY-1-INITIAL"


def _hash8(subscription_id: str) -> str:
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _names(subscription_id: str = SUBSCRIPTION_ID) -> tuple[str, str]:
    hash8 = _hash8(subscription_id)
    return f"policy-system-llm-{hash8}", f"kv-ps-llm-{hash8}"


def _operation(line: str) -> str:
    tokens = line.split()
    if tokens[:2] == ["cognitiveservices", "account"]:
        if tokens[2:3] == ["deployment"]:
            return "cognitiveservices account deployment " + tokens[3]
        return "cognitiveservices account " + tokens[2]
    return " ".join(tokens[:2])


def _create_operations(fixture: DeployLlmFixture) -> list[str]:
    return [
        operation
        for operation in (_operation(line) for line in fixture.read_az_log())
        if operation.endswith("create")
    ]


def _seed_fully_provisioned(fixture: DeployLlmFixture) -> tuple[str, str]:
    """Seeds every create-if-absent target *and* all three secret values, as they would look
    after a prior successful `deploy-llm.sh` run.
    """
    fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    account, vault = _names()
    endpoint = f"https://{account}.cognitiveservices.azure.com/"
    fixture.seed_existing_resource_group(RESOURCE_GROUP, location=DEFAULT_REGION)
    fixture.seed_existing_account(account, endpoint=endpoint, key1=DEFAULT_KEY1)
    fixture.seed_existing_deployment(account, CHAT_MODEL_NAME)
    fixture.seed_existing_deployment(account, EMBED_MODEL_NAME)
    fixture.seed_existing_keyvault(vault)
    fixture.seed_existing_secret(vault, "AZURE-API-BASE", endpoint)
    fixture.seed_existing_secret(vault, "AZURE-API-KEY", DEFAULT_KEY1)
    fixture.seed_existing_secret(vault, "AZURE-API-VERSION", "preview")
    return account, vault


def test_rerun_against_seeded_existing_resources_computes_identical_names(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    account, vault = _seed_fully_provisioned(deploy_llm_fixture)

    run = deploy_llm_fixture.run_deploy("--yes", expect=0)

    assert account in run.stdout
    assert vault in run.stdout


def test_rerun_makes_no_create_calls(deploy_llm_fixture: DeployLlmFixture) -> None:
    _seed_fully_provisioned(deploy_llm_fixture)

    deploy_llm_fixture.run_deploy("--yes", expect=0)

    assert _create_operations(deploy_llm_fixture) == []


def test_rerun_does_not_rewrite_secrets_with_unchanged_values(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    _seed_fully_provisioned(deploy_llm_fixture)

    deploy_llm_fixture.run_deploy("--yes", expect=0)

    set_calls = [
        line for line in deploy_llm_fixture.read_az_log() if line.startswith("keyvault secret set")
    ]
    assert set_calls == []


def test_rerun_exits_zero_with_up_to_date_message(deploy_llm_fixture: DeployLlmFixture) -> None:
    _seed_fully_provisioned(deploy_llm_fixture)

    run = deploy_llm_fixture.run_deploy("--yes", expect=0)

    assert "up to date" in run.stdout.lower()
