"""ps_service.domain_mapper.derivation — the DeriveObligationsAndCapabilities action.

Holds the public orchestrating function (`derive_obligations_and_capabilities`,
PLAN_REVIEWED.md §11 Increment 16, wiring Increments 11-15 plus §7.2's
`_read_requirements_by_role`), the whole-run Obligation derivation
(`_derive_obligations`, Increment 12), and the whole-run Capability
derivation (`_derive_capabilities`, Increment 14, §7.4).

Obligation identity is Role-scoped (`identity.obligation_id(role_node_id,
text)`, resolution of issue #42), so a given duty text under two different
Roles is two distinct Obligation nodes by construction — there is no
cross-Role collision to detect or resolve here. The whole-run registry
below still guarantees that the SAME Role independently minting the same
duty text twice in one run converges onto a single node (the code, not the
LLM, owns that).

**`_read_requirements_by_role`** (this increment) is §7.2's B3 fix (b): the
read-side complement to `graph_writer.py`'s write-side B3 fix (a)
(`_validate_role_references`). It reads every Requirement back from the
baseline graph, grouped by its `role_id` bookkeeping property (§7.2), and
raises `DomainMapperDerivationError` — naming the offending Requirement id
and its unresolved `role_id` — if any Requirement's `role_id` does not
resolve to a Role node in this baseline graph, BEFORE any LLM call is made
for any Role's Requirements. This defends against a baseline graph left in
a partially-written state by an older run or edited out of band; it should
never fire against a graph written by the current, B3-hardened
`persist_role_and_requirement_graph`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from ps_service.domain_mapper.errors import DomainMapperDerivationError
from ps_service.domain_mapper.graph_writer import (
    persist_obligation_and_capability_graph,
)
from ps_service.domain_mapper.identity import obligation_id
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
    OBLIGATION_DERIVATION_SYSTEM_PROMPT,
    parse_capability_response,
    parse_obligation_response,
)
from ps_service.llm_interface.completion import route_completion
from ps_service.llm_interface.models import ChatMessage
from ps_service.logging import LogEmitter, emit_log_entry

if TYPE_CHECKING:
    from ps_service.domain_mapper.falkordb_client import GraphHandle
    from ps_service.llm_interface.client import CompletionCaller

_COMPONENT = "domain_mapper"
_ACTION = "derive_obligations_and_capabilities"

_READ_REQUIREMENTS_BY_ROLE_QUERY = (
    "MATCH (req:Requirement) "
    "OPTIONAL MATCH (rl:Role {id: req.role_id}) "
    "RETURN req.id, req.text, req.role_id, rl.id, rl.name"
)


def derive_obligations_and_capabilities(
    regulatory_instrument_id: str,
    *,
    baseline_graph: GraphHandle,
    model: str,
    call_completion: CompletionCaller | None = None,
    emitter: LogEmitter | None = None,
) -> DerivationResult:
    """DeriveObligationsAndCapabilities — PLAN_REVIEWED.md §7.1/§11 Increment 16's flow.

    No `adapter` parameter (§7.1) — this action reads only the baseline
    graph's own fixed PS-Conceptual-Model shape, identical regardless of
    source.

    1. `_read_requirements_by_role(baseline_graph)` — §7.2's read-back,
       including its whole-collection dangling-`role_id` check, which runs
       to completion (raising on the first violation found) before any LLM
       call is made anywhere in this function.
    2. `_derive_obligations` (Increment 12) — whole-run
       mint/match/unmatchable resolution. Its own `outcome="unmatched"` log
       entries are emitted internally (§7.5) — not duplicated here.
    3. `_derive_capabilities` (Increment 14), called with `_derive_obligations`'s
       `obligation_nodes` output directly — whole-run Capability
       mint/match.

       Capability derivation's own `unmatched_obligation_ids` (issue #64)
       flows straight into this function's returned
       `DerivationResult.unmatched_obligation_ids`, mirroring
       `unmatched_requirement_ids`'s own path from step 2 — this is
       surfaced data, not a run failure. Every Obligation node is still
       persisted via `graph_writer.persist_obligation_and_capability_graph`
       (step 4) regardless of whether its own Capability derivation
       resolved.
    4. `graph_writer.persist_obligation_and_capability_graph` (Increment 15)
       — plain writer, no validation of its own (see that module's
       docstring for why).
    5. Emits one `outcome="succeeded"` entry for the whole call. No
       `bind_run_context()` call here (§6) — `run_id` is whatever the
       caller already bound, or `None`.
    6. Returns `DerivationResult`.
    """
    roles = tuple(_read_requirements_by_role(baseline_graph).values())

    obligation_nodes, has_edges, satisfied_by_edges, unmatched_requirement_ids = (
        _derive_obligations(roles, model=model, call_completion=call_completion, emitter=emitter)
    )
    capability_nodes, requires_edges, unmatched_obligation_ids = _derive_capabilities(
        obligation_nodes, model=model, call_completion=call_completion, emitter=emitter
    )

    persist_obligation_and_capability_graph(
        baseline_graph,
        obligation_nodes,
        has_edges,
        satisfied_by_edges,
        capability_nodes,
        requires_edges,
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


def _read_requirements_by_role(baseline_graph: GraphHandle) -> dict[str, RoleRequirements]:
    """PLAN_REVIEWED.md §7.2's exact Cypher and validation logic — the B3 read-side fix.

    `MATCH (req:Requirement) OPTIONAL MATCH (rl:Role {id: req.role_id})
    RETURN req.id, req.text, req.role_id, rl.id, rl.name` — every row is
    materialized FIRST, then every row's `rl.id` (the `OPTIONAL MATCH`
    result) is checked non-null for every row whose `req.role_id` is itself
    non-null, BEFORE any grouping/return happens. If any row has a non-null
    `req.role_id` but a null `rl.id` — a Requirement references a Role that
    doesn't exist in this baseline graph — raises
    `DomainMapperDerivationError` naming the dangling Requirement id and its
    unresolved `role_id`, on the FIRST violation found. This is a
    whole-collection check, not incremental, and runs before any LLM call
    is made for any Role's Requirements (this function makes no LLM calls
    itself, and is always called before `_derive_obligations` by
    `derive_obligations_and_capabilities`).

    A Requirement with no `role_id` at all (`req.role_id is None`) is not a
    dangling reference — there is nothing to resolve — and is silently
    excluded from every group (it belongs to no Role's derivation). Every
    `RequirementNode` `extraction.py::_build_requirement_graph` produces
    always sets `role_id`, so this should never occur from a correctly
    written baseline graph.

    Returns a `dict[role_node_id, RoleRequirements]`, grouping Requirements
    by their resolved Role node id in the order each Role is first
    encountered among the rows, each Role's own Requirements in row order.
    """
    result = baseline_graph.query(_READ_REQUIREMENTS_BY_ROLE_QUERY)
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

    Threaded by `_derive_obligations`'s own loop — never returned to a
    caller outside this module.

    `registry` is the single whole-run registry (`obligation_id -> (text,
    assigned_role_node_id)`), seeded empty once for the entire run. It
    exists so the SAME Role minting identical duty text twice in one run
    converges onto one node; cross-Role separation is already guaranteed by
    the Role-scoped `obligation_id` hash (#42), not by this registry.
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
    """Whole-run Obligation derivation, adapted for #42's Role-scoped Obligation identity.

    PLAN_REVIEWED.md §7.3.

    Iterates `roles` in document order, and within each Role its
    `requirements` in document order (the caller's own tuple order — this
    function imposes no reordering of its own). A single `registry`
    (`_DerivationState.registry`) spans the WHOLE run, seeded empty once at
    the start, never reset per-Role.

    For each Requirement: calls the LLM (Increment 11's prompt/parser) with
    `role_view` — the calling Role's own slice of `registry`, recomputed
    fresh immediately before every call (not cached once per Role) so that
    a Role's own earlier mint in this same run is visible to its own later
    Requirements' match decisions (Increment 12 test (a) requires this: a
    second, duty-matching Requirement under the SAME Role must be able to
    match the first's freshly-minted entry). Three outcomes:

    - `unmatchable` (explicit, or an unparseable/malformed
      `DomainMapperDerivationError` from `parse_obligation_response` —
      §7.5 unifies both under one mechanism, "surfaced, not silently
      skipped"): the Requirement's id is added to `unmatched_requirement_ids`,
      an `outcome="unmatched"` log entry is emitted, and derivation
      continues to the next Requirement — no exception propagates and no
      Obligation is involved. An `LlmProviderError` (a genuine infra
      failure calling the LLM at all) is, as in extraction, never caught
      here — it propagates and aborts the whole derivation run.
    - Otherwise, `_resolve_obligation_id` (below) computes the Obligation
      id/text against the WHOLE-run `registry` — either a mint or a
      same-Role reuse (the Role-scoped `obligation_id` hash means a
      different Role can never produce a colliding id). A `HAS` edge and
      Obligation node are created only the first time a given id appears
      (mint); a `SATISFIED_BY` edge is always written.

    Returns `(obligation_nodes, has_edges, satisfied_by_edges,
    unmatched_requirement_ids)` — pure data, no graph writes. Persisting
    this is Increment 15's `graph_writer.persist_obligation_and_capability_graph`
    (out of this batch's scope), the same split Increment 8 (pure) /
    Increment 9 (writer) established for Role/Requirement.
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
    """One Requirement's mint-or-match-or-unmatchable decision.

    Plus the whole-registry id resolution and the corresponding node/edge
    bookkeeping. Mutates `state` in place.
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
    """PLAN_REVIEWED.md §7.3 step 1 — "the slice of the whole-run registry already assigned to R".

    Recomputed fresh from the live whole-run `registry` at each call site
    (not cached once per Role) so a Role's own earlier mint in this run is
    visible to that same Role's later Requirements.
    """
    return {
        oid: text
        for oid, (text, assigned_role_node_id) in registry.items()
        if assigned_role_node_id == role_node_id
    }


def _mark_unmatched(
    requirement_id: str, state: _DerivationState, emitter: LogEmitter | None
) -> None:
    """§7.5 — unify the "explicit unmatchable" and "malformed response" failure modes.

    One mechanism: surfaced via the return value and an
    `outcome="unmatched"` log entry, never a silently-dropped Requirement
    and never an uncaught exception.
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

    Mutates `registry` in place on a mint; a reuse never mutates it.

    Returns `(final_obligation_id, final_text, is_new_mint)`.

    - `oid = obligation_id(role_node_id, proposed_text)` not in `registry`:
      mint it, register `(proposed_text, role_node_id)`, `is_new_mint=True`.
    - `oid` already in `registry`: the SAME Role re-minting/re-matching its
      own already-registered Obligation (only the same Role can produce
      this id — it is in the hash). Reuse the existing id/text,
      `is_new_mint=False`. This is what makes same-Role convergence correct
      even when the LLM independently mints the same text twice — the code,
      not the model, guarantees uniqueness.

    There is no cross-Role case: #42's Role-scoped `obligation_id` gives two
    different Roles two different ids for identical duty text, so each
    lands as its own mint with its own `HAS` edge.
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
    """Call the LLM once for one Requirement, returning its `ObligationAssignment`.

    Uses Increment 11's prompt/parser.

    A `DomainMapperDerivationError` from a malformed/unparseable LLM
    response propagates unchanged — this function does not catch it;
    unifying it with the explicit `unmatchable` outcome is
    `_process_requirement`'s job (§7.5), not this one's. An
    `LlmProviderError` from `route_completion` itself is likewise never
    caught here — it propagates and aborts the whole run, the same
    fail-fast infra boundary `extraction.py` follows.
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
    """System prompt + one user message carrying the Requirement's duty text and registry view.

    The duty text and the Role's own registry view are clearly delimited
    from the system prompt's instructions (L2's untrusted-content rule).
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


# --- Capability derivation (PLAN_REVIEWED.md §11 Increment 14) -------------


@dataclass
class _CapabilityDerivationState:
    """Mutable whole-run accumulator threaded through `_derive_capabilities`'s loop.

    Mirrors `_DerivationState`. `registry` is the single whole-run Capability
    registry (`capability_id -> (name, description)`), seeded empty once for
    the entire run — deliberately Obligation- and Role-independent (§7.4),
    unlike Obligation derivation's Role-scoped registry.
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
    """PLAN_REVIEWED.md §7.4 — whole-run Capability derivation.

    Dedups `obligations` by `.id` first (`_distinct_obligations`,
    first-seen text wins for a repeated id) — exactly one LLM call per
    DISTINCT Obligation, never more, even if the same Obligation appears
    more than once in `obligations` (e.g. because two Requirements both
    routed to it upstream — §7.4's explicit "you must not call the LLM
    twice for it" instruction). Since all downstream processing (the LLM
    call, node/edge creation) is keyed off this same deduped list, a
    repeated input entry also collapses to a single Capability set and a
    single `REQUIRES` edge per Capability it requires — not one per
    occurrence in the input.

    A single registry (`capability_id -> (name, description)`) spans the
    WHOLE run, across every distinct Obligation processed — Capability
    convergence is deliberately Obligation- AND Role-independent (§7.4),
    unlike Obligation derivation's own Role-scoped `role_view` — there is
    no role-qualification concept anywhere in this stage. If two different
    Obligations' LLM responses both name/match the same Capability (by
    `identity.capability_id(name)`), that is a legitimate single shared
    Capability node with multiple `REQUIRES` edges, one per Obligation —
    the registry, not the model, guarantees this, mirroring
    `_resolve_obligation_id`'s own code-guarantees-uniqueness philosophy.

    A single Obligation's response may name/match MORE THAN ONE
    Capability — each produces its own `REQUIRES` edge off the SAME
    Obligation node (multi-capability-per-Obligation support, ported from
    `spikes/cellar2/derive_capabilities.py`, a proven finding retained
    here per §7.4/§11 Increment 13's explicit instruction).

    Returns `(capability_nodes, requires_edges, unmatched_obligation_ids)` —
    pure data, no graph writes (Increment 15's
    `graph_writer.persist_obligation_and_capability_graph`, out of this
    batch's scope). A malformed/unparseable response for one Obligation
    (`DomainMapperDerivationError` from `parse_capability_response`) is
    isolated by `_process_obligation` (issue #64): that one Obligation's id
    is added to `unmatched_obligation_ids` and an `outcome="unmatched"` log
    entry is emitted, but derivation continues for every other distinct
    Obligation — the failure neither aborts the run nor poisons the
    whole-run registry state built up so far. `unmatched_obligation_ids` is
    threaded all the way into `DerivationResult`, mirroring
    `unmatched_requirement_ids` — surfaced, never silently dropped. An
    `LlmProviderError` (a genuine infra failure calling the LLM at all) is,
    as elsewhere in this module, never caught here — it still propagates
    and aborts the whole run.
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
    """One distinct Obligation's Capability mint-or-match decision.

    Mirrors `_process_requirement`'s isolation shape exactly (issue #64):
    a `DomainMapperDerivationError` from a malformed/unparseable response is
    caught here, one level up from `_derive_capabilities_for_obligation`
    (which does not catch it itself), and unified with the
    "surfaced, not silently dropped" outcome via `_mark_capability_unmatched`.
    Mutates `state` in place.
    """
    try:
        decisions = _derive_capabilities_for_obligation(
            obligation_node_id=obligation_node_id,
            obligation_text=obligation_text,
            registry=state.registry,
            model=model,
            call_completion=call_completion,
            emitter=emitter,
        )
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


def _mark_capability_unmatched(
    obligation_node_id: str, state: _CapabilityDerivationState, emitter: LogEmitter | None
) -> None:
    """§7.5-equivalent for Capability derivation (issue #64).

    Unifies the malformed-response failure mode with the same "surfaced,
    never silently dropped" mechanism `_mark_unmatched` establishes for
    Obligation derivation: the Obligation's id is recorded and an
    `outcome="unmatched"` log entry is emitted, never a silently-skipped
    Obligation and never an uncaught exception. Not extracted into a shared
    helper with `_mark_unmatched` — only the second occurrence of this
    log-emission shape in this module, below L2's third-occurrence
    extraction threshold.
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
    """First-seen-wins dedup by `.id`.

    The mechanism that guarantees the LLM is called at most once per
    distinct Obligation, even if the same Obligation appears more than once
    in `obligations` (§7.4).
    """
    seen: dict[str, str] = {}
    for obligation in obligations:
        seen.setdefault(obligation.id, str(obligation.properties["text"]))
    return list(seen.items())


def _to_capability_node(decision: CapabilityDecision) -> CapabilityNode:
    properties: dict[str, str | float] = {
        "name": decision.name,
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
    """Call the LLM once for one distinct Obligation, returning its `CapabilityDecision` list.

    Uses Increment 13's prompt/parser — possibly more than one entry
    (multi-capability-per-Obligation, §7.4).

    A `DomainMapperDerivationError` from a malformed/unparseable response
    propagates unchanged — this function does not catch it, mirroring
    `_derive_obligation_for_requirement`'s own split of responsibility. An
    `LlmProviderError` from `route_completion` itself is likewise never
    caught here.
    """
    messages = _build_capability_messages(obligation_text=obligation_text, registry=registry)
    result = route_completion(
        messages, model=model, call_completion=call_completion, emitter=emitter
    )
    return parse_capability_response(result.text, obligation_node_id, registry)


def _build_capability_messages(
    *, obligation_text: str, registry: dict[str, tuple[str, str | None]]
) -> list[ChatMessage]:
    """System prompt + one user message carrying the Obligation's duty text and the registry.

    The duty text and the WHOLE-run Capability registry built so far are
    clearly delimited from the system prompt's instructions (L2's
    untrusted-content rule).
    """
    registry_text = (
        "\n".join(f"- {cid}: {name}" for cid, (name, _description) in registry.items()) or "(empty)"
    )
    user_content = (
        "<obligation_text>\n"
        f"{obligation_text}\n"
        "</obligation_text>\n\n"
        f"Existing Capability registry:\n{registry_text}"
    )
    return [
        ChatMessage(role="system", content=CAPABILITY_DERIVATION_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_content),
    ]
