"""Quota preflight at the selected region (AC-BI-009/010), plus 4 of the spike's 6 proven
bugfixes (PLAN.md §5/S8): the real per-model+SKU usage key, the empty-usage-list skip, jq-`floor`
float handling, and the already-deployed skip. The other 2 (capacity-minimum null coalesce,
model-version) are covered by test_capacity_validation.py and this module's
`test_deployment_create_call_includes_the_real_model_version_from_model_list` respectively.

`check_quota` runs once, against `select_region`'s already-chosen region only -- no
fallback-region reporting, same convention as `validate_capacity_range` (see
test_capacity_validation.py's own module docstring).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# DeployPsFixture.seed_subscription's default id_
SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"


def _hash8(subscription_id: str) -> str:
    """Independently reproduce `subscription_hash8`'s first 8 hex chars (scripts/lib/deploy-llm-
    common.sh), same helper as test_confirmation_table.py's own `_hash8`.
    """
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


ACCOUNT_NAME = f"policy-system-llm-{_hash8(SUBSCRIPTION_ID)}"


def _usage_list_calls(fixture: DeployPsFixture) -> list[str]:
    return [
        line for line in fixture.read_az_log() if line.startswith("cognitiveservices usage list")
    ]


def test_quota_key_matches_real_azure_usage_entry_shape_openai_dot_sku_dot_modelname(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """Bugfix 2/6 (spikes/deploy-ps-azure/README.md "Bugs found and fixed": "Quota preflight
    always a no-op"). `quota_usage_key` must build the real Azure usage-entry name
    ("OpenAI.<Sku>.<ModelName>") -- the literal "chat"/"embed" keys `scripts/deploy-llm.sh`
    (issue #105) still uses never match any real entry, so an insufficient-quota scenario keyed
    correctly must actually be *seen* and hard-stop, not silently pass. `seed_usage` already
    writes the real-shaped key (see conftest's own docstring) -- this test proves the production
    code's `quota_usage_key` reads it back correctly.
    """
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    # Config's LLM_CHAT_MODEL_CAPACITY is 200 -- only 50 remaining (1000 limit, 950 in use).
    deploy_ps_fixture.seed_usage("swedencentral", chat=(950, 1000), embed=(0, 10_000))

    run = deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert "LLM_CHAT_MODEL_CAPACITY" in run.stderr
    assert "quota" in run.stderr.lower()
    assert "OpenAI.DataZoneStandard.gpt-5.4-mini" in run.stderr


def test_empty_usage_list_skips_quota_check_with_a_note_not_a_hard_fail(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """Bugfix 3/6 (spikes/deploy-ps-azure/README.md "Bugs found and fixed": "Empty usage list on
    a subscription/region with zero prior deployments"). Azure reports no usage entries at all
    for a region with no prior deployment -- this must be read as "can't check yet", printing a
    note and continuing, not as "0 remaining" (a false hard-fail).
    """
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    deploy_ps_fixture.seed_empty_usage("swedencentral")

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert "Note: Azure reports no usage/quota entry" in run.output
    assert "LLM_CHAT_MODEL_CAPACITY" in run.output


def test_float_usage_values_compare_correctly_via_jq_floor_not_bash_arithmetic(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """Bugfix 4/6 (spikes/deploy-ps-azure/README.md "Bugs found and fixed": "Float arithmetic in
    bash"). Azure reports `currentValue`/`limit` as floats (e.g. `950.5`) -- bash `$(( ))` cannot
    parse a decimal point and errors outright ("invalid arithmetic operator"), rather than merely
    miscomparing. `model_remaining_quota` must do the subtraction in `jq` (`floor`) instead, so a
    float-valued but *sufficient* quota still passes cleanly.
    """
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    # currentValue/limit are floats; remaining = floor(10000.0 - 50.5) = 9949 >= 200 (config's
    # LLM_CHAT_MODEL_CAPACITY) -- must not crash and must pass.
    deploy_ps_fixture.seed_usage("swedencentral", chat=(50.5, 10_000.0), embed=(0, 10_000))

    deploy_ps_fixture.run_deploy("--yes", expect=0)


def test_insufficient_embed_quota_fails_with_quota_increase_message(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    # Config's LLM_EMBED_MODEL_CAPACITY is 350 -- only 20 remaining (700 limit, 680 in use).
    deploy_ps_fixture.seed_usage("swedencentral", chat=(0, 10_000), embed=(680, 700))

    run = deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert "LLM_EMBED_MODEL_CAPACITY" in run.stderr
    assert "quota" in run.stderr.lower()


def test_sufficient_quota_at_both_models_passes(deploy_ps_fixture: DeployPsFixture) -> None:
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()  # conftest baseline's ample quota fits 200/350

    deploy_ps_fixture.run_deploy("--yes", expect=0)


def test_quota_check_runs_only_against_the_selected_region(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _usage_list_calls(deploy_ps_fixture) == [
        "cognitiveservices usage list --location swedencentral",
    ]


def test_rerun_with_existing_deployment_skips_its_own_quota_check_no_false_fail(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """Bugfix 5/6 (spikes/deploy-ps-azure/README.md "Bugs found and fixed": "Idempotent-rerun
    false 'insufficient quota'"). Once a deployment already exists, its own allocated capacity
    counts against `currentValue`, so re-requesting the same capacity on a rerun would otherwise
    read as "0 remaining" even though no *new* capacity is actually needed. `check_quota` must
    skip a model's quota check entirely once `deployment_exists` says that model's deployment is
    already there -- proven here by seeding both an existing chat deployment AND a chat quota
    that would fail the check if it ran (0 remaining), while embed's (unseeded) still fully
    enforces the real limit.
    """
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    deploy_ps_fixture.seed_existing_deployment(ACCOUNT_NAME, "gpt-5.4-mini")
    # Chat quota reads as fully exhausted (0 remaining) -- would fail if the check ran at all.
    deploy_ps_fixture.seed_usage("swedencentral", chat=(1000, 1000), embed=(0, 10_000))

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert "Chat deployment gpt-5.4-mini already exists" in run.output
    assert "skipping quota preflight" in run.output


def test_deployment_create_call_includes_the_real_model_version_from_model_list(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """Bugfix 6/6 (spikes/deploy-ps-azure/README.md "Bugs found and fixed": "Missing
    --model-version"). `az cognitiveservices account deployment create` (S9) hard-requires
    `--model-version`; `model_version` must read `.model.version` from the already-fetched
    `model list` response so S9's deployment-create call can pass it. S9 hasn't landed yet
    (deploy-ps.sh doesn't create deployments this slice), so this slice's own extension-point
    step logs the resolved versions it computes for S9 to consume -- proving `model_version`
    parses the real per-model version string, not a placeholder.
    """
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    deploy_ps_fixture.seed_model_availability(
        "swedencentral",
        chat_ga=True,
        embed_ga=True,
        chat_version="2024-11-20",
        embed_version="3",
    )

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert "chat model version 2024-11-20" in run.stderr
    assert "embed model version 3" in run.stderr
