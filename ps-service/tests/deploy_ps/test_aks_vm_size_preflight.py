"""AKS node VM-size allowlist + vCPU quota preflight for `scripts/deploy-ps.sh` (AC-BI-011;
PLAN.md §0.6/§5/S12).

Unlike every other slice in this plan, this AC has no spike-proven mechanism to port --
`spikes/deploy-ps-azure/`'s own README documents it as an undischarged manual step ("Manual steps
a real installer needs" #3). `check_aks_vm_size`, `vm_size_allowed`, and
`vm_family_quota_sufficient` are this run's own new design against documented Azure CLI surfaces
only (`az vm list-skus`, `az vm list-usage`), not yet empirically re-verified against a live
Azure subscription. Treat this module's coverage as weaker evidence than the ported-bugfix
modules (e.g. test_quota_check.py) until that re-verification happens.

Interim scope note (mirrors IMPL_SLICE_7.md's own precedent): S13 (`az aks create`) doesn't exist
yet at this slice, so the "no AKS-related call on failure" test below cannot assert against a
real `aks create` call the way a full end-to-end proof eventually will. It instead asserts the
only thing provable today: the fake `az` log contains no `aks`-prefixed invocation at all after a
failing preflight run -- `check_aks_vm_size` runs before anything AKS-related is even callable at
this slice. Once S13 lands and adds a real `az aks create` call downstream of this preflight,
strengthen this to a call-log ordering check the way test_provider_registration.py's own note
describes for its analogous case.

`seed_subscription`'s baseline already seeds AKS_NODE_VM_SIZE as unrestricted with ample vCPU
quota across every configured candidate region (conftest.py), so every test below only needs to
override the one region/piece its scenario cares about -- same bundling convention as every other
S5-S11 test module in this package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# select_region picks the first LLM_REGION_CANDIDATES entry whose models are Generally
# Available -- with seed_subscription's ample baseline, that is always swedencentral (same
# convention every other S8+ test module in this package relies on, e.g. test_quota_check.py).
SELECTED_REGION = "swedencentral"


def _aks_related_calls(fixture: DeployPsFixture) -> list[str]:
    return [line for line in fixture.read_az_log() if line.startswith("aks ")]


def _vm_list_usage_calls(fixture: DeployPsFixture) -> list[str]:
    return [line for line in fixture.read_az_log() if line.startswith("vm list-usage")]


def test_size_restricted_for_subscription_fails_with_allowlist_message_before_aks_create(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """A "NotAvailableForSubscription" restriction on AKS_NODE_VM_SIZE hard-stops with the actual
    reason code shown -- never a generic "not allowed" message (this AC's literal wording) -- and
    never reaches the quota check (fail-fast on the allowlist check alone, regardless of quota).
    """
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    deploy_ps_fixture.seed_vm_skus(
        SELECTED_REGION, restricted=True, restriction_reason="NotAvailableForSubscription"
    )

    run = deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert "Standard_D4as_v7" in run.stderr
    assert SELECTED_REGION in run.stderr
    assert "NotAvailableForSubscription" in run.stderr
    # Fails on the allowlist check alone -- never reaches the quota check.
    assert _vm_list_usage_calls(deploy_ps_fixture) == []
    assert _aks_related_calls(deploy_ps_fixture) == []


def test_size_allowed_but_insufficient_family_quota_fails_with_quota_message_before_aks_create(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """Size is allowed, but the family's remaining vCPU quota (limit minus current usage) falls
    short of AKS_NODE_COUNT x AKS_NODE_VM_SIZE_VCPUS (2 x 4 = 8) -- hard-stops with the actual
    limit/current/needed/remaining numbers shown, mirroring validate_model_quota's own
    message-style convention (explicit numbers, never a generic message).
    """
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    # Needs 8 vCPUs (2 nodes x 4 vCPUs); only 5 remaining (limit 10, current 5).
    deploy_ps_fixture.seed_vm_usage(SELECTED_REGION, current=5, limit=10)

    run = deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert "Standard_D4as_v7" in run.stderr
    assert "standardDASv7Family" in run.stderr
    assert SELECTED_REGION in run.stderr
    assert "need 8 vCPUs" in run.stderr
    assert "only 5 remaining" in run.stderr
    assert "quota" in run.stderr.lower()
    assert _aks_related_calls(deploy_ps_fixture) == []


def test_size_allowed_and_quota_sufficient_passes_cleanly(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """Size unrestricted, remaining quota exactly meets the 8-vCPU requirement (limit 10, current
    2 -- remaining 8) -- passes, and the preflight step is logged.
    """
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    deploy_ps_fixture.seed_vm_usage(SELECTED_REGION, current=2, limit=10)

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert f"Checking AKS node VM size and quota in {SELECTED_REGION}" in run.stderr


def test_neither_check_makes_an_aks_related_az_call_on_failure(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """Whether the preflight fails on the allowlist or the quota check, no AKS-related `az` call
    is ever made -- S13 (`az aks create`) doesn't exist yet, so this is scoped to what's actually
    callable at this slice (see this module's own docstring): the fake `az` log has zero
    `aks`-prefixed entries after either failure mode.
    """
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    deploy_ps_fixture.seed_vm_skus(SELECTED_REGION, restricted=True)

    deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert _aks_related_calls(deploy_ps_fixture) == []
