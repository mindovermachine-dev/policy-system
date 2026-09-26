"""Authentik's own Ingress object for `scripts/deploy-ps.sh` (S5, PLAN.md §3 as superseded by
CHANGES.md row F1; #129).

F1 rejected PLAN.md §0.6's original dual-hostname/dual-DNS-label design (infeasible: one Azure
Public IP has exactly one `dnsSettings.domainNameLabel`) in favor of **path-based routing under
PS Service's existing single hostname**: a *second* `Ingress` object, sharing the exact same
`${hostname}` `ensure_ps_service_ingress` already resolves and its own Ingress already declares,
routing only the `/auth` path prefix to the `authentik` dependency chart's own server `Service`
(name confirmed by rendering `helm template charts/policy-system -f values-prod.yaml` for the
real Helm release name `policy-system` -- HELM_RELEASE_NAME -- before writing this test/the
function: `{{ include "authentik.fullname" . }}-server` resolves to
`policy-system-authentik-server`, port name `http`; see IMPL_SLICE_5.md for the full derivation).

Per CHANGES.md Appendix A, this second Ingress deliberately carries **no** `tls:` block and
**no** `cert-manager.io/cluster-issuer` annotation -- PS Service's own Ingress (same host) already
provisions the one Certificate/Secret nginx-ingress applies to every Ingress object for that host;
a second cert-manager annotation here would race a second, competing Certificate request for the
same `secretName`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# Mirrors scripts/deploy-ps.sh's own S18/S16/S17/S5(#129) literals -- hardcoded here rather than
# parsed from the script, same precedent as every other deploy_ps test module.
PS_SERVICE_NAME = "policy-system-ps-service"
AUTHENTIK_SERVICE_NAME = "policy-system-authentik-server"
CLUSTER_ISSUER_NAME = "letsencrypt-prod"
INGRESS_CLASS = "webapprouting.kubernetes.azure.com"


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()


def _extract_host(manifest: str) -> str | None:
    for line in manifest.splitlines():
        stripped = line.strip().removeprefix("- ")
        if stripped.startswith("host:"):
            return stripped.removeprefix("host:").strip()
    return None


def test_ingress_manifest_exists_and_routes_auth_path_prefix_to_authentik_service(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    manifest = deploy_ps_fixture.read_kubectl_applied("Ingress", AUTHENTIK_SERVICE_NAME)
    assert manifest is not None, "kubectl apply -f - was never called for the Authentik Ingress"
    assert "path: /auth" in manifest
    assert "pathType: Prefix" in manifest
    assert f"name: {AUTHENTIK_SERVICE_NAME}" in manifest
    assert "name: http" in manifest
    assert f"ingressClassName: {INGRESS_CLASS}" in manifest


def test_ingress_shares_ps_services_hostname_not_a_distinct_one(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    ps_manifest = deploy_ps_fixture.read_kubectl_applied("Ingress", PS_SERVICE_NAME)
    authentik_manifest = deploy_ps_fixture.read_kubectl_applied("Ingress", AUTHENTIK_SERVICE_NAME)
    assert ps_manifest is not None
    assert authentik_manifest is not None

    ps_host = _extract_host(ps_manifest)
    authentik_host = _extract_host(authentik_manifest)
    assert ps_host is not None
    assert authentik_host is not None
    assert authentik_host == ps_host, (
        "Authentik's Ingress must share PS Service's own hostname (F1: path-based routing under "
        "one hostname), not resolve a distinct one"
    )


def test_ingress_has_no_tls_block_or_cert_manager_annotation(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    manifest = deploy_ps_fixture.read_kubectl_applied("Ingress", AUTHENTIK_SERVICE_NAME)
    assert manifest is not None
    assert "tls:" not in manifest
    assert "cert-manager.io/cluster-issuer" not in manifest


def test_rerun_with_unchanged_authentik_ingress_reports_no_change(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.run_deploy("--yes", expect=0)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    output_lines = deploy_ps_fixture.read_kubectl_apply_output_log()
    authentik_ingress_lines = [
        line for line in output_lines if line == f"ingress/{AUTHENTIK_SERVICE_NAME} unchanged"
    ]
    assert authentik_ingress_lines, (
        f"no unchanged Authentik ingress apply-output line: {output_lines}"
    )
