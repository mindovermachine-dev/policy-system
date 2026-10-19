"""Authentik's own Ingress object for `scripts/deploy-ps-prod.sh`.

Routing is **path-based under PS Service's existing single hostname**, because one Azure
Public IP has exactly one `dnsSettings.domainNameLabel`, which rules out a dual-hostname design:
a *second* `Ingress` object, sharing the exact same
`${hostname}` `ensure_ps_service_ingress` already resolves and its own Ingress already declares,
routing only the `/auth` path prefix to the `authentik` dependency chart's own server `Service`
(name confirmed by rendering `helm template charts/policy-system -f values-prod.yaml` for the
real Helm release name `policy-system` -- HELM_RELEASE_NAME -- before writing this test/the
function: `{{ include "authentik.fullname" . }}-server` resolves to
`policy-system-authentik-server`, port name `http`).

This second Ingress deliberately carries **no** `tls:` block and
**no** `cert-manager.io/cluster-issuer` annotation -- PS Service's own Ingress (same host) already
provisions the one Certificate/Secret nginx-ingress applies to every Ingress object for that host;
a second cert-manager annotation here would race a second, competing Certificate request for the
same `secretName`.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from conftest import DeployPsFixture

REPO_ROOT = Path(__file__).resolve().parents[3]

# Mirrors scripts/deploy-ps-prod.sh's own literals -- hardcoded here
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


CHART_DIR = REPO_ROOT / "charts" / "policy-system"
PATHS_FILE = CHART_DIR / "files" / "authentik-public-paths.txt"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy-ps-prod.sh"
BUNDLE_VERSION = "9.9.9"


def _file_paths(path_file: Path) -> tuple[str, ...]:
    """The path list as the chart defines it: one path per line, `#` lines are comments."""
    lines = (line.strip() for line in path_file.read_text(encoding="utf-8").splitlines())
    return tuple(line for line in lines if line and not line.startswith("#"))


# The public Ingress routes ONLY what an end user's browser and ps-cli need to log in and
# enroll. The admin UI (`/auth/if/admin/`), the admin API and the invitation API stay
# cluster-internal (PS Service reaches the latter in-cluster).
ALLOWED_PATHS = _file_paths(PATHS_FILE)


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
    """Nothing that would serve `/auth/if/admin/` or the admin,
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


def test_prod_ingress_paths_equal_the_chart_shared_file(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    manifest = deploy_ps_fixture.read_kubectl_applied("Ingress", AUTHENTIK_SERVICE_NAME)
    assert manifest is not None
    assert len(ALLOWED_PATHS) >= 1
    assert tuple(_paths(manifest)) == ALLOWED_PATHS


def test_prod_script_has_no_path_literals() -> None:
    script_lines = {line.strip() for line in DEPLOY_SCRIPT.read_text().splitlines()}
    for path in ALLOWED_PATHS:
        assert path not in script_lines, path
    assert "AUTHENTIK_PUBLIC_PATHS=(" not in DEPLOY_SCRIPT.read_text(encoding="utf-8")


def test_a_path_added_to_the_chart_file_reaches_the_applied_ingress(
    deploy_ps_fixture: DeployPsFixture, tmp_path: Path
) -> None:
    chart_copy = tmp_path / "chart-with-extra-path"
    shutil.copytree(CHART_DIR, chart_copy)
    extra = "/auth/extra-public-path/"
    shared = chart_copy / "files" / "authentik-public-paths.txt"
    shared.write_text(shared.read_text(encoding="utf-8") + extra + "\n", encoding="utf-8")
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy(
        "--yes", expect=0, extra_env={"PS_TEST_CHART_DIR": str(chart_copy)}
    )

    manifest = deploy_ps_fixture.read_kubectl_applied("Ingress", AUTHENTIK_SERVICE_NAME)
    assert manifest is not None
    assert _paths(manifest) == [*ALLOWED_PATHS, extra]


def test_the_chart_is_fetched_at_the_bundle_version_before_the_ingress_is_applied(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    (deploy_ps_fixture.root / "VERSION").write_text(f"{BUNDLE_VERSION}\n", encoding="utf-8")
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    pulls = [line for line in deploy_ps_fixture.read_helm_log() if line.startswith("pull ")]
    assert len(pulls) == 1
    assert f"--version {BUNDLE_VERSION}" in pulls[0]
    installs = [
        line
        for line in deploy_ps_fixture.read_helm_log()
        if line.startswith("upgrade --install policy-system ")
    ]
    assert installs
    assert all(f"--version {BUNDLE_VERSION}" in line for line in installs)


def test_a_local_chart_path_is_read_without_fetching(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0, extra_env={"PS_CHART_REF": str(CHART_DIR)})

    manifest = deploy_ps_fixture.read_kubectl_applied("Ingress", AUTHENTIK_SERVICE_NAME)
    assert manifest is not None
    assert tuple(_paths(manifest)) == ALLOWED_PATHS
    assert not [line for line in deploy_ps_fixture.read_helm_log() if line.startswith("pull ")]


def test_script_chart_version_skew_aborts_before_ingress_apply(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """A chart older than the script has no shared path file: stop with a Fix naming the chart
    version, before the Authentik Ingress is touched.
    """
    (deploy_ps_fixture.root / "VERSION").write_text(f"{BUNDLE_VERSION}\n", encoding="utf-8")
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(
        "--yes", expect=1, extra_env={"PS_TEST_HELM_PULL_OLD_CHART": "1"}
    )

    assert "Fix:" in run.output
    assert BUNDLE_VERSION in run.output
    assert deploy_ps_fixture.read_kubectl_applied("Ingress", AUTHENTIK_SERVICE_NAME) is None
    assert not [
        line
        for line in deploy_ps_fixture.read_helm_log()
        if line.startswith("upgrade --install policy-system ")
    ]


def test_a_failed_chart_fetch_aborts_before_ingress_apply(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--yes", expect=1, extra_env={"PS_TEST_HELM_PULL_FAIL": "1"})

    assert "Fix:" in run.output
    assert deploy_ps_fixture.read_kubectl_applied("Ingress", AUTHENTIK_SERVICE_NAME) is None
