"""ps_service.domain_mapper.extraction — the ExtractRolesAndRequirements
action.

Holds the public orchestrating function (`extract_roles_and_requirements`,
PLAN_REVIEWED.md §11 Increment 10, wiring Increments 4-9), the per-unit LLM
extraction call (Increment 7), and role canonicalization / Requirement-graph
building with collision handling (Increment 8).
"""

from __future__ import annotations

from typing import Protocol, cast

from ps_service.domain_mapper.adapters.base import DomainMappingAdapter
from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.domain_mapper.falkordb_client import GraphHandle
from ps_service.domain_mapper.graph_writer import persist_role_and_requirement_graph
from ps_service.domain_mapper.identity import requirement_id, role_id
from ps_service.domain_mapper.models import (
    ExtractionResult,
    ExtractionUnit,
    RequirementCandidate,
    RequirementExpressesEdge,
    RequirementNode,
    RoleDefinesEdge,
    RoleNode,
)
from ps_service.domain_mapper.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    parse_extraction_response,
)
from ps_service.llm_interface.client import CompletionCaller
from ps_service.llm_interface.completion import route_completion
from ps_service.llm_interface.models import ChatMessage
from ps_service.logging import LogEmitter, emit_log_entry

_COMPONENT = "domain_mapper"
_ACTION = "extract_roles_and_requirements"


class _RegulationNode(Protocol):
    """Structural stand-in for the `falkordb.Node` this module's own
    `MATCH (r:RegulatoryInstrument) RETURN r` read returns -- only `.properties` is
    ever read, mirroring `falkordb_client.GraphQueryResult`'s own minimal
    structural-Protocol style. A hand-written test fake needs only this one
    attribute to satisfy it."""

    @property
    def properties(self) -> dict[str, object]: ...


def extract_roles_and_requirements(
    regulation_id: str,
    *,
    adapter: DomainMappingAdapter,
    native_graph: GraphHandle,
    baseline_graph: GraphHandle,
    model: str,
    call_completion: CompletionCaller | None = None,
    emitter: LogEmitter | None = None,
) -> ExtractionResult:
    """ExtractRolesAndRequirements — PLAN_REVIEWED.md §5.2's 8-step flow.

    1. Reads the Regulation node's own properties from `native_graph` and
       MERGEs it into `baseline_graph` first (via
       `persist_role_and_requirement_graph`, step 6 below) — the
       `DEFINES`/`EXPRESSES` edges originate from it.
    2. `adapter.read_native_units(native_graph)`. An empty result is not an
       error (Q2 fix) — the Regulation node is still MERGEd and a
       well-formed, all-zero `ExtractionResult` is returned.
    3. Calls `_extract_candidates_for_unit` per unit, with per-unit failure
       isolation: a `DomainMapperExtractionError` for one unit is caught,
       logged (`outcome="error"`, `entity_id=unit.citation_ref`), and that
       unit contributes zero candidates — the loop continues. An
       `LlmProviderError` (infra failure) is not caught here; it propagates
       and aborts the whole call.
    4. `_canonicalize_roles` — deterministic Role dedup.
    5. `_build_requirement_graph` — Requirement nodes + collision
       disambiguation (B2 fix), never raising for a collision.
    6. `persist_role_and_requirement_graph` — validate-then-write.
    7. Emits one `outcome="collision"` entry per disambiguated id, then one
       `outcome="succeeded"` entry for the whole call. No `bind_run_context()`
       call here (PLAN_REVIEWED.md §6) — `run_id` is whatever the caller
       already bound, or `None`.
    8. Returns the `ExtractionResult`.
    """
    regulation_properties = _read_regulation_properties(native_graph, regulation_id)
    units = adapter.read_native_units(native_graph)

    candidates, skipped_unit_count = _extract_all_candidates(
        units, model=model, call_completion=call_completion, emitter=emitter
    )

    role_nodes, role_edges, role_node_ids = _canonicalize_roles(candidates, regulation_id)
    requirement_nodes, requirement_edges, collided_ids = _build_requirement_graph(
        candidates, regulation_id, role_node_ids
    )

    persist_role_and_requirement_graph(
        baseline_graph,
        regulation_id,
        regulation_properties,
        role_nodes,
        role_edges,
        requirement_nodes,
        requirement_edges,
    )

    for collided_id in collided_ids:
        emit_log_entry(
            component=_COMPONENT,
            action=_ACTION,
            entity_id=collided_id,
            outcome="collision",
            emitter=emitter,
        )
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        entity_id=regulation_id,
        outcome="succeeded",
        emitter=emitter,
    )

    return ExtractionResult(
        regulation_id=regulation_id,
        role_node_ids=role_node_ids,
        requirement_ids=tuple(node.id for node in requirement_nodes),
        candidate_count=len(candidates),
        skipped_unit_count=skipped_unit_count,
        requirement_id_collisions=collided_ids,
    )


def _read_regulation_properties(
    native_graph: GraphHandle, regulation_id: str
) -> dict[str, object]:
    """PLAN_REVIEWED.md §5.2 step 1: `MATCH (r:RegulatoryInstrument) RETURN r`, read
    back as a plain properties dict for `persist_role_and_requirement_graph`
    to MERGE into `baseline_graph`.

    Raises `DomainMapperExtractionError` if no Regulation node is found —
    `ExtractRolesAndRequirements`'s own pre-condition
    (`PersistNativeStructuralGraph` completed for this regulation) was not
    met.
    """
    result = native_graph.query("MATCH (r:RegulatoryInstrument) RETURN r")
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        raise DomainMapperExtractionError(
            f"no Regulation node found in native graph for {regulation_id!r}; "
            "PersistNativeStructuralGraph must complete before ExtractRolesAndRequirements"
        )
    node = cast(_RegulationNode, rows[0][0])
    return dict(node.properties)


def _extract_all_candidates(
    units: tuple[ExtractionUnit, ...],
    *,
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> tuple[list[RequirementCandidate], int]:
    """PLAN_REVIEWED.md §5.2 step 3: per-unit failure isolation.

    A `DomainMapperExtractionError` from one unit's malformed/unparseable
    LLM response is caught here, logged (`outcome="error"`,
    `entity_id=unit.citation_ref`), and that unit contributes zero
    candidates — the loop continues to the next unit. An `LlmProviderError`
    (a genuine infra failure) is never caught here — it propagates and
    aborts the whole call.
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
        except DomainMapperExtractionError:
            skipped_unit_count += 1
            emit_log_entry(
                component=_COMPONENT,
                action=_ACTION,
                entity_id=unit.citation_ref,
                outcome="error",
                emitter=emitter,
            )
    return candidates, skipped_unit_count


def _extract_candidates_for_unit(
    unit: ExtractionUnit,
    *,
    model: str,
    call_completion: CompletionCaller | None = None,
    emitter: LogEmitter | None = None,
) -> list[RequirementCandidate]:
    """Call the LLM once for one `ExtractionUnit`, returning its
    `RequirementCandidate`s.

    Builds a system + user `ChatMessage` pair (the unit's own text is
    delimited from the system prompt per L2's untrusted-content rule — this
    is regulatory text, technically untrusted per the CA doc's own posture,
    even though no mitigation beyond delimiting is designed here), calls
    `route_completion`, then delegates response parsing to
    `parse_extraction_response`. `emitter` is forwarded to `route_completion`
    unchanged — `route_completion` logs its own `llm_interface`-component
    entry regardless of this component's own logging (PLAN_REVIEWED.md §9);
    this is plain pass-through DI (L2: "must not construct its own
    infrastructure clients inline"), not a new logging concern of this
    function's own.

    A `DomainMapperExtractionError` from a malformed/unparseable LLM
    *response* propagates unchanged — this function does not catch it;
    per-unit failure isolation is the orchestrating
    `extract_roles_and_requirements`'s job (PLAN_REVIEWED.md §5.2 step 3),
    not this one's. An `LlmProviderError` from `route_completion` itself (a
    genuine infra failure calling the LLM at all) is likewise never caught
    here — it propagates and aborts the whole call, the fail-fast infra
    boundary this component follows throughout.
    """
    messages = _build_extraction_messages(unit)
    result = route_completion(
        messages, model=model, call_completion=call_completion, emitter=emitter
    )
    return parse_extraction_response(result.text, unit)


def _build_extraction_messages(unit: ExtractionUnit) -> list[ChatMessage]:
    """System prompt + one user message carrying the unit's own text,
    clearly delimited from the system prompt's instructions (L2 LLM
    Interface Patterns: "never interpolate [untrusted content] directly
    into a system/instruction prompt... delimit it clearly")."""
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


def _canonicalize_roles(
    candidates: list[RequirementCandidate], regulation_id: str
) -> tuple[tuple[RoleNode, ...], tuple[RoleDefinesEdge, ...], dict[str, str]]:
    """PLAN_REVIEWED.md §5.2 step 4 — deterministic dedup via
    `identity.role_id()`, no LLM call.

    Candidates sharing the same `role_name` collapse onto one Role node
    (`role_id()` is a pure function of `role_name` + `regulation_id`, so
    every candidate naming the same role deterministically computes the
    same id). The FIRST candidate (in document order, i.e. this list's own
    order — the caller is responsible for supplying candidates already in
    unit-then-response order) whose `role_name` produces a given `role_id`
    determines that Role's `confidence` and its `DEFINES` edge's
    `source_ref` (`unit_citation_ref`) — mirrors how `_build_requirement_graph`
    below treats first occurrence for Requirement-id collisions.

    Returns `(role_nodes, role_edges, role_node_ids)` where `role_node_ids`
    maps every distinct `role_name` string actually seen to its Role node's
    id — including a `role_name` spelling that happens to collapse onto an
    id another spelling already produced — so `_build_requirement_graph`
    can look up any candidate's `role_name` unconditionally.

    Pure, no logging/IO — stays cheaply testable with hand-written
    structural fakes, no emitter involved.
    """
    role_nodes: list[RoleNode] = []
    role_edges: list[RoleDefinesEdge] = []
    role_node_ids: dict[str, str] = {}
    seen_role_node_ids: set[str] = set()

    for candidate in candidates:
        node_id = role_id(candidate.role_name, regulation_id)
        role_node_ids[candidate.role_name] = node_id
        if node_id in seen_role_node_ids:
            continue
        seen_role_node_ids.add(node_id)
        role_nodes.append(
            RoleNode(
                id=node_id,
                properties={"name": candidate.role_name, "confidence": candidate.confidence},
            )
        )
        role_edges.append(
            RoleDefinesEdge(role_node_id=node_id, source_ref=candidate.unit_citation_ref)
        )

    return tuple(role_nodes), tuple(role_edges), role_node_ids


def _build_requirement_graph(
    candidates: list[RequirementCandidate],
    regulation_id: str,
    role_node_ids: dict[str, str],
) -> tuple[tuple[RequirementNode, ...], tuple[RequirementExpressesEdge, ...], tuple[str, ...]]:
    """PLAN_REVIEWED.md §5.2 step 5 — B2 fix: deterministic Requirement-id
    collision disambiguation, never an exception.

    Walks `candidates` in document order (the caller's own list order —
    unit order, then within-unit response order), computing
    `identity.requirement_id()` per candidate and tracking every distinct
    `text` already seen at each base id:

    - Same base id, same text as an already-seen occurrence at that id ->
      true duplicate, silently collapsed to one node (a trivial same-input
      sanity check, not a cross-call idempotency proof).
    - Same base id, a NEW distinct text -> deterministically disambiguated:
      persisted under `f"{base_id}#{n}"` (`n` = 2 for the second distinct
      text seen at that base id, 3 for a third, ...). Every disambiguated id
      is collected into `collided_ids`. No exception is ever raised for
      this case.

    Each Requirement node's `properties` carries `role_id` — a plain
    bookkeeping property (not an Edge Catalog relationship), the owning
    Role node's id from `role_node_ids[candidate.role_name]`
    (`_canonicalize_roles`'s output) — consumed later by `derivation.py`
    (PLAN_REVIEWED.md §7.2). Each `EXPRESSES` edge carries
    `source_ref = candidate.unit_citation_ref` (AC-001).

    Pure, no logging/IO — the orchestrating `extract_roles_and_requirements`
    (a later increment) is responsible for emitting one `outcome="collision"`
    log entry per id in the returned `collided_ids`.
    """
    requirement_nodes: list[RequirementNode] = []
    requirement_edges: list[RequirementExpressesEdge] = []
    collided_ids: list[str] = []
    texts_seen_by_base_id: dict[str, dict[str, str]] = {}

    for candidate in candidates:
        base_id = requirement_id(
            regulation_id,
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
