"""Across every rendered workload, only the proxy can reach the local TLS private key.

The whole chart is rendered with the proxy, Authentik and the CA Secret enabled, then each
workload's pod template is searched for any reference to the TLS Secret: a volume, a projected
source, an environment variable or an `envFrom` entry.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
CHART = REPO_ROOT / "charts" / "policy-system"
HELM_TIMEOUT_SECONDS = 60.0
TLS_SECRET = "policy-system-local-tls"
CA_SECRET = "policy-system-local-ca"
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}


def _render(*extra: str) -> list[dict[str, Any]]:
    helm = shutil.which("helm")
    assert helm is not None, "helm must be on PATH (devcontainer/CI prerequisite)"
    settings = [
        "llm.provider=ollama",
        "psService.localTestBypass.enabled=true",
        "proxy.enabled=true",
        f"proxy.tls.secretName={TLS_SECRET}",
        "psService.authentikHostname=ps.test",
        "authentik.enabled=true",
        f"psService.localCa.secretName={CA_SECRET}",
        *extra,
    ]
    args = [helm, "template", "ps", str(CHART)]
    for setting in settings:
        args += ["--set", setting]
    result = subprocess.run(  # noqa: S603 - helm is a resolved absolute path; args are literals
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=HELM_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _pod_spec(doc: dict[str, Any]) -> dict[str, Any]:
    template = doc["spec"]
    if doc["kind"] == "CronJob":
        template = template["jobTemplate"]["spec"]
    return template["template"]["spec"]


def _secret_names(node: object) -> set[str]:
    """Return every Secret name referenced anywhere inside a pod spec."""
    names: set[str] = set()
    if isinstance(node, dict):
        for key, value in cast("dict[str, object]", node).items():
            if key in {"secretName", "name"} and isinstance(value, str):
                names.add(value)
            names |= _secret_names(value)
    elif isinstance(node, list):
        for item in cast("list[object]", node):
            names |= _secret_names(item)
    return names


def _workloads_referencing(docs: list[dict[str, Any]], secret: str) -> set[str]:
    found: set[str] = set()
    for doc in docs:
        if doc["kind"] in WORKLOAD_KINDS and secret in _secret_names(_pod_spec(doc)):
            found.add(doc["metadata"]["name"])
    return found


def test_only_the_proxy_workload_references_the_tls_secret() -> None:
    docs = _render()

    referencing = _workloads_referencing(docs, TLS_SECRET)

    assert referencing == {"ps-policy-system-proxy"}


def test_the_check_detects_another_workload_sharing_the_tls_secret() -> None:
    docs = _render(f"psService.localCa.secretName={TLS_SECRET}")

    referencing = _workloads_referencing(docs, TLS_SECRET)

    assert {name for name in referencing if name.endswith("-ps-service")}


def test_every_workload_kind_in_the_render_is_covered() -> None:
    docs = _render()

    kinds = {doc["kind"] for doc in docs if doc["kind"] in WORKLOAD_KINDS}

    assert {"Deployment"} <= kinds
    unchecked = {
        doc["kind"]
        for doc in docs
        if "template" in doc.get("spec", {}) and doc["kind"] not in WORKLOAD_KINDS
    }
    assert unchecked == set()


def test_no_rendered_secret_carries_a_private_key() -> None:
    docs = _render()

    offenders = [
        doc["metadata"]["name"]
        for doc in docs
        if doc["kind"] == "Secret"
        and any("tls.key" in (doc.get(field) or {}) for field in ("data", "stringData"))
    ]

    assert offenders == []


def test_the_ca_secret_is_the_only_certificate_secret_the_service_pod_mounts() -> None:
    docs = _render()

    service = next(
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-ps-service")
    )
    volumes = _pod_spec(service).get("volumes", [])

    secret_volumes = [v["secret"] for v in volumes if "secret" in v]
    assert [s["secretName"] for s in secret_volumes] == [CA_SECRET]
    assert "items" not in secret_volumes[0]
