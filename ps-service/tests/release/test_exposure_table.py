"""The evaluator installation guide's exposure table agrees with `deploy/kind/cluster.yaml`.

Reads the real files off disk; no fakes. A row counts as off-loopback when its address cell says
`all interfaces` or `0.0.0.0`, and as loopback when it names `127.0.0.1:<port>`.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]
_INSTALL = _REPO / "docs" / "artifacts" / "evaluator-installation-guide.md"
_CLUSTER = _REPO / "deploy" / "kind" / "cluster.yaml"
_HEADING = "### What is exposed"
_LOOPBACK_ADDRESS = re.compile(r"`127\.0\.0\.1:(\d+)`")
_HOST_PORT = re.compile(r"[Hh]ost port `(\d+)`")


def _table_rows() -> list[list[str]]:
    text = _INSTALL.read_text()
    section = text.split(_HEADING, 1)[1].split("\n### ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ")]
    cells = [[c.strip() for c in row.strip().strip("|").split("|")] for row in rows]
    return [r for r in cells if r[0] != "Listener" and not set(r[0]) <= {"-", " "}]


def _table_non_loopback_ports() -> set[int]:
    ports: set[int] = set()
    for row in _table_rows():
        address = row[1]
        if "all interfaces" in address or "0.0.0.0" in address:
            ports.update(int(p) for p in _HOST_PORT.findall(address))
    return ports


def _table_loopback_ports() -> set[int]:
    return {int(p) for row in _table_rows() for p in _LOOPBACK_ADDRESS.findall(row[1])}


def _cluster_ports() -> tuple[set[int], set[int]]:
    config = yaml.safe_load(_CLUSTER.read_text())
    mappings = config["nodes"][0]["extraPortMappings"]
    loopback = {"127.0.0.1", "::1"}
    off = {m["hostPort"] for m in mappings if m.get("listenAddress", "0.0.0.0") not in loopback}
    on = {m["hostPort"] for m in mappings if m.get("listenAddress", "0.0.0.0") in loopback}
    return off, on


def test_exposure_table_non_loopback_set_equals_cluster_yaml() -> None:
    cluster_off, _ = _cluster_ports()

    assert _table_non_loopback_ports() == cluster_off
    assert cluster_off, "the cluster must expose the proxy port"


def test_exposure_table_rows_cover_every_cluster_mapping() -> None:
    _, cluster_loopback = _cluster_ports()

    assert _table_loopback_ports() == cluster_loopback
