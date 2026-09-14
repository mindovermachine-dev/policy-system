"""Tests for `tools/domain-mapper/exclusion_audit.py`'s row rendering (issue #27, Slice 1).

Row-rendering only at this slice — the aggregate-table/recommendation tests land in
Slice 4 (PLAN.md §3 Slice 1). Also carries this slice's end-to-end proof (per the
implementation brief: the full capture -> flag -> propose(AI-labeled) -> report-row chain,
exercised hermetically over the 3 fixture units `test_exclusion_audit_capture.py`
defines), since `render_report_row` is the chain's final consumer.

Loads `exclusion_audit.py` by path exactly as the sibling `test_exclusion_audit_*.py`
files do (own `_MODULE_NAME` so `sys.modules` keys never collide when this tool's own
test files run together in one session).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from litellm.types.utils import Choices, Message, ModelResponse

from ps_service.domain_mapper.models import ExtractionUnit

if TYPE_CHECKING:
    from types import ModuleType

    from domain_mapper._fakes import MakeEmitter

_TOOLS_DOMAIN_MAPPER_DIR = Path(__file__).resolve().parents[3] / "tools" / "domain-mapper"
_SCRIPT_PATH = _TOOLS_DOMAIN_MAPPER_DIR / "exclusion_audit.py"
_MODULE_NAME = "_exclusion_audit_report_under_test"


def _load_exclusion_audit_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_MODULE_NAME]
        raise
    return module


def _model_response(content: str) -> ModelResponse:
    return ModelResponse(
        id="x",
        model="fake-model",
        choices=[
            Choices(
                finish_reason="stop", index=0, message=Message(content=content, role="assistant")
            )
        ],
    )


# --- render_report_row: standalone shape -----------------------------------


def test_render_report_row_without_proposal_includes_capture_fields(
    make_emitter: MakeEmitter,
) -> None:
    module = _load_exclusion_audit_module()
    record = module.AuditUnitRecord(
        regulation="CRA",
        citation_ref="CRA-1.0 Art. 1",
        article_number="1",
        paragraph_number="1",
        candidate_count=0,
        status="ok",
        is_flagged=True,
    )

    row = module.render_report_row(record, None)

    assert "CRA" in row
    assert "CRA-1.0 Art. 1" in row
    assert row.startswith("|")
    assert row.endswith("|")


def test_render_report_row_standalone_still_carries_ai_proposed_tag(
    make_emitter: MakeEmitter,
) -> None:
    """CHANGES.md row 4: a single row, rendered standalone (outside any aggregate report
    table), still carries the `*(AI-proposed)*` tag — not only the final Markdown
    report's table header.
    """
    module = _load_exclusion_audit_module()
    record = module.AuditUnitRecord(
        regulation="CRA",
        citation_ref="CRA-1.0 Art. 1",
        article_number="1",
        paragraph_number="1",
        candidate_count=0,
        status="ok",
        is_flagged=True,
    )
    proposal = module.ClassificationProposal(
        citation_ref="CRA-1.0 Art. 1",
        label="correct_exclusion_scope_applicability",
        rationale="Describes the Regulation's own scope.",
    )

    row = module.render_report_row(record, proposal)

    assert "AI-proposed" in row
    assert proposal.label in row
    assert proposal.rationale in row


# --- end-to-end: capture -> flag -> propose(AI-labeled) -> report-row, 3 fixture units --


def _scripted_call_completion(responses: dict[str, str]):
    def _call(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        user_content = messages[1]["content"]
        for citation_ref, response in responses.items():
            if f"Citation: {citation_ref}" in user_content:
                return _model_response(response)
        raise AssertionError(f"no scripted response for message: {user_content!r}")

    return _call


def test_full_pipeline_end_to_end_over_three_fixture_units(make_emitter: MakeEmitter) -> None:
    """The vertical slice this issue's Slice 1 exists to prove: capture -> flag ->
    propose(AI-labeled) -> report-row, chained end-to-end over 3 hermetic fixture units —
    one that extracts 0 candidates (flagged, then classified), one that extracts >0
    (not flagged, no classification attempted), and one whose extraction response is
    malformed (captured as an error, never conflated with a genuine zero-candidate flag,
    and likewise never sent to classification — PLAN.md §4's design note).
    """
    module = _load_exclusion_audit_module()
    emitter, _log_path = make_emitter()

    unit_zero = ExtractionUnit(
        citation_ref="CRA-1.0 Art. 1(1)",
        text="This Regulation applies to products with digital elements made available on "
        "the market.",
        article_number="1",
        paragraph_number="1",
        article_heading="Subject matter and scope",
    )
    unit_nonzero = ExtractionUnit(
        citation_ref="CRA-1.0 Art. 13(1)",
        text="The manufacturer shall conduct a cybersecurity risk assessment.",
        article_number="13",
        paragraph_number="1",
        article_heading="Obligations of manufacturers",
    )
    unit_error = ExtractionUnit(
        citation_ref="CRA-1.0 Art. 99(9)",
        text="The manufacturer shall report actively exploited vulnerabilities without "
        "undue delay.",
        article_number="99",
        paragraph_number="9",
        article_heading="Reporting obligations",
    )

    extraction_responses = {
        unit_zero.citation_ref: json.dumps({"requirements": []}),
        unit_nonzero.citation_ref: json.dumps(
            {
                "requirements": [
                    {
                        "role_name": "Manufacturer",
                        "text": "Conduct a cybersecurity risk assessment.",
                        "type": "requirement",
                        "letter_suffix": None,
                        "confidence": 0.92,
                    }
                ]
            }
        ),
        unit_error.citation_ref: "{not valid json",
    }
    extraction_call_completion = _scripted_call_completion(extraction_responses)

    records = [
        module.capture_unit_record(
            unit,
            regulation="CRA",
            model="fake-model",
            call_completion=extraction_call_completion,
            emitter=emitter,
        )
        for unit in (unit_zero, unit_nonzero, unit_error)
    ]
    record_zero, record_nonzero, record_error = records

    assert record_zero.status == "ok"
    assert record_zero.is_flagged is True
    assert record_nonzero.status == "ok"
    assert record_nonzero.is_flagged is False
    assert record_error.status == "error"
    assert record_error.is_flagged is False

    classification_response = json.dumps(
        {
            "label": "correct_exclusion_scope_applicability",
            "rationale": "Describes the Regulation's own scope, not a duty on an actor.",
        }
    )
    classification_call_completion = _scripted_call_completion(
        {record_zero.citation_ref: classification_response}
    )
    proposal = module.propose_classification(
        record_zero.citation_ref,
        unit_zero.text,
        model="fake-model",
        call_completion=classification_call_completion,
        emitter=emitter,
    )
    assert proposal.label == "correct_exclusion_scope_applicability"
    assert proposal.source == "ai_proposed"

    rows = [
        module.render_report_row(record_zero, proposal),
        module.render_report_row(record_nonzero, None),
        module.render_report_row(record_error, None),
    ]

    assert "AI-proposed" in rows[0]
    assert "correct_exclusion_scope_applicability" in rows[0]
    assert record_zero.citation_ref in rows[0]

    assert "AI-proposed" not in rows[1]
    assert record_nonzero.citation_ref in rows[1]

    assert "AI-proposed" not in rows[2]
    assert record_error.citation_ref in rows[2]
    assert "error" in rows[2]


# --- main() cached-mode assembly: fixture JSON in, zero new extraction calls --------
#
# The canonical `summarize_regulation`/`render_findings_report` hermetic tests
# (`test_summarize_regulation_counts`, `test_recommendation_reflects_miss_rate`,
# `test_report_labels_classifications_as_ai_proposed`, PLAN.md §3 Slice 4's own named
# tests) live further below in this file — this section only adds the one thing they
# don't cover: `main()`'s cached-mode file-IO wiring (CHANGES.md row 1b).


def test_main_cached_mode_assembles_report_from_json_with_zero_extraction_calls(
    tmp_path: Path, make_emitter: MakeEmitter
) -> None:
    """CHANGES.md row 1b: `main()` in its (default) cached mode reads
    `records/{regulation}.json` and assembles the report WITHOUT calling
    `capture_unit_record`/`propose_classification`/`route_completion` at all. Proven here
    by never providing a working `call_completion` — if cached mode ever tried a live
    call, this fixture would raise `AssertionError`, not silently succeed.
    """
    module = _load_exclusion_audit_module()

    records_dir = tmp_path / "records"
    records_dir.mkdir()
    report_path = tmp_path / "findings.md"

    for regulation, filename, citation_ref in (
        ("CRA", "cra.json", "CRA Art. 1"),
        ("GDPR", "gdpr.json", "GDPR Art. 1"),
        ("NIS2", "nis2.json", "NIS2 Art. 1"),
    ):
        payload = {
            "regulation": regulation,
            "records": [
                {
                    "regulation": regulation,
                    "citation_ref": citation_ref,
                    "article_number": "1",
                    "paragraph_number": "1",
                    "candidate_count": 0,
                    "status": "ok",
                    "is_flagged": True,
                    "unit_text": "Some scope text.",
                    "article_heading": "Scope",
                    "classification": {
                        "citation_ref": citation_ref,
                        "label": "correct_exclusion_scope_applicability",
                        "rationale": "Describes scope, not a duty.",
                        "source": "ai_proposed",
                    },
                    "selection_tier": "A",
                    "tier_b_match_text": None,
                }
            ],
        }
        (records_dir / filename).write_text(json.dumps(payload), encoding="utf-8")

    def _forbidden_call_completion(*, model: str, messages: list[dict[str, str]], timeout: float):
        raise AssertionError(
            "cached-mode main() must never call the LLM -- this call_completion "
            "should never be invoked"
        )

    exit_code = module.main(
        [
            "--cache-dir",
            str(records_dir),
            "--report-path",
            str(report_path),
            "--record-source",
            "cached",
        ],
        call_completion=_forbidden_call_completion,
    )

    assert exit_code == 0
    assert report_path.exists()
    report_text = report_path.read_text(encoding="utf-8")
    assert "CRA Art. 1" in report_text
    assert "GDPR Art. 1" in report_text
    assert "NIS2 Art. 1" in report_text
    assert "AI-proposed" in report_text
    assert "Provisional" in report_text


# --- Slice 4: summarize_regulation / render_findings_report (AC-BI-008/009) -


def _record(
    *,
    regulation: str = "CRA",
    citation_ref: str,
    candidate_count: int,
    status: str = "ok",
) -> object:
    module = _load_exclusion_audit_module()
    return module.AuditUnitRecord(
        regulation=regulation,
        citation_ref=citation_ref,
        article_number="1",
        paragraph_number="1",
        candidate_count=candidate_count,
        status=status,
        is_flagged=(status == "ok" and candidate_count == 0),
    )


def _proposal(*, citation_ref: str, label: str, rationale: str = "Because.") -> object:
    module = _load_exclusion_audit_module()
    return module.ClassificationProposal(
        citation_ref=citation_ref, label=label, rationale=rationale
    )


def test_summarize_regulation_counts(make_emitter: MakeEmitter) -> None:
    """`summarize_regulation` rolls up a regulation's records + AI-proposed proposals into
    the AC-BI-008 per-regulation totals: units processed, zero-candidate count, and a
    classification breakdown covering the 3 AC-BI-006 labels plus a flagged-but-
    unclassified count and an error count (PLAN.md §4's design note: an errored capture
    never folds into the 3-way classification breakdown).
    """
    module = _load_exclusion_audit_module()

    records = [
        _record(citation_ref="Art. 1", candidate_count=0),  # flagged, classified scope
        _record(citation_ref="Art. 2", candidate_count=0),  # flagged, classified cond-perm
        _record(citation_ref="Art. 3", candidate_count=0),  # flagged, classified miss
        _record(citation_ref="Art. 4", candidate_count=0),  # flagged, NOT classified
        _record(citation_ref="Art. 5", candidate_count=3),  # not flagged
        _record(citation_ref="Art. 6", candidate_count=0, status="error"),  # capture error
    ]
    proposals = [
        _proposal(citation_ref="Art. 1", label="correct_exclusion_scope_applicability"),
        _proposal(citation_ref="Art. 2", label="correct_exclusion_conditional_permissive"),
        _proposal(citation_ref="Art. 3", label="miss"),
    ]

    summary = module.summarize_regulation(records, proposals)

    assert summary.regulation == "CRA"
    assert summary.units_processed == 6
    assert summary.zero_candidate_count == 4
    assert summary.classification_breakdown["correct_exclusion_scope_applicability"] == 1
    assert summary.classification_breakdown["correct_exclusion_conditional_permissive"] == 1
    assert summary.classification_breakdown["miss"] == 1
    assert summary.classification_breakdown["unclassified"] == 1
    assert summary.classification_breakdown["error"] == 1


def test_recommendation_reflects_miss_rate(make_emitter: MakeEmitter) -> None:
    """AC-BI-009: the report's recommendation wording is keyed to the observed
    (AI-proposed, unreviewed) miss rate — 0 misses states "no action"; >=1 miss states
    "open a follow-up issue to narrow the prompt", matching AC-BI-009's own two named
    outcomes verbatim.
    """
    module = _load_exclusion_audit_module()

    no_miss_records = [_record(citation_ref="Art. 1", candidate_count=0)]
    no_miss_proposals = [
        _proposal(citation_ref="Art. 1", label="correct_exclusion_scope_applicability")
    ]
    no_miss_summary = module.summarize_regulation(no_miss_records, no_miss_proposals)

    no_miss_report = module.render_findings_report(
        [no_miss_summary],
        {"CRA": [module.render_report_row(no_miss_records[0], no_miss_proposals[0])]},
        "Methodology text.",
    )
    assert "no action" in no_miss_report.lower()
    assert "open a follow-up issue to narrow the prompt" not in no_miss_report.lower()

    has_miss_records = [_record(citation_ref="Art. 1", candidate_count=0)]
    has_miss_proposals = [_proposal(citation_ref="Art. 1", label="miss")]
    has_miss_summary = module.summarize_regulation(has_miss_records, has_miss_proposals)

    has_miss_report = module.render_findings_report(
        [has_miss_summary],
        {"CRA": [module.render_report_row(has_miss_records[0], has_miss_proposals[0])]},
        "Methodology text.",
    )
    assert "open a follow-up issue to narrow the prompt" in has_miss_report.lower()
    assert "no action" not in has_miss_report.lower()


def test_report_labels_classifications_as_ai_proposed(make_emitter: MakeEmitter) -> None:
    """The assembled findings report never presents a classification as human-confirmed:
    every classification-carrying row is tagged AI-proposed/unreviewed, and the
    recommendation section carries the provisional-pending-human-review heading
    (CHANGES.md row 4, CONTEXT.md user decision 2).
    """
    module = _load_exclusion_audit_module()

    record = _record(citation_ref="Art. 1", candidate_count=0)
    proposal = _proposal(citation_ref="Art. 1", label="correct_exclusion_scope_applicability")
    summary = module.summarize_regulation([record], [proposal])

    report = module.render_findings_report(
        [summary],
        {"CRA": [module.render_report_row(record, proposal)]},
        "Methodology text.",
    )

    assert "AI-proposed" in report
    assert "unreviewed" in report
    assert (
        "**Provisional recommendation — pending human review of the AI-proposed "
        "classifications above (see user decision 2, issue #27 context)**" in report
    )
