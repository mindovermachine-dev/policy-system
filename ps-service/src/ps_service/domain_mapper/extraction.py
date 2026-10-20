"""ps_service.domain_mapper.extraction: the extract-roles-and-requirements action.

Holds the public orchestrating function (`extract_roles_and_requirements`), the per-unit LLM
extraction call, the definitions pass that finds where each Role is formally defined, and the
Role canonicalization and Requirement-graph building (with id-collision handling) that turn
LLM candidates into the nodes and edges of one gateway group.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol, cast

from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.domain_mapper.graph_writer import persist_role_and_requirement_graph
from ps_service.domain_mapper.identity import requirement_id, role_id
from ps_service.domain_mapper.models import (
    DefinedTermCandidate,
    ExtractionResult,
    ExtractionUnit,
    RequirementCandidate,
    RequirementExpressesEdge,
    RequirementNode,
    RoleDefinesEdge,
    RoleNode,
)
from ps_service.domain_mapper.prompts import (
    DEFINITIONS_EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    parse_definitions_response,
    parse_extraction_response,
)
from ps_service.domain_mapper.role_names import normalise_role_name
from ps_service.llm_interface.completion import route_completion
from ps_service.llm_interface.models import ChatMessage
from ps_service.logging import LogEmitter, emit_log_entry

if TYPE_CHECKING:
    from ps_service.domain_mapper.adapters.base import DomainMappingAdapter
    from ps_service.domain_mapper.falkordb_client import GraphHandle
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.llm_interface.client import CompletionCaller

_COMPONENT = "domain_mapper"
_ACTION = "extract_roles_and_requirements"

_DEFINITIONS_HEADING_PATTERN = re.compile(r"\bdefinitions\b", re.IGNORECASE)


class _RegulatoryInstrumentNode(Protocol):
    """Structural stand-in for the `falkordb.Node` from this module's RegulatoryInstrument read.

    Only `.properties` is read, in the minimal style of `falkordb_client.GraphQueryResult`, so
    a hand-written fake needs only that one attribute.
    """

    @property
    def properties(self) -> dict[str, object]: ...


def extract_roles_and_requirements(  # noqa: PLR0913 -- one stage seam: the native graph, gateway keywords, LLM and log collaborators
    regulatory_instrument_id: str,
    *,
    adapter: DomainMappingAdapter,
    native_graph: GraphHandle,
    submitter: GroupSubmitter,
    baseline_graph_name: str,
    audit_event_id: str,
    model: str,
    call_completion: CompletionCaller | None = None,
    emitter: LogEmitter | None = None,
) -> ExtractionResult:
    """Extract Roles and Requirements from one instrument's native graph into its baseline graph.

    Steps:

    1. Read the RegulatoryInstrument node's own properties from `native_graph`; the node is
       written first in the extraction group because the `DEFINES` and `EXPRESSES` edges
       originate from it.
    2. Read the extraction units with `adapter.read_native_units`. An empty result is not an
       error: the instrument node is still submitted and a well-formed, all-zero
       `ExtractionResult` is returned.
    3. Call the LLM per unit (`_extract_candidates_for_unit`) with per-unit failure isolation:
       a `DomainMapperExtractionError` for one unit is caught and logged
       (`outcome="error"`, `entity_id=unit.citation_ref`), and that unit contributes no
       candidates.
    4. Drop the candidates whose `role_name` is flagged as not an actor
       (`_drop_non_actor_candidates`), logging each, so they mint nothing.
    5. Run the definitions pass (`_build_defined_terms_map`) over the definitions-headed units,
       then deduplicate Roles deterministically (`_canonicalize_roles`). Each Role's
       `DEFINES.source_ref` is the citation of its formal definition, falling back to the first
       duty occurrence's `unit_citation_ref` when no matching defined term exists.
    6. Build Requirement nodes and edges (`_build_requirement_graph`), disambiguating id
       collisions without raising.
    7. Validate, build and submit the instrument, Roles, `DEFINES`, Requirements and `EXPRESSES`
       as ONE Graph Write Gateway group on `baseline_graph_name` under `audit_event_id`. Every
       edge endpoint is in the group, so extraction reads nothing from the baseline graph.
    8. Emit one `outcome="collision"` log entry per disambiguated id, one
       `outcome="definitions_fallback"` entry per Role whose `DEFINES.source_ref` fell back, and
       one `outcome="succeeded"` entry for the whole call. `run_id` is whatever the caller
       already bound, or `None`; no run context is bound here.

    Args:
        regulatory_instrument_id: Id of the RegulatoryInstrument node in `native_graph` to
            extract from.
        adapter: Domain Mapping Adapter paired with the instrument's ingestion source.
        native_graph: Read handle on the `{short}_native` graph.
        submitter: Gateway submitter for the baseline group.
        baseline_graph_name: Target `{short}_baseline` graph.
        audit_event_id: Audit event the group is recorded under.
        model: Model identifier passed to the LLM interface.
        call_completion: LLM completion caller; `None` uses the default.
        emitter: Log emitter; `None` uses the default.

    Returns:
        The `ExtractionResult` with the Role ids, Requirement ids, candidate, skipped-unit,
        minted-Role and dropped-non-actor counts and any collision ids; all counts are zero when
        the instrument has no units.

    Raises:
        DomainMapperExtractionError: The RegulatoryInstrument node is absent from
            `native_graph`, or the adapter finds a malformed citation.
        DomainMapperPersistenceError: A Requirement references an unknown Role, a property
            cannot be stored, or the gateway does not take the group.
        LlmProviderError: The LLM provider fails; this is an infrastructure failure and aborts
            the whole call.

    Side Effects:
        Calls the LLM per unit and for the definitions pass, submits one gateway group to the
        baseline graph, and emits structured log entries.
    """
    regulatory_instrument_properties = _read_regulatory_instrument_properties(
        native_graph, regulatory_instrument_id
    )
    units = adapter.read_native_units(native_graph, regulatory_instrument_id)

    candidates, skipped_unit_count = _extract_all_candidates(
        units, model=model, call_completion=call_completion, emitter=emitter
    )

    actor_candidates, non_actor_roles_dropped = _drop_non_actor_candidates(
        candidates, emitter=emitter
    )

    defined_terms = _build_defined_terms_map(
        units, model=model, call_completion=call_completion, emitter=emitter
    )

    role_nodes, role_edges, role_node_ids, fallback_roles = _canonicalize_roles(
        actor_candidates, regulatory_instrument_id, defined_terms
    )
    requirement_nodes, requirement_edges, collided_ids = _build_requirement_graph(
        actor_candidates, regulatory_instrument_id, role_node_ids
    )

    persist_role_and_requirement_graph(
        submitter,
        baseline_graph_name,
        audit_event_id,
        regulatory_instrument_id=regulatory_instrument_id,
        regulatory_instrument_properties=regulatory_instrument_properties,
        role_nodes=role_nodes,
        role_edges=role_edges,
        requirement_nodes=requirement_nodes,
        requirement_edges=requirement_edges,
    )

    for collided_id in collided_ids:
        emit_log_entry(
            component=_COMPONENT,
            action=_ACTION,
            entity_id=collided_id,
            outcome="collision",
            emitter=emitter,
        )
    for role_node_id, role_name in fallback_roles:
        emit_log_entry(
            component=_COMPONENT,
            action=_ACTION,
            entity_id=role_node_id,
            outcome="definitions_fallback",
            extra={"role_name": role_name, "regulatory_instrument_id": regulatory_instrument_id},
            emitter=emitter,
        )
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        entity_id=regulatory_instrument_id,
        outcome="succeeded",
        emitter=emitter,
    )

    return ExtractionResult(
        regulatory_instrument_id=regulatory_instrument_id,
        role_node_ids=role_node_ids,
        requirement_ids=tuple(node.id for node in requirement_nodes),
        candidate_count=len(candidates),
        skipped_unit_count=skipped_unit_count,
        requirement_id_collisions=collided_ids,
        roles_minted=len(set(role_node_ids.values())),
        non_actor_roles_dropped=non_actor_roles_dropped,
    )


def _read_regulatory_instrument_properties(
    native_graph: GraphHandle, regulatory_instrument_id: str
) -> dict[str, object]:
    """Return the properties of the RegulatoryInstrument node `regulatory_instrument_id`.

    The native graph holds every ingested version of a regulation, so the node is matched by id
    (`MATCH (r:RegulatoryInstrument {id: $rid}) RETURN r`). The properties are returned as a
    plain dict for `persist_role_and_requirement_graph` to submit as the instrument node.

    Raises:
        DomainMapperExtractionError: No such node exists, meaning native-graph persistence has
            not completed for this regulation.
    """
    result = native_graph.query(
        "MATCH (r:RegulatoryInstrument {id: $rid}) RETURN r",
        params={"rid": regulatory_instrument_id},
    )
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        raise DomainMapperExtractionError(
            f"no RegulatoryInstrument node found in native graph for {regulatory_instrument_id!r}; "
            "PersistNativeStructuralGraph must complete before ExtractRolesAndRequirements"
        )
    node = cast("_RegulatoryInstrumentNode", rows[0][0])
    return dict(node.properties)


def _extract_all_candidates(
    units: tuple[ExtractionUnit, ...],
    *,
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> tuple[list[RequirementCandidate], int]:
    """Extract candidates from every unit, isolating each unit's parse failures.

    A `DomainMapperExtractionError` from one unit's malformed or unparseable LLM response is
    caught and logged (`outcome="error"`, `entity_id=unit.citation_ref`,
    `extra={"error_kind": exc.error_kind}`); that unit contributes no candidates and the loop
    continues. Only `exc.error_kind` is logged, never `str(exc)` or `exc.args`, which embed the
    raw LLM payload that failed to parse. An `LlmProviderError` is not caught: it propagates and
    aborts the whole call.

    Args:
        units: Extraction units in document order.
        model: Model identifier for the LLM call.
        call_completion: LLM completion caller; `None` uses the default.
        emitter: Log emitter; `None` uses the default.

    Returns:
        `(candidates, skipped_unit_count)`: candidates in unit-then-response order, and the
        number of units skipped because their response could not be parsed.
    """
    candidates: list[RequirementCandidate] = []
    skipped_unit_count = 0
    for unit in units:
        try:
            candidates.extend(
                _extract_candidates_for_unit(
                    unit, model=model, call_completion=call_completion, emitter=emitter
                )
            )
        except DomainMapperExtractionError as exc:
            skipped_unit_count += 1
            emit_log_entry(
                component=_COMPONENT,
                action=_ACTION,
                entity_id=unit.citation_ref,
                outcome="error",
                extra={"error_kind": exc.error_kind},
                emitter=emitter,
            )
    return candidates, skipped_unit_count


def _drop_non_actor_candidates(
    candidates: list[RequirementCandidate], *, emitter: LogEmitter | None
) -> tuple[list[RequirementCandidate], int]:
    """Split off the candidates whose `role_name` the model flagged as not an actor.

    A flagged candidate (a document, fee, event, product or back-reference rather than a
    duty-bearing actor) must not mint a Role, a Requirement or, later, an Obligation, so it is
    removed before `_canonicalize_roles`. Each drop is logged with the candidate's name and its
    source location; the actors keep their order.

    Args:
        candidates: Every candidate the LLM returned, in unit-then-response order.
        emitter: Log emitter; `None` uses the default.

    Returns:
        `(actor_candidates, dropped_count)`.

    Side Effects:
        Emits one `non_actor_role_dropped` log entry per dropped candidate, carrying `role_name`
        and `source_ref` (the unit's citation).
    """
    actors: list[RequirementCandidate] = []
    for candidate in candidates:
        if candidate.role_is_actor:
            actors.append(candidate)
            continue
        emit_log_entry(
            component=_COMPONENT,
            action="non_actor_role_dropped",
            entity_id=candidate.unit_citation_ref,
            outcome="dropped",
            extra={"role_name": candidate.role_name, "source_ref": candidate.unit_citation_ref},
            emitter=emitter,
        )
    return actors, len(candidates) - len(actors)


def _extract_candidates_for_unit(
    unit: ExtractionUnit,
    *,
    model: str,
    call_completion: CompletionCaller | None = None,
    emitter: LogEmitter | None = None,
) -> list[RequirementCandidate]:
    """Call the LLM once for one `ExtractionUnit` and return its `RequirementCandidate`s.

    Builds a system and user `ChatMessage` pair, with the unit's own text delimited from the
    system prompt because regulatory text is untrusted input, calls `route_completion`, and
    delegates response parsing to `parse_extraction_response`. `emitter` is forwarded to
    `route_completion` unchanged; that call logs its own `llm_interface` entry.

    Args:
        unit: The extraction unit to extract from.
        model: Model identifier for the LLM call.
        call_completion: LLM completion caller; `None` uses the default.
        emitter: Log emitter; `None` uses the default.

    Returns:
        The candidates found in the unit; empty when the response lists none.

    Raises:
        DomainMapperExtractionError: The LLM response is malformed or unparseable. Not caught
            here; per-unit isolation is `_extract_all_candidates`'s job.
        LlmProviderError: The LLM call itself fails; this aborts the whole call.
    """
    messages = _build_extraction_messages(unit)
    result = route_completion(
        messages, model=model, call_completion=call_completion, emitter=emitter
    )
    return parse_extraction_response(result.text, unit)


def _build_extraction_messages(unit: ExtractionUnit) -> list[ChatMessage]:
    """Build the system prompt and one user message carrying the unit's own text.

    The unit text is delimited by `<regulation_text>` tags and never interpolated into the
    system prompt, because it is untrusted input.
    """
    user_content = (
        f"Article heading: {unit.article_heading}\n"
        f"Citation: {unit.citation_ref}\n\n"
        "<regulation_text>\n"
        f"{unit.text}\n"
        "</regulation_text>"
    )
    return [
        ChatMessage(role="system", content=EXTRACTION_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_content),
    ]


def _is_definitions_unit(unit: ExtractionUnit) -> bool:
    r"""Return whether `unit.article_heading` marks a definitions article.

    A pure, case-insensitive, word-boundary match (`_DEFINITIONS_HEADING_PATTERN`) on the
    heading verbatim. It matches "Definitions", "Article 2 - Definitions" and "Scope and
    definitions". A plain substring check would also match "Redefinitions of Scope", where
    "definitions" is not its own word (no `\b` boundary between "re" and "definitions"), so a
    word-boundary regex is used. Callers filter a copy of the units; the full unit traversal
    used for duty extraction is unaffected.
    """
    return bool(_DEFINITIONS_HEADING_PATTERN.search(unit.article_heading))


def _normalize_term(text: str) -> str:
    """Return `text` casefolded with internal whitespace collapsed, as a matching key.

    Used identically on both sides of the match: the pooled defined-term keys
    (`_pool_defined_terms`) and a candidate's `role_name` at lookup time in
    `_canonicalize_roles`.
    """
    return " ".join(text.casefold().split())


def _build_definitions_extraction_messages(unit: ExtractionUnit) -> list[ChatMessage]:
    """Build the definitions-pass system prompt and one user message carrying the unit's text.

    Same delimiting as `_build_extraction_messages`: the unit text never enters the system
    prompt.
    """
    user_content = (
        f"Article heading: {unit.article_heading}\n"
        f"Citation: {unit.citation_ref}\n\n"
        "<regulation_text>\n"
        f"{unit.text}\n"
        "</regulation_text>"
    )
    return [
        ChatMessage(role="system", content=DEFINITIONS_EXTRACTION_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_content),
    ]


def _extract_defined_terms_for_unit(
    unit: ExtractionUnit,
    *,
    model: str,
    call_completion: CompletionCaller | None = None,
    emitter: LogEmitter | None = None,
) -> list[DefinedTermCandidate]:
    """Call the LLM once for one definitions `ExtractionUnit` and return its defined terms.

    Same shape as `_extract_candidates_for_unit`.

    Raises:
        DomainMapperExtractionError: The response is malformed. Not caught here; isolation is
            `_extract_all_defined_terms`'s job.
        LlmProviderError: The LLM call fails; this aborts the whole call.
    """
    messages = _build_definitions_extraction_messages(unit)
    result = route_completion(
        messages, model=model, call_completion=call_completion, emitter=emitter
    )
    return parse_definitions_response(result.text, unit)


def _extract_all_defined_terms(
    definitions_units: tuple[ExtractionUnit, ...],
    *,
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> list[DefinedTermCandidate]:
    """Run the definitions pass over exactly `definitions_units` and return every term found.

    Makes no LLM call when `definitions_units` is empty. Per-unit failure isolation mirrors
    `_extract_all_candidates`: a `DomainMapperExtractionError` from one unit is caught and
    logged (`outcome="error"`, `entity_id=unit.citation_ref`,
    `extra={"error_kind": exc.error_kind}`), and that unit contributes no terms. An
    `LlmProviderError` is not caught.
    """
    term_candidates: list[DefinedTermCandidate] = []
    for unit in definitions_units:
        try:
            term_candidates.extend(
                _extract_defined_terms_for_unit(
                    unit, model=model, call_completion=call_completion, emitter=emitter
                )
            )
        except DomainMapperExtractionError as exc:
            emit_log_entry(
                component=_COMPONENT,
                action=_ACTION,
                entity_id=unit.citation_ref,
                outcome="error",
                extra={"error_kind": exc.error_kind},
                emitter=emitter,
            )
    return term_candidates


def _pool_defined_terms(term_candidates: list[DefinedTermCandidate]) -> dict[str, str]:
    """Pool defined terms into one map from normalized term to the defining `citation_ref`.

    Keys are normalized with `_normalize_term`, so `_canonicalize_roles` looks a Role up with a
    single dict access. The first occurrence in `term_candidates` order (unit traversal order)
    wins on a duplicate normalized term. Pure: no LLM call or I/O.
    """
    pooled: dict[str, str] = {}
    for candidate in term_candidates:
        key = _normalize_term(candidate.term)
        if key not in pooled:
            pooled[key] = candidate.citation_ref
    return pooled


def _build_defined_terms_map(
    units: tuple[ExtractionUnit, ...],
    *,
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> dict[str, str]:
    """Detect definitions units, run the dedicated LLM pass over them, and pool the results.

    Composes `_is_definitions_unit`, `_extract_all_defined_terms` and `_pool_defined_terms` into
    the one call `extract_roles_and_requirements` makes before `_canonicalize_roles`, which
    stays free of LLM calls and I/O. This helper is not pure: it issues LLM calls.

    Returns:
        Map from normalized defined term to its defining `citation_ref`; empty when no unit
        qualifies or no term is found.
    """
    definitions_units = tuple(unit for unit in units if _is_definitions_unit(unit))
    term_candidates = _extract_all_defined_terms(
        definitions_units, model=model, call_completion=call_completion, emitter=emitter
    )
    return _pool_defined_terms(term_candidates)


def _canonicalize_roles(
    candidates: list[RequirementCandidate],
    regulatory_instrument_id: str,
    defined_terms: dict[str, str],
) -> tuple[
    tuple[RoleNode, ...],
    tuple[RoleDefinesEdge, ...],
    dict[str, str],
    tuple[tuple[str, str], ...],
]:
    """Deduplicate Role candidates into Role nodes and `DEFINES` edges, deterministically.

    Each `role_name` is first normalised with `normalise_role_name` (whitespace, one leading
    article, a plural last word), then `role_id()` is applied to the normalised name, so
    "Manufacturer", "manufacturers" and "The manufacturer" collapse onto one Role node. The first
    candidate, in the order of `candidates` (unit-then-response order, which the caller must
    supply), whose normalised name produces a given id fixes that Role's `name` (its normalised
    spelling), `confidence` and its `DEFINES` edge's `source_ref`, as `_build_requirement_graph`
    does for Requirement-id collisions.

    For that first candidate, the normalised name is further normalized with `_normalize_term`
    and looked up in `defined_terms`. A hit sets `source_ref` to the defining `citation_ref`; a
    miss falls back to `candidate.unit_citation_ref` and records `(node_id, role_name)` (the
    normalised name) in the returned `fallback_roles`, so the orchestrator can log
    `outcome="definitions_fallback"`. The function reports what happened and the orchestrator
    logs it.

    Pure: no LLM call, logging or I/O, so it can be tested with hand-written fakes.

    Args:
        candidates: Requirement candidates in document order.
        regulatory_instrument_id: Id of the defining RegulatoryInstrument, folded into Role ids.
        defined_terms: Normalized term to defining `citation_ref`, from
            `_build_defined_terms_map`; empty when there are no definitions.

    Returns:
        `(role_nodes, role_edges, role_node_ids, fallback_roles)`. `role_node_ids` maps every
        distinct raw `role_name` spelling seen to its Role node id, including spellings that
        collapse onto an id another spelling already produced, so `_build_requirement_graph` can
        look up any candidate's `role_name`.
    """
    role_nodes: list[RoleNode] = []
    role_edges: list[RoleDefinesEdge] = []
    role_node_ids: dict[str, str] = {}
    seen_role_node_ids: set[str] = set()
    fallback_roles: list[tuple[str, str]] = []

    for candidate in candidates:
        role_name = normalise_role_name(candidate.role_name)
        node_id = role_id(role_name, regulatory_instrument_id)
        role_node_ids[candidate.role_name] = node_id
        if node_id in seen_role_node_ids:
            continue
        seen_role_node_ids.add(node_id)
        role_nodes.append(
            RoleNode(
                id=node_id,
                properties={"name": role_name, "confidence": candidate.confidence},
            )
        )
        matched_citation_ref = defined_terms.get(_normalize_term(role_name))
        if matched_citation_ref is not None:
            source_ref = matched_citation_ref
        else:
            source_ref = candidate.unit_citation_ref
            fallback_roles.append((node_id, role_name))
        role_edges.append(RoleDefinesEdge(role_node_id=node_id, source_ref=source_ref))

    return tuple(role_nodes), tuple(role_edges), role_node_ids, tuple(fallback_roles)


def _build_requirement_graph(
    candidates: list[RequirementCandidate],
    regulatory_instrument_id: str,
    role_node_ids: dict[str, str],
) -> tuple[tuple[RequirementNode, ...], tuple[RequirementExpressesEdge, ...], tuple[str, ...]]:
    """Build Requirement nodes and `EXPRESSES` edges, disambiguating base-id collisions.

    Walks `candidates` in document order (unit order, then within-unit response order),
    computing `identity.requirement_id()` per candidate and tracking each distinct `text` seen
    at each base id:

    - Same base id and same text as an earlier occurrence: a true duplicate, collapsed to one
      node.
    - Same base id and a new distinct text: persisted under `f"{base_id}#{n}"`, where `n` is 2
      for the second distinct text at that base id, 3 for the third, and so on. Every
      disambiguated id is collected into the returned `collided_ids`. No exception is raised.

    Each node's `properties` carry `text`, `type`, `confidence`, `status="active"` (a new
    Requirement is active at creation; deprecation happens later through supersession, never at
    ingest) and `role_id`, a plain bookkeeping property holding the owning Role's id, which
    `derivation.py` reads back. Each `EXPRESSES` edge carries
    `source_ref = candidate.unit_citation_ref`.

    Pure: no logging or I/O. The orchestrator logs one `outcome="collision"` entry per id in
    `collided_ids`.

    Args:
        candidates: Requirement candidates in document order.
        regulatory_instrument_id: Id of the RegulatoryInstrument the Requirements belong to.
        role_node_ids: Map from `role_name` to Role node id, from `_canonicalize_roles`; must
            contain every candidate's `role_name`.

    Returns:
        `(requirement_nodes, requirement_edges, collided_ids)`; all empty when there are no
        candidates.
    """
    requirement_nodes: list[RequirementNode] = []
    requirement_edges: list[RequirementExpressesEdge] = []
    collided_ids: list[str] = []
    texts_seen_by_base_id: dict[str, dict[str, str]] = {}

    for candidate in candidates:
        base_id = requirement_id(
            regulatory_instrument_id,
            candidate.unit_article_number,
            candidate.unit_paragraph_number,
            candidate.letter_suffix,
        )
        texts_seen = texts_seen_by_base_id.setdefault(base_id, {})
        if candidate.text in texts_seen:
            continue  # true duplicate: same id, same text already persisted

        occurrence_number = len(texts_seen) + 1
        final_id = base_id if occurrence_number == 1 else f"{base_id}#{occurrence_number}"
        texts_seen[candidate.text] = final_id
        if occurrence_number > 1:
            collided_ids.append(final_id)

        requirement_nodes.append(
            RequirementNode(
                id=final_id,
                properties={
                    "text": candidate.text,
                    "type": candidate.type,
                    "status": "active",
                    "confidence": candidate.confidence,
                    "role_id": role_node_ids[candidate.role_name],
                },
            )
        )
        requirement_edges.append(
            RequirementExpressesEdge(
                requirement_node_id=final_id, source_ref=candidate.unit_citation_ref
            )
        )

    return tuple(requirement_nodes), tuple(requirement_edges), tuple(collided_ids)
