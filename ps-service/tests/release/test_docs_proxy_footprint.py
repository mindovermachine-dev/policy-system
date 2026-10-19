"""The guides record the measured proxy footprint and keep the procedure to reproduce it.

Reads the real markdown files off disk; no fakes.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_INSTALL = _REPO / "docs" / "artifacts" / "installation-guide.md"
_OPERATIONS = _REPO / "docs" / "artifacts" / "operations-guide.md"
_SIZING_MARKER = "> **Why 4 CPUs and 8 GB:**"
_PROCEDURE_HEADING = "### Measuring the proxy footprint (evaluator)"


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("> ", " "))


def _sizing_block() -> str:
    text = _INSTALL.read_text().split(_SIZING_MARKER, 1)[1]
    return _flat(text.split("\n### ", 1)[0])


def _procedure() -> str:
    text = _OPERATIONS.read_text().split(_PROCEDURE_HEADING, 1)[1]
    return re.split(r"\n#{2,3} ", text, maxsplit=1)[0]


def test_sizing_section_records_the_measured_idle_and_load_figures() -> None:
    block = _sizing_block()

    assert "Proxy footprint: measured" in block
    assert "13 MiB" in block
    assert "19 MiB" in block
    assert "417m" in block


def test_sizing_section_names_the_measurement_conditions() -> None:
    block = _sizing_block()

    assert "nginx:" in block
    assert "2026-10-20" in block
    assert "metrics-server" in block


def test_sizing_section_states_the_minimum_host_size_and_that_it_is_unchanged() -> None:
    block = _sizing_block()

    assert "4 CPUs and 8 GB is the tested minimum" in block
    assert "the minimum above is unchanged" in block


def test_sizing_section_calls_the_declared_proxy_resources_limits_not_measurements() -> None:
    block = _sizing_block()

    assert "proxy.resources" in block
    assert "not a measurement" not in block
    assert "declared" in block


def test_sizing_section_links_to_the_procedure() -> None:
    block = _sizing_block()

    assert "scripts/measure-proxy-footprint.sh" in block
    assert "operations-guide.md#measuring-the-proxy-footprint-evaluator" in block


def test_procedure_names_the_script_the_load_profile_and_the_metrics_prerequisite() -> None:
    procedure = _procedure()

    assert "scripts/measure-proxy-footprint.sh" in procedure
    assert "--idle-seconds" in procedure
    assert "--load-seconds" in procedure
    assert "--concurrency" in procedure
    assert "metrics-server" in procedure
    assert "cgroup" in procedure


def test_procedure_points_at_the_recorded_measurement() -> None:
    procedure = _flat(_procedure())

    assert "installation-guide.md#evaluator-installation" in procedure
    assert "Record a new measurement" in procedure
