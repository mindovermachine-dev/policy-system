"""Tests for `tools/domain-mapper/exclusion_audit.py`'s sample-selection stage (issue #27,
Slice 3, PLAN.md §3 "Slice 3 — Widen sampling to the real Tier A + Tier B strategy, CRA
only", AC-BI-003).

Hermetic and pure: `select_sample_units` takes only a `tuple[ExtractionUnit, ...]` and two
size parameters — no LLM/IO. A fixture list of 50 synthetic `ExtractionUnit`s proves:
Tier A is exactly the first `tier_a_size` in document order; a Tier-A-window unit whose
text also matches the Tier B regex is tagged `"A"`, never separately re-selected by Tier B
(CHANGES.md row 1a: "don't double-count/double-call it"); Tier B never re-selects a Tier A
unit; Tier B stops at `tier_b_size` even when more candidates exist; a unit matching
neither tier is excluded from the sample entirely; the regex neither over-matches a bare
"may" with no conditional clause nor matches across a gap wider than the 80-char lookahead
window (PLAN.md §1 Open Question 4 — documented as a known, accepted boundary, not a bug).

Loads `exclusion_audit.py` by path, same pattern as the sibling `test_exclusion_audit_*.py`
files (`tools/domain-mapper/` is hyphenated, not an importable dotted package).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ps_service.domain_mapper.models import ExtractionUnit

if TYPE_CHECKING:
    from types import ModuleType

_TOOLS_DOMAIN_MAPPER_DIR = Path(__file__).resolve().parents[3] / "tools" / "domain-mapper"
_SCRIPT_PATH = _TOOLS_DOMAIN_MAPPER_DIR / "exclusion_audit.py"
_MODULE_NAME = "_exclusion_audit_sampling_under_test"


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


def _unit(index: int, text: str) -> ExtractionUnit:
    return ExtractionUnit(
        citation_ref=f"Art. {index}",
        text=text,
        article_number=str(index),
        paragraph_number="1",
        article_heading="Heading",
    )


_PLAIN_DUTY_TEXT = "The manufacturer shall ensure conformity with the essential requirements."

# A conditional-permissive-shaped text placed INSIDE the Tier A window (index 5, < 30) --
# must land tagged "A" by position, never separately picked up by Tier B.
_TIER_A_WINDOW_CONDITIONAL_TEXT = (
    "The authority may suspend the certificate where the manufacturer fails to cooperate."
)

# Bare "may" with no where/if/unless anywhere nearby -- must NOT match (regex must not
# over-match a bare possibility with no conditional clause).
_BARE_MAY_TEXT = "The authority may suspend the certificate immediately for serious non-compliance."

# "may" ... "where", but the gap between them is >80 chars -- must NOT match (PLAN.md §1
# Open Question 4's accepted false-negative boundary).
_LONG_GAP_TEXT = "The regulator may " + ("x" * 90) + " decide where appropriate in the case."

# No "may" at all -- an ordinary substantive duty, must not land in either tier.
_NO_MAY_TEXT = "The importer shall verify that the manufacturer has carried out the assessment."


def _tier_b_match_text(index: int) -> str:
    return f"A body may suspend authorisation {index} where the holder fails to comply."


def _build_fixture_units() -> tuple[ExtractionUnit, ...]:
    units: list[ExtractionUnit] = []

    # Tier A window: indices 0-29 (30 units), all plain duty text except index 5.
    for i in range(30):
        text = _TIER_A_WINDOW_CONDITIONAL_TEXT if i == 5 else _PLAIN_DUTY_TEXT
        units.append(_unit(i, text))

    # Index 30-32: Tier B candidates that must NOT match, placed before the real matches
    # so a selection bug that just took "the next N units" (rather than filtering) would
    # be caught.
    units.append(_unit(30, _BARE_MAY_TEXT))
    units.append(_unit(31, _LONG_GAP_TEXT))
    units.append(_unit(32, _NO_MAY_TEXT))

    # Index 33-44: 12 real Tier B matches -- more than tier_b_size (10), to prove the scan
    # stops at the cap rather than collecting all matches.
    for i in range(33, 45):
        units.append(_unit(i, _tier_b_match_text(i)))

    # Index 45-49: trailing plain units, proving the scan doesn't run past a satisfied cap
    # nor pick up anything once tier_b_size is already met.
    for i in range(45, 50):
        units.append(_unit(i, _PLAIN_DUTY_TEXT))

    return tuple(units)


_UNITS = _build_fixture_units()


def test_tier_a_is_exactly_first_30_units_in_document_order() -> None:
    module = _load_exclusion_audit_module()

    sampled = module.select_sample_units(_UNITS)

    tier_a = [s for s in sampled if s.selection_tier == "A"]
    assert [s.unit.citation_ref for s in tier_a] == [f"Art. {i}" for i in range(30)]
    assert all(s.tier_b_match_text is None for s in tier_a)


def test_tier_a_window_unit_matching_the_regex_stays_tagged_a_not_double_selected() -> None:
    """CHANGES.md row 1a: a Tier-A-window unit whose text also matches the Tier B regex is
    never double-counted/double-called as a separate Tier B entry.
    """
    module = _load_exclusion_audit_module()

    sampled = module.select_sample_units(_UNITS)

    matches = [s for s in sampled if s.unit.citation_ref == "Art. 5"]
    assert len(matches) == 1
    assert matches[0].selection_tier == "A"


def test_tier_b_stops_at_cap_even_with_more_candidates_available() -> None:
    module = _load_exclusion_audit_module()

    sampled = module.select_sample_units(_UNITS)

    tier_b = [s for s in sampled if s.selection_tier == "B"]
    assert len(tier_b) == 10
    # First 10 matches in document order (Art. 33 .. Art. 42) -- not Art. 43/44, which
    # exceed the cap.
    assert [s.unit.citation_ref for s in tier_b] == [f"Art. {i}" for i in range(33, 43)]


def test_tier_b_never_reselects_a_tier_a_unit() -> None:
    module = _load_exclusion_audit_module()

    sampled = module.select_sample_units(_UNITS)

    tier_a_refs = {s.unit.citation_ref for s in sampled if s.selection_tier == "A"}
    tier_b_refs = {s.unit.citation_ref for s in sampled if s.selection_tier == "B"}
    assert tier_a_refs.isdisjoint(tier_b_refs)


def test_unit_matching_neither_tier_is_excluded_from_the_sample() -> None:
    module = _load_exclusion_audit_module()

    sampled = module.select_sample_units(_UNITS)

    sampled_refs = {s.unit.citation_ref for s in sampled}
    for excluded_index in (30, 31, 32, 43, 44, 45, 46, 47, 48, 49):
        assert f"Art. {excluded_index}" not in sampled_refs


def test_regex_does_not_overmatch_bare_may_with_no_conditional_clause() -> None:
    module = _load_exclusion_audit_module()
    assert module._CONDITIONAL_PERMISSIVE_PATTERN.search(_BARE_MAY_TEXT) is None


def test_regex_does_not_match_across_a_gap_wider_than_the_lookahead_window() -> None:
    """PLAN.md §1 Open Question 4: an accepted false-negative boundary, not a bug."""
    module = _load_exclusion_audit_module()
    assert module._CONDITIONAL_PERMISSIVE_PATTERN.search(_LONG_GAP_TEXT) is None


def test_tier_b_match_text_is_captured_for_reporting() -> None:
    module = _load_exclusion_audit_module()

    sampled = module.select_sample_units(_UNITS)

    tier_b = [s for s in sampled if s.selection_tier == "B"]
    for sampled_unit in tier_b:
        assert sampled_unit.tier_b_match_text is not None
        assert "may" in sampled_unit.tier_b_match_text.lower()


def test_custom_tier_sizes_are_honored() -> None:
    module = _load_exclusion_audit_module()

    sampled = module.select_sample_units(_UNITS, tier_a_size=5, tier_b_size=2)

    tier_a = [s for s in sampled if s.selection_tier == "A"]
    tier_b = [s for s in sampled if s.selection_tier == "B"]
    assert [s.unit.citation_ref for s in tier_a] == [f"Art. {i}" for i in range(5)]
    assert len(tier_b) == 2
    # With tier_a_size=5, index 5's conditional text is now OUTSIDE the Tier A window and
    # becomes the first Tier B match.
    assert tier_b[0].unit.citation_ref == "Art. 5"
