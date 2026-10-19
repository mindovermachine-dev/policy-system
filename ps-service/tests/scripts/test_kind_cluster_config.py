"""Structural regression test for `deploy/kind/cluster.yaml`.

Pure YAML load: no `kind`/`helm`/container runtime needed, hermetic, no marker required.

Guards the host exposure of the evaluator cluster:
  - exactly one mapping listens off-loopback, and it points at the proxy's NodePort with
    `hostPort == containerPort`, so the issuer URL's port is byte-identical for LAN clients
    (who traverse the host-port mapping) and in-cluster pods (who reach the node's own container
    IP directly on the NodePort, bypassing the mapping);
  - the only other mapping is the FalkorDB Browser, on loopback;
  - no database, Redis or direct Authentik / PS Service port is mapped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CLUSTER_CONFIG_PATH = REPO_ROOT / "deploy" / "kind" / "cluster.yaml"
VALUES_PATH = REPO_ROOT / "charts" / "policy-system" / "values.yaml"

LOOPBACK = {"127.0.0.1", "::1"}
DATABASE_AND_REDIS_PORTS = {5432, 6379}
REMOVED_AUTHENTIK_NODE_PORTS = {30080}
REMOVED_PS_SERVICE_NODE_PORT = 30800


def _load_extra_port_mappings() -> list[dict[str, Any]]:
    with CLUSTER_CONFIG_PATH.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    mappings: list[dict[str, Any]] = config["nodes"][0]["extraPortMappings"]
    return mappings


def _proxy_node_port() -> int:
    with VALUES_PATH.open(encoding="utf-8") as f:
        values = yaml.safe_load(f)
    return int(values["proxy"]["service"]["nodePort"])


def _off_loopback(mappings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in mappings if m.get("listenAddress", "0.0.0.0") not in LOOPBACK]


def test_exactly_one_non_loopback_mapping() -> None:
    off_loopback = _off_loopback(_load_extra_port_mappings())

    assert len(off_loopback) == 1, f"expected one off-loopback mapping, found {off_loopback}"
    proxy_mapping = off_loopback[0]
    assert proxy_mapping["containerPort"] == _proxy_node_port()
    assert proxy_mapping["hostPort"] == proxy_mapping["containerPort"], (
        "hostPort must equal containerPort so the issuer URL's port is the same for LAN clients "
        "and for in-cluster pods that bypass the mapping"
    )
    assert proxy_mapping["listenAddress"] == "0.0.0.0"
    assert proxy_mapping["protocol"] == "TCP"


def test_the_only_other_mapping_is_the_falkordb_browser_on_loopback() -> None:
    loopback = [
        m
        for m in _load_extra_port_mappings()
        if m not in _off_loopback(_load_extra_port_mappings())
    ]

    assert loopback == [
        {"containerPort": 30300, "hostPort": 3001, "listenAddress": "127.0.0.1", "protocol": "TCP"}
    ]


def test_no_database_or_redis_mapping() -> None:
    for mapping in _load_extra_port_mappings():
        assert mapping["containerPort"] not in DATABASE_AND_REDIS_PORTS
        assert mapping["hostPort"] not in DATABASE_AND_REDIS_PORTS


def test_no_mapping_to_removed_authentik_nodeport() -> None:
    for mapping in _load_extra_port_mappings():
        assert mapping["containerPort"] not in REMOVED_AUTHENTIK_NODE_PORTS
        assert mapping["hostPort"] not in REMOVED_AUTHENTIK_NODE_PORTS


def test_no_mapping_to_removed_ps_service_nodeport() -> None:
    for mapping in _load_extra_port_mappings():
        assert mapping["containerPort"] != REMOVED_PS_SERVICE_NODE_PORT
        assert mapping["hostPort"] != 8000
