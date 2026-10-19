"""The documentation states how to limit access to the evaluator and what an upgrade removes.

Reads the real markdown files off disk; no fakes.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]
_INSTALL = _REPO / "docs" / "artifacts" / "installation-guide.md"
_EXPOSED_HEADING = "### What is exposed (evaluator)"
_UPGRADE_HEADING = "#### Upgrading an existing evaluator"


def _section(heading: str) -> str:
    text = re.sub(r"\s+", " ", _INSTALL.read_text().split(heading, 1)[1])
    return re.split(r" #{2,4} ", text, maxsplit=1)[0]


def test_limit_access_method_names_port_30443_firewall() -> None:
    section = _section(_EXPOSED_HEADING)

    method = section.split("**To limit access to your own machine:**", 1)[1]
    method = method.split("**", 1)[0]
    assert "30443" in method
    assert "firewall" in method
    assert "LAN" in method


def test_upgrade_note_names_8000_removal_and_reenrolment() -> None:
    section = _section(_UPGRADE_HEADING)

    assert "127.0.0.1:8000" in section
    assert re.search(r"re-enrol|enrol .* again", section, re.IGNORECASE)
    assert "signing" in section.lower()


_DOC_FILES = (
    _INSTALL,
    _REPO / "docs" / "artifacts" / "operations-guide.md",
    _REPO / "docs" / "artifacts" / "idp-configuration-contract.md",
    _REPO / "docs" / "artifacts" / "helm-chart-values-reference.md",
    _REPO / "docs" / "artifacts" / "user-guide.md",
    _REPO / "docs" / "architecture" / "customer-azure-deployment.md",
    _REPO / "docs" / "architecture" / "ps-solution-architecture.md",
    _REPO / "docs" / "secops" / "threat-coverage-map.md",
)
_CLUSTER_YAML = _REPO / "deploy" / "kind" / "cluster.yaml"
_CHART = _REPO / "charts" / "policy-system"
_SCRIPTS = (
    _REPO / "scripts" / "deploy-ps-eval.sh",
    _REPO / "scripts" / "lib" / "local-tls.sh",
)
_AUTHENTIK_ENTRY_CLAIMS = (
    "only LAN" + "-reachable",
    "publishes Authentik on every",
    "Authentik (port 30443)",
    "Authentik's HTTPS " + "NodePort",
    "HTTPS " + "NodePort of the Authentik",
    "node" + "PortHttps",
)
_REMOVED_BLUEPRINT = "authentik-local-tls" + "-blueprint"


def _tracked_texts() -> dict[Path, str]:
    paths = [*_DOC_FILES, _CLUSTER_YAML, *_SCRIPTS, *sorted(_CHART.rglob("*"))]
    return {
        path: path.read_text()
        for path in paths
        if path.is_file()
        and path.suffix in {".md", ".yaml", ".sh", ".tpl", ".txt"}
        and (_CHART not in path.parents or "tests" not in path.relative_to(_CHART).parts)
    }


def test_no_doc_claims_authentik_is_the_only_lan_entry() -> None:
    offenders = [
        f"{path.relative_to(_REPO)}: {needle}"
        for path, text in _tracked_texts().items()
        for needle in _AUTHENTIK_ENTRY_CLAIMS
        if needle in text
    ]

    assert offenders == []


def test_no_doc_claims_ps_service_is_this_machine_only() -> None:
    offenders = [
        f"{path.relative_to(_REPO)}: {line.strip()[:80]}"
        for path in _DOC_FILES
        for line in path.read_text().splitlines()
        if line.startswith("|")
        and "PS Service" in line.split("|")[1]
        and re.search(r"this machine only|not reachable from the lan", line, re.IGNORECASE)
    ]

    assert offenders == []


def test_cluster_yaml_comments_name_the_proxy() -> None:
    comments = " ".join(
        line.lstrip("# ").strip()
        for line in _CLUSTER_YAML.read_text().splitlines()
        if line.lstrip().startswith("#")
    )

    assert "the proxy Service NodePort" in comments
    assert "the proxy is the only off-loopback listener" in comments


def test_no_direct_authentik_or_ps_service_host_mapping_anywhere() -> None:
    mappings = yaml.safe_load(_CLUSTER_YAML.read_text())["nodes"][0]["extraPortMappings"]

    assert {mapping["hostPort"] for mapping in mappings} == {3001, 30443}
    for path, text in _tracked_texts().items():
        assert "30" + "800" not in text, path
        assert _REMOVED_BLUEPRINT not in text, path
    assert not (_CHART / "files" / f"{_REMOVED_BLUEPRINT}.yaml").exists()


def test_evaluator_docs_name_no_listener_on_8000() -> None:
    for path in _DOC_FILES:
        if path in {_INSTALL, _REPO / "docs" / "artifacts" / "user-guide.md"}:
            continue
        assert "127.0.0.1:8000" not in path.read_text(), path
    for line in _INSTALL.read_text().splitlines():
        if "127.0.0.1:8000" in line:
            assert "mapping" in line or "on `http" in line or "the former" in line, line
    assert "http://127.0.0.1:8000/mcp/" not in _INSTALL.read_text()


def test_upgrade_note_appears_exactly_once() -> None:
    text = re.sub(r"\s+", " ", _INSTALL.read_text())

    assert text.count("Signing credentials that were enrolled") == 1
    assert text.count("is no longer reached on `http://127.0.0.1:8000`") == 1


def test_installation_guide_gives_linux_ca_trust_steps() -> None:
    section = _section("### The server machine")

    assert "On Linux" in section
    assert "update-ca-certificates" in section
    assert "update-ca-trust" in section
