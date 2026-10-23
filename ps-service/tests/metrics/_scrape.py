"""Read samples out of a Prometheus scrape body."""

from __future__ import annotations

from dataclasses import dataclass

from prometheus_client.parser import text_string_to_metric_families


@dataclass(frozen=True)
class Sample:
    """One exposed sample: its series name, label set and value."""

    name: str
    labels: dict[str, str]
    value: float


def samples(body: bytes, name_prefix: str = "") -> list[Sample]:
    """Every sample in `body` whose series name starts with `name_prefix`."""
    return [
        Sample(sample.name, dict(sample.labels), sample.value)
        for family in text_string_to_metric_families(body.decode())
        for sample in family.samples
        if sample.name.startswith(name_prefix)
    ]


def label_values(body: bytes, name: str, label: str) -> set[str]:
    """The distinct values `label` takes on the series called exactly `name`."""
    return {s.labels[label] for s in samples(body, name) if s.name == name and label in s.labels}
