"""The guides record the ingress controller footprint, or that it is not measured, and the steps.

Reads the real markdown files off disk; no fakes. The sizing block is in exactly one of two states:
measured (controller image tag, date, host size, idle and load figures, the minimum host size) or
the literal marker `Footprint: not measured` with a reason and `minimum not re-validated`.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_INSTALL = _REPO / "docs" / "artifacts" / "evaluator-installation-guide.md"
_OPERATIONS = _REPO / "docs" / "artifacts" / "operations-guide.md"
_SIZING_MARKER = "> **Why 4 CPUs and 8 GB:**"
_PROCEDURE_HEADING = "### Measuring the ingress controller footprint (evaluator)"
_NOT_MEASURED = "Footprint: not measured"
_MEASURED = "Footprint: measured"
_RETIRED_FIGURES = ("13 MiB", "19 MiB", "417m", "nginx:1.31.7-alpine", "proxy.resources")
_FIGURE = re.compile(r"\d+(?:\.\d+)? ?(?:MiB|GiB)|\d+m CPU")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("> ", " "))


def _sizing_block() -> str:
    text = _INSTALL.read_text().split(_SIZING_MARKER, 1)[1]
    return _flat(re.split(r"\n#{3,4} ", text, maxsplit=1)[0])


def _procedure() -> str:
    text = _OPERATIONS.read_text().split(_PROCEDURE_HEADING, 1)[1]
    return re.split(r"\n#{2,3} ", text, maxsplit=1)[0]


def _sizing_note() -> str:
    block = _sizing_block()
    return block.split("4 CPUs and 8 GB is the tested minimum.", 1)[1]


def test_sizing_block_is_in_exactly_one_state() -> None:
    block = _sizing_block()

    assert (_MEASURED in block) != (_NOT_MEASURED in block)


def test_sizing_block_never_carries_the_retired_proxy_figures() -> None:
    block = _sizing_block()

    for retired in _RETIRED_FIGURES:
        assert retired not in block


def test_measured_state_names_tag_date_host_and_figures() -> None:
    block = _sizing_block()
    if _MEASURED not in block:
        return

    note = _sizing_note()
    assert re.search(r"ingress-nginx controller v\d+\.\d+\.\d+", note)
    assert re.search(r"\b20\d{2}-\d{2}-\d{2}\b", note)
    assert re.search(r"\d+ CPUs and \d+(?:\.\d+)? GiB", note)
    assert "idle" in note
    assert "load" in note
    assert len(_FIGURE.findall(note)) >= 4
    assert "minimum" in note


def test_not_measured_state_gives_a_reason_and_keeps_the_minimum_unvalidated() -> None:
    block = _sizing_block()
    if _NOT_MEASURED not in block:
        return

    note = _sizing_note()
    assert "minimum not re-validated" in note
    assert "because" in note
    assert _FIGURE.findall(note) == []


def test_sizing_block_states_the_minimum_host_size() -> None:
    assert "4 CPUs and 8 GB is the tested minimum" in _sizing_block()


def test_sizing_block_links_to_the_procedure() -> None:
    block = _sizing_block()

    assert "operations-guide.md#measuring-the-ingress-controller-footprint-evaluator" in block
    assert "measure-proxy-footprint" not in block


def test_procedure_uses_only_commands_that_exist() -> None:
    procedure = _procedure()

    assert "kubectl" in procedure
    assert "top" in procedure
    assert "metrics-server" in procedure
    assert "cgroup" in procedure
    assert "measure-proxy-footprint" not in procedure
    assert "ingress-nginx" in procedure


def test_procedure_points_at_the_recorded_measurement() -> None:
    procedure = _flat(_procedure())

    assert (
        "evaluator-installation-guide.md#step-1-install-the-tools-and-download-the-bundle"
        in procedure
    )
    assert "Record a new measurement" in procedure
