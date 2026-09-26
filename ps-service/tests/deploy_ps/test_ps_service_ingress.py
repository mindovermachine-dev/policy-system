"""PS Service Ingress for `scripts/deploy-ps.sh` (AC-BI-015 completion; PLAN.md §5/S18).

`ensure_ps_service_ingress` is ported verbatim from `spikes/deploy-ps-azure/deploy-ps.sh`'s own
equivalent (the trusted empirical reference): a TLS-terminated `Ingress` targeting the chart's
own rendered PS Service `Service` (name/port confirmed by reading `charts/policy-system/
templates/ps-service-service.yaml` before writing this slice: `{{ include
"policy-system.fullname" . }}-ps-service`, port name `http`), fronted by S17's `ClusterIssuer`
(the `cert-manager.io/cluster-issuer` annotation) and S16's app-routing ingress class. `main()`'s
closing `print_provisioning_summary` (also new in this slice) prints the resulting HTTPS URL and
never a secret value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# Mirrors scripts/deploy-ps.sh's own S18/S16/S17 literals -- hardcoded here rather than parsed
# from the script, same precedent as every other deploy_ps test module.
PS_SERVICE_NAME = "policy-system-ps-service"
CLUSTER_ISSUER_NAME = "letsencrypt-prod"
INGRESS_CLASS = "webapprouting.kubernetes.azure.com"

# The fake `az cognitiveservices account create`'s own default initial key values (conftest's
# FAKE_AZ_SCRIPT, PS_TEST_AZ_INITIAL_KEY1/2 defaults) -- the exact secret material a leaking
# summary line would expose.
FIXTURE_KEY1 = "FAKE-KEY-1-INITIAL"
FIXTURE_KEY2 = "FAKE-KEY-2-INITIAL"


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()


def test_ingress_manifest_has_cert_manager_cluster_issuer_annotation_and_tls_secret_name(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    manifest = deploy_ps_fixture.read_kubectl_applied("Ingress", PS_SERVICE_NAME)
    assert manifest is not None, "kubectl apply -f - was never called for the PS Service Ingress"
    assert f"cert-manager.io/cluster-issuer: {CLUSTER_ISSUER_NAME}" in manifest
    assert "tls:" in manifest
    assert f"secretName: {PS_SERVICE_NAME}-tls" in manifest
    assert f"ingressClassName: {INGRESS_CLASS}" in manifest


def test_ingress_targets_the_correct_ps_service_name_and_http_port(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    manifest = deploy_ps_fixture.read_kubectl_applied("Ingress", PS_SERVICE_NAME)
    assert manifest is not None
    assert f"name: {PS_SERVICE_NAME}" in manifest
    assert "port:" in manifest
    assert "name: http" in manifest


def test_rerun_with_unchanged_ingress_reports_no_change_via_apply_unchanged_output(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.run_deploy("--yes", expect=0)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    output_lines = deploy_ps_fixture.read_kubectl_apply_output_log()
    # Filtered to this Ingress's own name specifically, not "the last ingress/ line overall" --
    # S5 (#129/CHANGES.md row F1) added a second Ingress apply (Authentik's, ensure_authentik_
    # ingress) right after this one in main(), so PS Service's own line is no longer necessarily
    # last.
    ps_service_ingress_lines = [
        line for line in output_lines if line.startswith(f"ingress/{PS_SERVICE_NAME} ")
    ]
    assert ps_service_ingress_lines, (
        f"no PS Service ingress apply-output line recorded: {output_lines}"
    )
    assert ps_service_ingress_lines[-1] == f"ingress/{PS_SERVICE_NAME} unchanged"


def test_summary_line_prints_the_https_url_never_a_secret_value(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert "PS Service: https://" in run.stdout
    assert FIXTURE_KEY1 not in run.output
    assert FIXTURE_KEY2 not in run.output
