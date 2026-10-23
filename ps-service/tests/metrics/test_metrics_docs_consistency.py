"""The audit and architecture documents describe the delivered metrics, not their absence."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / "docs"
CONTAINER_DOC = DOCS / "architecture" / "ps-service-container-architecture.md"
SOLUTION_DOC = DOCS / "architecture" / "ps-solution-architecture.md"
AUDIT_DOC = DOCS / "audits" / "observability-audit.md"
METRICS_SOURCE = ROOT / "ps-service" / "src" / "ps_service" / "metrics"
_EXCLUDED_TREES = ("regulations", "policy")
_NO_METRICS_CLAIMS = (
    re.compile(r"(?i)\b(?:exposes?|has|with)\s+no\s+metrics\b(?!-)"),
    re.compile(r"(?i)\bno\s+metrics\s+of\s+any\s+kind\b"),
    re.compile(r"(?i)\bservice\b[^.\n]{0,60}\bno\s+metrics\b(?!-server)"),
)
_HISTORY_WORDS = ("delivered", "was")


def _doc_files() -> list[Path]:
    return [
        path
        for path in sorted(DOCS.rglob("*.md"))
        if path.relative_to(DOCS).parts[0] not in _EXCLUDED_TREES
    ]


def _claims(path: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not any(pattern.search(line) for pattern in _NO_METRICS_CLAIMS):
            continue
        allowed_history = (
            path == AUDIT_DOC
            and line.lstrip().startswith("|")
            and any(re.search(rf"\b{word}\b", line, re.IGNORECASE) for word in _HISTORY_WORDS)
        )
        if not allowed_history:
            found.append((number, line))
    return found


def _obs_27_row() -> str:
    rows = [
        line
        for line in AUDIT_DOC.read_text(encoding="utf-8").splitlines()
        if line.startswith("| OBS-27 ")
    ]
    assert len(rows) == 1
    return rows[0]


def test_obs_27_names_the_delivered_change() -> None:
    row = _obs_27_row()

    assert "#321" in row
    assert re.search(r"\bdelivered\b", row, re.IGNORECASE)
    assert not re.match(r"\|[^|]*\|[^|]*\|[^|]*\|\s*No metrics", row)


def test_no_document_claims_the_service_exposes_no_metrics() -> None:
    claims = {str(path.relative_to(ROOT)): _claims(path) for path in _doc_files()}

    assert {path: found for path, found in claims.items() if found} == {}


@pytest.mark.parametrize("phrase", ["no metrics of any kind", "exposes no metrics"])
def test_the_claim_patterns_do_catch_the_old_wording(phrase: str) -> None:
    assert any(pattern.search(f"The service {phrase}.") for pattern in _NO_METRICS_CLAIMS)
    unrelated = "kind cluster had no metrics-server"
    assert not any(pattern.search(unrelated) for pattern in _NO_METRICS_CLAIMS)


def test_the_container_architecture_has_a_metrics_component_section() -> None:
    text = CONTAINER_DOC.read_text(encoding="utf-8")

    assert "- [Metrics](#metrics)" in text
    assert "\n### Metrics\n" in text
    assert "| Metrics | `ps.service.metrics` |" in text
    assert "`ps_service.metrics`" in text
    assert "        Metrics[Metrics]" in text
    assert "ExposeMetrics" in text
    assert "CollectMetrics" in text


def test_every_metrics_module_is_registered_in_the_container_architecture() -> None:
    text = CONTAINER_DOC.read_text(encoding="utf-8")

    for module in sorted(METRICS_SOURCE.glob("*.py")):
        assert f"ps-service/src/ps_service/metrics/{module.name}" in text, module.name


def test_the_process_harness_documents_the_metrics_route() -> None:
    text = CONTAINER_DOC.read_text(encoding="utf-8")
    harness = text[text.index("\n### Process Harness\n") :]
    harness = harness[: harness.index("\n### Domain Schema\n")]

    assert "/metrics" in harness
    assert "outermost" in harness
    assert "ExposeMetrics" in harness


def test_the_solution_architecture_lists_the_metrics_component_with_the_same_domain_path() -> None:
    solution = SOLUTION_DOC.read_text(encoding="utf-8")
    container = CONTAINER_DOC.read_text(encoding="utf-8")

    assert "| Metrics |" in solution
    assert "ps.service.metrics" in solution
    assert "ps.service.metrics" in container
    assert "operational metrics" in solution
    assert "never through the chart proxy" in solution


def test_the_operations_guide_links_ready_and_metrics() -> None:
    guide = (DOCS / "artifacts" / "operations-guide.md").read_text(encoding="utf-8")
    ready = guide[guide.index("### What `/ready` reveals") : guide.index("### Metrics")]

    assert "[Metrics](#metrics)" in ready
