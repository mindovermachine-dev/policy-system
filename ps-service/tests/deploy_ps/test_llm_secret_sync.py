"""LLM secret sync into AKS + `values-prod.yaml` direct reference (AC-BI-013 script-half;
PLAN.md §5/S14).

`ensure_llm_secret` reads the three LLM credentials S9 already wrote to Key Vault
(`AZURE-API-KEY`/`AZURE-API-BASE`/`AZURE-API-VERSION`, dash-named to match Key Vault's own
character restrictions) and writes them into the cluster as a Kubernetes Secret, underscore-keyed
(`AZURE_API_KEY`/`AZURE_API_BASE`/`AZURE_API_VERSION`, the environment-variable convention
`charts/policy-system` expects) -- same `create --dry-run=client -o yaml | apply` idiom as
`scripts/sync-llm-secrets-to-kind.sh`, minus its kind-only context guard (this script already
pointed kubectl at the right cluster via S13's `ensure_aks_credentials`).

The no-op claim is proven via `kubectl apply`'s own machine-readable stdout ("unchanged" vs.
"created"/"configured"), not a separate diff computed by this test suite -- `apply_output_changed`
(scripts/deploy-ps.sh) greps exactly that string, and `FAKE_KUBECTL_SCRIPT` reproduces it
faithfully (conftest.py's own module comment).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# DeployPsFixture.seed_subscription's default id_
SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
LLM_SECRET_NAME = "policy-system-llm-credentials"
DEFAULT_KEY1 = "FAKE-KEY-1-INITIAL"
AZURE_API_VERSION_LITERAL = "preview"


def _hash8(subscription_id: str) -> str:
    """Independently reproduce `subscription_hash8`'s first 8 hex chars (scripts/lib/deploy-llm-
    common.sh), same helper as every other `deploy_ps` test module's own `_hash8`.
    """
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _account_name(subscription_id: str = SUBSCRIPTION_ID) -> str:
    return f"policy-system-llm-{_hash8(subscription_id)}"


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription(id_=SUBSCRIPTION_ID)


def _llm_secret_apply_output_lines(fixture: DeployPsFixture) -> list[str]:
    """Filters `read_kubectl_apply_output_log()` down to only the LLM Secret's own lines --
    S16/S17 (later slices) also `kubectl apply` a ClusterIssuer on every run, so the LLM Secret's
    line is no longer necessarily the log's last entry.
    """
    return [
        line
        for line in fixture.read_kubectl_apply_output_log()
        if line.startswith(f"secret/{LLM_SECRET_NAME} ")
    ]


def test_writes_llm_secret_with_underscore_keys_from_the_three_dash_named_kv_secrets(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    account = _account_name()
    expected_endpoint = f"https://{account}.cognitiveservices.azure.com/"

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    manifest = deploy_ps_fixture.read_kubectl_applied("Secret", LLM_SECRET_NAME)
    assert manifest is not None, "kubectl apply -f - was never called for the LLM Secret"
    assert f'AZURE_API_KEY: "{DEFAULT_KEY1}"' in manifest
    assert f'AZURE_API_BASE: "{expected_endpoint}"' in manifest
    assert f'AZURE_API_VERSION: "{AZURE_API_VERSION_LITERAL}"' in manifest
    # The dash-named Key Vault secret names never leak into the Secret manifest itself -- only
    # the underscore-keyed, chart-consumable env-var names do.
    assert "AZURE-API-KEY" not in manifest
    assert "AZURE-API-BASE" not in manifest
    assert "AZURE-API-VERSION" not in manifest


def test_rerun_with_unchanged_values_reports_no_change_via_kubectl_apply_unchanged_output(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """Two full runs against the same, unchanging subscription state: the second run's `kubectl
    apply` reports "unchanged" for the LLM Secret -- `apply_output_changed`'s own machine-
    readable signal (scripts/deploy-ps.sh), not a diff this test suite computes itself. The Key
    Vault values themselves never change between runs (S9's own `write_secret_if_changed` is
    already proven idempotent by test_llm_provisioning.py), so this isolates S14's own
    `ensure_llm_secret` -> `kubectl apply` idempotency claim.
    """
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)
    first_lines = _llm_secret_apply_output_lines(deploy_ps_fixture)
    assert first_lines == [f"secret/{LLM_SECRET_NAME} created"]

    deploy_ps_fixture.run_deploy("--yes", expect=0)
    second_lines = _llm_secret_apply_output_lines(deploy_ps_fixture)

    # Exactly one new LLM-Secret apply-output line was appended by the second run, and it
    # reports "unchanged" -- not "configured" (which would mean something actually differed) and
    # not a second "created" (which would mean the idempotency check never fired at all).
    assert second_lines == [
        f"secret/{LLM_SECRET_NAME} created",
        f"secret/{LLM_SECRET_NAME} unchanged",
    ]


def test_values_prod_file_constant_resolves_to_the_real_chart_file_path() -> None:
    """Parses the literal `readonly VALUES_PROD_FILE=...` assignment straight out of the real,
    checked-in `scripts/deploy-ps.sh` (never the fixture's isolated `tmp_path` copy, which has no
    `charts/` tree alongside it) and resolves it exactly as bash would -- `${SCRIPT_DIR}`
    substituted with the real `scripts/` directory's own absolute path -- then asserts the
    resulting path actually exists on disk.

    Deliberately reads the literal back out of the script rather than independently re-deriving
    "the correct" relative path: a wrong number of `../` segments in the real assignment is
    exactly the bug this test exists to catch. `scripts/deploy-ps.sh` lives ONE directory below
    the repo root (`scripts/`), unlike `spikes/deploy-ps-azure/deploy-ps.sh` (the empirical
    reference this constant's value is modeled on), which lives TWO directories below
    (`spikes/deploy-ps-azure/`) and therefore correctly uses two `../` segments where this script
    needs only one. Reproducing the formula instead of reading it back would risk both sides
    sharing the same off-by-one mistake and passing regardless.
    """
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    script_path = repo_root / "scripts" / "deploy-ps.sh"
    script_dir = script_path.parent
    text = script_path.read_text(encoding="utf-8")

    match = re.search(r'readonly VALUES_PROD_FILE="([^"]+)"', text)
    assert match, "VALUES_PROD_FILE constant not found in scripts/deploy-ps.sh"

    literal = match.group(1).replace("${SCRIPT_DIR}", str(script_dir))
    resolved = Path(literal).resolve()

    assert resolved.exists(), f"VALUES_PROD_FILE resolves to {resolved}, which does not exist"
    assert resolved == (repo_root / "charts" / "policy-system" / "values-prod.yaml").resolve()
