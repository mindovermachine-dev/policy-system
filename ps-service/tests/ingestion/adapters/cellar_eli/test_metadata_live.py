r"""Live verification: `extract_metadata`'s RDF-based `effective_date` resolution
(issue #62, `.orchestrator/tracker/issue-62-cellar-effective-date/PLAN_REVISED.md`
§8 item 10 / §9 Slice 10) against the real Cellar/ELI service.

`@pytest.mark.cellar_live` on every test in this module (via `pytestmark`):
real network calls to `publications.europa.eu` -- excluded from the fast
regression suite (`-m "not cellar_live"`) and must be run explicitly:

    uv run pytest ps-service/tests/ingestion/adapters/cellar_eli/test_metadata_live.py \
        -m cellar_live -v

Placed alongside `test_structure_live.py` (same directory, same
`@pytest.mark.cellar_live` convention) rather than extending
`ps-service/tests/api/test_ingestion_orchestration_live.py`: that module's
own purpose is `resolve_via_cellar` (the API-orchestration layer -- catalog
lookup, fetch-once caching, `PipelineStageError` wrapping); this module's
purpose is `extract_metadata` itself (`fetch_xhtml` + `fetch_rdf` +
`extract_metadata`, no orchestration layer involved) -- a cleaner match for
this issue's actual surface, and consistent with `test_structure_live.py`'s
existing precedent of testing `cellar_eli` adapter internals directly against
live data in this same directory.

## Headline finding from this slice (read before trusting any single date below)

Cellar's RDF `cdm:resource_legal_date_entry-into-force` /
`cdm:resource_legal_date_entry-into-force` predicate is **not a single
canonical value** for a regulation -- inspecting the live RDF's OWL
reification blocks (`owl:Axiom` wrapping `owl:annotatedProperty` /
`owl:annotatedTarget`, with `j.2:type_of_date` / `j.2:comment_on_date`
qualifiers Cellar attaches to each assertion) shows every regulation checked
in this slice asserts **multiple** dates under this one predicate name, each
tagged with a distinct semantic role:

- `type_of_date = EV` ("Entrée en Vigueur" / entry into force) -- the
  legal entry-into-force date, always `publication date + 20 days`
  (`comment_on_date` reads `DATPUB +20 ... ART <n>`).
- `type_of_date = MA` ("Mise en Application" / date of application) --
  when `comment_on_date` carries no `MA/PART` qualifier, this is the
  regulation's **general** "shall apply from" date -- the value
  `ps-domain-concepts.md`'s worked examples and this repo's own
  `test_pipeline_live.py::_GROUND_TRUTH` record as `effective_date`
  (confirmed exactly for CRA: `2027-12-11`, Art. 71.2, no `MA/PART`; and
  GDPR: `2018-05-25`, Art. 99, no `MA/PART`).
- `type_of_date = MA` **with** an `MA/PART` qualifier -- a narrower,
  staggered application date for one specific provision (e.g. CRA
  Art. 71.2's `2026-06-11`/`2026-09-11` partial dates; the AI Act's four
  `MA/PART` dates for Art. 113(a)/(b)/(c)).

`metadata.py`'s current `_pick_effective_date` (PLAN_REVISED.md §3, "earliest
of all candidate values wins") has **no awareness of `type_of_date` at
all** -- it pools every value under the predicate name, regardless of role,
and takes the minimum. Because `EV` is by construction always the earliest
(it is fixed at `publication + 20 days`, before any `MA` date), this means
**the current mechanism always resolves a multi-valued regulation to its EV
(legal entry-into-force) date, never its MA (application) date** -- the
opposite of what `ps-domain-concepts.md` and the pre-existing GDPR ground
truth define `effective_date` to mean for a `regulation`. This is confirmed
as a real regression below (AC-BI-012's CRA/GDPR assertions) -- not a
hypothetical. See `IMPL_SLICE_10_LIVE.md` for the full evidence and every
CELEX's raw reification data.

NIS2 (a `directive`) is unaffected: its predicate (`directive_date_transposition`)
uses a different, 2-value `ADOPTION`/`APPLICATION` `comment_on_date` scheme
(not `EV`/`MA`), and the domain-correct value (the transposition deadline,
`ADOPTION`) happens to already be the earlier of the two -- so the existing
min-wins tie-break is correct for NIS2, coincidentally, not because the rule
generalizes.

Fixing `_pick_effective_date`/`_EFFECTIVE_DATE_PREDICATES_BY_INSTRUMENT_TYPE`
to be `type_of_date`-aware is out of this slice's scope (Slice 10 is live
verification only, per the task brief) -- reported here as acceptance-gating
evidence for the orchestrator, not fixed in place.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
from defusedxml.ElementTree import fromstring as parse_xml

from ps_service.ingestion.adapters.cellar_eli.fetch import fetch_rdf, fetch_xhtml
from ps_service.ingestion.adapters.cellar_eli.metadata import (
    _own_subjects,  # pyright: ignore[reportPrivateUsage] -- live proof of §2.2's own-subject discovery, not just extract_metadata's end-to-end result
    _parse_subject_blocks,  # pyright: ignore[reportPrivateUsage] -- same reason
    extract_metadata,
)

if TYPE_CHECKING:
    from ps_service.ingestion.models import RegulatoryInstrumentMetadata

pytestmark = [pytest.mark.cellar_live]


def _resolve(celex: str) -> RegulatoryInstrumentMetadata:
    """The real, unmocked call chain this whole module exists to exercise:
    `fetch_xhtml` + `fetch_rdf` (two genuine Cellar/ELI HTTP round-trips) then
    `extract_metadata` (pure, no I/O) against their real bytes.
    """
    return extract_metadata(fetch_xhtml(celex), fetch_rdf(celex), celex)


# --- Part 1: core mechanism (PLAN_REVISED.md §8 item 10, §9 Slice 10) -------


def test_nis2_transposition_tie_break_resolves_the_earliest_duplicate_value() -> None:
    """NIS2 (`32022L2555`) is fact #1's real tie-break-under-duplicate-values
    case: `cdm:directive_date_transposition` is asserted twice on the same
    subject (`oj/JOL_2022_333_R_0002`) -- `2024-10-17` (comment_on_date tag
    `ADOPTION`, Art. 41.1: the Member-State transposition deadline) and
    `2024-10-18` (tag `APPLICATION`, Art. 41.1: when the national measures
    must apply). `ps-domain-concepts.md` line 624 and `metadata.py`'s own
    module docstring both record `2024-10-17` as the correct value -- the
    earlier of the two, matching `_pick_effective_date`'s earliest-wins rule.
    """
    metadata = _resolve("32022L2555")
    assert metadata.effective_date == date(2024, 10, 17)
    assert metadata.instrument_type == "directive"


def test_32025r0038_resolves_the_related_subject_entry_into_force_date() -> None:
    """`32025R0038`'s usable `cdm:resource_legal_date_entry-into-force` lives
    on a related resource (`oj/L_202500038`), not the CELEX's own trivial
    `resource/celex/32025R0038` node (fact #2) -- Tier 2 widening, no
    ambiguity (exactly one value asserted: `2025-02-04`, `type_of_date=EV`,
    `DATPUB +20`, Art. 26). This value was independently live-confirmed
    during planning (PLAN_REVISED.md §1's Slice-0 follow-up); re-asserted
    here as this issue's actual acceptance evidence, not carried on trust.
    """
    metadata = _resolve("32025R0038")
    assert metadata.effective_date == date(2025, 2, 4)
    assert metadata.instrument_type == "regulation"


def test_32019r0881_resolves_the_general_date_not_the_narrower_subset_date() -> None:
    """Live test of PLAN_REVISED.md §5's hypothesis for the ENISA Regulation
    (`32019R0881`): does Cellar's RDF metadata record only the *general*
    entry-into-force date, or does a narrower named-subset date leak in too?

    Inspecting the live RDF (via the production `_parse_subject_blocks`/
    `_own_subjects` helpers, not a separate ad hoc parser) shows **two**
    `resource_legal_date_entry-into-force` values on the same related
    subject (`oj/JOL_2019_151_R_0002`, discovered via Tier 2 widening, same
    shape as `32025R0038`):
    - `2019-06-27` (`type_of_date=EV`, `DATPUB +20`, Art. 69.1) -- the
      Regulation's own general entry into force.
    - `2021-06-28` (`type_of_date=MA` with an `MA/PART` qualifier, Art.
      69.2) -- Title III's cybersecurity-certification framework applying
      24 months later, a genuinely narrower named-subset date.

    The hypothesis holds for this CELEX specifically: `_pick_effective_date`'s
    earliest-wins rule selects `2019-06-27`, the general date, never the
    `MA/PART` subset date -- but only because the general date happens to be
    earlier here. This is the same coincidence noted for NIS2 in this
    module's docstring, not a `type_of_date`-aware selection -- flagged, not
    relied upon as a general guarantee.
    """
    rdf = fetch_rdf("32019R0881")
    blocks = _parse_subject_blocks(parse_xml(rdf))
    own_subjects = _own_subjects(blocks, "32019R0881")
    related_subject = "http://publications.europa.eu/resource/oj/JOL_2019_151_R_0002"
    assert related_subject not in own_subjects, (
        "expected this subject to require Tier 2 widening (no resource_legal_id_celex "
        "of its own), matching 32025R0038's shape -- if this now fails, the RDF shape "
        "changed and this test's own premise needs re-checking, not a loosened assertion"
    )
    assert blocks[related_subject]["resource_legal_date_entry-into-force"] == [
        "2019-06-27",
        "2021-06-28",
    ]

    metadata = _resolve("32019R0881")
    assert metadata.effective_date == date(2019, 6, 27)
    assert metadata.instrument_type == "regulation"


# --- Part 2: AC-BI-005 verify-then-select ------------------------------------


# Verify-then-select procedure (PLAN_REVISED.md Change #2 / FLAWS.md MAJOR #2):
# do not pre-commit to DORA. Checked, in order, whether each AC-BI-011
# candidate's date lives trivially on its own literal
# `resource/celex/{CELEX}` fetch-URL subject (which would mean even a naive,
# undiscovered "treat the fetch URL as the subject" implementation would
# succeed, proving nothing about §2.2's real discovery mechanism):
#
#   DORA (32022R2554): own subject discovered via `resource_legal_id_celex`
#   matching is `oj/JOL_2022_333_R_0001` -- NOT the literal fetch-URL subject
#   (`resource/celex/32022R2554`), which was live-confirmed to assert
#   neither `resource_legal_id_celex` nor any date predicate at all. DORA
#   genuinely requires §2.2's discovery mechanism -- checked first (it is
#   listed first among the six AC-BI-011 regression CELEX) and found
#   non-trivial on the first check, so no further candidates were needed.
#
# Selected: DORA (32022R2554).
def test_ac_bi_005_dora_requires_own_subject_discovery_not_a_naive_fetch_url_subject() -> None:
    """AC-BI-005's "additional real CELEX not yet checked in this issue,"
    selected via the verify-then-select procedure documented immediately
    above -- proves the *mechanism* (own-subject discovery + resolution),
    not merely that resolution succeeds.
    """
    rdf = fetch_rdf("32022R2554")
    blocks = _parse_subject_blocks(parse_xml(rdf))
    trivial_subject = "http://publications.europa.eu/resource/celex/32022R2554"
    own_subjects = _own_subjects(blocks, "32022R2554")

    assert trivial_subject not in own_subjects, (
        "DORA's own subject must NOT be its naive literal fetch-URL IRI -- "
        "that is exactly what makes this a genuine test of §2.2's discovery "
        "mechanism rather than a trivial case"
    )
    assert own_subjects == {"http://publications.europa.eu/resource/oj/JOL_2022_333_R_0001"}
    assert "resource_legal_date_entry-into-force" not in blocks.get(trivial_subject, {})

    metadata = _resolve("32022R2554")
    assert metadata.effective_date is not None
    assert metadata.instrument_type == "regulation"
    # NOT asserted against a specific expected date here: DORA's RDF asserts
    # two entry-into-force-type values (EV 2023-01-16, general MA
    # 2025-01-17 -- see this module's docstring); which one is domain-correct
    # is the EV/MA finding this slice surfaces, tracked under AC-BI-011
    # below, not this AC-BI-005 subject-resolution-mechanism test.


# --- Part 3: AC-BI-011 regression (6 CELEX) + AC-BI-012 regression (3 curated) --

# AC-BI-011: the 6 already-clean CELEX. IMPORTANT, itself a finding: no
# per-CELEX expected `effective_date` value is actually recorded anywhere in
# this codebase for these six -- `test_ingestion_orchestration_live.py`'s
# docstring (issue #61) only records that they "resolve cleanly" (i.e. don't
# raise) under the *old*, now-deleted heading-based mechanism, never the
# specific date each resolved to. The premise that these values were
# "already in this codebase's test fixtures/docstrings" does not hold up --
# reported here rather than silently assumed. Consequently this module only
# regression-tests the one thing that genuinely *was* established (successful
# resolution, no exception) for these six; it does not assert a byte-exact
# prior value that was never recorded.
_AC_BI_011_CELEX = [
    pytest.param("32022R2554", id="dora"),
    pytest.param("32022R2065", id="dsa"),
    pytest.param("32024R1689", id="ai_act"),
    pytest.param("32019R1150", id="p2b"),
    pytest.param("32021R0784", id="terrorist_content_online"),
    pytest.param("32023R2854", id="data_act"),
]


@pytest.mark.parametrize("celex", _AC_BI_011_CELEX)
def test_ac_bi_011_regulation_resolves_successfully(celex: str) -> None:
    """Regression re-run: each of the 6 already-clean CELEX must still
    resolve without raising under the new RDF-based mechanism. See this
    module's docstring and `IMPL_SLICE_10_LIVE.md` for the separate,
    substantial finding that the *specific* date each resolves to is very
    likely semantically wrong (EV instead of general MA) -- not asserted as
    a pass/fail here since no prior expected value exists to regress
    against; reported as diagnostic evidence instead.
    """
    metadata = _resolve(celex)
    assert metadata.effective_date is not None
    assert metadata.instrument_type == "regulation"


# AC-BI-012: the 3 curated catalog entries, against ps-domain-concepts.md's
# worked examples (CRA line 530, NIS2 line 624) and -- for GDPR, which has no
# worked example in ps-domain-concepts.md -- this repo's own
# `ps-service/tests/ingestion/test_pipeline_live.py::_GROUND_TRUTH["GDPR"]`
# (2018-05-25), the only other place this codebase records GDPR's
# established-correct effective_date.
_AC_BI_012_CASES = [
    pytest.param("32024R2847", date(2027, 12, 11), "regulation", id="cra"),
    pytest.param("32016R0679", date(2018, 5, 25), "regulation", id="gdpr"),
    pytest.param("32022L2555", date(2024, 10, 17), "directive", id="nis2"),
]


@pytest.mark.parametrize(
    ("celex", "expected_effective_date", "expected_instrument_type"), _AC_BI_012_CASES
)
def test_ac_bi_012_curated_catalog_effective_dates(
    celex: str, expected_effective_date: date, expected_instrument_type: str
) -> None:
    """The 3 curated `REGULATION_CATALOG` entries must resolve to exactly
    the `effective_date` already documented as correct for each. NIS2 here
    is the same live assertion as
    `test_nis2_transposition_tie_break_resolves_the_earliest_duplicate_value`
    above (not a fresh HTTP fetch's worth of new evidence) -- included in
    this parametrization so AC-BI-012 has its own explicit, correctly-labeled
    assertion rather than only an incidental Part-1 pass-through.
    """
    metadata = _resolve(celex)
    assert metadata.instrument_type == expected_instrument_type
    assert metadata.effective_date == expected_effective_date
