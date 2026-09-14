#!/usr/bin/env python3
r"""Maintainer script: audit Domain Mapper's prompt-level extraction exclusions (issue #27).

Domain Mapper's extraction prompt (`ps_service.domain_mapper.prompts.EXTRACTION_SYSTEM_PROMPT`)
deliberately instructs the model to emit no candidate at all for two categories of text —
scope/applicability clauses, and conditional-permissive ("may be X where <conditions>")
constructions (see that prompt's own module docstring for the rationale). From outside the
model this is indistinguishable from silent confidence-filtering, which AC-002 (every
extraction carries a confidence score, never dropped) exists to prevent for content the
model *does* extract — content never emitted gets no score at all. This audit tool exists
to tell "genuinely not a duty" from "the model quietly filtered something it should have
surfaced."

**Audit only — no prompt or production code changes.** This module only ever imports and
calls `ps_service.domain_mapper.extraction`/`prompts`/`models` — never edits them
(AC-BI-002).

**Slice 1** (`.orchestrator/tracker/issue-27-exclusion-audit/PLAN.md` §3, adjusted per
CHANGES.md rows 2/4): the hermetic capture -> flag -> propose(AI-labeled) -> report-row
chain, at tiny scope:

- `capture_unit_record` (AC-BI-001/004/005) reuses the exact per-unit extraction call
  `extract_roles_and_requirements` itself uses
  (`ps_service.domain_mapper.extraction._extract_candidates_for_unit`, imported directly —
  `ps-service/tests/domain_mapper/test_extraction.py` already establishes this exact
  module-private-import precedent) — never a reimplementation of the extraction call, the
  system prompt, or its response parsing.
- `propose_classification` (AC-BI-006/007) reuses the real `route_completion` (LLM
  Interface, no new client) with a new prompt string local to this module
  (`_CLASSIFICATION_SYSTEM_PROMPT`), paraphrasing — never editing — the same two
  exclusion-category definitions `prompts.py`'s own module docstring documents. Every
  `ClassificationProposal` this run produces carries `source="ai_proposed"`
  (CHANGES.md row 4) — no human-reviewed source exists yet
  (`.orchestrator/tracker/issue-27-exclusion-audit/CONTEXT.md` user decision 2): a
  sub-agent PROPOSES a classification per flagged unit; a human signs off separately.
- `render_report_row` renders one Markdown table row per unit, tagging any classification
  it carries `*(AI-proposed)*` in the row itself, not only a report's aggregate table
  header (CHANGES.md row 4) — so a row can never be mistaken for a human-confirmed
  classification even when read in isolation.

**Slice 3** (PLAN.md §3 "Slice 3 — Widen sampling to the real Tier A + Tier B strategy,
CRA only", adjusted per CHANGES.md row 1a): adds `select_sample_units` — the real,
document-order Tier A (first `_TIER_A_SIZE` units) + regex-flagged Tier B (up to
`_TIER_B_SIZE` further conditional-permissive units) sampler from PLAN.md §1, exercised
hermetically via `ps-service/tests/domain_mapper/test_exclusion_audit_sampling.py` and
live (additively over Slice 2's cached `records/cra.json`) via
`test_live_exclusion_audit.py`.

**Slice 4** (PLAN.md §3 "Slice 4 — All three regulations + the committed findings
report"): adds `RegulationSummary`/`summarize_regulation`/`render_findings_report`/
`load_regulation_records`, and a cached-mode-only `main()` that assembles the committed
`docs/audits/domain-mapper-exclusion-audit-findings.md` from `records/{regulation}.json`
without ever calling the LLM.

**Slice 5** (PLAN.md §3 "Slice 5 — CLI polish + AC-BI-002 close-out"): no new capture/
report logic — wraps Slice 4's `main()` in a real `argv`/`argparse` CLI surface
(`--cache-dir`, `--report-path`, `--record-source`, all documented in `--help`).
`--record-source` defaults to (and only accepts) `"cached"`, so running

    python tools/domain-mapper/exclusion_audit.py

with no flags at all can never trigger a live Azure OpenAI or FalkorDB call.

Usage:
    python tools/domain-mapper/exclusion_audit.py [--help]
    python tools/domain-mapper/exclusion_audit.py \
        --cache-dir .orchestrator/tracker/issue-27-exclusion-audit/records \
        --report-path docs/audits/domain-mapper-exclusion-audit-findings.md

This module is exercised hermetically via
`ps-service/tests/domain_mapper/test_exclusion_audit_capture.py`,
`test_exclusion_audit_classification.py`, `test_exclusion_audit_report.py`,
`test_exclusion_audit_sampling.py`, and `test_exclusion_audit_cli.py`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeIs, cast

from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.domain_mapper.extraction import (
    _extract_candidates_for_unit,  # pyright: ignore[reportPrivateUsage]  # audit tool reuses this module-internal per-unit extraction call directly, mirroring test_extraction.py's own established precedent (PLAN.md §0)
)
from ps_service.llm_interface.completion import route_completion
from ps_service.llm_interface.models import ChatMessage

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ps_service.domain_mapper.models import ExtractionUnit
    from ps_service.llm_interface.client import CompletionCaller
    from ps_service.logging import LogEmitter

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_RECORDS_DIR = (
    _REPO_ROOT / ".orchestrator" / "tracker" / "issue-27-exclusion-audit" / "records"
)
_DEFAULT_REPORT_PATH = _REPO_ROOT / "docs" / "audits" / "domain-mapper-exclusion-audit-findings.md"
_REPORT_REGULATIONS: tuple[tuple[str, str], ...] = (
    ("CRA", "cra.json"),
    ("GDPR", "gdpr.json"),
    ("NIS2", "nis2.json"),
)

type ClassificationLabel = Literal[
    "correct_exclusion_scope_applicability",
    "correct_exclusion_conditional_permissive",
    "miss",
]

_CLASSIFICATION_LABELS: tuple[ClassificationLabel, ...] = (
    "correct_exclusion_scope_applicability",
    "correct_exclusion_conditional_permissive",
    "miss",
)


@dataclass(frozen=True, slots=True)
class AuditUnitRecord:
    """One `ExtractionUnit`'s captured real-extraction outcome (AC-BI-004/005).

    `is_flagged` is `True` exactly when `status == "ok"` AND `candidate_count == 0` — a
    genuine zero-candidate result, never a unit whose call itself failed (`status ==
    "error"` always carries `is_flagged=False`; there is no "why zero" question to ask
    about a unit whose response could not even be parsed).
    """

    regulation: str
    citation_ref: str
    article_number: str
    paragraph_number: str
    candidate_count: int
    status: Literal["ok", "error"]
    is_flagged: bool


@dataclass(frozen=True, slots=True)
class ClassificationProposal:
    """An AI-proposed classification for one flagged (zero-candidate) unit (AC-BI-006/007).

    `source` is fixed at `"ai_proposed"` — no human-reviewed source exists yet for this
    run (CHANGES.md row 4, CONTEXT.md user decision 2). `render_report_row` renders it as
    a per-row `*(AI-proposed)*` tag, so a reader can never mistake a proposal for a
    human-confirmed classification, even reading one row in isolation.
    """

    citation_ref: str
    label: ClassificationLabel
    rationale: str
    source: Literal["ai_proposed"] = "ai_proposed"


# --- Sample selection (AC-BI-003, Slice 3: PLAN.md §1, adjusted per CHANGES.md row 6/7)
#
# Two tiers, both pure functions over the real adapter's real `tuple[ExtractionUnit, ...]`
# (no LLM/IO here). `_TIER_A_SIZE`/`_TIER_B_SIZE` are module constants, not hardcoded
# inline, so a follow-up widened run is a one-line change (PLAN.md §1).

_TIER_A_SIZE = 30
_TIER_B_SIZE = 10

# Tier B: a deliberately bounded near-neighbourhood match for "may be X where/if/unless
# <conditions>" (conditional-permissive), not a whole-article scan -- capped at 80 chars
# of lookahead (PLAN.md §1, Open Question 4: a genuinely long clause beyond 80 chars
# between "may" and its "where/if/unless" is an accepted false-negative risk for this
# bounded audit, not a correctness bug). `re.DOTALL` lets the lookahead cross a
# paragraph-internal newline within that same 80-char budget.
#
# CHANGES.md row 7: this regex also has a known false-POSITIVE shape -- e.g. "may impose
# fines where the infringement is severe" is an empowerment/duty-adjacent clause, not the
# target passive "may be X where <conditions>" pattern. Any real occurrence of that shape
# in a live run is noted in the audit's report/log as evidence against the regex's
# precision, never silently accepted or silently filtered here.
_CONDITIONAL_PERMISSIVE_PATTERN = re.compile(
    r"\bmay\b.{0,80}?\b(where|if|unless)\b", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True, slots=True)
class SampledUnit:
    """One `ExtractionUnit` selected into the audit sample, tagged with which tier selected it.

    Tier B additionally carries `tier_b_match_text` -- the exact regex match that
    triggered its inclusion -- so the report's Methodology section can show its work
    (AC-BI-003) rather than just assert the strategy in prose. `None` for a Tier A unit.
    """

    unit: ExtractionUnit
    selection_tier: Literal["A", "B"]
    tier_b_match_text: str | None = None


def select_sample_units(
    units: tuple[ExtractionUnit, ...],
    *,
    tier_a_size: int = _TIER_A_SIZE,
    tier_b_size: int = _TIER_B_SIZE,
) -> tuple[SampledUnit, ...]:
    """Select the audit sample from `units` (real adapter output, document order).

    Tier A: the first `tier_a_size` units, unconditionally.

    Tier B: up to `tier_b_size` further units, in document order, drawn only from
    `units[tier_a_size:]` (Tier A's own units are never re-selected, even one whose text
    also matches the conditional-permissive regex -- it is already tagged `"A"` by
    position), whose text matches `_CONDITIONAL_PERMISSIVE_PATTERN`. First match wins;
    scanning stops once `tier_b_size` units have been collected.
    """
    tier_a = tuple(SampledUnit(unit=unit, selection_tier="A") for unit in units[:tier_a_size])

    tier_b: list[SampledUnit] = []
    for unit in units[tier_a_size:]:
        if len(tier_b) >= tier_b_size:
            break
        match = _CONDITIONAL_PERMISSIVE_PATTERN.search(unit.text)
        if match is not None:
            tier_b.append(
                SampledUnit(unit=unit, selection_tier="B", tier_b_match_text=match.group(0))
            )

    return tier_a + tuple(tier_b)


def capture_unit_record(
    unit: ExtractionUnit,
    *,
    regulation: str,
    model: str,
    call_completion: CompletionCaller | None = None,
    emitter: LogEmitter | None = None,
) -> AuditUnitRecord:
    """Run the real per-unit extraction call for `unit` and capture its outcome.

    Calls `extraction._extract_candidates_for_unit` — the same call
    `extract_roles_and_requirements` itself uses (AC-BI-001), built against the REAL,
    unmodified `EXTRACTION_SYSTEM_PROMPT`, never a reimplementation.

    A `DomainMapperExtractionError` (a malformed/unparseable LLM response for this one
    unit) is caught here into `status="error"` — mirroring
    `extraction._extract_all_candidates`'s own per-unit failure isolation exactly. An
    `LlmProviderError` (an infra failure calling the LLM at all) is not caught here — it
    propagates and aborts the whole run, the same fail-fast boundary `extraction.py`
    follows throughout.
    """
    try:
        candidates = _extract_candidates_for_unit(
            unit, model=model, call_completion=call_completion, emitter=emitter
        )
    except DomainMapperExtractionError:
        return AuditUnitRecord(
            regulation=regulation,
            citation_ref=unit.citation_ref,
            article_number=unit.article_number,
            paragraph_number=unit.paragraph_number,
            candidate_count=0,
            status="error",
            is_flagged=False,
        )
    candidate_count = len(candidates)
    return AuditUnitRecord(
        regulation=regulation,
        citation_ref=unit.citation_ref,
        article_number=unit.article_number,
        paragraph_number=unit.paragraph_number,
        candidate_count=candidate_count,
        status="ok",
        is_flagged=candidate_count == 0,
    )


# --- Classification proposal (AC-BI-006/007) --------------------------------
#
# `_CLASSIFICATION_SYSTEM_PROMPT` paraphrases -- never edits -- the same two
# exclusion-category definitions `ps_service.domain_mapper.prompts.EXTRACTION_SYSTEM_PROMPT`'s
# own module docstring documents (`prompts.py:1-24`).

_CLASSIFICATION_SYSTEM_PROMPT = """You review one unit of an EU regulation/directive text \
that a compliance-extraction pass produced ZERO requirement candidates for, and classify \
why.

That extraction pass deliberately does not emit a candidate for two categories of text:
1. Scope/applicability clauses -- text describing what the regulation covers, to which \
products or entities it applies, or definitions of terms -- this describes the \
regulation's own scope, not a duty on a real-world actor.
2. Conditional-permissive constructions -- "may be X where/if/unless <conditions>" text \
that states a possibility gated on conditions, not an operative duty.

Given the unit's own text, decide exactly one of three outcomes:
- correct_exclusion_scope_applicability: the text is genuinely scope/applicability text \
(category 1).
- correct_exclusion_conditional_permissive: the text is genuinely a conditional-permissive \
construction (category 2).
- miss: the text states a genuine operative duty ("shall"/"shall not"/"should") that \
should have been extracted but was not -- neither exclusion category applies.

rationale: one sentence, grounded in the unit's own text, explaining your classification.

Return strict JSON: {"label": "correct_exclusion_scope_applicability" | \
"correct_exclusion_conditional_permissive" | "miss", "rationale": str}."""


class ExclusionAuditError(Exception):
    """A classification response could not be parsed into a `ClassificationProposal`."""


def _build_classification_messages(citation_ref: str, record_text: str) -> list[ChatMessage]:
    """System prompt + one user message carrying the unit's own text.

    Same delimiting shape as `extraction.py::_build_extraction_messages` — the unit's own
    text is never interpolated into the system prompt (L2 untrusted-content rule).
    """
    user_content = (
        f"Citation: {citation_ref}\n\n<regulation_text>\n{record_text}\n</regulation_text>"
    )
    return [
        ChatMessage(role="system", content=_CLASSIFICATION_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_content),
    ]


def propose_classification(
    citation_ref: str,
    record_text: str,
    *,
    model: str,
    call_completion: CompletionCaller | None = None,
    emitter: LogEmitter | None = None,
) -> ClassificationProposal:
    """Ask the real LLM to classify why a flagged unit produced zero candidates.

    Calls the REAL `route_completion` (LLM Interface reused, no new client or endpoint).
    Only ever called for a flagged (zero-candidate, `status="ok"`) unit — a unit that
    errored during capture has no text-based "why zero" question to answer. The returned
    proposal always carries `source="ai_proposed"` (CHANGES.md row 4).

    Raises `ExclusionAuditError` (naming `citation_ref`) on a malformed/unparseable
    response. An `LlmProviderError` from `route_completion` itself propagates unchanged —
    the same fail-fast infra boundary `capture_unit_record` follows.
    """
    messages = _build_classification_messages(citation_ref, record_text)
    result = route_completion(
        messages, model=model, call_completion=call_completion, emitter=emitter
    )
    return _parse_classification_response(result.text, citation_ref)


def _is_json_object(value: object) -> TypeIs[dict[str, object]]:
    """Narrow a `json.loads` result to a JSON object (keys are always strings)."""
    return isinstance(value, dict)


def _parse_classification_response(text: str, citation_ref: str) -> ClassificationProposal:
    """Parse one classification completion's raw text into a `ClassificationProposal`.

    Raises `ExclusionAuditError`, naming `citation_ref`, on malformed JSON, a non-object
    response, an unrecognized `label`, or a missing/empty `rationale`.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExclusionAuditError(
            f"classification response for {citation_ref!r} was not valid JSON: {exc}"
        ) from exc

    if not _is_json_object(payload):
        raise ExclusionAuditError(
            f"classification response for {citation_ref!r} was not a JSON object: {payload!r}"
        )

    label = payload.get("label")
    if not isinstance(label, str) or label not in _CLASSIFICATION_LABELS:
        raise ExclusionAuditError(
            f"classification response for {citation_ref!r} had an unrecognized label: {label!r}"
        )

    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ExclusionAuditError(
            f"classification response for {citation_ref!r} had a missing/empty rationale: "
            f"{rationale!r}"
        )

    return ClassificationProposal(citation_ref=citation_ref, label=label, rationale=rationale)


# --- Report row rendering (AC-BI-006/008, row-rendering only at Slice 1) ----


def render_report_row(record: AuditUnitRecord, proposal: ClassificationProposal | None) -> str:
    """Render one Markdown table row for `record`, with `proposal`'s label/rationale when given.

    A row carrying a classification always suffixes its label with `*(AI-proposed)*`
    (CHANGES.md row 4) — this per-row tag holds even for a row rendered standalone, never
    only via a report's aggregate table header (`test_exclusion_audit_report.py`'s own
    standalone-row test). Aggregate-table/methodology/recommendation rendering is Slice
    4's job (PLAN.md §3 Slice 1) — this function renders exactly one row.
    """
    if proposal is None:
        label_cell = "-"
        rationale_cell = "-"
    else:
        label_cell = f"{proposal.label} *(AI-proposed)*"
        rationale_cell = proposal.rationale
    flagged_cell = "yes" if record.is_flagged else "no"
    return (
        f"| {record.regulation} | {record.citation_ref} | {record.article_number} | "
        f"{record.paragraph_number} | {record.candidate_count} | {record.status} | "
        f"{flagged_cell} | {label_cell} | {rationale_cell} |"
    )


# --- Findings report assembly (Slice 4, AC-BI-008/009) ----------------------
#
# `summarize_regulation`/`render_findings_report` are pure functions over already-captured
# `AuditUnitRecord`/`ClassificationProposal` data -- no LLM/IO here, matching
# `select_sample_units`'s own "pure function over real data" shape. `main()`'s cached mode
# (CHANGES.md row 1b) is the only place in this module that touches the filesystem for
# report assembly, and it never imports/calls `route_completion` on that path -- report
# generation issues zero new extraction calls by construction, not just by convention.

_UNCLASSIFIED_KEY = "unclassified"
_ERROR_KEY = "error"


@dataclass(frozen=True, slots=True)
class RegulationSummary:
    """One regulation's AC-BI-008 rollup.

    Total units processed, zero-candidate (flagged) count, and a classification
    breakdown covering the 3 AC-BI-006 labels plus a flagged-but-unclassified count and
    a capture-error count (PLAN.md §4's design note: an errored capture never folds into
    the 3-way classification breakdown).
    """

    regulation: str
    units_processed: int
    zero_candidate_count: int
    classification_breakdown: dict[str, int]


def summarize_regulation(
    records: Sequence[AuditUnitRecord], proposals: Sequence[ClassificationProposal]
) -> RegulationSummary:
    """Roll up one regulation's captured records + AI-proposed classifications.

    `proposals` need only cover flagged (zero-candidate, `status="ok"`) units -- a flagged
    unit with no matching proposal counts against `_UNCLASSIFIED_KEY`, and a `status=
    "error"` record counts against `_ERROR_KEY`, never against the 3 AC-BI-006 labels.
    """
    regulation = records[0].regulation if records else ""
    proposals_by_ref = {proposal.citation_ref: proposal for proposal in proposals}

    breakdown: dict[str, int] = dict.fromkeys(_CLASSIFICATION_LABELS, 0)
    breakdown[_UNCLASSIFIED_KEY] = 0
    breakdown[_ERROR_KEY] = 0

    zero_candidate_count = 0
    for record in records:
        if record.status == _ERROR_KEY:
            breakdown[_ERROR_KEY] += 1
            continue
        if not record.is_flagged:
            continue
        zero_candidate_count += 1
        proposal = proposals_by_ref.get(record.citation_ref)
        if proposal is None:
            breakdown[_UNCLASSIFIED_KEY] += 1
        else:
            breakdown[proposal.label] += 1

    return RegulationSummary(
        regulation=regulation,
        units_processed=len(records),
        zero_candidate_count=zero_candidate_count,
        classification_breakdown=breakdown,
    )


_METHODOLOGY_TEXT = """\
Two tiers, per regulation, both pure functions over the real adapter's real
`tuple[ExtractionUnit, ...]` (no LLM/IO in the sampler itself) -- see
`select_sample_units` in `tools/domain-mapper/exclusion_audit.py`.

**Tier A -- first 30 units in document order.** Hypothesis, sanity-checked by the live
runs below (CHANGES.md row 6): EU regulations conventionally open with "Subject matter
and scope" and "Definitions" before substantive duties begin, so scope/applicability
exclusions cluster structurally at the start of the document. A document-order prefix
therefore deliberately front-loads exactly the text most likely to produce genuine
scope/applicability zero-candidate units, while also covering a real cross-section of
ordinary substantive articles for baseline comparison.

**Tier B -- up to 10 additional units for CRA (Slices 2-3), tightened to up to 8 for
GDPR/NIS2 in this Slice 4 run (per the orchestrator's corrected Slice 4 budget, below),
keyword-targeted.** Rationale: conditional-permissive constructions ("may be X where/
if/unless <conditions>") are scattered throughout substantive articles, not
front-loaded like scope text -- a document-order prefix alone would likely under-sample
this category. A deterministic, pure regex scan
(`\\bmay\\b.{0,80}?\\b(where|if|unless)\\b`, capped at 80 chars of lookahead) selects
further units -- in document order, first match wins, stopping once the tier cap is
reached or the regulation is exhausted (whichever comes first: GDPR's real run hit its
8-match cap; NIS2's real run found only 2 real matches in the rest of the document and
stopped there, short of the cap) -- from the rest of the regulation (excluding anything
Tier A already selected).

**Live-run budget (CHANGES.md row 1, tightened further for this Slice 4 run per the
orchestrator's corrected accounting):** the true cumulative extraction-call count going
into Slice 4 was 42 for CRA (37 persisted in `records/cra.json` + 5 extra real calls
from a prior slice's accidental test re-run, not reflected in the persisted cache
count). Against a 120-call ceiling and a 38-new-call-per-regulation hard cap, this run
actually spent 38 new extraction calls for GDPR (30 Tier A + 8 Tier B, hitting the cap)
and 32 for NIS2 (30 Tier A + only 2 real Tier B regex matches found, short of the cap)
-- 70 new calls total, for a true cumulative total of 112, an 8-call safety margin
against the 120 ceiling, never exceeded. Classification-proposal calls (one per flagged
unit) are separately budgeted, outside this ceiling (CONTEXT.md decision 5).

Still nowhere near the full corpus for any of the three regulations. Every row below is
an AI-proposed, unreviewed classification -- see the Recommendation section's heading.
"""


def _render_summary_table(summaries: Sequence[RegulationSummary]) -> str:
    header = (
        "| Regulation | Units processed | Zero-candidate (flagged) | "
        "Scope/applicability | Conditional-permissive | Miss | Unclassified | Error |"
    )
    separator = "|---|---|---|---|---|---|---|---|"
    lines = [header, separator]
    for summary in summaries:
        breakdown = summary.classification_breakdown
        lines.append(
            f"| {summary.regulation} | {summary.units_processed} | "
            f"{summary.zero_candidate_count} | "
            f"{breakdown.get('correct_exclusion_scope_applicability', 0)} | "
            f"{breakdown.get('correct_exclusion_conditional_permissive', 0)} | "
            f"{breakdown.get('miss', 0)} | {breakdown.get(_UNCLASSIFIED_KEY, 0)} | "
            f"{breakdown.get(_ERROR_KEY, 0)} |"
        )
    return "\n".join(lines)


def _render_recommendation(summaries: Sequence[RegulationSummary]) -> str:
    total_misses = sum(summary.classification_breakdown.get("miss", 0) for summary in summaries)
    if total_misses == 0:
        outcome = (
            "No action recommended: the AI-proposed classifications observed zero misses "
            "across the audited sample."
        )
    else:
        outcome = (
            "Open a follow-up issue to narrow the prompt: the AI-proposed classifications "
            f"observed {total_misses} miss(es) across the audited sample."
        )
    return (
        "**Provisional recommendation — pending human review of the AI-proposed "
        "classifications above (see user decision 2, issue #27 context)**\n\n"
        f"{outcome}"
    )


def render_findings_report(
    summaries: Sequence[RegulationSummary],
    rows_by_regulation: Mapping[str, Sequence[str]],
    methodology_text: str,
) -> str:
    """Assemble the full committed Markdown findings report (AC-BI-008/009).

    Every classification-carrying row is tagged `*(AI-proposed)*` by `render_report_row`
    itself (CHANGES.md row 4); this function additionally labels the detail table
    columns `*(AI-proposed, unreviewed)*` so a reader scanning only a table header still
    sees the caveat, and the Recommendation section carries the provisional-pending-
    human-review heading verbatim, never a bare conclusion (CONTEXT.md user decision 2).
    """
    sections = [
        "# Domain Mapper Exclusion Audit — Findings Report (Issue #27)",
        "",
        "## Methodology",
        "",
        methodology_text,
        "",
        "## Summary (AC-BI-008)",
        "",
        _render_summary_table(summaries),
        "",
        "## Per-unit detail *(AI-proposed, unreviewed)*",
        "",
    ]
    row_header = (
        "| Regulation | Citation | Article | Paragraph | Candidates | Status | Flagged | "
        "Classification *(AI-proposed, unreviewed)* | Rationale |"
    )
    row_separator = "|---|---|---|---|---|---|---|---|---|"
    for regulation, _ in _REPORT_REGULATIONS:
        rows = rows_by_regulation.get(regulation, ())
        sections.append(f"### {regulation}")
        sections.append("")
        sections.append(row_header)
        sections.append(row_separator)
        sections.extend(rows)
        sections.append("")
    sections.append("## Recommendation (AC-BI-009)")
    sections.append("")
    sections.append(_render_recommendation(summaries))
    sections.append("")
    return "\n".join(sections)


def _record_from_dict(entry: Mapping[str, object]) -> AuditUnitRecord:
    status = entry["status"]
    if status not in ("ok", "error"):
        raise ExclusionAuditError(
            f"cached record for {entry.get('citation_ref')!r} has an unrecognized "
            f"status: {status!r}"
        )
    return AuditUnitRecord(
        regulation=str(entry["regulation"]),
        citation_ref=str(entry["citation_ref"]),
        article_number=str(entry["article_number"]),
        paragraph_number=str(entry["paragraph_number"]),
        candidate_count=int(entry["candidate_count"]),  # pyright: ignore[reportArgumentType]
        status=status,
        is_flagged=bool(entry["is_flagged"]),
    )


def _proposal_from_dict(entry: Mapping[str, object]) -> ClassificationProposal:
    label = entry["label"]
    if label not in _CLASSIFICATION_LABELS:
        raise ExclusionAuditError(
            f"cached classification for {entry.get('citation_ref')!r} has an "
            f"unrecognized label: {label!r}"
        )
    return ClassificationProposal(
        citation_ref=str(entry["citation_ref"]),
        label=label,
        rationale=str(entry["rationale"]),
    )


def load_regulation_records(
    records_path: Path,
) -> tuple[list[AuditUnitRecord], list[ClassificationProposal]]:
    """Load one `records/{regulation}.json` cache file into `(records, proposals)`.

    This is Slice 2-4's own working-artifact shape -- pure file IO + parsing, no LLM call
    anywhere on this path (CHANGES.md row 1b: cached-mode report assembly issues zero new
    extraction calls).
    """
    payload = json.loads(records_path.read_text(encoding="utf-8"))
    entries = payload["records"]
    records = [_record_from_dict(entry) for entry in entries]
    proposals = [
        _proposal_from_dict(entry["classification"])
        for entry in entries
        if entry.get("classification") is not None
    ]
    return records, proposals


# --- CLI (Slice 5, PLAN.md §3 "Slice 5 -- CLI polish + AC-BI-002 close-out") -------
#
# No new capture/report logic here -- Slice 4 already wired cached-mode report assembly
# into `main()`'s body (unchanged below). This slice only wraps it in a real argv/
# argparse surface: `--cache-dir`/`--report-path`/`--record-source`, all documented in
# `--help`, with `--record-source` defaulting to (and, for now, only accepting) `"cached"`
# so simply running this script -- with no flags at all -- can never trigger a live Azure
# OpenAI or FalkorDB call (CHANGES.md row 1b's guardrail, now visible at the CLI surface
# too, not only in `main()`'s own keyword default).


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="exclusion_audit",
        description=(
            "Assemble the Domain Mapper exclusion-audit findings report (issue #27) "
            "from already-captured JSON records under --cache-dir. Cached mode ONLY: "
            "this CLI never makes a live Azure OpenAI or FalkorDB call -- the per-unit "
            "records it reads were captured separately by "
            "ps-service/tests/domain_mapper/test_live_exclusion_audit.py's live, "
            "explicitly-invoked tests (see PLAN.md/CHANGES.md under "
            ".orchestrator/tracker/issue-27-exclusion-audit/ for how)."
        ),
        epilog=(
            "Example: python tools/domain-mapper/exclusion_audit.py "
            "--cache-dir .orchestrator/tracker/issue-27-exclusion-audit/records "
            "--report-path docs/audits/domain-mapper-exclusion-audit-findings.md"
        ),
    )
    parser.add_argument(
        "--cache-dir",
        dest="records_dir",
        type=Path,
        default=_DEFAULT_RECORDS_DIR,
        help=(
            "Directory holding one {regulation}.json cache file per regulation "
            "(CRA/GDPR/NIS2), each a prior live capture run's captured "
            "AuditUnitRecord/ClassificationProposal data. "
            f"(default: {_DEFAULT_RECORDS_DIR})"
        ),
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=_DEFAULT_REPORT_PATH,
        help=(
            "Where the assembled findings-report Markdown is written; parent "
            f"directories are created if needed. (default: {_DEFAULT_REPORT_PATH})"
        ),
    )
    parser.add_argument(
        "--record-source",
        choices=["cached"],
        default="cached",
        help=(
            "Where per-unit records come from. Only 'cached' is implemented: records "
            "are read from --cache-dir and the real extraction/classification LLM is "
            "never called on this path. Defaulting to 'cached' (rather than requiring "
            "an explicit opt-in) is deliberate, so running this script with no flags "
            "at all can never accidentally spend real Azure OpenAI/FalkorDB calls."
        ),
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    call_completion: CompletionCaller | None = None,
) -> int:
    """Parse CLI args and assemble the committed findings report from cached JSON records.

    `argv` mirrors `tools/company-merge/company_merge_similarity_sweep.py`'s own CLI
    shape (`main(argv=None, ...)`, `if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))` below) -- pass an explicit list (e.g. `[]` for all
    defaults) when calling this from a test, exactly as that script's own CLI tests do;
    leaving it `None` outside of the `__main__` guard is only meaningful for a caller
    that wants real `sys.argv` parsing.

    CHANGES.md row 1b: `--record-source` defaults to (and, in this slice, only
    supports) `"cached"` -- it reads `--cache-dir`'s `{regulation}.json` files for
    CRA/GDPR/NIS2 and NEVER calls `capture_unit_record`/`propose_classification`/
    `route_completion`. `call_completion` is a keyword-only DI seam with deliberately no
    CLI flag (PLAN.md §3 Slice 5) -- accepted only so a caller can pass a poisoned fake
    (as `test_main_cached_mode_assembles_report_from_json_with_zero_extraction_calls`
    and this slice's own CLI tests do) to prove this function never reaches for it on
    the cached path; it is not read by cached-mode assembly itself. A live
    `--record-source` is out of scope for this slice -- `argparse` itself rejects any
    value other than `"cached"` (`choices=["cached"]`) before `main()`'s body ever
    runs, rather than silently falling back to a live sweep.
    """
    args = _parse_args(argv)
    records_dir = cast("Path", args.records_dir)
    report_path = cast("Path", args.report_path)
    record_source = cast('Literal["cached"]', args.record_source)

    if record_source != "cached":  # pragma: no cover -- unreachable: argparse's own
        # choices=["cached"] already rejects any other value before main() runs; kept
        # as a fail-fast belt-and-suspenders check, not a reachable branch.
        raise ValueError(
            f"only record_source='cached' is implemented (CHANGES.md row 1b); got {record_source!r}"
        )
    del call_completion  # unused on the cached path by design -- see docstring

    summaries: list[RegulationSummary] = []
    rows_by_regulation: dict[str, list[str]] = {}
    for regulation, filename in _REPORT_REGULATIONS:
        records, proposals = load_regulation_records(records_dir / filename)
        summaries.append(summarize_regulation(records, proposals))
        proposals_by_ref = {proposal.citation_ref: proposal for proposal in proposals}
        rows_by_regulation[regulation] = [
            render_report_row(record, proposals_by_ref.get(record.citation_ref))
            for record in records
        ]

    report_text = render_findings_report(summaries, rows_by_regulation, _METHODOLOGY_TEXT)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
