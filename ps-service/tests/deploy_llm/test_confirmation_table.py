"""Naming + confirmation table + `[Y/n]` prompt for `scripts/deploy-llm.sh` (AC-BI-002; PLAN.md
§5/S2, contributes AC-BI-010).

Region selection (a later slice) hasn't run yet at this point in the flow, so the table's
"region" row shows the configured candidate list in order, not a single resolved region
(PLAN.md §0.2) -- resource group / account / deployment / Key Vault rows show the actual
resolved values, since none of those depend on which region ends up selected.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployLlmFixture

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"


def _hash8(subscription_id: str) -> str:
    """Independently reproduce `sha256(subscription-id)`'s first 8 hex chars (PLAN.md §0.4)."""
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def test_table_lists_candidate_regions_in_configured_order(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)

    run = deploy_llm_fixture.run_deploy("--yes")

    assert "swedencentral, francecentral, westeurope, germanywestcentral" in run.stdout


def test_table_computes_account_and_vault_names_with_hash8_of_subscription_id(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)

    run = deploy_llm_fixture.run_deploy("--yes")

    hash8 = _hash8(SUBSCRIPTION_ID)
    assert f"policy-system-llm-{hash8}" in run.stdout
    assert f"kv-ps-llm-{hash8}" in run.stdout


def test_table_shows_resource_group_fixed_name(deploy_llm_fixture: DeployLlmFixture) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)

    run = deploy_llm_fixture.run_deploy("--yes")

    assert "rg-policy-system-llm" in run.stdout


def test_table_shows_both_deployments_with_sku_and_capacity(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)

    run = deploy_llm_fixture.run_deploy("--yes")

    assert "gpt-5.4-mini" in run.stdout
    assert "GlobalStandard" in run.stdout
    assert "300" in run.stdout
    assert "text-embedding-3-large" in run.stdout
    assert "DataZoneStandard" in run.stdout
    assert "350" in run.stdout


def test_prompt_text_is_exact_proceed_with_these_values_y_n(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)

    run = deploy_llm_fixture.run_deploy(stdin="\n")

    assert "Proceed with these values? [Y/n]" in run.stdout


def test_yes_flag_prints_table_but_skips_reading_stdin(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)

    # stdin=None -> /dev/null: if --yes did not skip the read, an unguarded read against a
    # closed stdin would still not hang, but a *guarded* one could silently swallow the EOF and
    # mask a bug where --yes forgot to skip reading. Asserting exactly one `account show` call
    # (below) is what actually proves the prompt path (which itself never re-reads the
    # subscription) never ran a second time.
    run = deploy_llm_fixture.run_deploy("--yes", stdin=None)

    assert "rg-policy-system-llm" in run.stdout
    assert deploy_llm_fixture.read_az_log().count("account show --query id -o tsv") == 1


def test_deploy_llm_common_lib_is_not_executable() -> None:
    """`scripts/lib/deploy-llm-common.sh` is sourced only, mirroring `scripts/release/lib/*.sh`'s
    lib/non-lib distinction (PLAN.md §4).
    """
    lib = Path(__file__).resolve().parents[3] / "scripts" / "lib" / "deploy-llm-common.sh"
    assert lib.exists(), f"{lib} does not exist"
    assert not os.access(lib, os.X_OK), f"{lib} must not be executable"
