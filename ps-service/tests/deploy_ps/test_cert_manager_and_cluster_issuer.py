"""cert-manager install + wait-for-Available before `ClusterIssuer` for `scripts/deploy-ps.sh`
(AC-BI-016; PLAN.md §5/S17).

`ensure_cert_manager`/`ensure_cluster_issuer` are ported verbatim from `spikes/deploy-ps-azure/
deploy-ps.sh`'s own equivalents (spike lines 1138-1183), the trusted empirical reference. The
spike README's own confirmed finding, corrected mid-run after an original wrong assumption: the
AKS application-routing add-on does NOT bundle cert-manager -- only the managed NGINX ingress
controller. This script installs cert-manager itself, via its own published OCI chart, and waits
for its controller/webhook/cainjector deployments to report `Available` (`kubectl wait`) BEFORE
creating the `ClusterIssuer` -- a fresh install's admission webhook needs its own cert issued
before it can admit one; applying a `ClusterIssuer` immediately after a fresh install
intermittently fails webhook admission otherwise (the exact race the spike found against a real
cluster).

`test_waits_for_all_three_cert_manager_deployments_available_before_creating_cluster_issuer`
below is the exact AC-BI-016 claim, proven as a call-log ORDERING assertion (the `kubectl wait`
line must appear before the `ClusterIssuer` apply line) -- confirmed red-before-green by
temporarily moving `ensure_cert_manager`'s own `kubectl wait` call to run AFTER
`ensure_cluster_issuer` in `scripts/deploy-ps.sh`'s `main()`; see IMPL_SLICE_17.md's evidence
block.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# Mirrors scripts/deploy-ps.sh's own S17 literals -- hardcoded here rather than parsed from the
# script, same precedent as every other deploy_ps test module.
CERT_MANAGER_RELEASE_NAME = "cert-manager"
CERT_MANAGER_CHART_REF = "oci://quay.io/jetstack/charts/cert-manager"
CLUSTER_ISSUER_NAME = "letsencrypt-prod"
INGRESS_CLASS = "webapprouting.kubernetes.azure.com"


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()


def _cert_manager_upgrade_calls(fixture: DeployPsFixture) -> list[str]:
    return [
        line
        for line in fixture.read_helm_log()
        if line.startswith(f"upgrade --install {CERT_MANAGER_RELEASE_NAME} ")
    ]


def test_installs_cert_manager_via_its_own_oci_chart_when_absent(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    calls = _cert_manager_upgrade_calls(deploy_ps_fixture)
    assert len(calls) == 1
    assert CERT_MANAGER_CHART_REF in calls[0]
    assert f"--namespace {CERT_MANAGER_RELEASE_NAME}" in calls[0]


def test_waits_for_all_three_cert_manager_deployments_available_before_creating_cluster_issuer(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """The exact AC-BI-016 claim and the exact race spikes/deploy-ps-azure/README.md's own "Bugs
    found and fixed" section confirmed against a real cluster. An ordering assertion against the
    raw kubectl call log -- not merely "both calls happened somewhere" -- since a regression that
    reordered the two calls (still both present) would otherwise pass a weaker test.
    """
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    log = deploy_ps_fixture.read_kubectl_log()
    wait_index = next(
        index for index, line in enumerate(log) if line.startswith("wait --for=condition=Available")
    )
    issuer_index = next(
        index
        for index, line in enumerate(log)
        if line == f"apply -f - kind=ClusterIssuer name={CLUSTER_ISSUER_NAME}"
    )
    assert wait_index < issuer_index, (
        "the cert-manager readiness wait must be logged BEFORE the ClusterIssuer apply -- "
        f"got wait at index {wait_index}, ClusterIssuer apply at index {issuer_index}: {log}"
    )


def test_rerun_with_cert_manager_already_installed_makes_no_helm_upgrade_call(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.seed_helm_release({}, release=CERT_MANAGER_RELEASE_NAME)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _cert_manager_upgrade_calls(deploy_ps_fixture) == []


def test_cluster_issuer_uses_the_app_routing_ingress_class_for_http01(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    manifest = deploy_ps_fixture.read_kubectl_applied("ClusterIssuer", CLUSTER_ISSUER_NAME)
    assert manifest is not None, "kubectl apply -f - was never called for the ClusterIssuer"
    assert "http01:" in manifest
    assert f"ingressClassName: {INGRESS_CLASS}" in manifest
