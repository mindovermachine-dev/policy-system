"""The evaluator profile names the networks Authentik trusts forwarding headers from."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
VALUES = REPO_ROOT / "charts" / "policy-system" / "values.yaml"
KIND_CONFIG = REPO_ROOT / "deploy" / "kind" / "cluster.yaml"

ENV_NAME = "AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS"

# The pod network kind assigns when the cluster config has no networking override. Source: the
# default pod subnet compiled into the pinned kind release (the same binary the deploy script
# runs), cross-checked against `networking.podSubnet` being absent from the cluster config.
KIND_DEFAULT_POD_SUBNET = ipaddress.ip_network("10.244.0.0/16")


def _configured_cidrs() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    values = yaml.safe_load(VALUES.read_text())
    env = values["authentik"]["global"]["env"]
    entries = [item["value"] for item in env if item["name"] == ENV_NAME]
    assert len(entries) == 1, f"{ENV_NAME} must be set exactly once"
    return [ipaddress.ip_network(part.strip()) for part in entries[0].split(",")]


def test_the_kind_cluster_config_does_not_override_the_pod_subnet() -> None:
    config = yaml.safe_load(KIND_CONFIG.read_text())

    assert "podSubnet" not in (config.get("networking") or {})


def test_trusted_proxy_cidr_covers_kind_pod_subnet() -> None:
    cidrs = _configured_cidrs()

    assert any(KIND_DEFAULT_POD_SUBNET.subnet_of(cidr) for cidr in cidrs)  # type: ignore[arg-type]


def test_trusted_proxy_cidr_is_narrower_than_the_private_ranges() -> None:
    for cidr in _configured_cidrs():
        assert cidr.prefixlen >= 16
