"""Helpers that relate a live Prometheus scrape to the metric catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client.parser import text_string_to_metric_families

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ps_service.metrics.catalog import MetricSpec

_INFO_FAMILIES = frozenset({"target_info", "otel_scope_info"})


def scrape_family_names(body: bytes) -> set[str]:
    """The metric family names in a scrape body, without the resource and scope info families."""
    return {
        family.name
        for family in text_string_to_metric_families(body.decode())
        if family.name not in _INFO_FAMILIES
    }


def sanitized(spec: MetricSpec) -> str:
    """The OpenTelemetry metric name as the exporter writes it, before unit and type suffixes."""
    return spec.name.replace(".", "_")


def uncatalogued_families(body: bytes, catalog: Iterable[MetricSpec]) -> list[str]:
    """Scrape families that no catalog entry accounts for."""
    prefixes = tuple(sanitized(spec) for spec in catalog)
    return sorted(name for name in scrape_family_names(body) if not name.startswith(prefixes))
