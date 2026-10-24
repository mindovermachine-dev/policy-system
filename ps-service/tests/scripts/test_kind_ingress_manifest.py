"""Structural tests for the vendored ingress controller manifest `deploy/kind/ingress-nginx.yaml`.

Pure YAML load: no `kind`/`helm`/container runtime needed. The manifest is the upstream
kind-provider manifest at a pinned release with exactly one edit, the controller Service: a
NodePort whose https port is the one `deploy/kind/cluster.yaml` publishes on the host.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = REPO_ROOT / "deploy" / "kind" / "ingress-nginx.yaml"
CLUSTER_CONFIG_PATH = REPO_ROOT / "deploy" / "kind" / "cluster.yaml"
VALUES_PATH = REPO_ROOT / "charts" / "policy-system" / "values.yaml"

PINNED_RELEASE = "controller-v1.15.1"
UPSTREAM_SHA256 = "2a3ae008c8786431115502644e77ab398fdebfb721a5d1195ed3089cde3299df"
HTTPS_NODE_PORT = 30443
IMAGE_PIN = re.compile(r"^[^\s@]+:[^\s@]+@sha256:[0-9a-f]{64}$")


def _manifest_text() -> str:
    return MANIFEST_PATH.read_text(encoding="utf-8")


def _documents() -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(_manifest_text()) if d]


def _controller_service() -> dict[str, Any]:
    matches = [
        d
        for d in _documents()
        if d["kind"] == "Service" and d["metadata"]["name"] == "ingress-nginx-controller"
    ]
    assert len(matches) == 1
    return matches[0]


def _containers() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for doc in _documents():
        if doc["kind"] == "Deployment":
            found.extend(doc["spec"]["template"]["spec"]["containers"])
    return found


def test_the_manifest_is_the_pinned_upstream_release_with_only_the_service_edit() -> None:
    text = _manifest_text()
    upstream = text.replace("  type: NodePort\n", "  type: LoadBalancer\n", 1).replace(
        f"    nodePort: {HTTPS_NODE_PORT}\n", "", 1
    )

    assert hashlib.sha256(upstream.encode("utf-8")).hexdigest() == UPSTREAM_SHA256, (
        f"the manifest must equal upstream {PINNED_RELEASE} apart from the controller Service edit"
    )
    assert f"app.kubernetes.io/version: {PINNED_RELEASE.removeprefix('controller-v')}" in text


def test_controller_service_is_a_nodeport_with_the_https_node_port() -> None:
    spec = _controller_service()["spec"]
    ports = {p["name"]: p for p in spec["ports"]}

    assert spec["type"] == "NodePort"
    assert ports["https"]["nodePort"] == HTTPS_NODE_PORT
    assert ports["https"]["targetPort"] == "https"


def test_only_the_https_port_has_an_explicit_node_port() -> None:
    ports = _controller_service()["spec"]["ports"]

    explicit = {p["name"]: p["nodePort"] for p in ports if "nodePort" in p}
    assert explicit == {"https": HTTPS_NODE_PORT}


def test_no_node_port_other_than_https_is_published_by_the_cluster() -> None:
    config = yaml.safe_load(CLUSTER_CONFIG_PATH.read_text(encoding="utf-8"))
    mapped = {m["containerPort"] for m in config["nodes"][0]["extraPortMappings"]}
    service_ports = {
        p["nodePort"] for p in _controller_service()["spec"]["ports"] if "nodePort" in p
    }

    assert mapped & service_ports == {HTTPS_NODE_PORT}


def test_the_nginx_ingress_class_exists() -> None:
    classes = [d for d in _documents() if d["kind"] == "IngressClass"]

    assert [c["metadata"]["name"] for c in classes] == ["nginx"]
    assert classes[0]["spec"]["controller"] == "k8s.io/ingress-nginx"


def test_every_image_is_pinned_by_tag_and_digest() -> None:
    images = [c["image"] for c in _containers()]

    assert images, "the manifest must define the controller and certgen containers"
    for image in images:
        assert IMAGE_PIN.match(image), f"image {image} must be pinned as name:tag@sha256:digest"


def test_the_controller_does_not_require_a_node_label() -> None:
    deployment = next(
        d
        for d in _documents()
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "ingress-nginx-controller"
    )
    selector = deployment["spec"]["template"]["spec"].get("nodeSelector", {})

    assert selector == {"kubernetes.io/os": "linux"}


def test_trusted_proxy_cidr_equals_the_cluster_pod_subnet() -> None:
    config = yaml.safe_load(CLUSTER_CONFIG_PATH.read_text(encoding="utf-8"))
    values = yaml.safe_load(VALUES_PATH.read_text(encoding="utf-8"))
    env = values["authentik"]["global"]["env"]
    cidrs = [e["value"] for e in env if e["name"] == "AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS"]

    assert cidrs == [config["networking"]["podSubnet"]]
