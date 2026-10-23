"""Re-run behaviour and call ordering of `scripts/deploy-ps-prod.sh` around the chart Ingress.

An unchanged re-run reports nothing to do. On a first install cert-manager and the ClusterIssuer
are in place before the single `helm upgrade` that renders the Ingresses, existing Ingresses are
patched with Helm ownership metadata (never deleted or applied) before that upgrade, and the
upgrade keeps its `--wait --wait-for-jobs --timeout 10m` flags. These tests exercise the script
against fake `kubectl` and `helm`; they establish call ordering and metadata only, not live
certificate or rollout behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "deploy-ps-prod.sh"
HELM_RELEASE_NAME = "policy-system"
UP_TO_DATE = "Policy System already up to date -- no changes made."
RELEASE_UPGRADE_PREFIX = f"helm upgrade --install {HELM_RELEASE_NAME} "
WAIT_FLAGS = "--wait --wait-for-jobs --timeout 10m"


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()


def _ownership_writes(fixture: DeployPsFixture) -> list[str]:
    return [
        line
        for line in fixture.read_kubectl_log()
        if line.startswith(("annotate ingress", "label ingress"))
    ]


def _script_constant(name: str) -> str:
    pattern = rf'^readonly {name}="([^"$]*)"$'
    match = re.search(pattern, SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None, f"{name} is not a literal constant in the script"
    return match.group(1)


def test_rerun_with_no_changes_reports_up_to_date_and_does_no_adoption(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.run_deploy("--yes", expect=0)
    events_after_first_run = len(deploy_ps_fixture.read_event_log())
    writes_after_first_run = len(_ownership_writes(deploy_ps_fixture))

    second = deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert UP_TO_DATE in second.stdout
    assert len(_ownership_writes(deploy_ps_fixture)) == writes_after_first_run
    new_events = deploy_ps_fixture.read_event_log()[events_after_first_run:]
    assert not [line for line in new_events if line.startswith(RELEASE_UPGRADE_PREFIX)]
    assert not [line for line in new_events if " annotate " in line or " label " in line]


def test_helm_wait_timeout_is_retained_and_cert_manager_precedes_the_release(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    for name in (_script_constant_name("PS"), _script_constant_name("AUTHENTIK")):
        deploy_ps_fixture.seed_ingress(name)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    events = deploy_ps_fixture.read_event_log()
    install = next(
        i
        for i, line in enumerate(events)
        if line.startswith("helm upgrade --install cert-manager ")
    )
    wait = next(
        i
        for i, line in enumerate(events)
        if line.startswith("kubectl wait --for=condition=Available")
    )
    issuer = next(i for i, line in enumerate(events) if i > wait and line == "kubectl apply -f -")
    adopt = next(i for i, line in enumerate(events) if line.startswith("kubectl annotate ingress "))
    release = next(i for i, line in enumerate(events) if line.startswith(RELEASE_UPGRADE_PREFIX))
    assert install < wait < issuer < release
    assert adopt < release
    assert WAIT_FLAGS in events[release]
    assert not [line for line in deploy_ps_fixture.read_kubectl_log() if "kind=Ingress" in line]
    assert not [line for line in deploy_ps_fixture.read_kubectl_log() if line.startswith("delete")]


def _script_constant_name(which: str) -> str:
    release = _script_constant("HELM_RELEASE_NAME")
    return f"{release}-ps-service" if which == "PS" else f"{release}-authentik-server"


def test_script_constants_match_the_chart_contract() -> None:
    assert _script_constant("HELM_RELEASE_NAME") == HELM_RELEASE_NAME
    assert _script_constant("INGRESS_CLASS") == "webapprouting.kubernetes.azure.com"
    assert _script_constant("CLUSTER_ISSUER_NAME") == "letsencrypt-prod"
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'readonly PS_SERVICE_NAME="${HELM_RELEASE_NAME}-ps-service"' in text
    assert 'readonly AUTHENTIK_SERVICE_NAME="${HELM_RELEASE_NAME}-authentik-server"' in text
    assert 'ingress.tls.secretName="${PS_SERVICE_NAME}-tls"' in text
