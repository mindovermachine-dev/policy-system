"""AKS cluster creation with AAD+RBAC+hardening flags for `scripts/deploy-ps.sh` (AC-BI-006,
AC-BI-012; PLAN.md §5/S13).

`ensure_aks_cluster`'s exact `az aks create` flag set -- AAD-integrated auth, Azure RBAC
authorization, disabled local accounts, and Azure CNI network policy -- is ported verbatim from
`spikes/deploy-ps-azure/deploy-ps.sh`'s own `ensure_aks_cluster` (spike lines 939-950), the
trusted empirical reference this whole plan cites. The headline regression this module guards:
`spikes/deploy-ps-azure/README.md`'s own "Bugs found and fixed" section, confirmed against a
real subscription during the spike run, states verbatim: "Missing `--network-plugin` (new,
mine) -- `--network-policy azure` requires an explicit `--network-plugin azure`; omitting it
fails `az aks create` outright. Fixed." `test_creates_cluster_with_network_plugin_azure_and_
network_policy_azure_together` below asserts both flags land in the SAME `aks create` call, not
merely that each flag appears somewhere in the log.

`grant_aks_rbac_access` grants the deploying identity -- reusing `fetch_signed_in_user_object_
id`'s result, the same resolver S9's Key Vault access grant already uses (PLAN.md §0.5, no
service-principal branch) -- the "Azure Kubernetes Service RBAC Cluster Admin" role at the
cluster's own resource-ID scope, never the subscription root: without --enable-azure-rbac
authorizing the deploying identity there, every kubectl/helm call in S14+ is rejected regardless
of the operator's subscription-level Owner/Contributor role S6's rbac_preflight already checked
-- a different scope for a different concern.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# DeployPsFixture.seed_subscription's default id_/user_id.
SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
USER_OBJECT_ID = "22222222-3333-4444-5555-666666666666"
RESOURCE_GROUP = "rg-policy-system"
AKS_RBAC_ADMIN_ROLE = "Azure Kubernetes Service RBAC Cluster Admin"


def _hash8(subscription_id: str) -> str:
    """Independently reproduce `subscription_hash8`'s first 8 hex chars (scripts/lib/deploy-llm-
    common.sh), same helper as test_llm_provisioning.py's/test_confirmation_table.py's own
    `_hash8`.
    """
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _cluster_name(subscription_id: str = SUBSCRIPTION_ID) -> str:
    """Independently reproduce `aks_cluster_name`'s deterministic naming
    (scripts/lib/deploy-llm-common.sh).
    """
    return f"aks-policy-system-{_hash8(subscription_id)}"


def _cluster_resource_id(
    cluster_name: str,
    *,
    subscription_id: str = SUBSCRIPTION_ID,
    resource_group: str = RESOURCE_GROUP,
) -> str:
    """Independently reproduce the fake `az aks create`'s own deterministic resource-ID shape
    (conftest.py's `_aks_cluster_resource_id`) -- a real Azure AKS resource ID's actual
    structure.
    """
    return (
        f"/subscriptions/{subscription_id}/resourcegroups/{resource_group}"
        f"/providers/Microsoft.ContainerService/managedClusters/{cluster_name}"
    )


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()  # bundles Owner role + S8/S12 ample-headroom baseline


def _aks_create_calls(fixture: DeployPsFixture) -> list[str]:
    return [line for line in fixture.read_az_log() if line.startswith("aks create")]


def _role_assignment_create_calls(fixture: DeployPsFixture) -> list[str]:
    return [line for line in fixture.read_az_log() if line.startswith("role assignment create")]


def test_creates_cluster_with_network_plugin_azure_and_network_policy_azure_together(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """The exact AC-BI-012 regression: `--network-plugin azure` MUST be present alongside
    `--network-policy azure` in the SAME `az aks create` call -- omitting the former fails the
    real `az aks create` outright (spike README, quoted in `scripts/deploy-ps.sh`'s own
    `ensure_aks_cluster` comment). A test that only checked each flag appeared *somewhere* in the
    log would not catch a regression that moved one of them to a different, unrelated call.
    """
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    calls = _aks_create_calls(deploy_ps_fixture)
    assert len(calls) == 1
    assert "--network-plugin azure" in calls[0]
    assert "--network-policy azure" in calls[0]


def test_creates_cluster_with_aad_rbac_and_disable_local_accounts_flags(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """AC-BI-006: AAD-integrated auth, Azure RBAC authorization, and local (cert-based) accounts
    disabled -- all three hardening flags in the same `aks create` call.
    """
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    calls = _aks_create_calls(deploy_ps_fixture)
    assert len(calls) == 1
    assert "--enable-aad" in calls[0]
    assert "--enable-azure-rbac" in calls[0]
    assert "--disable-local-accounts" in calls[0]


def test_rerun_with_existing_cluster_makes_no_aks_create_call(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.seed_aks_cluster(_cluster_name())

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _aks_create_calls(deploy_ps_fixture) == []


def test_grants_aks_rbac_cluster_admin_role_scoped_to_the_cluster_only(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """The AKS RBAC grant's `--scope` is the cluster's own resource ID -- never the subscription
    root S6's `rbac_preflight` already checked (a different scope for a different concern) --
    and the role is the built-in "Azure Kubernetes Service RBAC Cluster Admin", granted to the
    signed-in user's object id (reusing `fetch_signed_in_user_object_id`'s result, PLAN.md §0.5).
    """
    _seed(deploy_ps_fixture)
    cluster_id = _cluster_resource_id(_cluster_name())

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    calls = _role_assignment_create_calls(deploy_ps_fixture)
    assert len(calls) == 1
    tokens = calls[0].split()
    scope_value = tokens[tokens.index("--scope") + 1]
    # The scope is exactly the cluster's own resource ID -- not merely a call whose --scope
    # value happens to start with "/subscriptions/<id>" (every valid Azure resource ID under
    # this subscription does), and specifically not the bare subscription-root scope S6's
    # rbac_preflight already checked.
    assert scope_value == cluster_id
    subscription_root_scope = f"/subscriptions/{SUBSCRIPTION_ID}"
    assert scope_value != subscription_root_scope
    assert f"--role {AKS_RBAC_ADMIN_ROLE}" in calls[0]
    assert f"--assignee {USER_OBJECT_ID}" in calls[0]


def test_rerun_with_role_already_granted_makes_no_role_assignment_create_call(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    cluster_name = _cluster_name()
    cluster_id = deploy_ps_fixture.seed_aks_cluster(cluster_name)
    deploy_ps_fixture.seed_aks_rbac_granted(cluster_id)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _role_assignment_create_calls(deploy_ps_fixture) == []
