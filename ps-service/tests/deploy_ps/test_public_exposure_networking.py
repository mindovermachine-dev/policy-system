"""Public exposure: application-routing add-on + DNS label for `scripts/deploy-ps.sh` (supports
AC-BI-015; PLAN.md §5/S16).

`ensure_approuting`/`fetch_ingress_public_ip`/`fetch_public_ip_resource_id`/`ensure_dns_label`/
`fetch_public_ip_fqdn` are ported verbatim from `spikes/deploy-ps-azure/deploy-ps.sh`'s own
equivalents (spike lines 1073-1136), the trusted empirical reference: `az aks approuting enable`
turns on the managed NGINX ingress controller, then this polls that controller's Service for its
LoadBalancer public IP, then sets Azure's own public-IP DNS label (`<label>.<region>.
cloudapp.azure.com` -- no customer-owned domain or DNS zone required, the spike's own resolved
"DNS zone for the hostname" decision). `<label>` is S5's already-computed, subscription-hash8-
derived `dns_label` -- unchanged since S5, reused here rather than a new naming scheme.

`fetch_ingress_public_ip`'s poll uses the `${VAR:-default}` test-friendly-timeout pattern S7's
`PROVIDER_REGISTRATION_WAIT_ATTEMPTS`/`_INTERVAL_SECONDS` established (scripts/deploy-ps.sh) --
`INGRESS_IP_WAIT_ATTEMPTS`/`_INTERVAL_SECONDS`.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# Mirrors DeployPsFixture.seed_subscription's default id_, and scripts/deploy-ps.sh's own
# RESOURCE_GROUP_NAME/DEFAULT_INGRESS_IP -- hardcoded here rather than imported, same precedent as
# every other deploy_ps test module.
DEFAULT_SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
RESOURCE_GROUP_NAME = "rg-policy-system"
DEFAULT_INGRESS_IP = "20.99.0.1"


def _hash8(subscription_id: str = DEFAULT_SUBSCRIPTION_ID) -> str:
    """Independently reproduce `subscription_hash8`'s first 8 hex chars (scripts/lib/deploy-llm-
    common.sh), same helper as every other deploy_ps test module's own `_hash8`.
    """
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _cluster_name(subscription_id: str = DEFAULT_SUBSCRIPTION_ID) -> str:
    """Independently reproduce `aks_cluster_name`'s deterministic naming (scripts/lib/deploy-llm-
    common.sh).
    """
    return f"aks-policy-system-{_hash8(subscription_id)}"


def _dns_label(subscription_id: str = DEFAULT_SUBSCRIPTION_ID) -> str:
    """Independently reproduce `dns_label`'s deterministic naming (scripts/lib/deploy-llm-
    common.sh) -- unchanged since S5, reused here as the value `ensure_dns_label` is expected to
    set.
    """
    return f"ps-{_hash8(subscription_id)}"


def _node_resource_group(cluster_name: str, region: str = "swedencentral") -> str:
    """Independently reproduce the fake `az aks create`'s own deterministic `nodeResourceGroup`
    shape (conftest.py's `_default_node_resource_group`) -- "swedencentral" matches
    `seed_subscription`'s own bundled baseline, which makes every region candidate ample, so
    `select_region` always picks the first configured candidate.
    """
    return f"MC_{RESOURCE_GROUP_NAME}_{cluster_name}_{region}"


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()


def _approuting_enable_calls(fixture: DeployPsFixture) -> list[str]:
    return [line for line in fixture.read_az_log() if line.startswith("aks approuting enable")]


def _public_ip_update_calls(fixture: DeployPsFixture) -> list[str]:
    return [line for line in fixture.read_az_log() if line.startswith("network public-ip update")]


def _dns_settings(record: dict[str, object]) -> dict[str, str]:
    """Narrows `record["dnsSettings"]` from `read_public_ip`'s generic `dict[str, object]` (the
    Azure public-IP record's shape is untyped JSON) down to `dict[str, str]` for this module's
    two leaf string fields -- basedpyright strict mode has no way to know the nested shape
    itself, so an explicit cast is unavoidable to index a second level in.
    """
    return cast("dict[str, str]", record["dnsSettings"])


def test_enables_approuting_addon_when_absent(deploy_ps_fixture: DeployPsFixture) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    calls = _approuting_enable_calls(deploy_ps_fixture)
    assert len(calls) == 1
    assert f"--name {_cluster_name()}" in calls[0]
    assert f"--resource-group {RESOURCE_GROUP_NAME}" in calls[0]


def test_rerun_with_addon_already_enabled_makes_no_approuting_enable_call(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    cluster_name = _cluster_name()
    deploy_ps_fixture.seed_aks_cluster(cluster_name)
    deploy_ps_fixture.seed_approuting_enabled(cluster_name)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _approuting_enable_calls(deploy_ps_fixture) == []


def test_sets_dns_label_matching_the_deterministic_hash8_derived_name(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    node_resource_group = _node_resource_group(_cluster_name())

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    record = deploy_ps_fixture.read_public_ip(node_resource_group, DEFAULT_INGRESS_IP)
    assert record is not None, "az network public-ip update was never called"
    dns_settings = _dns_settings(record)
    assert dns_settings["domainNameLabel"] == _dns_label()
    assert dns_settings["fqdn"] == f"{_dns_label()}.swedencentral.cloudapp.azure.com"


def test_rerun_with_label_already_set_makes_no_public_ip_update_call(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    cluster_name = _cluster_name()
    node_resource_group = _node_resource_group(cluster_name)
    deploy_ps_fixture.seed_aks_cluster(cluster_name, node_resource_group=node_resource_group)
    deploy_ps_fixture.seed_approuting_enabled(cluster_name)
    deploy_ps_fixture.seed_public_ip(
        node_resource_group, DEFAULT_INGRESS_IP, domain_label=_dns_label()
    )

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _public_ip_update_calls(deploy_ps_fixture) == []


def test_polls_for_the_ingress_public_ip_and_times_out_with_a_clear_message_if_never_assigned(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """`fetch_ingress_public_ip` (scripts/deploy-ps.sh) fails explicitly, naming the resource it
    was waiting on, rather than a bash `set -u`-triggered crash or a silent empty hostname
    downstream -- the test-overridable `INGRESS_IP_WAIT_ATTEMPTS`/`_INTERVAL_SECONDS` env vars
    (same `${VAR:-default}` pattern S7's provider-registration poll established) let this run in
    milliseconds instead of the real ~5-minute worst case.
    """
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.seed_no_ingress_ip()

    run = deploy_ps_fixture.run_deploy(
        "--yes",
        expect=1,
        extra_env={
            "INGRESS_IP_WAIT_ATTEMPTS": "2",
            "INGRESS_IP_WAIT_INTERVAL_SECONDS": "0",
        },
    )

    assert "Timed out waiting for the app-routing ingress controller" in run.output
