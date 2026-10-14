"""ps_service.domain_mapper.derivation: the derive-obligations-and-capabilities action.

Holds the public orchestrating function (`derive_obligations_and_capabilities`), the read-back
of Requirements grouped by Role (`_read_requirements_by_role`), the whole-run Obligation
derivation (`_derive_obligations`) and the whole-run Capability derivation
(`_derive_capabilities`).

Obligation identity is Role-scoped (`identity.obligation_id(role_node_id, text)`), so a given
duty text under two different Roles is two distinct Obligation nodes by construction and there
is no cross-Role collision to resolve. The whole-run registry still guarantees that the SAME
Role minting the same duty text twice in one run converges onto a single node: the code, not
the LLM, owns that.

`_read_requirements_by_role` is the read-side complement to the write-side check in
`graph_writer.py` (`_validate_role_references`). It reads every Requirement back from the
baseline graph, grouped by its `role_id` bookkeeping property, and raises
`DomainMapperDerivationError` (naming the Requirement id and its unresolved `role_id`) if any
`role_id` does not resolve to a Role node, before any LLM call is made. This defends against a
baseline graph left partially written or edited out of band; it does not fire against a graph
written by `persist_role_and_requirement_graph`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from ps_service.domain_mapper.errors import (
    DomainMapperDerivationError,
    DomainMapperPersistenceError,
)
from ps_service.domain_mapper.graph_writer import (
    persist_obligation_and_capability_graph,
)
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.domain_mapper.models import (
    CapabilityDecision,
    CapabilityNode,
    CapabilityRequiresEdge,
    DerivationResult,
    ObligationAssignment,
    ObligationHasEdge,
    ObligationNode,
    RequirementSatisfiedByEdge,
    RoleRequirements,
)
from ps_service.domain_mapper.prompts import (
    CAPABILITY_DERIVATION_SYSTEM_PROMPT,
    CAPABILITY_REUSE_VERIFICATION_SYSTEM_PROMPT,
    OBLIGATION_DERIVATION_SYSTEM_PROMPT,
    parse_capability_response,
    parse_capability_reuse_verdict,
    parse_obligation_response,
)
from ps_service.ingestion.graph_submission import require_caught_up
from ps_service.llm_interface.completion import route_completion
from ps_service.llm_interface.models import ChatMessage
from ps_service.logging import LogEmitter, emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ps_service.domain_mapper.falkordb_client import GraphHandle
    from ps_service.domain_mapper.models import CapabilityReuseVerdict
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.llm_interface.client import CompletionCaller

_COMPONENT = "domain_mapper"
_ACTION = "derive_obligations_and_capabilities"
_VERIFY_REUSE_ACTION = "verify_capability_reuse"

_READ_REQUIREMENTS_BY_ROLE_QUERY = (
    "MATCH (:RegulatoryInstrument {id: $rid})-[:EXPRESSES]->(req:Requirement) "
    "OPTIONAL MATCH (rl:Role {id: req.role_id}) "
    "RETURN req.id, req.text, req.role_id, rl.id, rl.name"
)


def derive_obligations_and_capabilities(
    regulatory_instrument_id: str,
    *,
    baseline_graph: GraphHandle,
    submitter: GroupSubmitter,
    baseline_graph_name: str,
    audit_event_id: str,
    model: str,
    call_completion: CompletionCaller | None = None,
    emitter: LogEmitter | None = None,
) -> DerivationResult:
    """Derive Obligations and Capabilities from one instrument's Requirements in its baseline graph.

    Takes no `adapter`: it reads only the baseline graph's fixed domain-model shape, identical
    regardless of source.

    Steps:

    0. `require_caught_up` on the baseline graph. A group an earlier run committed but did not
       apply (extraction's, on a solo resume) would make the read below stale, so the stage
       fails with `DomainMapperPersistenceError` before any read, LLM call or write.
    1. `_read_requirements_by_role`, scoped to the Requirements this instrument version
       `EXPRESSES`, including its whole-collection dangling-`role_id` check, which completes
       before any LLM call.
    2. `_derive_obligations`: whole-run mint, match or unmatchable resolution. It emits its own
       `outcome="unmatched"` log entries.
    3. `_derive_capabilities` over the resulting Obligation nodes: whole-run Capability mint or
       match. Obligations whose Capability response was malformed are returned in
       `DerivationResult.unmatched_obligation_ids` as surfaced data, not a run failure; every
       Obligation node is still submitted.
    4. `graph_writer.persist_obligation_and_capability_graph` submits the Obligations,
       Capabilities and edges as ONE Graph Write Gateway group on `baseline_graph_name` under
       `audit_event_id`. `baseline_graph` is only read. Nothing to write submits nothing.
    5. Emit one `outcome="succeeded"` log entry for the whole call. `run_id` is whatever the
       caller already bound, or `None`; no run context is bound here.

    Args:
        regulatory_instrument_id: Id of the RegulatoryInstrument whose Requirements are derived
            from.
        baseline_graph: Read handle on the `{short}_baseline` graph.
        submitter: Gateway submitter for the derivation group.
        baseline_graph_name: Target `{short}_baseline` graph.
        audit_event_id: Audit event the group is recorded under.
        model: Model identifier passed to the LLM interface.
        call_completion: LLM completion caller; `None` uses the default.
        emitter: Log emitter; `None` uses the default.

    Returns:
        The `DerivationResult` with the Obligation and Capability ids written and the ids of
        unmatched Requirements and Obligations; the tuples are empty when there is nothing to
        derive.

    Raises:
        DomainMapperDerivationError: A Requirement's `role_id` does not resolve to a Role node.
        DomainMapperPersistenceError: The baseline graph is behind its log, a property cannot be
            stored, or the gateway does not take the group.
        LlmProviderError: The LLM provider fails; this is an infrastructure failure and aborts
            the whole run.

    Side Effects:
        Reads the baseline graph, calls the LLM per Requirement and per distinct Obligation (plus
        one verification call per Capability reuse proposal), submits one gateway group, and
        emits structured log entries.
    """
    require_caught_up(submitter, baseline_graph_name, DomainMapperPersistenceError)
    roles = tuple(_read_requirements_by_role(baseline_graph, regulatory_instrument_id).values())

    obligation_nodes, has_edges, satisfied_by_edges, unmatched_requirement_ids = (
        _derive_obligations(roles, model=model, call_completion=call_completion, emitter=emitter)
    )
    capability_nodes, requires_edges, unmatched_obligation_ids = _derive_capabilities(
        obligation_nodes, model=model, call_completion=call_completion, emitter=emitter
    )

    persist_obligation_and_capability_graph(
        submitter,
        baseline_graph_name,
        audit_event_id,
        graph=baseline_graph,
        obligation_nodes=obligation_nodes,
        has_edges=has_edges,
        satisfied_by_edges=satisfied_by_edges,
        capability_nodes=capability_nodes,
        requires_edges=requires_edges,
    )

    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        entity_id=regulatory_instrument_id,
        outcome="succeeded",
        emitter=emitter,
    )

    return DerivationResult(
        regulatory_instrument_id=regulatory_instrument_id,
        obligation_node_ids=tuple(node.id for node in obligation_nodes),
        capability_node_ids=tuple(node.id for node in capability_nodes),
        unmatched_requirement_ids=unmatched_requirement_ids,
        unmatched_obligation_ids=unmatched_obligation_ids,
    )


def _read_requirements_by_role(
    baseline_graph: GraphHandle, regulatory_instrument_id: str
) -> dict[str, RoleRequirements]:
    """Read the instrument's Requirements back from the baseline graph, grouped by Role.

    Runs `MATCH (:RegulatoryInstrument {id: $rid})-[:EXPRESSES]->(req:Requirement)
    OPTIONAL MATCH (rl:Role {id: req.role_id}) RETURN req.id, req.text, req.role_id, rl.id,
    rl.name`. Every row is materialized first and checked before any grouping: a row with a
    non-null `req.role_id` but a null `rl.id` means the Requirement references a Role absent
    from this baseline graph. The check is whole-collection and runs before any LLM call (this
    function makes none).

    A Requirement with no `role_id` is not a dangling reference and is excluded from every
    group, since it belongs to no Role's derivation. `extraction.py::_build_requirement_graph`
    always sets `role_id`, so this does not occur in a correctly written baseline graph.

    Args:
        baseline_graph: Read handle on the baseline graph.
        regulatory_instrument_id: Id of the RegulatoryInstrument whose Requirements are read.

    Returns:
        Map from Role node id to its `RoleRequirements`, with Roles in the order first
        encountered among the rows and each Role's Requirements in row order; empty when the
        instrument has no Requirements.

    Raises:
        DomainMapperDerivationError: On the first Requirement whose `role_id` does not resolve to
            a Role node, naming the Requirement id and the unresolved `role_id`.
    """
    result = baseline_graph.query(
        _READ_REQUIREMENTS_BY_ROLE_QUERY, params={"rid": regulatory_instrument_id}
    )
    rows = cast("list[list[object]]", result.result_set)

    for row in rows:
        requirement_id, _requirement_text, requirement_role_id, role_node_id, _role_name = row
        if requirement_role_id is not None and role_node_id is None:
            raise DomainMapperDerivationError(
                f"Requirement {requirement_id!r} references role_id "
                f"{requirement_role_id!r}, which does not resolve to any "
                "Role node in this baseline graph"
            )

    role_order: list[str] = []
    role_names: dict[str, str] = {}
    requirements_by_role: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        requirement_id, requirement_text, requirement_role_id, role_node_id, role_name = row
        if requirement_role_id is None:
            continue
        role_node_id = cast("str", role_node_id)
        if role_node_id not in requirements_by_role:
            requirements_by_role[role_node_id] = []
            role_names[role_node_id] = cast("str", role_name)
            role_order.append(role_node_id)
        requirements_by_role[role_node_id].append(
            (cast("str", requirement_id), cast("str", requirement_text))
        )

    return {
        role_node_id: RoleRequirements(
            role_node_id=role_node_id,
            role_name=role_names[role_node_id],
            requirements=tuple(requirements_by_role[role_node_id]),
        )
        for role_node_id in role_order
    }


@dataclass
class _DerivationState:
    """Mutable whole-run accumulator threaded through `_process_requirement`.

    Created and owned by `_derive_obligations`; never returned outside this module.

    Attributes:
        registry: The single whole-run registry, `obligation_id -> (text, assigned_role_node_id)`,
            seeded empty once per run. It makes the SAME Role minting identical duty text twice
            converge onto one node; cross-Role separation comes from the Role-scoped
            `obligation_id` hash, not from this registry.
        obligation_nodes: Obligation nodes minted so far.
        has_edges: `HAS` edges, one per minted Obligation.
        satisfied_by_edges: `SATISFIED_BY` edges, one per matched or minted Requirement.
        unmatched_requirement_ids: Requirements the LLM judged unmatchable or answered
            malformed.
    """

    registry: dict[str, tuple[str, str]] = field(default_factory=dict)
    obligation_nodes: list[ObligationNode] = field(default_factory=list)
    has_edges: list[ObligationHasEdge] = field(default_factory=list)
    satisfied_by_edges: list[RequirementSatisfiedByEdge] = field(default_factory=list)
    unmatched_requirement_ids: list[str] = field(default_factory=list)


def _derive_obligations(
    roles: tuple[RoleRequirements, ...],
    *,
    model: str,
    call_completion: CompletionCaller | None = None,
    emitter: LogEmitter | None = None,
) -> tuple[
    tuple[ObligationNode, ...],
    tuple[ObligationHasEdge, ...],
    tuple[RequirementSatisfiedByEdge, ...],
    tuple[str, ...],
]:
    """Run the whole-run Obligation derivation over `roles` and return pure data.

    Iterates `roles` in the given document order and, within each Role, its `requirements` in
    the given order; no reordering is applied. One registry spans the whole run and is never
    reset per Role.

    For each Requirement the LLM is called with the calling Role's own slice of the registry,
    recomputed immediately before every call so that a Role's earlier mint in this run is visible
    to its later Requirements' match decisions. Two outcomes:

    - Unmatchable, either explicitly or because the response was malformed
      (`DomainMapperDerivationError` from `parse_obligation_response`): the Requirement id is
      added to the unmatched ids, an `outcome="unmatched"` log entry is emitted, and derivation
      continues. No Obligation is involved. An `LlmProviderError` is not caught and aborts the
      run.
    - Otherwise `_resolve_obligation_id` decides between a mint and a same-Role reuse against
      the whole-run registry. The Obligation node and `HAS` edge are created only on a mint; a
      `SATISFIED_BY` edge is always written.

    Persisting the result is `graph_writer.persist_obligation_and_capability_graph`'s job.

    Args:
        roles: Roles with their Requirements, in document order.
        model: Model identifier for the LLM call.
        call_completion: LLM completion caller; `None` uses the default.
        emitter: Log emitter; `None` uses the default.

    Returns:
        `(obligation_nodes, has_edges, satisfied_by_edges, unmatched_requirement_ids)`; all
        empty when `roles` is empty.

    Raises:
        LlmProviderError: The LLM call fails.

    Side Effects:
        Calls the LLM once per Requirement and emits one `outcome="unmatched"` entry per
        unmatched Requirement. Writes nothing to the graph.
    """
    state = _DerivationState()
    for role in roles:
        for requirement_id, requirement_text in role.requirements:
            _process_requirement(
                role,
                requirement_id,
                requirement_text,
                state,
                model=model,
                call_completion=call_completion,
                emitter=emitter,
            )
    return (
        tuple(state.obligation_nodes),
        tuple(state.has_edges),
        tuple(state.satisfied_by_edges),
        tuple(state.unmatched_requirement_ids),
    )


def _process_requirement(
    role: RoleRequirements,
    requirement_id: str,
    requirement_text: str,
    state: _DerivationState,
    *,
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> None:
    """Decide one Requirement as mint, match or unmatchable, and record the result in `state`.

    Includes the whole-run registry id resolution and the node and edge bookkeeping. Mutates
    `state` in place.
    """
    role_view = _role_view(state.registry, role.role_node_id)

    try:
        assignment = _derive_obligation_for_requirement(
            requirement_id=requirement_id,
            role_node_id=role.role_node_id,
            role_name=role.role_name,
            requirement_text=requirement_text,
            role_view=role_view,
            model=model,
            call_completion=call_completion,
            emitter=emitter,
        )
    except DomainMapperDerivationError:
        _mark_unmatched(requirement_id, state, emitter)
        return

    if assignment.obligation_node_id is None or assignment.obligation_text is None:
        _mark_unmatched(requirement_id, state, emitter)
        return

    final_id, final_text, is_new_mint = _resolve_obligation_id(
        proposed_text=assignment.obligation_text,
        role_node_id=role.role_node_id,
        registry=state.registry,
    )
    if is_new_mint:
        state.obligation_nodes.append(
            ObligationNode(
                id=final_id,
                properties={"text": final_text, "confidence": assignment.confidence},
            )
        )
        state.has_edges.append(
            ObligationHasEdge(role_node_id=role.role_node_id, obligation_node_id=final_id)
        )
    state.satisfied_by_edges.append(
        RequirementSatisfiedByEdge(requirement_id=requirement_id, obligation_node_id=final_id)
    )


def _role_view(registry: dict[str, tuple[str, str]], role_node_id: str) -> dict[str, str]:
    """Return the slice of the whole-run `registry` already assigned to `role_node_id`.

    Maps obligation id to text. It is recomputed from the live registry at each call site, not
    cached per Role, so a Role's own earlier mint is visible to that Role's later Requirements.
    """
    return {
        oid: text
        for oid, (text, assigned_role_node_id) in registry.items()
        if assigned_role_node_id == role_node_id
    }


def _mark_unmatched(
    requirement_id: str, state: _DerivationState, emitter: LogEmitter | None
) -> None:
    """Record `requirement_id` as unmatched and log it.

    One mechanism covers both the explicit unmatchable outcome and a malformed response: the
    id is surfaced in the result and as an `outcome="unmatched"` log entry, never silently
    dropped and never raised.
    """
    state.unmatched_requirement_ids.append(requirement_id)
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        entity_id=requirement_id,
        outcome="unmatched",
        emitter=emitter,
    )


def _resolve_obligation_id(
    *,
    proposed_text: str,
    role_node_id: str,
    registry: dict[str, tuple[str, str]],
) -> tuple[str, str, bool]:
    """Resolve one proposed duty text to its final Obligation id against the whole-run registry.

    Mutates `registry` on a mint; a reuse does not.

    - `obligation_id(role_node_id, proposed_text)` not in `registry`: mint it and register
      `(proposed_text, role_node_id)`.
    - Already in `registry`: the SAME Role is re-minting or re-matching its own registered
      Obligation (only that Role can produce the id, since the Role is in the hash). The
      existing id and text are reused. This keeps same-Role convergence correct even when the
      LLM independently mints the same text twice.

    There is no cross-Role case: the Role-scoped `obligation_id` gives two Roles two ids for
    identical duty text, so each is its own mint with its own `HAS` edge.

    Args:
        proposed_text: The duty text proposed by the LLM.
        role_node_id: Id of the Role that bears the duty.
        registry: The whole-run registry, `obligation_id -> (text, assigned_role_node_id)`.

    Returns:
        `(final_obligation_id, final_text, is_new_mint)`.
    """
    oid = obligation_id(role_node_id, proposed_text)
    existing = registry.get(oid)

    if existing is None:
        registry[oid] = (proposed_text, role_node_id)
        return oid, proposed_text, True

    existing_text, _existing_role_node_id = existing
    return oid, existing_text, False


def _derive_obligation_for_requirement(
    *,
    requirement_id: str,
    role_node_id: str,
    role_name: str,
    requirement_text: str,
    role_view: dict[str, str],
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> ObligationAssignment:
    """Call the LLM once for one Requirement and return its `ObligationAssignment`.

    Args:
        requirement_id: Id of the Requirement being decided.
        role_node_id: Id of the Role that bears the Requirement.
        role_name: Name of that Role, shown to the LLM.
        requirement_text: The Requirement's duty text.
        role_view: The Role's existing Obligations, `obligation_id -> text`.
        model: Model identifier for the LLM call.
        call_completion: LLM completion caller; `None` uses the default.
        emitter: Log emitter; `None` uses the default.

    Returns:
        The parsed assignment; its `obligation_*` fields are `None` when the Requirement is
        unmatchable.

    Raises:
        DomainMapperDerivationError: The LLM response is malformed. Not caught here; unifying it
            with the unmatchable outcome is `_process_requirement`'s job.
        LlmProviderError: The LLM call itself fails; this aborts the whole run.
    """
    messages = _build_obligation_messages(
        role_name=role_name, requirement_text=requirement_text, role_view=role_view
    )
    result = route_completion(
        messages, model=model, call_completion=call_completion, emitter=emitter
    )
    return parse_obligation_response(result.text, requirement_id, role_node_id, role_view)


def _build_obligation_messages(
    *, role_name: str, requirement_text: str, role_view: dict[str, str]
) -> list[ChatMessage]:
    """Build the system prompt and one user message carrying the duty text and registry view.

    The duty text and the Role's own registry view are delimited from the system prompt's
    instructions because they are untrusted input.
    """
    registry_text = "\n".join(f"- {oid}: {text}" for oid, text in role_view.items()) or "(empty)"
    user_content = (
        f"Role: {role_name}\n\n"
        "<requirement_text>\n"
        f"{requirement_text}\n"
        "</requirement_text>\n\n"
        f"Existing Obligation registry for this Role:\n{registry_text}"
    )
    return [
        ChatMessage(role="system", content=OBLIGATION_DERIVATION_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_content),
    ]


# --- Capability derivation ---


@dataclass
class _CapabilityDerivationState:
    """Mutable whole-run accumulator threaded through `_derive_capabilities`'s loop.

    Mirrors `_DerivationState`, but its registry is deliberately Obligation- and Role-independent,
    unlike Obligation derivation's Role-scoped registry.

    Attributes:
        registry: The single whole-run Capability registry,
            `capability_id -> (name, description)`, seeded empty once per run.
        capability_nodes: Capability nodes minted so far.
        requires_edges: `REQUIRES` edges recorded so far.
        unmatched_obligation_ids: Obligations whose Capability derivation was malformed or
            failed verification.
    """

    registry: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    capability_nodes: list[CapabilityNode] = field(default_factory=list)
    requires_edges: list[CapabilityRequiresEdge] = field(default_factory=list)
    unmatched_obligation_ids: list[str] = field(default_factory=list)


def _derive_capabilities(
    obligations: tuple[ObligationNode, ...],
    *,
    model: str,
    call_completion: CompletionCaller | None = None,
    emitter: LogEmitter | None = None,
) -> tuple[tuple[CapabilityNode, ...], tuple[CapabilityRequiresEdge, ...], tuple[str, ...]]:
    """Run the whole-run Capability derivation over `obligations` and return pure data.

    `obligations` is de-duplicated by `.id` first (`_distinct_obligations`; the first-seen text
    wins), so there is exactly one LLM call per DISTINCT Obligation even if the same Obligation
    appears more than once (for example because two Requirements routed to it). A repeated input
    entry therefore also collapses to a single Capability set and a single `REQUIRES` edge per
    Capability.

    One registry spans the whole run. Capability convergence is Obligation- and Role-independent:
    if two Obligations' responses name or match the same Capability (by
    `identity.capability_id(name)`), it is one shared node with one `REQUIRES` edge per
    Obligation. The registry, not the model, guarantees this. One Obligation's response may name
    more than one Capability, each giving its own `REQUIRES` edge from the same Obligation.

    A malformed response for one Obligation (`DomainMapperDerivationError` from
    `parse_capability_response`) is isolated by `_process_obligation`: that Obligation's id is
    added to the unmatched ids and an `outcome="unmatched"` log entry is emitted, but derivation
    continues for every other Obligation and the registry built so far is not affected. An
    `LlmProviderError` is not caught and aborts the run.

    Persisting the result is `graph_writer.persist_obligation_and_capability_graph`'s job.

    Args:
        obligations: Obligation nodes to derive Capabilities for; each must carry a `text`
            property.
        model: Model identifier for the LLM call.
        call_completion: LLM completion caller; `None` uses the default.
        emitter: Log emitter; `None` uses the default.

    Returns:
        `(capability_nodes, requires_edges, unmatched_obligation_ids)`; all empty when
        `obligations` is empty.

    Raises:
        LlmProviderError: The LLM call fails.

    Side Effects:
        Calls the LLM per distinct Obligation (plus one verification call per reuse proposal) and
        emits log entries. Writes nothing to the graph.
    """
    state = _CapabilityDerivationState()

    for obligation_node_id, obligation_text in _distinct_obligations(obligations):
        _process_obligation(
            obligation_node_id,
            obligation_text,
            state,
            model=model,
            call_completion=call_completion,
            emitter=emitter,
        )

    return (
        tuple(state.capability_nodes),
        tuple(state.requires_edges),
        tuple(state.unmatched_obligation_ids),
    )


def _process_obligation(
    obligation_node_id: str,
    obligation_text: str,
    state: _CapabilityDerivationState,
    *,
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> None:
    """Decide one distinct Obligation's Capabilities in two phases, mutating `state` in place.

    1. Resolve: derive the Obligation's `CapabilityDecision`s, then send each reuse proposal
       (`is_reuse`) through `_verify_reuse`, one verification call per reuse. First mints pass
       through unverified. `state` does not change in this phase.
    2. Apply: register each new Capability and append the `REQUIRES` edges.

    A `DomainMapperDerivationError` raised anywhere in the Resolve phase (a malformed derivation
    or verification response) is caught before any state change and recorded through
    `_mark_capability_unmatched`, so an Obligation gets either all of its `REQUIRES` edges or
    none. An `LlmProviderError` is not caught.

    If a verifier mints the same id as another mint in the same response, the apply loop creates
    the node once (registry guard) but `requires_edges` holds the `(obligation, capability)` pair
    twice. This is harmless: the graph writer de-duplicates edges by identity before submitting.
    """
    try:
        proposed_decisions = _derive_capabilities_for_obligation(
            obligation_node_id=obligation_node_id,
            obligation_text=obligation_text,
            registry=state.registry,
            model=model,
            call_completion=call_completion,
            emitter=emitter,
        )
        decisions = [
            _verify_reuse(
                decision,
                obligation_text=obligation_text,
                registry=state.registry,
                model=model,
                call_completion=call_completion,
                emitter=emitter,
            )
            if decision.is_reuse
            else decision
            for decision in proposed_decisions
        ]
    except DomainMapperDerivationError:
        _mark_capability_unmatched(obligation_node_id, state, emitter)
        return

    for decision in decisions:
        if decision.capability_node_id not in state.registry:
            state.registry[decision.capability_node_id] = (
                decision.name,
                decision.description,
            )
            state.capability_nodes.append(_to_capability_node(decision))
        state.requires_edges.append(
            CapabilityRequiresEdge(
                obligation_node_id=obligation_node_id,
                capability_node_id=decision.capability_node_id,
            )
        )


def _verify_reuse(
    decision: CapabilityDecision,
    *,
    obligation_text: str,
    registry: Mapping[str, tuple[str, str | None]],
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> CapabilityDecision:
    """Verify one Capability reuse proposal with exactly one LLM call.

    1. One `route_completion` call judges whether the proposed existing Capability's
       description covers the Obligation's whole duty.
    2. `parse_capability_reuse_verdict` parses the verdict; a malformed verdict raises
       `DomainMapperDerivationError` and emits no verdict line.
    3. On `accept`, emit `outcome="reuse_accepted"` and return `decision` unchanged (same
       Capability, no registry change, first-pass confidence kept).
    4. On `reject`, `_mint_verifier_capability` turns the verdict into a decision minting the
       verifier's Capability, or raises when that Capability already exists.

    On `reject` the verdict must carry a `confidence` in [0, 1], because the minted
    `CapabilityDecision` needs one and has no default; a missing or invalid value is a malformed
    verdict. On `accept` the verdict's `confidence` is optional and ignored.

    Args:
        decision: The reuse proposal to verify.
        obligation_text: The Obligation's duty text.
        registry: The whole-run Capability registry, read only.
        model: Model identifier for the LLM call.
        call_completion: LLM completion caller; `None` uses the default.
        emitter: Log emitter; `None` uses the default.

    Returns:
        `decision` itself on accept, or a new non-reuse decision carrying the verifier's
        Capability on reject.

    Raises:
        DomainMapperDerivationError: The verdict is malformed, or the verifier's Capability is
            already in `registry`.
        LlmProviderError: The LLM call fails; this is never caught.

    Side Effects:
        One LLM call and one `verify_capability_reuse` log entry.
    """
    messages = _build_reuse_verification_messages(
        obligation_text=obligation_text,
        capability_name=decision.name,
        capability_description=decision.description,
    )
    completion = route_completion(
        messages, model=model, call_completion=call_completion, emitter=emitter
    )
    verdict = parse_capability_reuse_verdict(
        completion.text, decision.obligation_node_id, decision.capability_node_id
    )
    if verdict.verdict == "accept":
        _emit_reuse_verdict(
            decision.obligation_node_id,
            decision.capability_node_id,
            outcome="reuse_accepted",
            extra=None,
            emitter=emitter,
        )
        return decision
    return _mint_verifier_capability(decision, verdict, registry=registry, emitter=emitter)


def _mint_verifier_capability(
    decision: CapabilityDecision,
    verdict: CapabilityReuseVerdict,
    *,
    registry: Mapping[str, tuple[str, str | None]],
    emitter: LogEmitter | None,
) -> CapabilityDecision:
    """Turn a rejected reuse into a decision minting the verifier's Capability.

    `parse_capability_reuse_verdict` guarantees a reject carries a non-empty `new_name` and a
    `confidence`; the model types them as optional only because `accept` leaves them null.

    Raises:
        DomainMapperDerivationError: The verifier's Capability is already in `registry` (a
            collision). Its audit line is emitted first; the Obligation is then marked unmatched
            by the caller, with no retry and no second call.
    """
    new_name = cast("str", verdict.new_name)
    minted_id = capability_id(new_name)
    if minted_id in registry:
        _emit_reuse_verdict(
            decision.obligation_node_id,
            decision.capability_node_id,
            outcome="reuse_rejected",
            extra={"collision": True, "proposed_capability_id": minted_id},
            emitter=emitter,
        )
        raise DomainMapperDerivationError(
            f"reuse verification for obligation {decision.obligation_node_id!r} rejected "
            f"capability {decision.capability_node_id!r} but proposed {minted_id!r}, which is "
            "already in the capability registry"
        )
    _emit_reuse_verdict(
        decision.obligation_node_id,
        decision.capability_node_id,
        outcome="reuse_rejected",
        extra={"minted_capability_id": minted_id},
        emitter=emitter,
    )
    return CapabilityDecision(
        obligation_node_id=decision.obligation_node_id,
        capability_node_id=minted_id,
        name=new_name,
        description=verdict.new_description,
        confidence=cast("float", verdict.confidence),
        is_reuse=False,
    )


def _emit_reuse_verdict(
    obligation_node_id: str,
    matched_capability_id: str,
    *,
    outcome: str,
    extra: Mapping[str, object] | None,
    emitter: LogEmitter | None,
) -> None:
    """Emit one `verify_capability_reuse` audit line keyed by (Obligation, matched Capability).

    `extra` keys are flattened into top-level keys of the JSON log line.
    """
    emit_log_entry(
        component=_COMPONENT,
        action=_VERIFY_REUSE_ACTION,
        entity_id=(obligation_node_id, matched_capability_id),
        outcome=outcome,
        extra=extra,
        emitter=emitter,
    )


def _mark_capability_unmatched(
    obligation_node_id: str, state: _CapabilityDerivationState, emitter: LogEmitter | None
) -> None:
    """Record `obligation_node_id` as unmatched for Capability derivation and log it.

    Uses the same "surfaced, never silently dropped" mechanism as `_mark_unmatched`: the id is
    recorded and an `outcome="unmatched"` log entry is emitted. It is not shared with
    `_mark_unmatched` because this is only the second occurrence of the shape, below the
    third-occurrence extraction threshold.
    """
    state.unmatched_obligation_ids.append(obligation_node_id)
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        entity_id=obligation_node_id,
        outcome="unmatched",
        emitter=emitter,
    )


def _distinct_obligations(obligations: tuple[ObligationNode, ...]) -> list[tuple[str, str]]:
    """Return `(id, text)` pairs of `obligations`, de-duplicated by id with the first text winning.

    This guarantees the LLM is called at most once per distinct Obligation even if the same
    Obligation appears more than once in `obligations`.
    """
    seen: dict[str, str] = {}
    for obligation in obligations:
        seen.setdefault(obligation.id, str(obligation.properties["text"]))
    return list(seen.items())


def _to_capability_node(decision: CapabilityDecision) -> CapabilityNode:
    """Build the Capability node for `decision`, active at creation, with `description` if set."""
    properties: dict[str, str | float] = {
        "name": decision.name,
        "status": "active",
        "confidence": decision.confidence,
    }
    if decision.description is not None:
        properties["description"] = decision.description
    return CapabilityNode(id=decision.capability_node_id, properties=properties)


def _derive_capabilities_for_obligation(
    *,
    obligation_node_id: str,
    obligation_text: str,
    registry: dict[str, tuple[str, str | None]],
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> list[CapabilityDecision]:
    """Call the LLM once for one distinct Obligation and return its `CapabilityDecision` list.

    The list may hold more than one entry when the Obligation requires several Capabilities.

    Args:
        obligation_node_id: Id of the Obligation being decided.
        obligation_text: The Obligation's duty text.
        registry: The whole-run Capability registry, shown to the LLM.
        model: Model identifier for the LLM call.
        call_completion: LLM completion caller; `None` uses the default.
        emitter: Log emitter; `None` uses the default.

    Returns:
        The parsed decisions, mints and reuse proposals alike.

    Raises:
        DomainMapperDerivationError: The response is malformed. Not caught here;
            `_process_obligation` isolates it.
        LlmProviderError: The LLM call fails; this is never caught here.
    """
    messages = _build_capability_messages(obligation_text=obligation_text, registry=registry)
    result = route_completion(
        messages, model=model, call_completion=call_completion, emitter=emitter
    )
    return parse_capability_response(result.text, obligation_node_id, registry)


def _build_capability_messages(
    *, obligation_text: str, registry: dict[str, tuple[str, str | None]]
) -> list[ChatMessage]:
    """Build the system prompt and one user message carrying the duty text and the registry.

    The duty text and the whole-run Capability registry built so far are delimited from the
    system prompt's instructions because they are untrusted input. Each registry entry is
    rendered as `id: name — description` (or `— (no description)`), so the model can judge
    whether an existing Capability covers the Obligation's whole duty.
    """
    registry_text = (
        "\n".join(
            f"- {cid}: {name} — {description or '(no description)'}"
            for cid, (name, description) in registry.items()
        )
        or "(empty)"
    )
    user_content = (
        "<obligation_text>\n"
        f"{obligation_text}\n"
        "</obligation_text>\n\n"
        "<capability_registry>\n"
        f"{registry_text}\n"
        "</capability_registry>"
    )
    return [
        ChatMessage(role="system", content=CAPABILITY_DERIVATION_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_content),
    ]


def _build_reuse_verification_messages(
    *, obligation_text: str, capability_name: str, capability_description: str | None
) -> list[ChatMessage]:
    """Build the system prompt and one user message carrying one Obligation and Capability pair.

    The duty text and the Capability's name and description sit in their own tags in the user
    message and never in the system prompt, because they are untrusted input.
    """
    user_content = (
        "<obligation_text>\n"
        f"{obligation_text}\n"
        "</obligation_text>\n\n"
        f"<capability_name>{capability_name}</capability_name>\n"
        f"<capability_description>{capability_description or '(no description)'}"
        "</capability_description>"
    )
    return [
        ChatMessage(role="system", content=CAPABILITY_REUSE_VERIFICATION_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_content),
    ]
