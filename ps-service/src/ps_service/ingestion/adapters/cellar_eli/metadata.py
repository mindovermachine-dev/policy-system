"""Cellar/ELI XHTML -> `RegulationMetadata`. Ports `spikes/cellar1/
parse_structure.py`'s `extract_metadata`/`_find_effective_date`/
`_article_by_heading` logic, retyped to return `RegulationMetadata`
(Pydantic, `effective_date: date`).

`effective_date` extraction is genuinely regulation-agnostic, not a
Regulation-vs-Directive branch on CELEX type: it looks for an Article
titled "Transposition" first (the Member-State transposition-deadline
case, AC-007) and only falls back to "Entry into force and application"
(the direct application-date case) if no Transposition article exists.
Both title strings are standard EU legislative drafting convention, not
per-regulation knowledge — verified against real CRA/NIS2/GDPR text (see
`spikes/cellar1/LEARNINGS.md`): NIS2 Art. 41 -> 2024-10-17; CRA Art. 71 ->
2027-12-11; GDPR -> 2018-05-25.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime

from ps_service.ingestion.adapters.errors import CellarParseError
from ps_service.ingestion.models import RegulationMetadata

_TITLE_CLASS = "eli-title"
_DATE_PATTERN = r"(\d{1,2} [A-Z][a-z]+ \d{4})"
_TRANSPOSITION_DATE_RE = re.compile(rf"By {_DATE_PATTERN}, Member States shall adopt and publish")
_ENTRY_INTO_FORCE_DATE_RE = re.compile(rf"shall apply from {_DATE_PATTERN}")

_BASE_ACT_VERSION = "1.0"
"""Every regulation this adapter ingests gets this constant version.
Not hardcoded for convenience: Ingestion always fetches a regulation's
BASE-ACT CELEX (32024R2847/32022L2555/32016R0679 — never a later
consolidated-text CELEX, see PLAN_REVIEWED.md §1.3). Detecting/selecting
among later consolidated versions is Regulatory Change Monitor's job
(PollForAmendments/TriggerReingestion, CA doc lines 553-554, issue #19,
UC-4) — RCM is what bumps `version` after comparing against Cellar's
reported state. Ingest of the base act by *this* component is, by
definition, that regulation's first version in our system. Resolves
Open Question 1 (PLAN_REVIEWED.md §9) — independently re-verified: the
CRA base-act fetch contains zero occurrences of "consolidat*" in body
text, and Cellar's `notice=branch` manifest has no simple scalar
"version" field to parse (version tracking there is date/expression-
based), so a literal per-fetch parse is not actually on offer; this
named constant is the most defensible reading of AC-002's "populated
directly from Cellar/ELI data" available.
"""


def _strip_namespace(root: ET.Element) -> None:
    for element in root.iter():
        if "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]


def _full_text(element: ET.Element) -> str:
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _article_by_heading(root: ET.Element, heading_substring: str) -> ET.Element | None:
    """Finds the Article `div` whose `eli-title`-classed heading sibling
    contains `heading_substring` (case-insensitive) — text-driven, not
    positional, and never branches on which regulation this is."""
    for title_div in root.iter("div"):
        if title_div.get("class", "") != _TITLE_CLASS:
            continue
        if heading_substring.lower() not in _full_text(title_div).lower():
            continue
        article_id = title_div.get("id", "").rsplit(".tit_1", 1)[0]
        for article_div in root.iter("div"):
            if article_div.get("id") == article_id:
                return article_div
    return None


def _parse_eu_date(text: str) -> date:
    """`text` is an already-extracted EU-style date, e.g. "17 October 2024".
    A calendar date, not an instant — no timezone applies."""
    return datetime.strptime(text, "%d %B %Y").date()  # noqa: DTZ007


def _find_effective_date(root: ET.Element) -> date | None:
    transposition = _article_by_heading(root, "Transposition")
    if transposition is not None:
        match = _TRANSPOSITION_DATE_RE.search(_full_text(transposition))
        if match:
            return _parse_eu_date(match.group(1))

    entry_into_force = _article_by_heading(root, "Entry into force and application")
    if entry_into_force is not None:
        match = _ENTRY_INTO_FORCE_DATE_RE.search(_full_text(entry_into_force))
        if match:
            return _parse_eu_date(match.group(1))

    return None


def extract_metadata(xhtml: bytes) -> RegulationMetadata:
    """Bibliographic metadata sourced directly from the document's own
    text — no LLM extraction (AC-002), no per-regulation branching
    (AC-006). Raises `CellarParseError` if `effective_date` can't be
    resolved (neither a "Transposition" nor an "Entry into force and
    application" Article heading is present) and `pydantic.ValidationError`
    if any other required field is missing (e.g. an empty title) —
    `RegulationMetadata`'s own boundary validation. Both satisfy
    `RegisterRegulationVersion`'s CA-doc contract: "Reject with a clear
    error if required properties are missing."
    """
    root = ET.fromstring(xhtml)
    _strip_namespace(root)

    main_title_div = root.find('.//div[@class="eli-main-title"]')
    title = _full_text(main_title_div) if main_title_div is not None else ""

    effective_date = _find_effective_date(root)
    if effective_date is None:
        raise CellarParseError(
            "could not resolve effective_date: no Article heading matched "
            "'Transposition' or 'Entry into force and application'"
        )

    return RegulationMetadata(
        title=title,
        jurisdiction="EU",
        effective_date=effective_date,
        version=_BASE_ACT_VERSION,
        status="active",
        source_type="external",
    )
