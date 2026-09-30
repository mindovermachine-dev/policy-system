"""Authentik's own Ingress object for `scripts/deploy-ps-prod.sh` (S5, PLAN.md §3 as superseded by
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

from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# Mirrors scripts/deploy-ps-prod.sh's own S18/S16/S17/S5(#129) literals -- hardcoded here
# rather than parsed from the script, same precedent as every other deploy_ps test module.
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


# Issue #165: the public Ingress routes ONLY what an end user's browser and ps-cli need to log in
# and enroll (the 8 issue paths). The admin UI (`/auth/if/admin/`), the admin API and the
# invitation API stay cluster-internal (PS Service reaches the latter in-cluster, OD-1 = B).
ALLOWED_PATHS = (
    "/auth/application/o/",
    "/auth/device",
    "/auth/flows/-/default/",
    "/auth/if/flow/",
    "/auth/api/v3/flows/executor/",
    "/auth/api/v3/root/config/",
    "/auth/api/v3/core/brands/current/",
    "/auth/static/",
)


def _paths(manifest: str) -> list[str]:
    return [
        line.strip().removeprefix("- ").removeprefix("path:").strip()
        for line in manifest.splitlines()
        if line.strip().removeprefix("- ").startswith("path:")
    ]


def test_ingress_manifest_exists_and_routes_exactly_the_allowlisted_paths_to_authentik(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    manifest = deploy_ps_fixture.read_kubectl_applied("Ingress", AUTHENTIK_SERVICE_NAME)
    assert manifest is not None, "kubectl apply -f - was never called for the Authentik Ingress"
    assert _paths(manifest) == list(ALLOWED_PATHS)
    assert manifest.count("pathType: Prefix") == len(ALLOWED_PATHS)
    assert manifest.count(f"name: {AUTHENTIK_SERVICE_NAME}") == len(ALLOWED_PATHS) + 1
    assert manifest.count("name: http") == len(ALLOWED_PATHS)
    assert f"ingressClassName: {INGRESS_CLASS}" in manifest


def test_ingress_manifest_is_valid_yaml_with_one_backend_per_allowlisted_path(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """The rendered manifest must parse (a stray heredoc terminator would corrupt it) and each
    allowlisted path must point at Authentik's `http` port.
    """
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    manifest = deploy_ps_fixture.read_kubectl_applied("Ingress", AUTHENTIK_SERVICE_NAME)
    assert manifest is not None
    document = cast("dict[str, Any]", yaml.safe_load(manifest))
    paths = document["spec"]["rules"][0]["http"]["paths"]
    assert [p["path"] for p in paths] == list(ALLOWED_PATHS)
    for entry in paths:
        assert entry["pathType"] == "Prefix"
        assert entry["backend"]["service"] == {
            "name": AUTHENTIK_SERVICE_NAME,
            "port": {"name": "http"},
        }


def test_ingress_never_routes_the_admin_ui_admin_api_or_a_bare_auth_prefix(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """AC-BI-019 (manifest half): nothing that would serve `/auth/if/admin/` or the admin,
    core-users or invitation APIs -- and no bare `/auth` or `/auth/api/v3/` catch-all.
    """
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    manifest = deploy_ps_fixture.read_kubectl_applied("Ingress", AUTHENTIK_SERVICE_NAME)
    assert manifest is not None
    paths = _paths(manifest)
    for forbidden in (
        "/auth",
        "/auth/",
        "/auth/if",
        "/auth/if/",
        "/auth/if/admin/",
        "/auth/if/user/",
        "/auth/api/v3/",
        "/auth/api/v3/core/",
        "/auth/api/v3/core/users/",
        "/auth/api/v3/admin/",
        "/auth/api/v3/stages/invitation/invitations/",
    ):
        assert forbidden not in paths, forbidden
    assert not any(p.startswith(("/auth/if/admin", "/auth/api/v3/core/users")) for p in paths)


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
