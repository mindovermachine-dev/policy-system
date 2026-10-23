"""Adoption of existing Ingresses into the Helm release by `scripts/deploy-ps-prod.sh`.

Before `helm upgrade`, an Ingress that exists without Helm ownership gets the ownership
annotations and label so the release manages it in place. Nothing else is touched: no Ingress is
deleted or applied, and an Ingress already owned by this release or absent is left alone. An
Ingress owned by a different release stops the run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

HELM_RELEASE_NAME = "policy-system"
PS_SERVICE_NAME = "policy-system-ps-service"
AUTHENTIK_SERVICE_NAME = "policy-system-authentik-server"
INGRESS_NAMES = (PS_SERVICE_NAME, AUTHENTIK_SERVICE_NAME)
NAMESPACE = "default"
MANAGED_BY = "app.kubernetes.io/managed-by"
RELEASE_NAME_KEY = "meta.helm.sh/release-name"
RELEASE_NAMESPACE_KEY = "meta.helm.sh/release-namespace"
ISSUER_KEY = "cert-manager.io/cluster-issuer"
OWNED = {
    "labels": {MANAGED_BY: "Helm"},
    "annotations": {RELEASE_NAME_KEY: HELM_RELEASE_NAME, RELEASE_NAMESPACE_KEY: NAMESPACE},
}


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()


def _writes(fixture: DeployPsFixture) -> list[str]:
    return [
        line
        for line in fixture.read_kubectl_log()
        if line.startswith(("annotate ingress", "label ingress"))
    ]


def test_script_applied_ingress_gets_only_helm_ownership_metadata_before_upgrade(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    for name in INGRESS_NAMES:
        deploy_ps_fixture.seed_ingress(
            name, labels={"x": "y"}, annotations={ISSUER_KEY: "letsencrypt-prod"}
        )

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    events = deploy_ps_fixture.read_event_log()
    upgrade_index = next(
        index
        for index, line in enumerate(events)
        if line.startswith(f"helm upgrade --install {HELM_RELEASE_NAME} ")
    )
    for name in INGRESS_NAMES:
        annotate = (
            f"kubectl annotate ingress {name} {RELEASE_NAME_KEY}={HELM_RELEASE_NAME} "
            f"{RELEASE_NAMESPACE_KEY}={NAMESPACE} --overwrite"
        )
        label = f"kubectl label ingress {name} {MANAGED_BY}=Helm --overwrite"
        assert events.index(annotate) < upgrade_index
        assert events.index(label) < upgrade_index
        assert deploy_ps_fixture.read_ingress_metadata(name) == {
            "labels": {"x": "y", MANAGED_BY: "Helm"},
            "annotations": {
                ISSUER_KEY: "letsencrypt-prod",
                RELEASE_NAME_KEY: HELM_RELEASE_NAME,
                RELEASE_NAMESPACE_KEY: NAMESPACE,
            },
        }


def test_ingress_already_owned_by_this_release_is_left_alone(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    for name in INGRESS_NAMES:
        deploy_ps_fixture.seed_ingress(
            name, labels=OWNED["labels"], annotations=OWNED["annotations"]
        )

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _writes(deploy_ps_fixture) == []
    for name in INGRESS_NAMES:
        assert deploy_ps_fixture.read_ingress_metadata(name) == OWNED


def test_partial_helm_metadata_is_completed(deploy_ps_fixture: DeployPsFixture) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.seed_ingress(PS_SERVICE_NAME, labels=OWNED["labels"])
    deploy_ps_fixture.seed_ingress(AUTHENTIK_SERVICE_NAME, annotations=OWNED["annotations"])

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    for name in INGRESS_NAMES:
        assert deploy_ps_fixture.read_ingress_metadata(name) == OWNED


def test_missing_ingress_is_left_alone(deploy_ps_fixture: DeployPsFixture) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _writes(deploy_ps_fixture) == []
    assert all(deploy_ps_fixture.read_ingress_metadata(name) is None for name in INGRESS_NAMES)


def test_kubectl_failure_other_than_not_found_stops_the_run_before_the_upgrade(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    for name in INGRESS_NAMES:
        deploy_ps_fixture.seed_ingress(name, labels={"x": "y"})

    run = deploy_ps_fixture.run_deploy(
        "--yes",
        expect=1,
        extra_env={"PS_TEST_KUBECTL_GET_INGRESS_ERROR": "Forbidden: cannot get ingresses"},
    )

    assert PS_SERVICE_NAME in run.stderr
    assert "Forbidden: cannot get ingresses" in run.stderr
    assert "Fix:" in run.stderr
    assert _writes(deploy_ps_fixture) == []
    for name in INGRESS_NAMES:
        assert deploy_ps_fixture.read_ingress_metadata(name) == {
            "labels": {"x": "y"},
            "annotations": {},
        }
    assert not [
        line
        for line in deploy_ps_fixture.read_helm_log()
        if line.startswith(f"upgrade --install {HELM_RELEASE_NAME} ")
    ]


def test_ingress_owned_by_another_release_stops_the_run_without_writes(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    foreign = {
        "labels": {MANAGED_BY: "Helm"},
        "annotations": {RELEASE_NAME_KEY: "other-release", RELEASE_NAMESPACE_KEY: NAMESPACE},
    }
    deploy_ps_fixture.seed_ingress(
        PS_SERVICE_NAME, labels=foreign["labels"], annotations=foreign["annotations"]
    )
    deploy_ps_fixture.seed_ingress(AUTHENTIK_SERVICE_NAME)

    run = deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert PS_SERVICE_NAME in run.stderr
    assert "other-release" in run.stderr
    assert _writes(deploy_ps_fixture) == []
    assert deploy_ps_fixture.read_ingress_metadata(PS_SERVICE_NAME) == foreign
    assert deploy_ps_fixture.read_ingress_metadata(AUTHENTIK_SERVICE_NAME) == {
        "labels": {},
        "annotations": {},
    }
    assert not [
        line
        for line in deploy_ps_fixture.read_helm_log()
        if line.startswith(f"upgrade --install {HELM_RELEASE_NAME} ")
    ]


def test_adoption_never_deletes_or_applies_an_ingress(deploy_ps_fixture: DeployPsFixture) -> None:
    _seed(deploy_ps_fixture)
    for name in INGRESS_NAMES:
        deploy_ps_fixture.seed_ingress(name)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert not [
        line
        for line in deploy_ps_fixture.read_kubectl_log()
        if line.startswith("delete") or "kind=Ingress" in line
    ]
    assert not [line for line in deploy_ps_fixture.read_kubectl_log() if "replace" in line]


def test_rerun_after_adoption_does_no_adoption_writes(deploy_ps_fixture: DeployPsFixture) -> None:
    _seed(deploy_ps_fixture)
    for name in INGRESS_NAMES:
        deploy_ps_fixture.seed_ingress(name)
    deploy_ps_fixture.run_deploy("--yes", expect=0)
    writes_after_first_run = len(_writes(deploy_ps_fixture))
    assert writes_after_first_run > 0

    second = deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert len(_writes(deploy_ps_fixture)) == writes_after_first_run
    assert "Policy System already up to date -- no changes made." in second.stdout
