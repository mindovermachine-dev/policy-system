"""`scripts/sync-llm-secrets-to-kind.sh`: kind-context guard (AC-BI-015, PLAN.md §5/S11).

Running against a non-`kind-*` kubectl context must abort before writing anything -- and before
reading anything out of Key Vault -- protecting against writing Azure credentials into the wrong
cluster. This is the new script's first behavior and the first consumer of the fake `kubectl`
(CHANGES.md Row 3/Appendix A, not PLAN.md §2.3's original racy version).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployLlmFixture

BASH_SHEBANG = "#!/usr/bin/env bash"
SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"


def _hash8(subscription_id: str) -> str:
    """Independently reproduce `sha256(subscription-id)`'s first 8 hex chars (PLAN.md §0.4)."""
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _vault_name(subscription_id: str = SUBSCRIPTION_ID) -> str:
    return f"kv-ps-llm-{_hash8(subscription_id)}"


def test_non_kind_context_aborts_before_reading_key_vault(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    # Seed a fully-provisioned subscription -- if the guard failed to fire, the script would
    # otherwise have everything it needs to proceed and read Key Vault.
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    vault = _vault_name()
    deploy_llm_fixture.seed_existing_secret(vault, "AZURE-API-KEY", "sentinel-key")
    deploy_llm_fixture.seed_existing_secret(
        vault, "AZURE-API-BASE", "https://example.cognitiveservices.azure.com/"
    )
    deploy_llm_fixture.seed_existing_secret(vault, "AZURE-API-VERSION", "preview")
    deploy_llm_fixture.seed_kubectl_context("my-other-cluster")

    run = deploy_llm_fixture.run_sync(expect=1)

    assert "kind-" in run.stderr
    assert "my-other-cluster" in run.stderr
    # No az call at all was made -- stronger than "no keyvault secret show call", since the
    # guard runs before any Azure interaction whatsoever (Fail Fast at Boundaries).
    assert deploy_llm_fixture.read_az_log() == []
    assert not any("keyvault secret" in line for line in deploy_llm_fixture.read_az_log())


def test_kind_prefixed_context_passes_the_guard(deploy_llm_fixture: DeployLlmFixture) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    vault = _vault_name()
    deploy_llm_fixture.seed_existing_secret(vault, "AZURE-API-KEY", "sentinel-key")
    deploy_llm_fixture.seed_existing_secret(
        vault, "AZURE-API-BASE", "https://example.cognitiveservices.azure.com/"
    )
    deploy_llm_fixture.seed_existing_secret(vault, "AZURE-API-VERSION", "preview")
    deploy_llm_fixture.seed_kubectl_context("kind-policy-system")

    run = deploy_llm_fixture.run_sync(expect=0)

    assert "kind-" not in run.stderr


def test_sync_script_has_bash_shebang_executable_bit_and_strict_mode() -> None:
    script = Path(__file__).resolve().parents[3] / "scripts" / "sync-llm-secrets-to-kind.sh"
    assert os.access(script, os.X_OK), f"{script} is not executable"
    lines = script.read_text(encoding="utf-8").splitlines()
    assert lines[0] == BASH_SHEBANG, (
        f"sync-llm-secrets-to-kind.sh does not start with `{BASH_SHEBANG}`"
    )
    assert "set -euo pipefail" in lines, "sync-llm-secrets-to-kind.sh does not enable strict mode"
