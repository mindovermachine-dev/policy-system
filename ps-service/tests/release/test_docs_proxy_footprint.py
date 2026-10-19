"""The guides state the proxy footprint is not yet measured and give the procedure.

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
_FIGURE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:Mi|MiB|MB|Gi|GiB|GB|KiB|kB|m|millicores?|%)(?![A-Za-z])"
)


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("> ", " "))


def _sizing_block() -> str:
    text = _INSTALL.read_text().split(_SIZING_MARKER, 1)[1]
    return _flat(text.split("\n### ", 1)[0])


def _procedure() -> str:
    text = _OPERATIONS.read_text().split(_PROCEDURE_HEADING, 1)[1]
    return re.split(r"\n#{2,3} ", text, maxsplit=1)[0]


def test_sizing_section_says_the_proxy_footprint_is_to_be_measured() -> None:
    block = _sizing_block()

    assert "to be measured" in block
    assert "scripts/measure-proxy-footprint.sh" in block
    assert "operations-guide.md#measuring-the-proxy-footprint-evaluator" in block


def test_sizing_section_keeps_the_minimum_unchanged() -> None:
    block = _sizing_block()

    assert "4 CPUs and 8 GB is the tested minimum" in block
    assert "unchanged" in block


def test_sizing_section_calls_the_declared_proxy_resources_limits_not_measurements() -> None:
    block = _sizing_block()

    assert "proxy.resources" in block
    assert "not a measurement" in block


def test_no_measured_proxy_figure_is_claimed_in_the_sizing_text() -> None:
    block = _sizing_block()
    claimed = _FIGURE.findall(block.replace("8 GB", "").replace("4 CPUs", ""))

    assert claimed == []


def test_procedure_names_the_script_the_load_profile_and_the_metrics_prerequisite() -> None:
    procedure = _procedure()

    assert "scripts/measure-proxy-footprint.sh" in procedure
    assert "--idle-seconds" in procedure
    assert "--load-seconds" in procedure
    assert "--concurrency" in procedure
    assert "metrics-server" in procedure
    assert "cgroup" in procedure


def test_procedure_records_no_measurement_and_says_where_results_go() -> None:
    procedure = _procedure()

    assert _FIGURE.findall(procedure) == []
    assert "no measured figure" in _flat(procedure)
