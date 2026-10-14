"""Cellar/ELI XHTML and RDF to `RegulatoryInstrumentMetadata`.

`title` comes from the XHTML document's `eli-main-title` div.

`effective_date` comes from the document's RDF/XML metadata:

- Subject-scoped resolution (`_parse_subject_blocks`, `_resolve_predicate_candidates`): the
  CELEX's own subjects first, widening to other subjects only when none asserts the predicate.
- Instrument-type predicate dispatch (`_EFFECTIVE_DATE_PREDICATES_BY_INSTRUMENT_TYPE`): it
  branches only on the structural `instrument_type`, never on which regulation this is.
- A role-priority tie-break (`_pick_effective_date`) for when Cellar/ELI asserts more than one
  value for the same predicate. Each value's semantic role (`MA_GENERAL`, `EV`, `MA_PART` for a
  regulation; `ADOPTION`, `APPLICATION` for a directive) is read from Cellar's own `owl:Axiom`
  reification (`_parse_reification_blocks`). Earliest-wins applies only to values with no
  recognized role.
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
identifier's own lexical form, never on which regulation it names.
"""


def _base_celex(identifier: str) -> str:
    """Return the base-act CELEX for `identifier`.

    A base-act CELEX (`32024R2847`) is returned unchanged; a consolidated-expression CELEX
    (`02024R2847-20241120`) is reduced to its base act (`32024R2847`: drop the `-YYYYMMDD`
    suffix, sector `0` becomes `3`).

    Raises:
        CellarParseError: `identifier` has any other shape, including a sector-`0` identifier
            with no consolidation-date suffix.
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
    """Return `_base_celex(identifier)`, or None for an unparseable value instead of raising.

    Used to normalize the `resource_legal_id_celex` values a subject asserts before comparing
    them with the own CELEX. An unparseable value simply fails to match (the subject is treated
    as foreign) instead of aborting the whole resolution.
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
"""The version given to every regulation this adapter ingests.

Ingestion fetches a regulation's base-act CELEX, never a consolidated-text CELEX, so the ingest
is that regulation's first version in the system. Cellar's base-act fetch carries no scalar
version field to parse. Detecting later consolidated versions and bumping `version` belongs to
the regulatory change monitor.
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

    Every element anywhere in the document carrying an `rdf:about` attribute is a subject block,
    detected by that attribute and not by tag name, because Cellar serializes a subject either as
    a generic `rdf:Description` or as a typed node (for example `cdm:resource_legal`). Only a
    subject's direct children are recorded as its predicates, keyed by local tag name. Duplicate
    predicate values are kept, because the date tie-break needs every value. The tree is walked
    without calling `_strip_namespace` first, since subject identity depends on the
    namespace-qualified `rdf:about` attribute.
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
`comment_on_date` children (e.g. {"MA", "MA/PART"} or {"ADOPTION"}). A missing
key means no reification was found for that exact triple, and role classification
treats the value as untagged."""


def _parse_reification_blocks(root: ET.Element) -> _Reifications:
    """Associate each date value with its owl:Axiom-asserted semantic-role tokens.

    Matches on the exact RDF triple (subject IRI, predicate local name, literal value text), the
    granularity at which OWL reification operates. Two different values sharing subject,
    predicate and literal text would be indistinguishable; that is a limit of the source data.
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
    """Return the subject IRIs asserting `own_id` as a `resource_legal_id_celex` value.

    The CELEX's own subject is discovered from the graph through this property, never assumed to
    be the URL the document was fetched from. `own_id` must already be a base-act CELEX (as
    returned by `_base_celex`). Each asserted value is normalized through `_base_celex_or_none`
    before comparison, because a consolidated expression's own subject asserts the unreduced form
    (for example `"02024R2847-20241120"`), which never equals `"32024R2847"` raw.
    """
    return {
        iri
        for iri, predicates in blocks.items()
        if own_id in {_base_celex_or_none(v) for v in predicates.get("resource_legal_id_celex", [])}
    }


def _resolve_predicate_candidates(
    blocks: _SubjectBlocks, identifier: str, predicate_names: tuple[str, ...]
) -> list[tuple[str, str, str]]:
    """Resolve candidate `(subject_iri, predicate_name, value_text)` triples for `predicate_names`.

    Candidates carry their origin (subject IRI and predicate local name) so the role tie-break
    can look each one up in `_Reifications` by its exact triple.

    Tier 1: subjects asserting `identifier`'s own CELEX id. If any asserts one of
    `predicate_names`, its values (pooled across every matching own subject) are returned, and no
    foreign subject is consulted.

    Tier 2 (only when tier 1 is empty): widen to every subject except the own subjects and except
    any subject that is itself a distinct legal act, meaning one asserting a
    `resource_legal_id_celex` that normalizes to a different base CELEX than `identifier`'s. Such
    a subject has its own identity, so its properties are facts about a different instrument.
    Values of every surviving subject are pooled.

    Returns:
        The candidate triples; empty if no admissible subject matches at either tier.

    Raises:
        CellarParseError: `identifier` is not a base-act or consolidated-expression CELEX.
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

The `regulation` tuple carries two names because the `notice=non-inferred` fetch profile
(`fetch.py`'s `_RDF_HEADERS`) can emit the bare, generalized `date_entry-into-force` name or only
the non-generalized `resource_legal_date_entry-into-force` name. `national_transposition` is
absent because this adapter only derives `regulation` or `directive` from a CELEX type code. The
branch key is the structural `InstrumentType` value, never a CELEX literal.
"""


_DATE_ROLE_PRIORITY_BY_INSTRUMENT_TYPE: dict[InstrumentType, tuple[str, ...]] = {
    # MA (general application) beats EV (entry into force) beats MA/PART (a narrower,
    # named-subset application date, e.g. one Title's staggered start). MA/PART must never
    # outrank EV: an act with only EV and MA/PART is correctly dated at EV.
    "regulation": ("MA_GENERAL", "EV", "MA_PART"),
    # ADOPTION (the transposition deadline, the documented meaning of a directive's
    # effective_date) beats APPLICATION (when the transposed measures take legal effect),
    # selected by tag and not by which date is earlier.
    "directive": ("ADOPTION", "APPLICATION"),
}
"""Semantic roles in priority order per `InstrumentType`, the structural dispatch used by
`_pick_effective_date` alongside `_EFFECTIVE_DATE_PREDICATES_BY_INSTRUMENT_TYPE`."""


def _date_role(instrument_type: InstrumentType, tokens: frozenset[str]) -> str | None:
    """Classify one value's semantic role from its reification tokens.

    Branches only on the structural `instrument_type` and the tokens Cellar asserts
    (EV, MA, MA/PART, ADOPTION, APPLICATION), never on which instrument this is. Returns None for
    an untagged value or an unrecognized token set, which `_pick_effective_date` treats as having
    no known role.
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
    """Pick the effective date from candidates by semantic role, falling back to earliest.

    Each `(subject_iri, predicate_name, value_text)` candidate is looked up in `reifications` by
    its exact triple (a missing key means untagged) and classified with `_date_role`. Candidates
    are grouped by role; roles are walked in `_DATE_ROLE_PRIORITY_BY_INSTRUMENT_TYPE` order and
    the earliest date in the first non-empty role bucket is returned. If no candidate has a
    recognized role (untagged duplicates, or no reification data at all), the earliest date across
    every candidate is returned. The earlier date is the conservative choice for a compliance
    deadline: it never understates how soon a company must comply.

    Returns:
        The chosen date, or None if `candidates` is empty.

    Raises:
        ValueError: A candidate's `value_text` is not an ISO `YYYY-MM-DD` date.
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
    """Resolve `effective_date` from RDF subject blocks.

    Looks up `instrument_type`'s predicate names, resolves candidate values with the tiered
    subject scoping of `_resolve_predicate_candidates`, and applies the role-priority tie-break of
    `_pick_effective_date`, which parses each `xsd:date` value (`YYYY-MM-DD`) to a `date`.

    Returns:
        The date, or None if no admissible subject asserts any of the predicate names.
    """
    predicate_names = _EFFECTIVE_DATE_PREDICATES_BY_INSTRUMENT_TYPE[instrument_type]
    candidates = _resolve_predicate_candidates(blocks, identifier, predicate_names)
    return _pick_effective_date(candidates, reifications, instrument_type)


def _instrument_type_from_celex(identifier: str) -> InstrumentType:
    """Map a CELEX identifier's type-code letter to its `instrument_type`.

    `R` gives regulation and `L` gives directive; the same code path serves every CELEX.

    Raises:
        CellarParseError: The identifier is neither a base-act nor a consolidated-expression
            CELEX, or its type code is any other descriptor (for example `D` for a decision);
            no default type is assumed.
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
    """Extract bibliographic metadata from a Cellar/ELI document's own text and RDF metadata.

    No LLM extraction and no per-regulation branching. `instrument_type` is derived from the
    CELEX type code, `title` from the `eli-main-title` div of `xhtml`, and `effective_date` from
    the structured RDF metadata in `rdf` (see the module docstring). Jurisdiction is `EU`, status
    `active`, source type `external`, and version the constant base-act version.

    Args:
        xhtml: The Official Journal XHTML document as bytes.
        rdf: The RDF/XML metadata document as bytes.
        identifier: CELEX number, base-act (`32024R2847`) or consolidated-expression
            (`02024R2847-20241120`); used for the type code and to scope RDF subject resolution.

    Returns:
        The metadata, with `celex` set to the base-act form of `identifier`.

    Raises:
        CellarParseError: The CELEX type code is unsupported, or `effective_date` cannot be
            resolved because no admissible RDF subject asserts a target predicate.
        pydantic.ValidationError: Another required field is missing or invalid, for example an
            empty title (there is no title fallback).
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
