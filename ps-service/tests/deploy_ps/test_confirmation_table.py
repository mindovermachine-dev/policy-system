"""Naming + confirmation table + `[Y/n]` prompt for `scripts/deploy-ps.sh` (contributes
AC-BI-019; PLAN.md §5/S5).

Region *selection* (a later slice, S8) hasn't run yet at this point in the flow, so the table's
region row shows the configured candidate list in order, not a single resolved region -- same
convention as `deploy-llm.sh`'s own table. Unlike `deploy-llm.sh`, this table also shows the AKS
cluster name and DNS label, both new S5 naming-lib additions (`aks_cluster_name`/`dns_label`,
PLAN.md §0.4).

Every test here writes a config with `TLS_CONTACT_EMAIL` already filled in, so the only
interactive `read` the script performs is the `[Y/n]` confirmation prompt itself --
`test_blank_tls_contact_email_prompts_interactively` (in `test_config_validation.py`'s sibling
below) is the one test that exercises the *other* interactive prompt
(`prompt_for_tls_contact_email`), in isolation.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"

CONFIG_WITH_TLS_EMAIL = """# scripts/ps-defaults.conf — evaluator-tunable Azure customer-tenant
# deployment defaults (issue #111). No secrets. See docs/architecture/customer-azure-deployment.md.

LLM_REGION_CANDIDATES=(swedencentral francecentral westeurope germanywestcentral)
LLM_CHAT_MODEL_NAME="gpt-5.4-mini"
LLM_CHAT_MODEL_SKU="DataZoneStandard"
LLM_CHAT_MODEL_CAPACITY=200
LLM_EMBED_MODEL_NAME="text-embedding-3-large"
LLM_EMBED_MODEL_SKU="Standard"
LLM_EMBED_MODEL_CAPACITY=350
TLS_CONTACT_EMAIL="tls-contact@example.test"
"""


def _hash8(subscription_id: str) -> str:
    """Independently reproduce `sha256(subscription-id)`'s first 8 hex chars (PLAN.md §0.4)."""
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _seed(fixture: DeployPsFixture) -> None:
    fixture.config_path.write_text(CONFIG_WITH_TLS_EMAIL, encoding="utf-8")
    fixture.seed_subscription(id_=SUBSCRIPTION_ID)


def test_table_computes_account_vault_cluster_dns_label_names_with_hash8_of_subscription_id(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(stdin="N\n")

    hash8 = _hash8(SUBSCRIPTION_ID)
    assert f"policy-system-llm-{hash8}" in run.stdout
    assert f"kv-ps-llm-{hash8}" in run.stdout
    assert f"aks-policy-system-{hash8}" in run.stdout
    assert f"ps-{hash8}" in run.stdout


def test_table_shows_rg_policy_system_not_the_old_llm_suffixed_name(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(stdin="N\n")

    assert "rg-policy-system" in run.stdout
    assert "rg-policy-system-llm" not in run.stdout


def test_table_shows_both_deployments_with_sku_and_capacity(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(stdin="N\n")

    assert "gpt-5.4-mini" in run.stdout
    assert "DataZoneStandard" in run.stdout
    assert "200" in run.stdout
    assert "text-embedding-3-large" in run.stdout
    assert "Standard" in run.stdout
    assert "350" in run.stdout


def test_prompt_text_is_exact_proceed_with_these_values_y_n(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(stdin="\n")

    assert "Proceed with these values? [Y/n]" in run.stdout


def test_yes_flag_prints_table_but_skips_reading_stdin(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    # stdin=None -> /dev/null: TLS_CONTACT_EMAIL is already filled in (no prompt), and --yes
    # must skip the [Y/n] read too, or this would hang/fail on the closed stream. Asserting
    # exactly one `account show` call for the subscription id (below) is what actually proves
    # neither interactive step re-read anything.
    run = deploy_ps_fixture.run_deploy("--yes", stdin=None)

    assert "rg-policy-system" in run.stdout
    assert deploy_ps_fixture.read_az_log().count("account show --query id -o tsv") == 1


def test_deploy_llm_common_lib_is_not_executable() -> None:
    """`scripts/lib/deploy-llm-common.sh` is sourced only, mirroring `scripts/deploy-llm.sh`'s
    own lib/non-lib distinction (PLAN.md §4) -- unaffected by deploy-ps.sh's new additions.
    """
    import os
    from pathlib import Path

    lib = Path(__file__).resolve().parents[3] / "scripts" / "lib" / "deploy-llm-common.sh"
    assert lib.exists(), f"{lib} does not exist"
    assert not os.access(lib, os.X_OK), f"{lib} must not be executable"
