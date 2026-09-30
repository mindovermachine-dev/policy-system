"""Structural regression test for `deploy/kind/cluster.yaml` (issue #160, Slice 1).

Pure YAML load — no `kind`/`helm`/container runtime needed, hermetic, no marker required
(same tier as `test_verify_chart_independence.py`'s home directory, but that one is marked
`integration` because it spawns a real `helm` subprocess; this one only parses a static
YAML file on disk).

Guards two things together:
  - The two pre-existing loopback-only `extraPortMappings` entries (ps-service NodePort,
    falkordb-browser NodePort) are untouched — a regression guard for AC-BI-007's "no
    *other* port newly reachable" framing.
  - The new third entry (Authentik's server Service HTTPS NodePort, issues #160/#165; the
    plain-HTTP 30080 is no longer host-mapped) has
    `hostPort == containerPort` and `listenAddress: "0.0.0.0"` — the exact shape PLAN.md
    §1.2 specifies, needed so the issuer URL is byte-identical for LAN clients (who
    traverse the host-port mapping) and in-cluster pods (who reach the node's own
    container IP directly on the NodePort, bypassing the mapping entirely).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CLUSTER_CONFIG_PATH = REPO_ROOT / "deploy" / "kind" / "cluster.yaml"

AUTHENTIK_NODE_PORT = 30443
PLAIN_HTTP_NODE_PORT = 30080


def _load_extra_port_mappings() -> list[dict[str, Any]]:
    with CLUSTER_CONFIG_PATH.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    mappings: list[dict[str, Any]] = config["nodes"][0]["extraPortMappings"]
    return mappings


def test_cluster_config_has_exactly_three_port_mappings() -> None:
    mappings = _load_extra_port_mappings()
    assert len(mappings) == 3, (
        f"expected exactly 3 extraPortMappings entries (ps-service, falkordb-browser, "
        f"authentik), found {len(mappings)}: {mappings}"
    )


def test_first_two_port_mappings_are_unchanged_loopback_entries() -> None:
    mappings = _load_extra_port_mappings()

    ps_service_entry = mappings[0]
    assert ps_service_entry["containerPort"] == 30800
    assert ps_service_entry["hostPort"] == 8000
    assert ps_service_entry["listenAddress"] == "127.0.0.1"
    assert ps_service_entry["protocol"] == "TCP"

    falkordb_browser_entry = mappings[1]
    assert falkordb_browser_entry["containerPort"] == 30300
    assert falkordb_browser_entry["hostPort"] == 3001
    assert falkordb_browser_entry["listenAddress"] == "127.0.0.1"
    assert falkordb_browser_entry["protocol"] == "TCP"


def test_no_plain_http_authentik_host_mapping_remains() -> None:
    # Issue #165 (OD-3): the plain-HTTP NodePort 30080 stays reachable inside the node only;
    # only the HTTPS NodePort is mapped to the host.
    mappings = _load_extra_port_mappings()
    assert all(m["containerPort"] != PLAIN_HTTP_NODE_PORT for m in mappings)
    assert all(m["hostPort"] != PLAIN_HTTP_NODE_PORT for m in mappings)


def test_third_port_mapping_exposes_authentik_https_on_the_lan() -> None:
    mappings = _load_extra_port_mappings()
    authentik_entry = mappings[2]

    assert authentik_entry["containerPort"] == AUTHENTIK_NODE_PORT
    assert authentik_entry["hostPort"] == AUTHENTIK_NODE_PORT, (
        "hostPort must equal containerPort so the issuer URL's port is byte-identical "
        "for LAN clients (traversing this mapping) and in-cluster pods (reaching the "
        "node's own container IP directly on the NodePort, bypassing this mapping)"
    )
    assert authentik_entry["listenAddress"] == "0.0.0.0", (
        "must be LAN-reachable (not loopback-only like the other two entries), since "
        "LAN clients must reach Authentik through this mapping"
    )
    assert authentik_entry["protocol"] == "TCP"
