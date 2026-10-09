"""Content lint: the docs, skill and docstrings describe the full UC-4 re-ingest (issue #201).

`check_regulations` used to re-run only the Ingestion stage, and several documents said so. These
tests pin the opposite on every place that describes the behaviour (UC-4, the container
architecture, the solution architecture, the user guide, the `ps-check-regulations` skill and the
`trigger_reingestion` / package docstrings), and that no source file still cites the issue number.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ps_service import change_monitor
from ps_service.change_monitor import trigger

_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "ps-service" / "src" / "ps_service"
_DOCS = {
    "uc4": _REPO / "docs" / "architecture" / "ps-primary-use-cases.md",
    "container": _REPO / "docs" / "architecture" / "ps-service-container-architecture.md",
    "solution": _REPO / "docs" / "architecture" / "ps-solution-architecture.md",
    "user_guide": _REPO / "docs" / "artifacts" / "user-guide.md",
    "skill": _REPO / "ps-skills" / "ps-plugin" / "skills" / "ps-check-regulations" / "SKILL.md",
}

# Wording that described the old ingestion-only behaviour; it must not come back.
_BANNED = (
    "only the Ingestion stage",
    "counts are always 0",
    "its counts are always 0",
    "stops after Ingestion",
    "not yet chained",
    "drives only the Ingestion",
    "Deliberately stops",
    "0/0/0 because",
)


def _text(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


@pytest.mark.parametrize("name", sorted(_DOCS))
def test_no_document_describes_the_reingest_as_ingestion_only(name: str) -> None:
    text = _text(_DOCS[name])

    for phrase in _BANNED:
        assert phrase not in text, f"{name}: stale wording {phrase!r}"


def test_uc4_states_the_order_succession_last_resume_and_repair() -> None:
    text = _text(_DOCS["uc4"])

    assert "Ingestion → Domain Mapper → Company Merge" in text
    assert "as the last write" in text
    assert "only after Company Merge has returned" in text
    assert "leaves the prior version `active`" in text
    assert "repaired" in text


def test_container_architecture_documents_the_runner_markers_and_known_limits() -> None:
    text = _text(_DOCS["container"])

    assert "injected `PipelineRunner`" in text
    assert "`ReingestProgress`" in text
    assert "`absorbed`" in text
    assert "Company Merge still reads the whole baseline" in text
    assert "is not mapped" in text  # the legacy-link-then-newer-version limit
    assert "**Covered.**" in text


def test_user_guide_and_skill_say_the_counts_are_real_and_failures_carry_a_reason_code() -> None:
    guide = _text(_DOCS["user_guide"])
    skill = _text(_DOCS["skill"])

    assert "runs the full pipeline (Ingestion, Domain Mapper, Company Merge)" in guide
    assert "real" in skill
    assert "Company Merge" in skill
    assert "reason_code" in skill
    assert "pipeline_stage_failed (stage: derivation)" in skill


def test_trigger_docstrings_describe_the_full_pipeline_with_succession_last() -> None:
    module_doc = " ".join((trigger.__doc__ or "").split())
    function_doc = " ".join((trigger.trigger_reingestion.__doc__ or "").split())
    package_doc = " ".join((change_monitor.__doc__ or "").split())

    assert "Company Merge" in module_doc
    assert "LAST write" in module_doc
    assert "written last" in function_doc
    assert "repair" in function_doc
    assert "Company Merge" in package_doc
    assert "last" in package_doc
    for doc in (module_doc, function_doc, package_doc):
        for phrase in _BANNED:
            assert phrase not in doc


def test_no_source_file_cites_the_issue_number() -> None:
    offenders = [
        str(path.relative_to(_SRC))
        for path in _SRC.rglob("*.py")
        if "#201" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
