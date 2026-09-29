"""Core LLM resource provisioning: create-if-absent chain + three-secret write (supports
AC-BI-015; PLAN.md §5/S9). Own copy of scripts/deploy-llm.sh's provisioning chain, RG-scoped
under S1's renamed `rg-policy-system` (PLAN.md §0.1/§0.2) -- mirrors
ps-service/tests/deploy_llm/test_provisioning.py's own S8-equivalent test shape.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# DeployPsFixture.seed_subscription's default id_
SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
RESOURCE_GROUP = "rg-policy-system"
CHAT_MODEL_NAME = "gpt-5.4-mini"
EMBED_MODEL_NAME = "text-embedding-3-large"
DEFAULT_KEY1 = "FAKE-KEY-1-INITIAL"


def _hash8(subscription_id: str) -> str:
    """Independently reproduce `subscription_hash8`'s first 8 hex chars (scripts/lib/deploy-llm-
    common.sh), same helper as test_quota_check.py's/test_confirmation_table.py's own `_hash8`.
    """
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _names(subscription_id: str = SUBSCRIPTION_ID) -> tuple[str, str]:
    hash8 = _hash8(subscription_id)
    return f"policy-system-llm-{hash8}", f"kv-ps-llm-{hash8}"


def _operation(line: str) -> str:
    """Normalizes one az.log line to its operation name (verb chain, no flag values), so
    call-sequence assertions stay stable across flag-order/content changes. Same normalization as
    deploy_llm/test_provisioning.py's own `_operation`.
    """
    tokens = line.split()
    if tokens[:2] == ["cognitiveservices", "account"]:
        if tokens[2:3] == ["deployment"]:
            return "cognitiveservices account deployment " + tokens[3]
        return "cognitiveservices account " + tokens[2]
    return " ".join(tokens[:2])


def _create_sequence(fixture: DeployPsFixture) -> list[str]:
    """The `create`-suffixed operations this module's own S9 LLM-provisioning chain is
    responsible for -- excludes `aks create` (S13, out of this module's scope: a fresh
    `main()` run reaches S13's `ensure_aks_cluster` too, appending its own `aks create` call
    later in the same log, but that call's correctness is `test_aks_cluster_creation.py`'s
    concern, not this file's).
    """
    return [
        operation
        for operation in (_operation(line) for line in fixture.read_az_log())
        if operation.endswith("create") and not operation.startswith("aks")
    ]


def _seed_ready_subscription(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription(id_=SUBSCRIPTION_ID)


def test_fresh_subscription_creates_rg_account_both_deployments_vault_and_three_secrets(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed_ready_subscription(deploy_ps_fixture)
    account, vault = _names()

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _create_sequence(deploy_ps_fixture) == [
        "group create",
        "cognitiveservices account create",
        "cognitiveservices account deployment create",
        "cognitiveservices account deployment create",
        "keyvault create",
    ]
    state = deploy_ps_fixture.azure_state
    assert (state / "resource-groups" / RESOURCE_GROUP).exists()
    assert (state / "accounts" / f"{account}.json").exists()
    assert (state / "deployments" / account / CHAT_MODEL_NAME).exists()
    assert (state / "deployments" / account / EMBED_MODEL_NAME).exists()
    assert (state / "keyvaults" / f"{vault}.json").exists()
    assert deploy_ps_fixture.read_secret(vault, "AZURE-API-BASE") is not None
    assert deploy_ps_fixture.read_secret(vault, "AZURE-API-KEY") is not None
    assert deploy_ps_fixture.read_secret(vault, "AZURE-API-VERSION") is not None


def test_resource_group_name_is_rg_policy_system(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """Confirms S1's rename is visible here too, not just in S5's confirmation table -- the
    actual `group create` call and the resulting on-disk state both use `rg-policy-system`, and
    the retired `-llm`-suffixed name never appears anywhere in the created state or log.
    """
    _seed_ready_subscription(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert (deploy_ps_fixture.azure_state / "resource-groups" / RESOURCE_GROUP).exists()
    assert any(
        line.startswith("group create") and f"--name {RESOURCE_GROUP} " in f"{line} "
        for line in deploy_ps_fixture.read_az_log()
    )
    old_name_rg = deploy_ps_fixture.azure_state / "resource-groups" / "rg-policy-system-llm"
    assert not old_name_rg.exists()
    assert not any("rg-policy-system-llm" in line for line in deploy_ps_fixture.read_az_log())


def test_keyvault_access_policy_grants_exactly_get_list_set_scoped_to_this_vault(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed_ready_subscription(deploy_ps_fixture)
    _, vault = _names()

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    policy_calls = [
        line for line in deploy_ps_fixture.read_az_log() if line.startswith("keyvault set-policy")
    ]
    assert len(policy_calls) == 1
    assert f"--name {vault}" in policy_calls[0]
    tokens = policy_calls[0].split()
    permissions_index = tokens.index("--secret-permissions")
    # Exactly ["get", "list", "set"] follow the flag -- not a superset (e.g. an accidental
    # "purge"/"backup"/"delete") and not a subset, up to the next flag or end of line.
    granted: list[str] = []
    for token in tokens[permissions_index + 1 :]:
        if token.startswith("--"):
            break
        granted.append(token)
    assert granted == ["get", "list", "set"]


def test_successful_run_output_never_contains_the_raw_api_key_value(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """S9 has no closing summary line of its own (that's S18's job -- unlike
    scripts/deploy-llm.sh's own `print_provisioning_summary`, which already exists at this
    point in that script's own history); this proves the secret-writing step itself, which does
    run here, never leaks the raw value onto stdout/stderr while it does its work.
    """
    _seed_ready_subscription(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert DEFAULT_KEY1 not in run.output
    assert "Writing LLM secrets to" in run.output


def test_rerun_with_everything_existing_makes_no_create_calls(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """Idempotent rerun: resource group, account, both deployments, and vault already exist --
    no `create` call for any of them. Key Vault access-policy grant still runs every time
    (Azure's `set-policy` is itself idempotent, not a "create") and secrets are still
    write-if-changed (unaffected -- already-matching values make no `secret set` call either).
    """
    account, vault = _names()
    _seed_ready_subscription(deploy_ps_fixture)
    deploy_ps_fixture.seed_existing_resource_group(RESOURCE_GROUP, location="swedencentral")
    deploy_ps_fixture.seed_existing_account(account)
    deploy_ps_fixture.seed_existing_deployment(account, CHAT_MODEL_NAME)
    deploy_ps_fixture.seed_existing_deployment(account, EMBED_MODEL_NAME)
    deploy_ps_fixture.seed_existing_keyvault(vault)
    deploy_ps_fixture.seed_existing_secret(
        vault, "AZURE-API-BASE", f"https://{account}.cognitiveservices.azure.com/"
    )
    deploy_ps_fixture.seed_existing_secret(vault, "AZURE-API-KEY", DEFAULT_KEY1)
    deploy_ps_fixture.seed_existing_secret(vault, "AZURE-API-VERSION", "preview")

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _create_sequence(deploy_ps_fixture) == []
    secret_set_calls = [
        line for line in deploy_ps_fixture.read_az_log() if line.startswith("keyvault secret set")
    ]
    assert secret_set_calls == []
