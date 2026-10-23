"""The deploy script does not own Ingress manifests: the chart renders them.

cert-manager and the ClusterIssuer stay in the script (cluster-level resources), and the script
holds no Ingress manifest, body-size annotation or Authentik path list of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "deploy-ps-prod.sh"

REMOVED_STRINGS = (
    "ensure_ps_service_ingress",
    "ensure_authentik_ingress",
    "read_authentik_public_paths",
    "AUTHENTIK_PUBLIC_PATHS_FILE",
    "proxy-body-size",
    f"{150}m",
    "kind: Ingress",
)


def test_script_applies_no_ingress_manifest_and_has_no_body_size_literal(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert [string for string in REMOVED_STRINGS if string in text] == []

    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert [line for line in deploy_ps_fixture.read_kubectl_log() if "kind=Ingress" in line] == []
