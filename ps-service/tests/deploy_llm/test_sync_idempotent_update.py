"""`scripts/sync-llm-secrets-to-kind.sh`: idempotent rerun (AC-BI-017, PLAN.md §5/S13;
CHANGES.md Row 2 adds the raw-key-value test).

Should need no script change if S12 already used the `create --dry-run=client -o yaml |
apply -f -` idiom -- this slice proves that property end-to-end, exactly as S9 did for
`deploy-llm.sh`'s own idempotency (AC-BI-011).
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployLlmFixture

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
SECRET_NAME = "policy-system-llm-credentials"
INITIAL_KEY = "initial-azure-api-key"
ROTATED_KEY = "rotated-azure-api-key"
API_BASE_VALUE = "https://policy-system-llm-abc12345.cognitiveservices.azure.com/"
API_VERSION_VALUE = "preview"


def _hash8(subscription_id: str) -> str:
    """Independently reproduce `sha256(subscription-id)`'s first 8 hex chars (PLAN.md §0.4)."""
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _vault_name(subscription_id: str = SUBSCRIPTION_ID) -> str:
    return f"kv-ps-llm-{_hash8(subscription_id)}"


def _stringdata_value(manifest: str, key: str) -> str | None:
    match = re.search(rf'^  {re.escape(key)}: "(.*)"$', manifest, re.MULTILINE)
    return match.group(1) if match else None


def _seed_synced_vault(fixture: DeployLlmFixture, *, api_key: str) -> str:
    fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    vault = _vault_name()
    fixture.seed_existing_secret(vault, "AZURE-API-KEY", api_key)
    fixture.seed_existing_secret(vault, "AZURE-API-BASE", API_BASE_VALUE)
    fixture.seed_existing_secret(vault, "AZURE-API-VERSION", API_VERSION_VALUE)
    fixture.seed_kubectl_context("kind-policy-system")
    return vault


def test_rerun_overwrites_the_previously_applied_manifest_rather_than_erroring(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    _seed_synced_vault(deploy_llm_fixture, api_key=INITIAL_KEY)

    deploy_llm_fixture.run_sync(expect=0)
    second_run = deploy_llm_fixture.run_sync(expect=0)

    assert second_run.returncode == 0
    manifest = deploy_llm_fixture.read_applied_manifest("default", SECRET_NAME)
    assert manifest is not None
    assert _stringdata_value(manifest, "AZURE_API_KEY") == INITIAL_KEY


def test_rerun_reflects_a_rotated_key_value(deploy_llm_fixture: DeployLlmFixture) -> None:
    vault = _seed_synced_vault(deploy_llm_fixture, api_key=INITIAL_KEY)

    deploy_llm_fixture.run_sync(expect=0)
    first_manifest = deploy_llm_fixture.read_applied_manifest("default", SECRET_NAME)
    assert first_manifest is not None
    assert _stringdata_value(first_manifest, "AZURE_API_KEY") == INITIAL_KEY

    # Simulate `deploy-llm.sh --rotate-key` having rotated the stored secret value in Key Vault.
    deploy_llm_fixture.seed_existing_secret(vault, "AZURE-API-KEY", ROTATED_KEY)
    deploy_llm_fixture.run_sync(expect=0)

    second_manifest = deploy_llm_fixture.read_applied_manifest("default", SECRET_NAME)
    assert second_manifest is not None
    assert _stringdata_value(second_manifest, "AZURE_API_KEY") == ROTATED_KEY


def test_rerun_never_prints_the_raw_azure_api_key_value(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    _seed_synced_vault(deploy_llm_fixture, api_key=INITIAL_KEY)

    first_run = deploy_llm_fixture.run_sync(expect=0)
    second_run = deploy_llm_fixture.run_sync(expect=0)

    assert INITIAL_KEY not in first_run.stdout
    assert INITIAL_KEY not in first_run.stderr
    assert INITIAL_KEY not in second_run.stdout
    assert INITIAL_KEY not in second_run.stderr
