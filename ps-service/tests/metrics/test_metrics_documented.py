"""Tests that keep the operations guide, the metric catalog and the live scrape in step."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from opentelemetry.metrics import Counter, Histogram

from metrics._catalog_checks import sanitized, scrape_family_names, uncatalogued_families
from ps_service.metrics import build_metrics
from ps_service.metrics.catalog import METRIC_CATALOG

if TYPE_CHECKING:
    from ps_service.metrics import MetricsHandle
    from ps_service.metrics.catalog import MetricSpec

_GUIDE = Path(__file__).resolve().parents[3] / "docs" / "artifacts" / "operations-guide.md"
_EXPOSURE_PHRASES = (
    "in-cluster only",
    "the chart proxy returns 403",
    "deny `/metrics` on any ingress",
    "do not expose the service with nodeport",
    "anonymous",
    "no dependency names",
    "no graph names",
)


def _guide_section() -> str:
    text = _GUIDE.read_text(encoding="utf-8")
    start = text.index("\n### Metrics\n")
    end = text.index("\n### ", start + 1)
    return text[start:end]


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


def test_guide_has_a_metrics_section_listed_in_the_table_of_contents() -> None:
    text = _GUIDE.read_text(encoding="utf-8")

    assert "- [Metrics](#metrics)" in text
    assert _guide_section().strip()


def test_guide_states_the_exposure_rules() -> None:
    section = _flat(_guide_section())

    missing = [phrase for phrase in _EXPOSURE_PHRASES if phrase not in section]

    assert missing == []


def test_guide_shows_prometheus_and_collector_scrape_examples() -> None:
    section = _guide_section()

    assert "scrape_configs:" in section
    assert "metrics_path: /metrics" in section
    assert "prometheus:" in section
    assert "receivers:" in section


def _record_one_value(handle: MetricsHandle, spec: MetricSpec) -> None:
    """Record one sample for `spec`, because the exporter emits no family for an empty stream.

    Observable gauges already report through the callback `build_metrics` registered.
    """
    attributes = dict.fromkeys(spec.labels, "x")
    instrument = handle.instruments[spec.name]
    if isinstance(instrument, Histogram):
        instrument.record(1.0, attributes)
    elif isinstance(instrument, Counter):
        instrument.add(1, attributes)


def test_every_catalog_metric_is_documented_with_its_labels() -> None:
    section = _guide_section()
    undocumented: list[str] = []
    for spec in METRIC_CATALOG:
        handle = build_metrics(unhealthy_counts=lambda: (0, 0), pending_entries=lambda: 0)
        _record_one_value(handle, spec)
        families = {
            name
            for name in scrape_family_names(handle.render()[0])
            if name.startswith(sanitized(spec))
        }
        handle.shutdown()
        if not families:
            undocumented.append(f"{spec.name}: no family in a scrape")
        undocumented.extend(name for name in sorted(families) if name not in section)
        if spec.name not in section:
            undocumented.append(spec.name)
        undocumented.extend(f"{spec.name}:{label}" for label in spec.labels if label not in section)

    assert undocumented == []


def test_every_live_scrape_family_maps_to_a_catalog_entry() -> None:
    handle = build_metrics()

    assert uncatalogued_families(handle.render()[0], METRIC_CATALOG) == []
    handle.shutdown()


def test_an_instrument_outside_the_catalog_is_reported() -> None:
    handle = build_metrics()
    handle.meter_provider.get_meter("ps_service").create_counter("rogue.events").add(1)

    assert uncatalogued_families(handle.render()[0], METRIC_CATALOG) == ["rogue_events"]
    handle.shutdown()
