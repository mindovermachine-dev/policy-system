"""Cellar/ELI XHTML + RDF -> `RegulatoryInstrumentMetadata`.

`title` is sourced from the XHTML document's `eli-main-title` div, ported
from `spikes/cellar1/parse_structure.py`'s `extract_metadata` logic.

`effective_date` is sourced from the document's RDF/XML metadata, per
`PLAN_REVISED.md` (issue #62): subject-scoped resolution
(`_parse_subject_blocks`/`_resolve_predicate_candidates`, own-subject-first
with a principled widen), an instrument-type predicate dispatch
(`_EFFECTIVE_DATE_PREDICATES_BY_INSTRUMENT_TYPE`) that is genuinely
regulation-agnostic (never branches on which regulation this is, only on
the structural `instrument_type`), and a role-priority tie-break
(`_pick_effective_date`, PLAN_REVISED.md §3, post-Slice-10) for the case
where Cellar/ELI asserts more than one value for the same predicate
(AC-BI-004). Each value's semantic role (`MA_GENERAL`/`EV`/`MA_PART` for a
regulation, `ADOPTION`/`APPLICATION` for a directive) is read from Cellar's
own `owl:Axiom` reification (`_parse_reification_blocks`); earliest-wins is
retained only as a fallback for genuinely untagged values (verified against
NIS2's own duplicate `date_transposition` values, 2024-10-17/2024-10-18 --
both reified, `ADOPTION` wins by tag, coincidentally also the earlier one).
The earlier XHTML Article-heading-based extraction mechanism this replaced
has been removed (see IMPL_SLICE_5.md for the decision to stop calling it,
and IMPL_SLICE_9_CLEANUP_2.md for its removal).
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING

from defusedxml.ElementTree import fromstring as parse_xml

from ps_service.ingestion.adapters.errors import CellarParseError
from ps_service.ingestion.models import InstrumentType, RegulatoryInstrumentMetadata

if TYPE_CHECKING:
    import xml.etree.ElementTree as ET

_CELEX_TYPE_CODE_RE = re.compile(r"^\d{5}([A-Z]{1,2})\d+$")
"""CELEX legislation form: 1 sector digit + 4-digit year + 1-2 letter descriptor
(the type code) + running number. e.g. `32016R0679` -> `R`, `32022L2555` -> `L`.
Greedy `[A-Z]{1,2}` backtracks to 1 letter because the next char is a digit."""

_CONSOLIDATED_CELEX_RE = re.compile(r"^0(\d{4}[A-Z]{1,2}\d+)-\d{8}$")
"""Consolidated-expression CELEX: sector `0` (consolidated acts), the base act's
year+type+number, then a `-YYYYMMDD` "amendments incorporated up to" suffix.
e.g. `02024R2847-20241120` -> base group `2024R2847`. Branches only on the
identifier's own lexical form, never on which regulation it names (AC-006/AC-011).
"""


def _base_celex(identifier: str) -> str:
    """The base-act CELEX for `identifier`.

    A base-act CELEX (`32024R2847`) is passed through unchanged; a
    consolidated-expression CELEX (`02024R2847-20241120`) is reduced to its
    base act (`32024R2847`: drop the `-YYYYMMDD` suffix, sector `0` -> `3`).
    Raises `CellarParseError` for any other shape — including a sector-`0`
    identifier with no consolidation-date suffix.
    """
    consolidated = _CONSOLIDATED_CELEX_RE.match(identifier)
    if consolidated is not None:
        return f"3{consolidated.group(1)}"
    if not identifier.startswith("0") and _CELEX_TYPE_CODE_RE.match(identifier) is not None:
        return identifier
    raise CellarParseError(
        f"identifier {identifier!r} is neither a base-act CELEX nor a "
        "consolidated-expression CELEX (0YYYY<T>NNNN-YYYYMMDD)"
    )


def _base_celex_or_none(identifier: str) -> str | None:
    """`_base_celex`, but `None` for an unparseable value instead of raising.

    Per PLAN_REVISED.md §2.2 (Defect 2's fix). Used only when normalizing a
    *foreign* subject's own asserted `resource_legal_id_celex` value(s) for
    comparison against `own_id` -- an unparseable value must simply fail to
    match (conservatively still treated as foreign, the same "when in doubt,
    treat as foreign" policy the guard already had), not abort the whole
    resolution with an unrelated exception.
    """
    try:
        return _base_celex(identifier)
    except CellarParseError:
        return None


_INSTRUMENT_TYPE_BY_CELEX_CODE: dict[str, InstrumentType] = {
    "R": "regulation",
    "L": "directive",
}

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


def _local_name(tag: str) -> str:
    """The unqualified local name of a Clark-notation tag (`{ns}local` -> `local`)."""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _strip_namespace(root: ET.Element) -> None:
    for element in root.iter():
        element.tag = _local_name(element.tag)


def _full_text(element: ET.Element) -> str:
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


_RDF_NAMESPACE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_RDF_ABOUT_ATTRIBUTE = f"{{{_RDF_NAMESPACE}}}about"

type _SubjectBlocks = dict[str, dict[str, list[str]]]
"""Subject IRI -> predicate local name -> asserted text values (multiplicity preserved)."""


def _parse_subject_blocks(
    root: ET.Element,
) -> _SubjectBlocks:
    """Group an RDF/XML document's predicate assertions by subject IRI.

    Per PLAN_REVISED.md §2.1: every element anywhere in the document
    (`root.iter()`) carrying an `rdf:about` attribute is a subject block --
    detected by that attribute's presence, never by tag name, since Cellar
    serializes a subject either as a generic `rdf:Description` or as a typed
    node (e.g. `cdm:resource_legal`). Only a subject's *direct* children are
    recorded as its predicates, keyed by local tag name (namespace
    stripped); duplicate predicate values are preserved, not deduplicated,
    since AC-BI-004's tie-break needs every value. The tree is walked
    without first calling `_strip_namespace` -- subject identity depends on
    the namespace-qualified `rdf:about` attribute.
    """
    blocks: _SubjectBlocks = {}
    for element in root.iter():
        subject_iri = element.get(_RDF_ABOUT_ATTRIBUTE)
        if subject_iri is None:
            continue
        predicates = blocks.setdefault(subject_iri, {})
        for child in element:
            predicates.setdefault(_local_name(child.tag), []).append(_full_text(child))
    return blocks


_RDF_RESOURCE_ATTRIBUTE = f"{{{_RDF_NAMESPACE}}}resource"
_ANNOTATION_TOKEN_RE = re.compile(r"\{([^|{}]+)\|")

type _Reifications = dict[tuple[str, str, str], frozenset[str]]
"""(subject IRI, predicate local name, asserted value text) -> the union of
authority-table label tokens found in that owl:Axiom's `type_of_date` and/or
`comment_on_date` children (e.g. {"MA", "MA/PART"} or {"ADOPTION"}). Missing
key (no `.get(..., frozenset())` hit) means no reification was found for
that exact triple -- role classification (§3) treats this as "untagged"."""


def _parse_reification_blocks(root: ET.Element) -> _Reifications:
    """Associate each date value with its owl:Axiom-asserted semantic-role tokens.

    Per the post-Slice-10 Defect 1 fix. Matches on the exact RDF triple
    (subject IRI + predicate local name + literal value text) -- the same
    granularity OWL reification itself operates at; two logically different
    values that happened to share both subject, predicate, AND literal text
    would be indistinguishable by this (or any) reification read -- not
    observed in any CELEX checked (CRA/GDPR/NIS2/32019R0881 all have
    distinct values within each subject's own candidate set), flagged as a
    known, low-probability structural limit of the source data itself, not
    an implementation gap.
    """
    reifications: _Reifications = {}
    for element in root.iter():
        children = {_local_name(child.tag): child for child in element}
        source = children.get("annotatedSource")
        prop = children.get("annotatedProperty")
        target = children.get("annotatedTarget")
        if source is None or prop is None or target is None:
            continue
        subject_iri = source.get(_RDF_RESOURCE_ATTRIBUTE)
        predicate_uri = prop.get(_RDF_RESOURCE_ATTRIBUTE)
        if subject_iri is None or predicate_uri is None:
            continue
        predicate_name = predicate_uri.rsplit("#", 1)[-1]
        value_text = _full_text(target)
        tokens: set[str] = set()
        for label in ("type_of_date", "comment_on_date"):
            annotation = children.get(label)
            if annotation is not None:
                tokens.update(_ANNOTATION_TOKEN_RE.findall(_full_text(annotation)))
        reifications[(subject_iri, predicate_name, value_text)] = frozenset(tokens)
    return reifications


def _own_subjects(blocks: _SubjectBlocks, own_id: str) -> set[str]:
    """Subject IRIs asserting `own_id` as their (normalized) `resource_legal_id_celex` value.

    Per PLAN_REVISED.md §2.2: the CELEX's own subject is discovered from the
    graph itself via this property -- the one already proven, in
    `cellar_consolidated.py`, to identify a CELEX's own subject in this
    codebase -- never assumed to be the URL the document was fetched from.
    `own_id` is expected to already be a base-act CELEX (as returned by
    `_base_celex`), not a raw, possibly-consolidated-expression identifier.

    Post-Slice-10 fix (Defect 2): each asserted value is normalized through
    `_base_celex_or_none` before comparison, not compared raw -- a
    consolidated-expression `identifier`'s own subject asserts
    `resource_legal_id_celex` in unreduced form (e.g.
    `"02024R2847-20241120"`), which never equals `own_id` (`"32024R2847"`)
    without this normalization.
    """
    return {
        iri
        for iri, predicates in blocks.items()
        if own_id in {_base_celex_or_none(v) for v in predicates.get("resource_legal_id_celex", [])}
    }


def _resolve_predicate_candidates(
    blocks: _SubjectBlocks, identifier: str, predicate_names: tuple[str, ...]
) -> list[tuple[str, str, str]]:
    """Resolve candidate `(subject_iri, predicate_name, value_text)` triples.

    Tiered per PLAN_REVISED.md §2.3. Post-Slice-10: candidates carry their
    origin (subject IRI + predicate local name), not just the bare value
    text, so §3's role-based tie-break can look each one up in
    `_Reifications` by its exact triple.

    Tier 1: subjects asserting `identifier`'s own CELEX id ("own subjects",
    §2.2) -- this is AC-BI-004's literal scope. If any own subject asserts
    one of `predicate_names`, its values (pooled across every own subject
    that matches) are returned directly; a foreign subject is never
    consulted once Tier 1 has a match.

    Tier 2 (only reached when Tier 1 is empty): widen to every subject
    except the own subjects and except any subject that is itself a
    distinct legal act -- one asserting a `resource_legal_id_celex` value
    that normalizes (`_base_celex_or_none`, Defect 2's fix, both sides
    normalized) to a *different* base CELEX than `identifier`'s. This is
    the concrete guard against the "unrelated cited/amending act"
    contamination risk: such a subject has its own independent identity, so
    its properties are facts about a *different* instrument, never this
    one. Every surviving subject's values are pooled -- §3's role-based
    pick handles however many surviving subjects there are uniformly, no
    separate single-vs-multi-subject branch is needed here any more.

    Returns an empty list if no admissible subject matches at either tier.
    """
    own_id = _base_celex(identifier)

    def _values_for(subject_iris: set[str]) -> list[tuple[str, str, str]]:
        return [
            (iri, name, value)
            for iri, predicates in blocks.items()
            if iri in subject_iris
            for name in predicate_names
            for value in predicates.get(name, [])
        ]

    own_subjects = _own_subjects(blocks, own_id)
    own_matches = _values_for(own_subjects)
    if own_matches:
        return own_matches

    excluded = {
        iri
        for iri, predicates in blocks.items()
        if predicates.get("resource_legal_id_celex")
        and own_id not in {_base_celex_or_none(v) for v in predicates["resource_legal_id_celex"]}
    }
    widened_subjects = set(blocks) - own_subjects - excluded
    return _values_for(widened_subjects)


_EFFECTIVE_DATE_PREDICATES_BY_INSTRUMENT_TYPE: dict[InstrumentType, tuple[str, ...]] = {
    "regulation": ("date_entry-into-force", "resource_legal_date_entry-into-force"),
    "directive": ("date_transposition", "directive_date_transposition"),
}
"""RDF predicate local names to search for `effective_date`, per `instrument_type`.

Per PLAN_REVISED.md §4. The `regulation` tuple carries **two** names, not
one: Cellar's adopted `notice=non-inferred` fetch profile (`fetch.py`'s
`_RDF_HEADERS`) never emits the bare, generalized/inferred
`date_entry-into-force` name for a regulation -- only the non-generalized
`resource_legal_date_entry-into-force` name, live-confirmed for
`32025R0038` (same subject, same value). Without both names, every
regulation CELEX would fail to resolve under this mechanism (§10 item 9).
The `directive` tuple's two names were already both confirmed present and
need no change. `national_transposition` is intentionally absent -- this
adapter only ever computes `regulation`/`directive` from a CELEX type
code. Branch key is the structural `InstrumentType` value, never a CELEX
literal (AC-BI-010).
"""


_DATE_ROLE_PRIORITY_BY_INSTRUMENT_TYPE: dict[InstrumentType, tuple[str, ...]] = {
    # MA (general application) beats EV (entry into force) beats MA/PART
    # (a narrower, named-subset application date, e.g. one Title's staggered
    # start) -- MA/PART must never outrank EV: live-confirmed via 32019R0881,
    # which has ONLY EV + MA/PART (no general MA at all) and is
    # domain-correct at EV, not the later MA/PART value.
    "regulation": ("MA_GENERAL", "EV", "MA_PART"),
    # ADOPTION (the transposition deadline, ps-domain-concepts.md's own
    # documented meaning of a directive's effective_date) beats APPLICATION
    # (when the transposed measures take legal effect) -- selected by tag,
    # not because ADOPTION happens to be numerically earlier for NIS2.
    "directive": ("ADOPTION", "APPLICATION"),
}
"""Per PLAN_REVISED.md §3 (post-Slice-10, Defect 1's fix). Structural
`InstrumentType`-keyed dispatch, alongside `_EFFECTIVE_DATE_PREDICATES_BY_
INSTRUMENT_TYPE` -- satisfies AC-BI-010 the same way."""


def _date_role(instrument_type: InstrumentType, tokens: frozenset[str]) -> str | None:
    """Classify one value's semantic role from its reification tokens (§2.1b).

    Branches only on the structural `instrument_type` and the reification
    VALUE tokens Cellar itself asserts (EV/MA/MA-PART/ADOPTION/APPLICATION)
    -- never on which regulation/directive this is (AC-BI-010). Returns
    `None` for an untagged value or an unrecognized token set, which
    `_pick_effective_date` treats as "no known role."
    """
    if instrument_type == "regulation":
        if "MA" in tokens:
            return "MA_PART" if "MA/PART" in tokens else "MA_GENERAL"
        return "EV" if "EV" in tokens else None
    if instrument_type == "directive":
        if "ADOPTION" in tokens:
            return "ADOPTION"
        return "APPLICATION" if "APPLICATION" in tokens else None
    return None


def _pick_effective_date(
    candidates: list[tuple[str, str, str]],
    reifications: _Reifications,
    instrument_type: InstrumentType,
) -> date | None:
    """The tie-break rule (PLAN_REVISED.md §3, post-Slice-10 rewrite).

    Selects by semantic role, with earliest-wins as a fallback only when no
    candidate carries a recognized role tag.

    For each `(subject_iri, predicate_name, value_text)` candidate, look up
    its reification tokens by the exact triple (missing key -> `frozenset()`,
    i.e. untagged) and classify a role via `_date_role`. Group by role; walk
    `_DATE_ROLE_PRIORITY_BY_INSTRUMENT_TYPE[instrument_type]` in order and
    return the earliest date in the first non-empty role bucket. If no
    candidate matched any recognized role (an untagged duplicate, or
    reification data genuinely absent, e.g. `32025R0038`'s single unreified
    value, or a synthetic fixture with no `owl:Axiom` blocks at all), fall
    back to the earliest date across every candidate.

    Domain justification for the untagged fallback: this is a
    compliance-obligation system; when source data is genuinely ambiguous
    about a deadline with no role information at all, the conservative
    choice is the earlier one -- it never understates how soon a company
    must comply.
    """
    if not candidates:
        return None
    dated: list[tuple[date, str | None]] = []
    for subject_iri, predicate_name, value_text in candidates:
        tokens = reifications.get((subject_iri, predicate_name, value_text), frozenset())
        dated.append((date.fromisoformat(value_text), _date_role(instrument_type, tokens)))
    by_role: dict[str | None, list[date]] = {}
    for value, role in dated:
        by_role.setdefault(role, []).append(value)
    for role in _DATE_ROLE_PRIORITY_BY_INSTRUMENT_TYPE[instrument_type]:
        if role in by_role:
            return min(by_role[role])
    return min(value for value, _ in dated)  # untagged fallback


def _find_effective_date_from_rdf(
    blocks: _SubjectBlocks,
    reifications: _Reifications,
    identifier: str,
    instrument_type: InstrumentType,
) -> date | None:
    """Resolve `effective_date` from RDF subject blocks, per PLAN_REVISED.md §4.

    Looks up `instrument_type`'s predicate names, resolves candidate values
    via `_resolve_predicate_candidates`'s tiered subject-scoping (§2.3), and
    applies the role-priority tie-break (§3, `_pick_effective_date`), which
    parses each Cellar/ELI `xsd:date` value (`YYYY-MM-DD`) to a `date`.
    Returns `None` if no admissible subject asserts any of the predicate
    names.
    """
    predicate_names = _EFFECTIVE_DATE_PREDICATES_BY_INSTRUMENT_TYPE[instrument_type]
    candidates = _resolve_predicate_candidates(blocks, identifier, predicate_names)
    return _pick_effective_date(candidates, reifications, instrument_type)


def _instrument_type_from_celex(identifier: str) -> InstrumentType:
    """Map a CELEX identifier's type-code letter to its `instrument_type`.

    `R` -> regulation, `L` -> directive. Structural document metadata, not
    per-instrument knowledge — same code path for every CELEX. Raises
    `CellarParseError` naming the code for any other descriptor (e.g. `D`
    decision), never a default (AC-BI-012).
    """
    identifier = _base_celex(identifier)
    match = _CELEX_TYPE_CODE_RE.match(identifier)
    if match is None:
        raise CellarParseError(f"could not parse a CELEX type code from identifier {identifier!r}")
    code = match.group(1)
    try:
        return _INSTRUMENT_TYPE_BY_CELEX_CODE[code]
    except KeyError:
        raise CellarParseError(
            f"unsupported CELEX type code {code!r} in identifier {identifier!r}: "
            f"only 'R' (regulation) and 'L' (directive) are supported"
        ) from None


def extract_metadata(xhtml: bytes, rdf: bytes, identifier: str) -> RegulatoryInstrumentMetadata:
    """Bibliographic metadata sourced from the document's own text and RDF metadata.

    No LLM extraction (AC-002), no per-regulation branching (AC-006).
    `identifier` is the source CELEX number, used to derive `instrument_type`
    from its type code and to scope RDF subject resolution (§2 of
    PLAN_REVISED.md). `title` is still sourced from `xhtml`; `effective_date`
    is resolved from `rdf`'s structured Cellar/ELI metadata (own-subject/
    widen tiering, §2.3, plus the tie-break, §3) rather than XHTML Article
    headings. Raises `CellarParseError` if the CELEX type code is
    unsupported or if `effective_date` can't be resolved (no admissible RDF
    subject asserts any of the instrument type's target predicates) and
    `pydantic.ValidationError` if any other required field is missing (e.g.
    an empty title) — `RegulatoryInstrumentMetadata`'s own boundary
    validation. Both satisfy `RegisterRegulatoryInstrumentVersion`'s CA-doc
    contract: "Reject with a clear error if required properties are
    missing.".
    """
    instrument_type = _instrument_type_from_celex(identifier)
    root = parse_xml(xhtml)
    _strip_namespace(root)

    main_title_div = root.find('.//div[@class="eli-main-title"]')
    title = _full_text(main_title_div) if main_title_div is not None else ""

    rdf_root = parse_xml(rdf)
    blocks = _parse_subject_blocks(rdf_root)
    reifications = _parse_reification_blocks(rdf_root)
    effective_date = _find_effective_date_from_rdf(
        blocks, reifications, identifier, instrument_type
    )
    if effective_date is None:
        predicate_names = _EFFECTIVE_DATE_PREDICATES_BY_INSTRUMENT_TYPE[instrument_type]
        raise CellarParseError(
            f"could not resolve effective_date for identifier {identifier!r}: "
            f"no {predicate_names} predicate found on any admissible subject "
            "in RDF metadata"
        )

    return RegulatoryInstrumentMetadata(
        title=title,
        jurisdiction="EU",
        effective_date=effective_date,
        version=_BASE_ACT_VERSION,
        status="active",
        source_type="external",
        instrument_type=instrument_type,
        celex=_base_celex(identifier),
    )
