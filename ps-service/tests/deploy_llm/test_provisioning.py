"""Core provisioning against a fresh subscription: create-if-absent chain + three-secret write
(AC-BI-009, AC-BI-010 completion, AC-BI-012, AC-BI-013 first enforcement; PLAN.md §5/S8).

`test_deploy_llm_sh_never_calls_printf_or_echo_directly_on_the_key_variable` is explicitly
dropped per PLAN.md §5/S8 and §10 point 5 -- static "how might it leak" source-grepping is the
wrong test shape; `test_successful_run_output_never_contains_the_raw_api_key_value` below is the
real, dynamic AC-BI-013 proof in its place.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployLlmFixture

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
RESOURCE_GROUP = "rg-policy-system-llm"
CHAT_MODEL_NAME = "gpt-5.4-mini"
EMBED_MODEL_NAME = "text-embedding-3-large"
DEFAULT_KEY1 = "FAKE-KEY-1-INITIAL"


def _hash8(subscription_id: str) -> str:
    """Independently reproduce `sha256(subscription-id)`'s first 8 hex chars (PLAN.md §0.4)."""
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _names(subscription_id: str = SUBSCRIPTION_ID) -> tuple[str, str]:
    hash8 = _hash8(subscription_id)
    return f"policy-system-llm-{hash8}", f"kv-ps-llm-{hash8}"


def _operation(line: str) -> str:
    """Normalizes one az.log line to its operation name (verb chain, no flag values), so
    call-sequence assertions stay stable across flag-order/content changes.
    """
    tokens = line.split()
    if tokens[:2] == ["cognitiveservices", "account"]:
        if tokens[2:3] == ["deployment"]:
            return "cognitiveservices account deployment " + tokens[3]
        return "cognitiveservices account " + tokens[2]
    return " ".join(tokens[:2])


def _create_sequence(fixture: DeployLlmFixture) -> list[str]:
    return [
        operation
        for operation in (_operation(line) for line in fixture.read_az_log())
        if operation.endswith("create")
    ]


def test_fresh_subscription_creates_rg_account_both_deployments_vault_and_three_secrets(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    account, vault = _names()

    deploy_llm_fixture.run_deploy("--yes", expect=0)

    assert _create_sequence(deploy_llm_fixture) == [
        "group create",
        "cognitiveservices account create",
        "cognitiveservices account deployment create",
        "cognitiveservices account deployment create",
        "keyvault create",
    ]
    state = deploy_llm_fixture.azure_state
    assert (state / "resource-groups" / RESOURCE_GROUP).exists()
    assert (state / "accounts" / f"{account}.json").exists()
    assert (state / "deployments" / account / CHAT_MODEL_NAME).exists()
    assert (state / "deployments" / account / EMBED_MODEL_NAME).exists()
    assert (state / "keyvaults" / f"{vault}.json").exists()
    assert deploy_llm_fixture.read_secret(vault, "AZURE-API-BASE") is not None
    assert deploy_llm_fixture.read_secret(vault, "AZURE-API-KEY") is not None
    assert deploy_llm_fixture.read_secret(vault, "AZURE-API-VERSION") is not None


def test_account_and_vault_names_match_the_confirmation_table(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    account, vault = _names()

    run = deploy_llm_fixture.run_deploy("--yes", expect=0)

    # The table (printed before provisioning) and the actual create calls must use the exact
    # same computed names -- both come from the same main() variables (S2/S10 stay consistent).
    assert account in run.stdout
    assert vault in run.stdout
    assert any(account in line for line in deploy_llm_fixture.read_az_log())
    assert any(vault in line for line in deploy_llm_fixture.read_az_log())


def test_keyvault_access_policy_grants_exactly_get_list_set_scoped_to_this_vault(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    _, vault = _names()

    deploy_llm_fixture.run_deploy("--yes", expect=0)

    policy_calls = [
        line for line in deploy_llm_fixture.read_az_log() if line.startswith("keyvault set-policy")
    ]
    assert len(policy_calls) == 1
    assert f"--name {vault}" in policy_calls[0]
    assert "--secret-permissions get list set" in policy_calls[0]


def test_secret_values_written_match_account_endpoint_and_key1_and_fixed_api_version(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    account, vault = _names()

    deploy_llm_fixture.run_deploy("--yes", expect=0)

    assert (
        deploy_llm_fixture.read_secret(vault, "AZURE-API-BASE")
        == f"https://{account}.cognitiveservices.azure.com/"
    )
    assert deploy_llm_fixture.read_secret(vault, "AZURE-API-KEY") == DEFAULT_KEY1
    assert deploy_llm_fixture.read_secret(vault, "AZURE-API-VERSION") == "preview"


def test_successful_run_output_never_contains_the_raw_api_key_value(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)

    run = deploy_llm_fixture.run_deploy("--yes", expect=0)

    assert DEFAULT_KEY1 not in run.output
    assert "AZURE-API-KEY" in run.output
