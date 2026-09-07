"""ps_service.domain_mapper.governance — the DeriveGovernanceArtifacts action (issue #54, S3).

Holds the public orchestrating function (`derive_governance_artifacts`), the
whole-run Policy derivation (`_derive_policies`, mirroring `derivation.py`'s
`_derive_obligations` mint/match/unmatchable shape), and the per-Policy
Standard / per-Standard Control derivation (`_derive_standards`/
`_derive_controls`, mirroring `_derive_capabilities`'s plain per-item shape).

**D1 (`PLAN.md` §3)**: this action reads Capabilities back from
`{short}_baseline` via fixed Cypher, the same way the already-shipped
`derive_obligations_and_capabilities` reads Requirements back directly — no
separate "Domain Mapping Adapter" object. There is, and will be for the
foreseeable future, exactly one way to read Policy/Standard/Control
derivation inputs: a Capability already sitting in the baseline graph.

**AC-BI-008 (defense-in-depth)**: `_require_internal_source` checks the
`RegulatoryInstrument`'s own `source_type` and raises
`DomainMapperGovernanceError` for anything other than `"internal"`, before
any Capability is read or any LLM call is made. The orchestration
(`ps_service.api.ingestion_orchestration`) never invokes this function for
an external `RegulatoryInstrument` in practice — this is a second,
independent guard inside the function itself, not the only one.

**AC-BI-014**: a Capability whose Policy derivation resolves to
`unmatchable` (or whose LLM response is malformed/unparseable) is surfaced
via `GovernanceDerivationResult.unmatched_capability_ids` and an
`outcome="unmatched"` log entry — never silently skipped. Derivation
continues for every other Capability.

**AC-BI-005**: every derived Policy gets exactly one Standard, and every
Standard gets exactly one Control, in this slice — satisfying "every derived
Policy having >=1 Standard" and "every derived Control belonging to exactly
one Standard." Standard/Control derivation runs once per DISTINCT Policy/
Standard produced in this run (the whole-run Policy registry in
`_derive_policies` already collapses a Capability that matches an
already-processed Policy onto the same node, so that Policy's Standard is
never re-derived).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from ps_service.domain_mapper.errors import DomainMapperGovernanceError
from ps_service.domain_mapper.graph_writer import persist_governance_graph
from ps_service.domain_mapper.identity import control_id, policy_id, standard_id
from ps_service.domain_mapper.models import (
    ControlDecision,
    ControlNode,
    GovernanceDerivationResult,
    PolicyAssignment,
    PolicyGovernedByEdge,
    PolicyNode,
    PolicySupportedByEdge,
    StandardDecision,
    StandardImplementedByEdge,
    StandardNode,
)
from ps_service.domain_mapper.prompts import (
    CONTROL_DERIVATION_SYSTEM_PROMPT,
    POLICY_DERIVATION_SYSTEM_PROMPT,
    STANDARD_DERIVATION_SYSTEM_PROMPT,
    parse_control_response,
    parse_policy_response,
    parse_standard_response,
)
from ps_service.llm_interface.completion import route_completion
from ps_service.llm_interface.models import ChatMessage
from ps_service.logging import LogEmitter, emit_log_entry

if TYPE_CHECKING:
    from ps_service.domain_mapper.falkordb_client import GraphHandle
    from ps_service.llm_interface.client import CompletionCaller

_COMPONENT = "domain_mapper"
_ACTION = "derive_governance_artifacts"

# The Standard/Control version minted the first (and, in this slice, only)
# time a distinct Policy/Standard is processed within one run — a plain
# fixed literal, not derived from any prior version, since there is no
# re-derivation-of-an-existing-Policy path yet (that is Company Merge's job,
# S4, operating on ALREADY-persisted Policy nodes across runs).
_FIRST_VERSION = "1"

_READ_SOURCE_TYPE_QUERY = "MATCH (r:RegulatoryInstrument {id: $id}) RETURN r.source_type"
_READ_CAPABILITIES_QUERY = "MATCH (c:Capability) RETURN c.id, c.name"


def derive_governance_artifacts(
    regulatory_instrument_id: str,
    *,
    baseline_graph: GraphHandle,
    model: str,
    call_completion: CompletionCaller | None = None,
    emitter: LogEmitter | None = None,
) -> GovernanceDerivationResult:
    """DeriveGovernanceArtifacts — `PLAN.md` S3's second internal-pipeline stage.

    1. `_require_internal_source` — AC-BI-008's defense-in-depth guard,
       before any Capability is read or any LLM call is made.
    2. `_read_capabilities` — reads every Capability back from
       `baseline_graph` (D1: fixed Cypher, no adapter object).
    3. `_derive_policies` — whole-run mint/match/unmatchable resolution per
       Capability (AC-BI-005/006/014).
    4. `_derive_standards` — one Standard per distinct Policy minted in step 3.
    5. `_derive_controls` — one Control per Standard produced in step 4.
    6. `graph_writer.persist_governance_graph` — plain writer, no validation
       of its own (mirrors `persist_obligation_and_capability_graph`'s own
       design note).
    7. Emits one `outcome="succeeded"` entry for the whole call.
    8. Returns `GovernanceDerivationResult`.

    Args:
        regulatory_instrument_id: The internal-source RegulatoryInstrument
            this run derives governance artifacts for.
        baseline_graph: The `{short}_baseline` graph handle to read
            Capabilities from and write the governance layer to.
        model: The LLM model id to route completions to.
        call_completion: Injectable completion transport; `None` uses the
            real `litellm` transport (`route_completion`'s own default).
        emitter: Optional explicit log emitter; otherwise the process default.

    Returns:
        A `GovernanceDerivationResult` naming every minted Policy/Standard/
        Control id and any Capability whose Policy could not be resolved.

    Raises:
        DomainMapperGovernanceError: `regulatory_instrument_id`'s own
            `source_type` is not `"internal"` (AC-BI-008), or a genuinely
            malformed Standard/Control LLM response could not be parsed at
            all (Policy-level malformed responses are isolated per
            Capability instead — see `_process_capability`).
    """
    _require_internal_source(baseline_graph, regulatory_instrument_id)
    capabilities = _read_capabilities(baseline_graph)

    policy_nodes, governed_by_edges, unmatched_capability_ids = _derive_policies(
        capabilities, model=model, call_completion=call_completion, emitter=emitter
    )
    standard_nodes, supported_by_edges = _derive_standards(
        policy_nodes, model=model, call_completion=call_completion, emitter=emitter
    )
    control_nodes, implemented_by_edges = _derive_controls(
        standard_nodes, model=model, call_completion=call_completion, emitter=emitter
    )

    persist_governance_graph(
        baseline_graph,
        policy_nodes,
        governed_by_edges,
        standard_nodes,
        supported_by_edges,
        control_nodes,
        implemented_by_edges,
    )

    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        entity_id=regulatory_instrument_id,
        outcome="succeeded",
        emitter=emitter,
    )

    return GovernanceDerivationResult(
        regulatory_instrument_id=regulatory_instrument_id,
        policy_node_ids=tuple(node.id for node in policy_nodes),
        standard_node_ids=tuple(node.id for node in standard_nodes),
        control_node_ids=tuple(node.id for node in control_nodes),
        unmatched_capability_ids=unmatched_capability_ids,
    )


def _require_internal_source(baseline_graph: GraphHandle, regulatory_instrument_id: str) -> None:
    """AC-BI-008's defense-in-depth guard.

    Reads `regulatory_instrument_id`'s own `source_type` property directly
    off `baseline_graph` and raises `DomainMapperGovernanceError` for
    anything other than `"internal"` (including a missing RegulatoryInstrument
    row entirely) — before any Capability is read or any LLM call is made.
    The orchestration never calls this function for an `external`
    RegulatoryInstrument in practice; this is a second, independent guard.
    """
    result = baseline_graph.query(_READ_SOURCE_TYPE_QUERY, params={"id": regulatory_instrument_id})
    rows = cast("list[list[object]]", result.result_set)
    source_type = rows[0][0] if rows else None
    if source_type != "internal":
        raise DomainMapperGovernanceError(
            f"DeriveGovernanceArtifacts refuses to run for RegulatoryInstrument "
            f"{regulatory_instrument_id!r} whose source_type is {source_type!r}, not 'internal'"
        )


@dataclass(frozen=True, slots=True)
class _CapabilityRow:
    """One Capability read back from the baseline graph."""

    capability_node_id: str
    name: str


def _read_capabilities(baseline_graph: GraphHandle) -> tuple[_CapabilityRow, ...]:
    """Read every Capability node's `(id, name)` off `baseline_graph`, in return order."""
    result = baseline_graph.query(_READ_CAPABILITIES_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    return tuple(
        _CapabilityRow(capability_node_id=cast("str", row[0]), name=cast("str", row[1]))
        for row in rows
    )


# --- Policy derivation (mirrors derivation.py's Obligation mint/match/unmatchable shape) ---


@dataclass
class _PolicyDerivationState:
    """Mutable whole-run accumulator threaded through `_process_capability`.

    `registry` is the single whole-run registry (`policy_id -> title`),
    seeded empty once for the entire run — deliberately Capability-
    independent (a Policy commonly governs several Capabilities at once,
    `ps-domain-concepts.md`), mirroring `_CapabilityDerivationState`'s own
    whole-run registry shape in `derivation.py`.
    """

    registry: dict[str, str] = field(default_factory=dict)
    policy_nodes: list[PolicyNode] = field(default_factory=list)
    governed_by_edges: list[PolicyGovernedByEdge] = field(default_factory=list)
    unmatched_capability_ids: list[str] = field(default_factory=list)


def _derive_policies(
    capabilities: tuple[_CapabilityRow, ...],
    *,
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> tuple[tuple[PolicyNode, ...], tuple[PolicyGovernedByEdge, ...], tuple[str, ...]]:
    """Whole-run Policy derivation — one mint/match/unmatchable decision per Capability.

    Iterates `capabilities` in the order they were read back from the
    baseline graph. A single `registry` spans the WHOLE run, so a Capability
    that matches (or independently mints the same title as) an
    already-processed Policy converges onto that same node — the code, not
    the LLM, guarantees this (mirrors `_resolve_obligation_id`'s own
    code-guarantees-uniqueness philosophy).

    Returns `(policy_nodes, governed_by_edges, unmatched_capability_ids)` —
    pure data, no graph writes. `policy_nodes` holds only NEWLY MINTED
    Policies (one entry per distinct id, first occurrence) — a Capability
    that matches an already-registered Policy still gets its own
    `GOVERNED_BY` edge, but does not add a second `PolicyNode`.
    """
    state = _PolicyDerivationState()
    for capability in capabilities:
        _process_capability(
            capability, state, model=model, call_completion=call_completion, emitter=emitter
        )
    return (
        tuple(state.policy_nodes),
        tuple(state.governed_by_edges),
        tuple(state.unmatched_capability_ids),
    )


def _process_capability(
    capability: _CapabilityRow,
    state: _PolicyDerivationState,
    *,
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> None:
    """One Capability's mint-or-match-or-unmatchable Policy decision.

    Mutates `state` in place. A malformed/unparseable LLM response
    (`DomainMapperGovernanceError` from `parse_policy_response`) and an
    explicit `unmatchable` outcome are unified under the same "surfaced, not
    silently skipped" mechanism (AC-BI-014) — mirrors `_process_requirement`'s
    isolation shape in `derivation.py` exactly. An `LlmProviderError` (a
    genuine infra failure calling the LLM at all) is never caught here — it
    propagates and aborts the whole run.
    """
    try:
        assignment = _derive_policy_for_capability(
            capability=capability,
            registry=state.registry,
            model=model,
            call_completion=call_completion,
            emitter=emitter,
        )
    except DomainMapperGovernanceError:
        _mark_capability_unmatched(capability.capability_node_id, state, emitter)
        return

    if assignment.policy_node_id is None or assignment.policy_title is None:
        _mark_capability_unmatched(capability.capability_node_id, state, emitter)
        return

    final_id, is_new_mint = _resolve_policy_id(
        proposed_title=assignment.policy_title, registry=state.registry
    )
    if is_new_mint:
        state.policy_nodes.append(
            PolicyNode(
                id=final_id,
                properties={
                    "title": assignment.policy_title,
                    "status": "draft",
                    "confidence": assignment.confidence,
                },
            )
        )
    state.governed_by_edges.append(
        PolicyGovernedByEdge(
            capability_node_id=capability.capability_node_id, policy_node_id=final_id
        )
    )


def _mark_capability_unmatched(
    capability_node_id: str, state: _PolicyDerivationState, emitter: LogEmitter | None
) -> None:
    """AC-BI-014 — unify the "explicit unmatchable" and "malformed response" failure modes.

    One mechanism: surfaced via the return value and an
    `outcome="unmatched"` log entry, never a silently-dropped Capability and
    never an uncaught exception.
    """
    state.unmatched_capability_ids.append(capability_node_id)
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        entity_id=capability_node_id,
        outcome="unmatched",
        emitter=emitter,
    )


def _resolve_policy_id(*, proposed_title: str, registry: dict[str, str]) -> tuple[str, bool]:
    """Resolve one proposed Policy title to its final id against the whole-run registry.

    Mutates `registry` in place on a mint; a reuse never mutates it.

    Returns `(final_policy_id, is_new_mint)`. Mirrors
    `_resolve_obligation_id`'s exact shape, minus the Role-scoping (Policy
    identity is title-only, `identity.policy_id`).
    """
    pid = policy_id(proposed_title)
    if pid in registry:
        return pid, False
    registry[pid] = proposed_title
    return pid, True


def _derive_policy_for_capability(
    *,
    capability: _CapabilityRow,
    registry: dict[str, str],
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> PolicyAssignment:
    """Call the LLM once for one Capability, returning its `PolicyAssignment`.

    A `DomainMapperGovernanceError` from a malformed/unparseable LLM
    response propagates unchanged — this function does not catch it;
    unifying it with the explicit `unmatchable` outcome is
    `_process_capability`'s job. An `LlmProviderError` from
    `route_completion` itself is likewise never caught here.
    """
    messages = _build_policy_messages(capability_name=capability.name, registry=registry)
    result = route_completion(
        messages, model=model, call_completion=call_completion, emitter=emitter
    )
    return parse_policy_response(result.text, capability.capability_node_id, registry)


def _build_policy_messages(*, capability_name: str, registry: dict[str, str]) -> list[ChatMessage]:
    """System prompt + one user message carrying the Capability's name and the registry.

    The Capability name and the WHOLE-run Policy registry built so far are
    clearly delimited from the system prompt's instructions (L2's
    untrusted-content rule).
    """
    registry_text = "\n".join(f"- {pid}: {title}" for pid, title in registry.items()) or "(empty)"
    user_content = (
        "<capability_name>\n"
        f"{capability_name}\n"
        "</capability_name>\n\n"
        f"Existing Policy registry:\n{registry_text}"
    )
    return [
        ChatMessage(role="system", content=POLICY_DERIVATION_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_content),
    ]


# --- Standard derivation (weak entity, one per distinct Policy) ------------


def _derive_standards(
    policy_nodes: tuple[PolicyNode, ...],
    *,
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> tuple[tuple[StandardNode, ...], tuple[PolicySupportedByEdge, ...]]:
    """One Standard per distinct Policy in `policy_nodes` (AC-BI-005's ">=1 Standard").

    `policy_nodes` already holds only newly-minted (first-occurrence)
    Policies, so this derives exactly one Standard per distinct Policy
    produced this run — never re-derived for a Capability that merely
    matched an already-processed Policy.
    """
    standard_nodes: list[StandardNode] = []
    supported_by_edges: list[PolicySupportedByEdge] = []
    for policy in policy_nodes:
        decision = _derive_standard_for_policy(
            policy, model=model, call_completion=call_completion, emitter=emitter
        )
        sid = standard_id(policy.id, _FIRST_VERSION)
        standard_nodes.append(_to_standard_node(sid, decision))
        supported_by_edges.append(
            PolicySupportedByEdge(policy_node_id=policy.id, standard_node_id=sid)
        )
    return tuple(standard_nodes), tuple(supported_by_edges)


def _derive_standard_for_policy(
    policy: PolicyNode,
    *,
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> StandardDecision:
    """Call the LLM once for one Policy, returning its `StandardDecision`.

    A `DomainMapperGovernanceError` from a malformed/unparseable response
    propagates unchanged and aborts the whole run — unlike Policy
    derivation, there is no per-item isolation here: a Standard is not
    optional the way a Policy match can be `unmatchable` (AC-BI-005 requires
    every Policy to have >=1 Standard).
    """
    title = cast("str", policy.properties["title"])
    messages = _build_standard_messages(policy_title=title)
    result = route_completion(
        messages, model=model, call_completion=call_completion, emitter=emitter
    )
    return parse_standard_response(result.text, policy.id)


def _build_standard_messages(*, policy_title: str) -> list[ChatMessage]:
    """System prompt + one user message carrying the Policy's title."""
    user_content = f"<policy_title>\n{policy_title}\n</policy_title>"
    return [
        ChatMessage(role="system", content=STANDARD_DERIVATION_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_content),
    ]


def _to_standard_node(standard_node_id: str, decision: StandardDecision) -> StandardNode:
    properties: dict[str, str | float] = {
        "title": decision.title,
        "implementation_status": "draft",
        "confidence": decision.confidence,
    }
    if decision.description is not None:
        properties["description"] = decision.description
    return StandardNode(id=standard_node_id, properties=properties)


# --- Control derivation (weak entity, one per Standard) ---------------------


def _derive_controls(
    standard_nodes: tuple[StandardNode, ...],
    *,
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> tuple[tuple[ControlNode, ...], tuple[StandardImplementedByEdge, ...]]:
    """One Control per Standard in `standard_nodes`.

    AC-BI-005's "every derived Control belonging to exactly one Standard."
    """
    control_nodes: list[ControlNode] = []
    implemented_by_edges: list[StandardImplementedByEdge] = []
    for standard in standard_nodes:
        decision = _derive_control_for_standard(
            standard, model=model, call_completion=call_completion, emitter=emitter
        )
        cid = control_id(standard.id, decision.type)
        control_nodes.append(_to_control_node(cid, decision))
        implemented_by_edges.append(
            StandardImplementedByEdge(standard_node_id=standard.id, control_node_id=cid)
        )
    return tuple(control_nodes), tuple(implemented_by_edges)


def _derive_control_for_standard(
    standard: StandardNode,
    *,
    model: str,
    call_completion: CompletionCaller | None,
    emitter: LogEmitter | None,
) -> ControlDecision:
    """Call the LLM once for one Standard, returning its `ControlDecision`.

    A `DomainMapperGovernanceError` from a malformed/unparseable response
    propagates unchanged and aborts the whole run — mirrors
    `_derive_standard_for_policy`'s own no-isolation reasoning.
    """
    title = cast("str", standard.properties["title"])
    messages = _build_control_messages(standard_title=title)
    result = route_completion(
        messages, model=model, call_completion=call_completion, emitter=emitter
    )
    return parse_control_response(result.text, standard.id)


def _build_control_messages(*, standard_title: str) -> list[ChatMessage]:
    """System prompt + one user message carrying the Standard's title."""
    user_content = f"<standard_title>\n{standard_title}\n</standard_title>"
    return [
        ChatMessage(role="system", content=CONTROL_DERIVATION_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_content),
    ]


def _to_control_node(control_node_id: str, decision: ControlDecision) -> ControlNode:
    """Build a `ControlNode`, never including any of the four operational fields (AC-BI-017)."""
    properties: dict[str, str | float] = {
        "type": decision.type,
        "title": decision.title,
        "implementation_status": "planned",
        "confidence": decision.confidence,
    }
    if decision.description is not None:
        properties["description"] = decision.description
    return ControlNode(id=control_node_id, properties=properties)
