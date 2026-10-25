"""Content lint: the docs describe the shutdown stop budget.

The architecture doc and the Helm values reference both name it. Anchors are quoted text,
never line numbers.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_ARCH = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_VALUES_REFERENCE = _REPO / "docs" / "artifacts" / "helm-chart-values-reference.md"
_BUDGET_SETTING = "PS_SERVICE_SHUTDOWN_STOP_BUDGET_SECONDS"


def _process_harness_section(text: str) -> str:
    """The text from the `### Process Harness` heading up to the next `###` heading."""
    _, _, after = text.partition("\n### Process Harness\n")
    assert after, "the architecture doc has no Process Harness section"
    section, _, _ = after.partition("\n### ")
    return section


def test_architecture_doc_has_no_fixed_reconciler_stop_timeout() -> None:
    assert "stop_reconciler(timeout=5.0)" not in _ARCH.read_text(encoding="utf-8")


def test_architecture_process_harness_names_the_shutdown_stop_budget() -> None:
    section = _process_harness_section(_ARCH.read_text(encoding="utf-8"))

    assert _BUDGET_SETTING in section


def test_values_reference_documents_the_termination_grace_formula() -> None:
    text = _VALUES_REFERENCE.read_text(encoding="utf-8")

    assert "terminationGracePeriodSeconds" in text
    assert "`gracefulShutdownSeconds + 15 + 5`" in text
    assert _BUDGET_SETTING in text
