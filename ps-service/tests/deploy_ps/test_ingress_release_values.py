"""Ingress values `scripts/deploy-ps-prod.sh` passes to the Helm release.

The release enables the chart-owned Ingress with the app-routing class, the cert-manager issuer
annotation and the TLS secret, and the public URL, the OIDC issuer and the Authentik public URL
all derive from the one hostname the script resolved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from conftest import DeployPsFixture

HELM_RELEASE_NAME = "policy-system"
PS_SERVICE_NAME = "policy-system-ps-service"
CLUSTER_ISSUER_NAME = "letsencrypt-prod"
INGRESS_CLASS = "webapprouting.kubernetes.azure.com"


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()


def _section(values: dict[str, object], key: str) -> dict[str, object]:
    return cast("dict[str, object]", values[key])


def _release_values(fixture: DeployPsFixture) -> dict[str, object]:
    values = fixture.read_helm_release_values(HELM_RELEASE_NAME)
    assert values is not None, "helm upgrade --install was never called"
    return values


def _host(url: str) -> str:
    return url.removeprefix("https://").split("/", maxsplit=1)[0]


def _upgrade_calls(fixture: DeployPsFixture) -> list[str]:
    return [
        line
        for line in fixture.read_helm_log()
        if line.startswith(f"upgrade --install {HELM_RELEASE_NAME} ")
    ]


def test_release_enables_ingress_with_class_issuer_annotation_and_tls_secret(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    ingress = _section(_release_values(deploy_ps_fixture), "ingress")
    assert ingress["enabled"] is True
    assert ingress["className"] == INGRESS_CLASS
    assert _section(ingress, "annotations") == {
        "cert-manager.io/cluster-issuer": CLUSTER_ISSUER_NAME
    }
    assert _section(ingress, "tls") == {"secretName": f"{PS_SERVICE_NAME}-tls"}


def test_public_url_issuer_and_authentik_public_url_derive_from_one_hostname(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    ps_service = _section(_release_values(deploy_ps_fixture), "psService")
    public_url = cast("str", ps_service["publicUrl"])
    issuer = cast("str", _section(ps_service, "auth")["issuer"])
    authentik_public_url = cast("str", _section(ps_service, "authentik")["publicUrl"])
    hosts = {_host(public_url), _host(issuer), _host(authentik_public_url)}
    assert len(hosts) == 1
    assert hosts.pop().endswith(".cloudapp.azure.com")


def test_release_without_ingress_values_triggers_a_new_helm_upgrade_call(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.run_deploy("--yes", expect=0)
    first = _release_values(deploy_ps_fixture)
    calls_after_first_run = len(_upgrade_calls(deploy_ps_fixture))
    deploy_ps_fixture.seed_helm_release(
        {key: value for key, value in first.items() if key != "ingress"},
        release=HELM_RELEASE_NAME,
    )

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert len(_upgrade_calls(deploy_ps_fixture)) == calls_after_first_run + 1
    assert _section(_release_values(deploy_ps_fixture), "ingress")["enabled"] is True


def test_release_with_a_changed_ingress_class_triggers_a_new_helm_upgrade_call(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.run_deploy("--yes", expect=0)
    first = _release_values(deploy_ps_fixture)
    calls_after_first_run = len(_upgrade_calls(deploy_ps_fixture))
    stale_ingress = {**_section(first, "ingress"), "className": "other"}
    deploy_ps_fixture.seed_helm_release(
        {**first, "ingress": stale_ingress}, release=HELM_RELEASE_NAME
    )

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert len(_upgrade_calls(deploy_ps_fixture)) == calls_after_first_run + 1


def test_rerun_with_unchanged_inputs_makes_no_second_helm_upgrade_call(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.run_deploy("--yes", expect=0)
    first_calls = _upgrade_calls(deploy_ps_fixture)

    second = deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _upgrade_calls(deploy_ps_fixture) == first_calls
    assert "Policy System already up to date -- no changes made." in second.stdout
