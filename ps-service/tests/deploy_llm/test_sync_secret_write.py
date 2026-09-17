"""`scripts/sync-llm-secrets-to-kind.sh`: secret write (AC-BI-016, AC-BI-013 for this script;
PLAN.md §5/S12; CHANGES.md Row 2 adds the raw-key-value test).

A successful run reads the three Key Vault secrets `deploy-llm.sh` wrote and writes them into a
Secret named `policy-system-llm-credentials`, in the active namespace, with keys renamed from the
Key Vault's dash convention to the underscore convention `charts/policy-system` expects, via the
`kubectl create secret ... --dry-run=client -o yaml | kubectl apply -f -` idiom.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployLlmFixture

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
SECRET_NAME = "policy-system-llm-credentials"
API_KEY_VALUE = "sentinel-azure-api-key-value"
API_BASE_VALUE = "https://policy-system-llm-abc12345.cognitiveservices.azure.com/"
API_VERSION_VALUE = "preview"


def _hash8(subscription_id: str) -> str:
    """Independently reproduce `sha256(subscription-id)`'s first 8 hex chars (PLAN.md §0.4)."""
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _vault_name(subscription_id: str = SUBSCRIPTION_ID) -> str:
    return f"kv-ps-llm-{_hash8(subscription_id)}"


def _stringdata_value(manifest: str, key: str) -> str | None:
    """Extracts one `stringData` key's value from a captured fake-`kubectl` manifest."""
    match = re.search(rf'^  {re.escape(key)}: "(.*)"$', manifest, re.MULTILINE)
    return match.group(1) if match else None


def _seed_synced_vault(
    fixture: DeployLlmFixture,
    *,
    api_key: str = API_KEY_VALUE,
    api_base: str = API_BASE_VALUE,
    api_version: str = API_VERSION_VALUE,
) -> str:
    """Seeds Key Vault the way S8's provisioning would have left it after a successful
    `deploy-llm.sh` run, plus a kind-prefixed context so the guard passes.
    """
    fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    vault = _vault_name()
    fixture.seed_existing_secret(vault, "AZURE-API-KEY", api_key)
    fixture.seed_existing_secret(vault, "AZURE-API-BASE", api_base)
    fixture.seed_existing_secret(vault, "AZURE-API-VERSION", api_version)
    fixture.seed_kubectl_context("kind-policy-system")
    return vault


def test_writes_secret_named_policy_system_llm_credentials(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    _seed_synced_vault(deploy_llm_fixture)

    deploy_llm_fixture.run_sync(expect=0)

    manifest = deploy_llm_fixture.read_applied_manifest("default", SECRET_NAME)
    assert manifest is not None
    assert f"name: {SECRET_NAME}" in manifest


def test_secret_contains_azure_api_key_base_version_with_underscore_names(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    _seed_synced_vault(deploy_llm_fixture)

    deploy_llm_fixture.run_sync(expect=0)

    manifest = deploy_llm_fixture.read_applied_manifest("default", SECRET_NAME)
    assert manifest is not None
    assert "AZURE_API_KEY" in manifest
    assert "AZURE_API_BASE" in manifest
    assert "AZURE_API_VERSION" in manifest


def test_secret_values_match_what_deploy_llm_wrote_to_key_vault(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    _seed_synced_vault(deploy_llm_fixture)

    deploy_llm_fixture.run_sync(expect=0)

    manifest = deploy_llm_fixture.read_applied_manifest("default", SECRET_NAME)
    assert manifest is not None
    assert _stringdata_value(manifest, "AZURE_API_KEY") == API_KEY_VALUE
    assert _stringdata_value(manifest, "AZURE_API_BASE") == API_BASE_VALUE
    assert _stringdata_value(manifest, "AZURE_API_VERSION") == API_VERSION_VALUE


def test_writes_into_the_active_namespace_not_a_hardcoded_one(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    _seed_synced_vault(deploy_llm_fixture)
    deploy_llm_fixture.seed_kubectl_namespace("custom-ns")

    deploy_llm_fixture.run_sync(expect=0)

    assert deploy_llm_fixture.read_applied_manifest("custom-ns", SECRET_NAME) is not None
    assert deploy_llm_fixture.read_applied_manifest("default", SECRET_NAME) is None


def test_sync_never_passes_explicit_namespace_flag_to_kubectl(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    _seed_synced_vault(deploy_llm_fixture)
    deploy_llm_fixture.seed_kubectl_namespace("custom-ns")

    deploy_llm_fixture.run_sync(expect=0)

    log = deploy_llm_fixture.read_kubectl_log()
    assert log, "expected at least the apply -f - invocation to be logged"
    for line in log:
        tokens = line.split()
        assert "-n" not in tokens
        assert "--namespace" not in tokens
        assert not any(token.startswith("--namespace=") for token in tokens)


def test_sync_never_prints_the_raw_azure_api_key_value(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    _seed_synced_vault(deploy_llm_fixture, api_key="sentinel-value-must-not-leak")

    run = deploy_llm_fixture.run_sync(expect=0)

    assert "sentinel-value-must-not-leak" not in run.stdout
    assert "sentinel-value-must-not-leak" not in run.stderr
