"""FalkorDB persistence for `ps_service.company_merge`'s single-tenant graph.

Every write this module makes is a Graph Write Gateway group: each `persist_*` / `backfill_*`
function builds gateway primitives (`build_*_primitives`) and submits them as one group through
`graph_submission.submit_group`. The module issues no Cypher write. The few reads it makes
(existing ids, embedding state) go through `graph_submission.read_rows`, which records FalkorDB
reachability.

Decisions that depend on graph state are made before submission, from a batched read, because a
gateway upsert merges over an existing node and so cannot express them:

- Create-only (RegulatoryInstrument excepted): a Role, Requirement, Obligation, Capability,
  PracticeArea, RiskPath or draft governance node whose id the graph already holds is left out of
  the group, so an existing node's properties are never overwritten and nothing is logged for it.
  For an id repeated in one input the first occurrence is kept. Role/Requirement/Obligation ids
  can legitimately recur across merge runs with drifted content, exactly like a Capability's
  exact-key match.
- Set-if-null: an embedding backfill is submitted only for a node that exists and has no
  embedding; an already-embedded node is untouched. `persist_canonical_nodes` mints only for a
  `match_kind="new"` resolution; an exact or semantic match writes nothing onto the node it
  resolved to.
- Edge rewiring (`persist_rewired_edges`): one `UpsertEdge` per edge, identity
  `TYPE:source->target`. The rewrite rule is endpoint-agnostic: for both ends, an id with an entry
  in `canonical_id_by_incoming_id` (`incoming_id -> canonical_id`, built by `merge.py` from
  Capability dedup) is rewritten to the canonical id, otherwise it passes through. In practice
  only Capability, Policy and redirected Obligation endpoints are mapped, so `REQUIRES`'
  Capability target is the one rewritten for the core edges (a matched Capability is never
  minted, so its baseline-local id would dangle). An edge whose endpoint the graph does not hold
  is dropped before submission.
- Values generated at write time (`created_at`, embeddings, uuids) are fixed before submission
  from a `ValueSource` and carried in the group, so a replay reproduces them.

Obligation is a passthrough node: its identity is Role-scoped, so it is never deduped across
sources. Only Capability (and internal-SoP-derived Policy) is canonically deduped by Company
Merge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from pydantic import ValidationError

from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.generated_values import SYSTEM_VALUES
from ps_service.company_merge.graph_submission import edge_identity, read_rows, submit_group
from ps_service.company_merge.models import GovernanceDraftCounts
from ps_service.graph_gateway.models import NodeRef, UpsertEdge, UpsertNode

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.generated_values import ValueSource
    from ps_service.company_merge.graph_submission import GraphWriteTarget
    from ps_service.company_merge.models import (
        BareEdge,
        BaselineGraph,
        BaselineNode,
        CanonicalResolution,
        ProvenanceEdge,
    )
    from ps_service.graph_gateway.models import Primitive

__all__ = [
    "ANY_LABEL_EXISTENCE_READ",
    "backfill_canonical_embeddings",
    "build_obligation_primitives",
    "build_practice_area_and_risk_path_primitives",
    "classification_write_counts",
    "missing_classification_endpoints",
    "persist_canonical_nodes",
    "persist_governance_drafts",
    "persist_obligation_passthrough",
    "persist_practice_area_and_risk_path_passthrough",
    "persist_rewired_edges",
    "persist_role_and_requirement_passthrough",
    "read_existing_ids",
    "rewired_edge_endpoint_ids",
    "unresolved_classification_endpoint_ids",
    "validate_classification_edge_endpoints",
]

# Which of `$ids` exist as a node under any label. A `Merged<Kind>` marker carries the id of a
# node that no longer exists, so it never counts; one `AND NOT` per marker label.
ANY_LABEL_EXISTENCE_READ = (
    "UNWIND $ids AS id MATCH (n {id: id}) "
    "WHERE NOT n:MergedObligation AND NOT n:MergedRole AND NOT n:MergedCapability RETURN id"
)

_REGULATORY_INSTRUMENT_LABEL = "RegulatoryInstrument"
_ROLE_LABEL = "Role"
_REQUIREMENT_LABEL = "Requirement"
_OBLIGATION_LABEL = "Obligation"
_CAPABILITY_LABEL = "Capability"
_POLICY_LABEL = "Policy"
_STANDARD_LABEL = "Standard"
_CONTROL_LABEL = "Control"
_PRACTICE_AREA_LABEL = "PracticeArea"
_RISK_PATH_LABEL = "RiskPath"

# Fixed, code-owned, never string-interpolated: `$ids` always flows in via `params`
# (L2 Query Safety).
_EXISTING_OBLIGATIONS_QUERY = "MATCH (o:Obligation) WHERE o.id IN $ids RETURN o.id"
_DRAFT_STATUS = "draft"

# (source_label, target_label) per relationship_type: the Edge Catalog shape, mirrored from the
# Domain Mapper writer for the core and governance edges, plus the classification-layer edges.
# Every edge function below is endpoint-label-generic and driven by this one table, so adding an
# edge type means adding its entry here.
_EDGE_ENDPOINT_LABELS: dict[
    Literal[
        "HAS",
        "SATISFIED_BY",
        "REQUIRES",
        "GOVERNED_BY",
        "SUPPORTED_BY",
        "IMPLEMENTED_BY",
        "COVERS",
        "OWNS",
        "MITIGATED_BY",
        "VERIFIED_BY",
    ],
    tuple[str, str],
] = {
    "HAS": (_ROLE_LABEL, _OBLIGATION_LABEL),
    "SATISFIED_BY": (_REQUIREMENT_LABEL, _OBLIGATION_LABEL),
    "REQUIRES": (_OBLIGATION_LABEL, _CAPABILITY_LABEL),
    "GOVERNED_BY": (_CAPABILITY_LABEL, _POLICY_LABEL),
    "SUPPORTED_BY": (_POLICY_LABEL, _STANDARD_LABEL),
    "IMPLEMENTED_BY": (_STANDARD_LABEL, _CONTROL_LABEL),
    "COVERS": (_PRACTICE_AREA_LABEL, _CAPABILITY_LABEL),
    "OWNS": (_PRACTICE_AREA_LABEL, _POLICY_LABEL),
    "MITIGATED_BY": (_RISK_PATH_LABEL, _CAPABILITY_LABEL),
    "VERIFIED_BY": (_RISK_PATH_LABEL, _CONTROL_LABEL),
}


_EDGE_LABELS = tuple(sorted({label for pair in _EDGE_ENDPOINT_LABELS.values() for label in pair}))
"""Every node label an edge endpoint can carry, sorted: the endpoint reads run in this order."""


def read_existing_ids(graph: GraphHandle, label: str, node_ids: Iterable[str]) -> set[str]:
    """Return which of `node_ids` are `label` nodes of `graph`, from one batched read.

    `label` is always one of this module's fixed label literals, never baseline- or LLM-sourced, so
    interpolating it is safe; the ids travel as a query parameter.

    Args:
        graph: The FalkorDB graph to read.
        label: Node label to match; must be a code-owned literal, never external text.
        node_ids: Candidate node ids; duplicates are collapsed. Empty reads nothing.

    Returns:
        The subset of `node_ids` held by `graph` as `label` nodes; empty when none exists or when
        `node_ids` is empty.

    Side Effects:
        One read query on `graph`; FalkorDB reachability is recorded in the dependency health.

    Raises:
        CompanyMergePersistenceError: `FalkorDB query failed: <error>` if the read could not be
            made; nothing has been submitted by then.
    """
    ids = sorted(set(node_ids))
    if not ids:
        return set()
    rows = read_rows(graph, f"MATCH (n:{label}) WHERE n.id IN $ids RETURN n.id", {"ids": ids})
    return {str(row[0]) for row in rows}


def _unstorable(what: str) -> CompanyMergePersistenceError:
    return CompanyMergePersistenceError(
        f"{what} has a property the graph cannot store "
        "(a reserved key, or a value that is not finite or is out of range)"
    )


def _create_only_nodes(
    label: str, nodes: tuple[BaselineNode, ...], existing: set[str]
) -> tuple[UpsertNode, ...]:
    """Return one node primitive per id not in `existing`, keeping the first occurrence.

    An id repeated in one input keeps its first occurrence and the rest change nothing (the opposite
    of the Domain Mapper's last-wins fold). Raises `CompanyMergePersistenceError` when a node
    carries a property the gateway cannot store (a reserved key, a non-finite number).
    """
    created: dict[str, UpsertNode] = {}
    for node in nodes:
        if node.id in existing or node.id in created:
            continue
        try:
            created[node.id] = UpsertNode(label=label, id=node.id, properties=dict(node.properties))
        except ValidationError as exc:
            raise _unstorable(f"{label} {node.id!r}") from exc
    return tuple(created.values())


def build_role_and_requirement_primitives(
    existing_roles: set[str],
    existing_requirements: set[str],
    regulatory_instrument_id: str,
    regulatory_instrument_properties: dict[str, object],
    role_nodes: tuple[BaselineNode, ...],
    requirement_nodes: tuple[BaselineNode, ...],
    provenance_edges: tuple[ProvenanceEdge, ...],
) -> tuple[Primitive, ...]:
    """Build the group's mutations: instrument, Roles, Requirements, `DEFINES`, `EXPRESSES`.

    Pure: nothing is read or submitted. The instrument is refreshed on every run (an upsert that
    merges the properties over the node) with `id` left out of the property bag, because the bag is
    a whole copy of the native node whose `id` may differ in casing and must never rewrite the match
    key. A Role or Requirement is create-only (`_create_only_nodes`): one already in `existing_*` is
    left out of the group, so it keeps every property and nothing is logged for it. A provenance
    edge carries the identity `{type}:{instrument}->{target}` and its `source_ref`; one whose target
    is in neither the graph nor the group is dropped silently (a group edge with a missing endpoint
    would fail the whole group); a repeated edge folds to one primitive, the last `source_ref`
    winning.

    Args:
        existing_roles: Role ids the graph already holds.
        existing_requirements: Requirement ids the graph already holds.
        regulatory_instrument_id: Id of the RegulatoryInstrument the edges start from.
        regulatory_instrument_properties: Properties merged onto the instrument node; an `id` key is
            ignored.
        role_nodes: Baseline Role nodes.
        requirement_nodes: Baseline Requirement nodes.
        provenance_edges: `DEFINES` (to a Role) and `EXPRESSES` (to a Requirement) edges.

    Returns:
        The instrument primitive, then the new Role and Requirement primitives, then the edge
        primitives.

    Raises:
        CompanyMergePersistenceError: a node or edge carries a property the gateway cannot store;
            nothing has been submitted by then.
    """
    try:
        instrument = UpsertNode(
            label=_REGULATORY_INSTRUMENT_LABEL,
            id=regulatory_instrument_id,
            properties={k: v for k, v in regulatory_instrument_properties.items() if k != "id"},
        )
    except ValidationError as exc:
        raise _unstorable(f"{_REGULATORY_INSTRUMENT_LABEL} {regulatory_instrument_id!r}") from exc
    roles = _create_only_nodes(_ROLE_LABEL, role_nodes, existing_roles)
    requirements = _create_only_nodes(_REQUIREMENT_LABEL, requirement_nodes, existing_requirements)
    present = {
        _ROLE_LABEL: existing_roles | {node.id for node in roles},
        _REQUIREMENT_LABEL: existing_requirements | {node.id for node in requirements},
    }
    source = NodeRef(label=_REGULATORY_INSTRUMENT_LABEL, id=regulatory_instrument_id)
    edges: dict[str, UpsertEdge] = {}
    for edge in provenance_edges:
        target_label = _ROLE_LABEL if edge.relationship_type == "DEFINES" else _REQUIREMENT_LABEL
        if edge.target_id not in present[target_label]:
            continue
        identity = edge_identity(edge.relationship_type, regulatory_instrument_id, edge.target_id)
        try:
            edges[identity] = UpsertEdge(
                type=edge.relationship_type,
                identity=identity,
                source=source,
                target=NodeRef(label=target_label, id=edge.target_id),
                properties={"source_ref": edge.source_ref},
            )
        except ValidationError as exc:
            raise _unstorable(f"{edge.relationship_type} edge to {edge.target_id!r}") from exc
    return (instrument, *roles, *requirements, *edges.values())


def persist_role_and_requirement_passthrough(
    target: GraphWriteTarget,
    graph: GraphHandle,
    regulatory_instrument_id: str,
    regulatory_instrument_properties: dict[str, object],
    role_nodes: tuple[BaselineNode, ...],
    requirement_nodes: tuple[BaselineNode, ...],
    provenance_edges: tuple[ProvenanceEdge, ...],
) -> None:
    """Persist one regulation's RegulatoryInstrument/Role/Requirement nodes and provenance edges.

    One gateway group on `target`'s graph: the instrument (refreshed every run), then every Role and
    Requirement the graph does not hold yet, then the `DEFINES`/`EXPRESSES` edges. Role and
    Requirement ids can legitimately recur across merge runs with different properties, so an
    existing node keeps every property; that is decided here, before submission, from one batched
    read per label on `graph`, because a gateway upsert would merge over the existing node.
    Obligation is in the same passthrough category (`persist_obligation_passthrough`).

    Idempotent: re-running with identical input leaves the same graph and logs nothing new (the
    gateway drops the mutations that change nothing).

    Args:
        target: Submitter, graph name and audit event the group is written under.
        graph: Read handle on the graph `target` names, used for the existing-id reads.
        regulatory_instrument_id: Id of the RegulatoryInstrument.
        regulatory_instrument_properties: Properties merged onto the instrument node.
        role_nodes: Baseline Role nodes.
        requirement_nodes: Baseline Requirement nodes.
        provenance_edges: `DEFINES`/`EXPRESSES` edges from the instrument.

    Returns:
        None.

    Side Effects:
        Two read queries on `graph` and one gateway group submitted on `target`, after which the
        graph is caught up.

    Raises:
        CompanyMergePersistenceError: the existing-node read failed (`FalkorDB query failed:`), a
            node or edge carries a property the gateway cannot store, or the gateway did not take
            the group (`FalkorDB write failed:`).
    """
    existing_roles = read_existing_ids(
        graph,
        _ROLE_LABEL,
        (
            *(node.id for node in role_nodes),
            *(e.target_id for e in provenance_edges if e.relationship_type == "DEFINES"),
        ),
    )
    existing_requirements = read_existing_ids(
        graph,
        _REQUIREMENT_LABEL,
        (
            *(node.id for node in requirement_nodes),
            *(e.target_id for e in provenance_edges if e.relationship_type == "EXPRESSES"),
        ),
    )
    primitives = build_role_and_requirement_primitives(
        existing_roles,
        existing_requirements,
        regulatory_instrument_id,
        regulatory_instrument_properties,
        role_nodes,
        requirement_nodes,
        provenance_edges,
    )
    submit_group(target, primitives)


def count_new_obligations(single_tenant_graph: GraphHandle, obligation_ids: tuple[str, ...]) -> int:
    """Count how many of `obligation_ids` are not yet Obligation nodes in `single_tenant_graph`.

    One parameterized read (`$ids`, never interpolated), meant to run before
    `persist_obligation_passthrough` so the answer is the delta that write will make. Net-new is
    decided by this read, not by driver statistics: `GraphQueryResult` exposes only `result_set`.

    Args:
        single_tenant_graph: The graph to read.
        obligation_ids: Obligation ids to check; duplicates count once. An empty tuple issues no
            query.

    Returns:
        The number of distinct ids with no Obligation node in the graph; 0 for empty input.

    Side Effects:
        At most one read query; FalkorDB reachability is recorded in the dependency health.

    Raises:
        CompanyMergePersistenceError: `FalkorDB query failed: <error>` if the read could not be
            made.
    """
    unique_ids = sorted(set(obligation_ids))
    if not unique_ids:
        return 0
    rows = read_rows(single_tenant_graph, _EXISTING_OBLIGATIONS_QUERY, {"ids": unique_ids})
    return len(set(unique_ids) - {cast("str", row[0]) for row in rows})


def build_obligation_primitives(
    existing: set[str], obligation_nodes: tuple[BaselineNode, ...]
) -> tuple[UpsertNode, ...]:
    """Build one create-only `Obligation` primitive per id `existing` lacks, first occurrence kept.

    Pure: nothing is read or submitted. An Obligation the graph holds keeps every property and is
    left out of the group.

    Args:
        existing: Obligation ids the graph already holds.
        obligation_nodes: Baseline Obligation nodes.

    Returns:
        The primitives for the new Obligations, in input order; empty when all exist.

    Raises:
        CompanyMergePersistenceError: a node carries a property the gateway cannot store.
    """
    return _create_only_nodes(_OBLIGATION_LABEL, obligation_nodes, existing)


def persist_obligation_passthrough(
    target: GraphWriteTarget,
    single_tenant_graph: GraphHandle,
    obligation_nodes: tuple[BaselineNode, ...],
) -> None:
    """Persist one regulation's Obligation nodes as one gateway group on `target`'s graph.

    Create-only, like Role/Requirement: an Obligation is Role-scoped (a weak entity of exactly one
    Role) and never deduped across sources. Re-merging a regulation against a baseline whose content
    has drifted makes the same Obligation id recur with different properties, so an existing
    Obligation keeps every property. That is decided here, before submission, from one batched read
    on `single_tenant_graph` (a gateway upsert would merge over the existing node): an Obligation
    the graph holds is left out of the group and nothing is logged for it. For an id repeated in the
    input the first occurrence wins.

    Called by `merge.py` after `persist_role_and_requirement_passthrough` and before
    `persist_rewired_edges`, so the `HAS`/`SATISFIED_BY`/`REQUIRES` edge writes find these nodes.

    Args:
        target: Submitter, graph name and audit event the group is written under.
        single_tenant_graph: Read handle on the graph `target` names.
        obligation_nodes: Baseline Obligation nodes.

    Returns:
        None.

    Side Effects:
        One read query on `single_tenant_graph`; one gateway group submitted on `target` unless
        every Obligation already exists.

    Raises:
        CompanyMergePersistenceError: the existing-node read failed (`FalkorDB query failed:`), a
            node carries a property the gateway cannot store, or the gateway did not take the group
            (`FalkorDB write failed:`).
    """
    existing = read_existing_ids(
        single_tenant_graph, _OBLIGATION_LABEL, (node.id for node in obligation_nodes)
    )
    submit_group(target, build_obligation_primitives(existing, obligation_nodes))


def build_draft_governance_primitives(
    existing: dict[str, set[str]],
    policy_nodes: tuple[BaselineNode, ...],
    standard_nodes: tuple[BaselineNode, ...],
    control_nodes: tuple[BaselineNode, ...],
    *,
    owner: tuple[str, str],
    policy_embeddings: Mapping[str, tuple[float, ...]] | None,
    created_at: str,
) -> tuple[tuple[UpsertNode, ...], int]:
    """Build the create-only draft nodes of a governance tree and count authored non-draft statuses.

    Pure. Returns one `UpsertNode` per Policy, Standard and Control id that `existing` does not
    hold, in Policy, Standard, Control order, the first occurrence of a repeated id kept
    (create-only). `status` is forced to `"draft"` after the artifact's own properties are copied,
    so an artifact-authored `approved` never wins; `version` defaults to the lifecycle's string
    `"1"`. A Policy also carries the owner pair and `created_at`; its artifact-supplied embedding
    travels in the primitive's `embedding` field. The override count describes the input nodes,
    whether or not the graph already holds them.

    Args:
        existing: Label ("Policy", "Standard", "Control") to the ids the graph already holds.
        policy_nodes: Baseline Policy nodes.
        standard_nodes: Baseline Standard nodes.
        control_nodes: Baseline Control nodes.
        owner: The importing caller's verified `(subject, issuer)` pair, stored on each Policy as
            `owner_subject` / `owner_issuer`.
        policy_embeddings: Policy id to artifact-supplied vector, or None for none; a Policy
            without an entry gets no embedding.
        created_at: ISO-8601 timestamp stamped on every Policy; the caller draws it once.

    Returns:
        A pair: the primitives for the new nodes, and the number of input nodes whose authored
        `status` was neither absent nor `"draft"`.

    Raises:
        CompanyMergePersistenceError: a node carries a property the gateway cannot store.
    """
    owner_subject, owner_issuer = owner
    status_overridden = 0
    created: dict[tuple[str, str], UpsertNode] = {}

    def add(label: str, node_id: str, properties: dict[str, object], *, embedding: object) -> None:
        if node_id in existing.get(label, ()) or (label, node_id) in created:
            return
        try:
            created[(label, node_id)] = UpsertNode(
                label=label,
                id=node_id,
                properties=properties,
                embedding=cast("tuple[float, ...] | None", embedding),
            )
        except ValidationError as exc:
            raise _unstorable(f"{label} {node_id!r}") from exc

    for policy in policy_nodes:
        properties: dict[str, object] = dict(policy.properties)
        status_overridden += _status_override_count(properties)
        properties["status"] = _DRAFT_STATUS
        properties.setdefault("version", "1")
        properties["owner_subject"] = owner_subject
        properties["owner_issuer"] = owner_issuer
        properties["created_at"] = created_at
        add(
            _POLICY_LABEL,
            policy.id,
            properties,
            embedding=(policy_embeddings or {}).get(policy.id),
        )

    for label, nodes in ((_STANDARD_LABEL, standard_nodes), (_CONTROL_LABEL, control_nodes)):
        for node in nodes:
            properties = dict(node.properties)
            status_overridden += _status_override_count(properties)
            properties["status"] = _DRAFT_STATUS
            add(label, node.id, properties, embedding=None)

    return tuple(created.values()), status_overridden


def persist_governance_drafts(
    target: GraphWriteTarget,
    single_tenant_graph: GraphHandle,
    policy_nodes: tuple[BaselineNode, ...],
    standard_nodes: tuple[BaselineNode, ...],
    control_nodes: tuple[BaselineNode, ...],
    *,
    owner: tuple[str, str],
    policy_embeddings: Mapping[str, tuple[float, ...]] | None,
    values: ValueSource = SYSTEM_VALUES,
) -> GovernanceDraftCounts:
    """Persist an internal instrument's Policy/Standard/Control tree as `draft`, as one group.

    The write behind `governance_import.import_draft_governance`, used by live internal ingest. An
    imported tree has not been through the customer's governance, so it arrives `draft`, owned by
    the importing caller (`owner` is their verified `(sub, iss)` pair, stored as
    `owner_subject`/`owner_issuer` exactly as `policy_lifecycle.create_policy_draft` stores them),
    and can only become `approved` through the normal propose/approve flow.

    Business Rules:
        No dedup of any kind: each node is keyed on its own content-hashed id. A draft must never
        converge onto an existing canonical Policy, since the draft Standards/Controls would then
        hang under a possibly-approved parent and the whole-tree-moves-together rule would break.
        Near-duplicate detection is the near-miss review flow's job.

    Create-only is decided here, before submission, from one batched existing-id read per label (a
    gateway upsert would merge over the existing node), so a node that already exists, including one
    a user has since approved, keeps its status, owner and content, and nothing is logged for it.
    `created_at` is drawn from `values` once, before the group is built, so the group carries it and
    a replay reproduces it.

    Args:
        target: Submitter, graph name and audit event the group is written under.
        single_tenant_graph: Read handle on the graph `target` names.
        policy_nodes: Baseline Policy nodes.
        standard_nodes: Baseline Standard nodes.
        control_nodes: Baseline Control nodes.
        owner: The importing caller's verified `(subject, issuer)` pair.
        policy_embeddings: Artifact-supplied Policy vectors by id, kept so a later cleanup flow
            still has them, or None; a Policy without one gets no embedding.
        values: Source of the `created_at` instant; defaults to the system clock.

    Returns:
        `GovernanceDraftCounts` describing the input (not the nodes actually created).

    Side Effects:
        Three read queries on `single_tenant_graph`; one gateway group submitted on `target` unless
        every node already exists.

    Raises:
        CompanyMergePersistenceError: an existing-id read failed (`FalkorDB query failed:`), a node
            carries a property the gateway cannot store, or the gateway did not take the group
            (`FalkorDB write failed:`).
    """
    created_at = values.now().isoformat()
    existing = {
        label: read_existing_ids(single_tenant_graph, label, (node.id for node in nodes))
        for label, nodes in (
            (_POLICY_LABEL, policy_nodes),
            (_STANDARD_LABEL, standard_nodes),
            (_CONTROL_LABEL, control_nodes),
        )
    }
    primitives, status_overridden = build_draft_governance_primitives(
        existing,
        policy_nodes,
        standard_nodes,
        control_nodes,
        owner=owner,
        policy_embeddings=policy_embeddings,
        created_at=created_at,
    )
    submit_group(target, primitives)
    return GovernanceDraftCounts(
        policies=len(policy_nodes),
        standards=len(standard_nodes),
        controls=len(control_nodes),
        status_overridden=status_overridden,
    )


def _status_override_count(properties: dict[str, object]) -> int:
    """Return 1 when the artifact authored a `status` other than `draft`, else 0."""
    authored = properties.get("status")
    return 1 if authored is not None and authored != _DRAFT_STATUS else 0


def build_practice_area_and_risk_path_primitives(
    existing_practice_areas: set[str],
    existing_risk_paths: set[str],
    practice_area_nodes: tuple[BaselineNode, ...],
    risk_path_nodes: tuple[BaselineNode, ...],
) -> tuple[UpsertNode, ...]:
    """Build the create-only PracticeArea primitives, then the create-only RiskPath primitives.

    Pure: nothing is read or submitted. An id the graph holds is left out; for a repeated id the
    first occurrence wins; every PracticeArea precedes every RiskPath.

    Args:
        existing_practice_areas: PracticeArea ids the graph already holds.
        existing_risk_paths: RiskPath ids the graph already holds.
        practice_area_nodes: Baseline PracticeArea nodes.
        risk_path_nodes: Baseline RiskPath nodes.

    Returns:
        The primitives for the new nodes; empty when there is nothing new.

    Raises:
        CompanyMergePersistenceError: a node carries a property the gateway cannot store.
    """
    return (
        *_create_only_nodes(_PRACTICE_AREA_LABEL, practice_area_nodes, existing_practice_areas),
        *_create_only_nodes(_RISK_PATH_LABEL, risk_path_nodes, existing_risk_paths),
    )


def persist_practice_area_and_risk_path_passthrough(
    target: GraphWriteTarget,
    single_tenant_graph: GraphHandle,
    practice_area_nodes: tuple[BaselineNode, ...],
    risk_path_nodes: tuple[BaselineNode, ...],
) -> None:
    """Persist PracticeArea/RiskPath nodes with exact-identity passthrough.

    PracticeArea/RiskPath ids are content-hashed from `name` alone and are expected to recur across
    separate baselines (see `ps-domain-concepts.md`'s identity notes), so they are create-only, like
    Capability/Policy (`persist_canonical_nodes`) and Role/Requirement/Obligation: an existing
    node's properties are never overwritten. That is decided here, before submission, from one
    batched existing-node read per label on `single_tenant_graph` (a gateway upsert would merge over
    the existing node): a node the graph holds is left out of the group and nothing is logged for
    it. Every PracticeArea precedes every RiskPath in the one group; for an id repeated in the input
    the first occurrence wins.

    No dedup call precedes this: identity convergence is structural (identical `name` gives an
    identical hash and so an identical node id), so there is nothing to compute.

    Args:
        target: Submitter, graph name and audit event the group is written under.
        single_tenant_graph: Read handle on the graph `target` names.
        practice_area_nodes: Baseline PracticeArea nodes.
        risk_path_nodes: Baseline RiskPath nodes.

    Returns:
        None.

    Side Effects:
        Reads existing ids per label and submits one gateway group on `target` unless nothing is
        new. A baseline with no PracticeArea and no RiskPath nodes issues no read and submits no
        group.

    Raises:
        CompanyMergePersistenceError: an existing-node read failed (`FalkorDB query failed:`), a
            node carries a property the gateway cannot store, or the gateway did not take the group
            (`FalkorDB write failed:`).
    """
    existing_practice_areas = read_existing_ids(
        single_tenant_graph, _PRACTICE_AREA_LABEL, (node.id for node in practice_area_nodes)
    )
    existing_risk_paths = read_existing_ids(
        single_tenant_graph, _RISK_PATH_LABEL, (node.id for node in risk_path_nodes)
    )
    submit_group(
        target,
        build_practice_area_and_risk_path_primitives(
            existing_practice_areas, existing_risk_paths, practice_area_nodes, risk_path_nodes
        ),
    )


def unresolved_classification_endpoint_ids(
    classification_edges: tuple[BareEdge, ...], canonical_id_by_incoming_id: dict[str, str]
) -> set[str]:
    """Return the endpoint ids of `classification_edges` that have no canonical-map entry (pure).

    These are the endpoints whose existence must be known before a classification edge is written:
    PracticeArea/RiskPath sources and `VERIFIED_BY`'s Control target.

    Args:
        classification_edges: `COVERS`/`OWNS`/`MITIGATED_BY`/`VERIFIED_BY` edges.
        canonical_id_by_incoming_id: `incoming_id -> canonical_id` map from dedup.

    Returns:
        Every source or target id of the edges that is not a key of `canonical_id_by_incoming_id`;
        empty for no edges.
    """
    return {
        endpoint_id
        for edge in classification_edges
        for endpoint_id in (edge.source_id, edge.target_id)
        if endpoint_id not in canonical_id_by_incoming_id
    }


def missing_classification_endpoints(
    known_ids: set[str],
    classification_edges: tuple[BareEdge, ...],
    canonical_id_by_incoming_id: dict[str, str],
) -> set[str]:
    """Return the unresolved endpoint ids of `classification_edges` that `known_ids` does not hold.

    Pure. `known_ids` is every node id the graph holds, or will hold once the group is applied. The
    live check (`validate_classification_edge_endpoints`) and the in-memory one Restore makes over
    its primitives so far share this decision.

    Args:
        known_ids: Node ids that exist or will exist.
        classification_edges: `COVERS`/`OWNS`/`MITIGATED_BY`/`VERIFIED_BY` edges.
        canonical_id_by_incoming_id: `incoming_id -> canonical_id` map from dedup.

    Returns:
        The endpoint ids with neither a canonical-map entry nor membership in `known_ids`; empty
        when every endpoint is resolved.
    """
    return (
        unresolved_classification_endpoint_ids(classification_edges, canonical_id_by_incoming_id)
        - known_ids
    )


def validate_classification_edge_endpoints(
    single_tenant_graph: GraphHandle,
    classification_edges: tuple[BareEdge, ...],
    canonical_id_by_incoming_id: dict[str, str],
) -> None:
    """Require every classification edge endpoint to exist, before any such edge is written.

    Every `COVERS`/`OWNS`/`MITIGATED_BY`/`VERIFIED_BY` endpoint must resolve. Capability and Policy
    targets are covered by `canonical_id_by_incoming_id` (built by Capability/Policy dedup).
    PracticeArea/RiskPath sources and `VERIFIED_BY`'s Control target are passthrough nodes with no
    entry in that map by design; by the time this runs, `merge.py` and the restore flow have already
    persisted every passthrough node of the baseline (all node kinds are written before any edge,
    the ordering `persist_role_and_requirement_passthrough` relies on too). So "absent from both the
    baseline and the single-tenant graph" reduces to one batched existence check against
    `single_tenant_graph`.

    This guarantee is scoped to these four edge types. The six core edge types are not checked here:
    a passthrough endpoint missing from the graph writes nothing for them rather than raising.

    Args:
        single_tenant_graph: The graph to check endpoints against.
        classification_edges: The edges about to be written.
        canonical_id_by_incoming_id: `incoming_id -> canonical_id` map from dedup; endpoints that
            are keys need no existence check.

    Returns:
        None when every endpoint resolves.

    Side Effects:
        At most one read query; none when every endpoint is in the map.

    Raises:
        CompanyMergePersistenceError: an endpoint absent from the map is also absent from the graph
            (`classification edge endpoint(s) [...] not found in the single-tenant graph`), or the
            read failed (`FalkorDB query failed:`).
    """
    unresolved_ids = unresolved_classification_endpoint_ids(
        classification_edges, canonical_id_by_incoming_id
    )
    if not unresolved_ids:
        return
    rows = read_rows(
        single_tenant_graph,
        ANY_LABEL_EXISTENCE_READ,
        {"ids": sorted(unresolved_ids)},
    )
    existing_ids = {cast("str", row[0]) for row in rows}
    missing = missing_classification_endpoints(
        existing_ids, classification_edges, canonical_id_by_incoming_id
    )
    if missing:
        raise CompanyMergePersistenceError(
            f"classification edge endpoint(s) {sorted(missing)!r} not found in "
            "the single-tenant graph"
        )


def classification_write_counts(graph: BaselineGraph) -> dict[str, int]:
    """Count the PracticeArea/RiskPath nodes and classification edges of `graph`.

    Returns the counts of PracticeArea and RiskPath nodes and of the four classification edge types
    that a merge or restore of `graph` writes, for the caller's succeeded audit entry. Public so the
    live merge and the offline restore compute the same six counts through a normal import.

    Args:
        graph: The baseline graph being merged or restored.

    Returns:
        A dict with the keys `practice_area_count`, `risk_path_count`, `covers_count`, `owns_count`,
        `mitigated_by_count` and `verified_by_count`; each is 0 when the baseline has none.
    """
    edge_counts = {
        relationship_type: sum(
            1 for edge in graph.classification_edges if edge.relationship_type == relationship_type
        )
        for relationship_type in ("COVERS", "OWNS", "MITIGATED_BY", "VERIFIED_BY")
    }
    return {
        "practice_area_count": len(graph.practice_area_nodes),
        "risk_path_count": len(graph.risk_path_nodes),
        "covers_count": edge_counts["COVERS"],
        "owns_count": edge_counts["OWNS"],
        "mitigated_by_count": edge_counts["MITIGATED_BY"],
        "verified_by_count": edge_counts["VERIFIED_BY"],
    }


def build_canonical_node_primitives(
    existing_ids: set[str],
    incoming_nodes: tuple[BaselineNode, ...],
    resolutions: tuple[CanonicalResolution, ...],
    *,
    kind: Literal["Capability", "Policy"],
    values: ValueSource,
) -> tuple[UpsertNode, ...]:
    """Build one create-only `kind` node per `match_kind="new"` resolution the graph does not hold.

    Pure apart from `values`: nothing is read or submitted. `created_at` is drawn from `values` once
    per new resolution, in resolution order, here, so the primitive carries a literal rather than a
    value drawn at apply time. The embedding travels in the primitive's `embedding` field, present
    only when the resolution has one; a mint that never triggered a comparison (an empty working
    index) carries none and is backfilled later. A canonical id in `existing_ids` is left out of the
    group, so the node keeps every property and nothing is logged for it.

    `incoming_nodes` and `resolutions` are 1:1 by construction (`dedup.dedupe_canonical_nodes`
    builds exactly one `CanonicalResolution` per incoming node, `incoming_id` set to that node's own
    `id`), so there is no validate-then-write pass.

    Args:
        existing_ids: Canonical ids of `kind` the graph already holds.
        incoming_nodes: The baseline nodes the resolutions were made for.
        resolutions: One resolution per incoming node; only `match_kind="new"` ones mint.
        kind: Node label to mint.
        values: Source of the `created_at` instants.

    Returns:
        The primitives for the newly minted nodes, in resolution order; empty when none is new.

    Raises:
        CompanyMergePersistenceError: a node carries a property the gateway cannot store.
    """
    nodes_by_id = {node.id: node for node in incoming_nodes}
    minted: dict[str, UpsertNode] = {}
    for resolution in resolutions:
        if resolution.match_kind != "new":
            continue
        node = nodes_by_id[resolution.incoming_id]
        properties: dict[str, object] = dict(node.properties)
        properties["created_at"] = values.now().isoformat()
        if resolution.canonical_id in existing_ids or resolution.canonical_id in minted:
            continue
        try:
            minted[resolution.canonical_id] = UpsertNode(
                label=kind,
                id=resolution.canonical_id,
                properties=properties,
                embedding=resolution.embedding,
            )
        except ValidationError as exc:
            raise _unstorable(f"{kind} {resolution.canonical_id!r}") from exc
    return tuple(minted.values())


def persist_canonical_nodes(
    target: GraphWriteTarget,
    single_tenant_graph: GraphHandle,
    incoming_nodes: tuple[BaselineNode, ...],
    resolutions: tuple[CanonicalResolution, ...],
    *,
    kind: Literal["Capability", "Policy"],
    values: ValueSource = SYSTEM_VALUES,
) -> None:
    """Mint every `match_kind="new"` `CanonicalResolution` as a `kind` node, as one group.

    Writes into the graph `target` names. A `match_kind="exact"`/`"semantic"` resolution gets no
    write at all: it already resolved onto an existing canonical node, and this function never
    touches one. Obligation is not canonically deduped (`persist_obligation_passthrough`), so `kind`
    is Capability or Policy.

    Create-only is the invariant that keeps an existing canonical node's properties intact. It is
    decided here, before submission, from one batched existing-id read on `single_tenant_graph` (a
    gateway upsert would merge over the existing node); see `build_canonical_node_primitives` for
    what a new node carries. The properties are the underlying `BaselineNode.properties` plus
    `created_at`, set once at mint and never touched again; the near-miss review workflow's
    "earlier-created wins" winner selection depends on it.

    Args:
        target: Submitter, graph name and audit event the group is written under.
        single_tenant_graph: Read handle on the graph `target` names.
        incoming_nodes: The baseline nodes the resolutions were made for.
        resolutions: One resolution per incoming node.
        kind: Node label to mint, "Capability" or "Policy".
        values: Source of the `created_at` instants; defaults to the system clock.

    Returns:
        None.

    Side Effects:
        One read query on `single_tenant_graph`; one gateway group submitted on `target` unless
        nothing is new.

    Raises:
        CompanyMergePersistenceError: the existing-node read failed (`FalkorDB query failed:`), a
            node carries a property the gateway cannot store, or the gateway did not take the group
            (`FalkorDB write failed:`).
    """
    existing = read_existing_ids(
        single_tenant_graph,
        kind,
        (r.canonical_id for r in resolutions if r.match_kind == "new"),
    )
    submit_group(
        target,
        build_canonical_node_primitives(
            existing, incoming_nodes, resolutions, kind=kind, values=values
        ),
    )


def _read_embedding_state(
    graph: GraphHandle, label: str, node_ids: Iterable[str]
) -> dict[str, bool]:
    """Map each of `node_ids` that is a `label` node of `graph` to whether it has no embedding yet.

    One batched read with the ids as a parameter; the vector itself is never returned. An id the
    graph does not hold is absent from the result. `label` must be a code-owned literal. Raises
    `CompanyMergePersistenceError` (`FalkorDB query failed:`) if the read fails.
    """
    ids = sorted(set(node_ids))
    if not ids:
        return {}
    rows = read_rows(
        graph,
        f"MATCH (n:{label}) WHERE n.id IN $ids RETURN n.id, n.embedding IS NULL",
        {"ids": ids},
    )
    return {str(row[0]): bool(row[1]) for row in rows}


def build_embedding_backfill_primitives(
    embedding_is_null: dict[str, bool],
    embeddings: dict[str, tuple[float, ...]],
    *,
    label: str,
) -> tuple[UpsertNode, ...]:
    """Build one `UpsertNode(label, id, embedding=...)` per id that exists and has no embedding yet.

    Pure. An id missing from `embedding_is_null` is absent from the graph (a gateway upsert would
    create it, which a backfill must never do), and an id mapped to `False` already has an embedding
    (a gateway upsert would replace it). Neither is submitted, so nothing is logged for them. Input
    order is kept. `embedding` is a reserved property key, so it travels in the primitive's
    `embedding` field and no other property is named: the node keeps everything else it has.

    Args:
        embedding_is_null: Id to whether that existing node has no embedding.
        embeddings: Id to the vector to set.
        label: Node label of the ids.

    Returns:
        The primitives to submit; empty when nothing needs a backfill.

    Raises:
        CompanyMergePersistenceError: an embedding the gateway cannot store (non-finite value).
    """
    primitives: list[UpsertNode] = []
    for node_id, embedding in embeddings.items():
        if not embedding_is_null.get(node_id, False):
            continue
        try:
            primitives.append(UpsertNode(label=label, id=node_id, embedding=embedding))
        except ValidationError as exc:
            raise _unstorable(f"{label} {node_id!r} embedding") from exc
    return tuple(primitives)


def backfill_canonical_embeddings(
    target: GraphWriteTarget,
    single_tenant_graph: GraphHandle,
    *,
    kind: Literal["Capability", "Policy"],
    embeddings: dict[str, tuple[float, ...]],
    checkpoint_requested: bool = False,
) -> None:
    """Set each `embeddings` entry on an already-existing `kind` canonical node that has none.

    Distinct from `persist_canonical_nodes`' create-only mint, and never touching a node's other
    properties. Set-if-null is decided here, before submission: one batched read of which ids exist
    and have no embedding (`_read_embedding_state`), then one group of
    `build_embedding_backfill_primitives`. A node that already has an embedding, or is absent, is
    left out; a re-run therefore submits and logs nothing. Every id is expected to exist already
    (`dedup.dedupe_canonical_nodes` only reports pre-existing nodes); `merge.py` calls this after
    the mint and the edge writes.

    Args:
        target: Submitter, graph name and audit event the group is written under.
        single_tenant_graph: Read handle on the graph `target` names.
        kind: Node label of the ids, "Capability" or "Policy".
        embeddings: Node id to the vector to set. Empty submits nothing.
        checkpoint_requested: Asks the gateway to record the graph's digest after this group; the
            merge sets it when this backfill is the last group it writes to `policy_system`.

    Returns:
        None.

    Side Effects:
        One read query on `single_tenant_graph`; one gateway group submitted on `target` when some
        node needs an embedding.

    Raises:
        CompanyMergePersistenceError: the state read failed (`FalkorDB query failed:`), an embedding
            cannot be stored, or the gateway did not take the group (`FalkorDB write failed:`).
    """
    state = _read_embedding_state(single_tenant_graph, kind, embeddings)
    submit_group(
        target,
        build_embedding_backfill_primitives(state, embeddings, label=kind),
        checkpoint_requested=checkpoint_requested,
    )


def backfill_incoming_capability_embeddings(
    target: GraphWriteTarget,
    baseline_graph: GraphHandle,
    *,
    embeddings: dict[str, tuple[float, ...]],
) -> None:
    """Set each `embeddings` entry on its own incoming `{short}_baseline` Capability node, if null.

    The incoming-side twin of `backfill_canonical_embeddings`: the same set-if-null contract and the
    same "only `embedding` is ever touched" contract, but `target` names the regulation's own
    `{short}_baseline` graph, `baseline_graph` is its read handle, and the kind is always Capability
    (there is no Policy-side incoming embedding to cache, see
    `DedupResult.incoming_embedding_backfills`).

    Every `(incoming_id, embedding)` pair comes from `DedupResult.incoming_embedding_backfills`. A
    later `graph_reader.read_baseline_graph` of the same, unchanged baseline reads the cached value
    back onto `BaselineNode.embedding`, letting `dedup.find_best_semantic_match` skip the
    `route_embedding` call for it.

    Args:
        target: Submitter, graph name and audit event for the baseline graph.
        baseline_graph: Read handle on the baseline graph `target` names.
        embeddings: Incoming Capability id to the vector to cache.

    Returns:
        None.

    Side Effects:
        One read query on `baseline_graph`; one gateway group submitted on `target` when some node
        needs an embedding. The group requests the graph digest checkpoint, as it is the last group
        the merge writes to the baseline graph.

    Raises:
        CompanyMergePersistenceError: the state read failed (`FalkorDB query failed:`), an embedding
            cannot be stored, or the gateway did not take the group (`FalkorDB write failed:`).
    """
    state = _read_embedding_state(baseline_graph, _CAPABILITY_LABEL, embeddings)
    submit_group(
        target,
        build_embedding_backfill_primitives(state, embeddings, label=_CAPABILITY_LABEL),
        checkpoint_requested=True,  # last group the merge writes to the baseline graph
    )


def _dedupe_eligible_endpoint_ids(edge: BareEdge) -> tuple[str, ...]:
    """Return `edge`'s endpoints whose label (per `_EDGE_ENDPOINT_LABELS`) is canonically deduped.

    Capability and Policy endpoints are the only ones that went through
    `dedup.dedupe_canonical_nodes`, so only they are guaranteed an entry in a correctly built
    `canonical_id_by_incoming_id`. `HAS` and `SATISFIED_BY` yield nothing (all passthrough);
    `REQUIRES` yields only its Capability target; `GOVERNED_BY` yields both ends;
    `SUPPORTED_BY`/`IMPLEMENTED_BY` yield nothing (Standard/Control are passthrough weak entities).
    The tuple is source-before-target when both are eligible; callers should not rely on more.
    """
    source_label, target_label = _EDGE_ENDPOINT_LABELS[edge.relationship_type]
    dedupe_eligible_labels = (_CAPABILITY_LABEL, _POLICY_LABEL)
    ids: list[str] = []
    if source_label in dedupe_eligible_labels:
        ids.append(edge.source_id)
    if target_label in dedupe_eligible_labels:
        ids.append(edge.target_id)
    return tuple(ids)


def _validate_rewired_edge_endpoints(
    bare_edges: tuple[BareEdge, ...], canonical_id_by_incoming_id: dict[str, str]
) -> None:
    """Require every dedupe-eligible endpoint of `bare_edges` to have a canonical-id mapping.

    A whole-collection pass that runs before any read or write, not interleaved per edge. Every
    eligible endpoint went through `dedup.dedupe_canonical_nodes` and so has an entry in a correctly
    built map; an absent entry means the caller-supplied mapping is incomplete, which is a hard
    pre-write error rather than a pass-through. Raises `CompanyMergePersistenceError` on the first
    violation found.
    """
    for edge in bare_edges:
        for incoming_id in _dedupe_eligible_endpoint_ids(edge):
            if incoming_id not in canonical_id_by_incoming_id:
                raise CompanyMergePersistenceError(
                    f"{edge.relationship_type} edge references incoming id "
                    f"{incoming_id!r}, which has no CanonicalResolution entry in "
                    "canonical_id_by_incoming_id"
                )


def build_rewired_edge_primitives(
    existing: Mapping[str, set[str]],
    bare_edges: tuple[BareEdge, ...],
    canonical_id_by_incoming_id: dict[str, str],
) -> tuple[UpsertEdge, ...]:
    """Build one `UpsertEdge` per distinct edge whose two endpoints, after the rewrite, both exist.

    Pure: nothing is read or submitted. Each endpoint goes through `canonical_id_by_incoming_id` (a
    missing entry passes through). An edge with a missing endpoint is dropped silently, because a
    group edge with a missing endpoint would fail the whole group. The identity is
    `{type}:{source}->{target}` (`edge_identity`): a repeated edge, or two that rewrite to the same
    ends, fold to one primitive (first position kept). The edge carries no properties.

    Args:
        existing: Node label to the ids the graph holds; must have a key for every label an edge
            endpoint can carry (as `rewired_edge_endpoint_ids` produces).
        bare_edges: Edges to write, of the ten Edge Catalog types.
        canonical_id_by_incoming_id: `incoming_id -> canonical_id` map from dedup.

    Returns:
        The edge primitives in first-seen order; empty when no edge has both endpoints present.
    """
    edges: dict[str, UpsertEdge] = {}
    for edge in bare_edges:
        source_label, target_label = _EDGE_ENDPOINT_LABELS[edge.relationship_type]
        source_id = canonical_id_by_incoming_id.get(edge.source_id, edge.source_id)
        target_id = canonical_id_by_incoming_id.get(edge.target_id, edge.target_id)
        if source_id not in existing[source_label] or target_id not in existing[target_label]:
            continue
        identity = edge_identity(edge.relationship_type, source_id, target_id)
        edges.setdefault(
            identity,
            UpsertEdge(
                type=edge.relationship_type,
                identity=identity,
                source=NodeRef(label=source_label, id=source_id),
                target=NodeRef(label=target_label, id=target_id),
            ),
        )
    return tuple(edges.values())


def rewired_edge_endpoint_ids(
    bare_edges: tuple[BareEdge, ...], canonical_id_by_incoming_id: dict[str, str]
) -> dict[str, set[str]]:
    """Return the node ids `bare_edges` will touch after the rewrite, per endpoint label (pure).

    Validates first (`_validate_rewired_edge_endpoints`): a dedupe-eligible endpoint without a
    mapping entry raises before anything is read. Every label of the Edge Catalog is a key, so a
    caller can read the existing ids of each label in one batched read per label.

    Args:
        bare_edges: Edges to write, of the ten Edge Catalog types.
        canonical_id_by_incoming_id: `incoming_id -> canonical_id` map from dedup.

    Returns:
        Node label to the set of rewritten endpoint ids of that label; a label with no endpoint maps
        to an empty set.

    Raises:
        CompanyMergePersistenceError: a dedupe-eligible endpoint has no mapping entry.
    """
    _validate_rewired_edge_endpoints(bare_edges, canonical_id_by_incoming_id)
    endpoint_ids: dict[str, set[str]] = {label: set() for label in _EDGE_LABELS}
    for edge in bare_edges:
        source_label, target_label = _EDGE_ENDPOINT_LABELS[edge.relationship_type]
        endpoint_ids[source_label].add(
            canonical_id_by_incoming_id.get(edge.source_id, edge.source_id)
        )
        endpoint_ids[target_label].add(
            canonical_id_by_incoming_id.get(edge.target_id, edge.target_id)
        )
    return endpoint_ids


def persist_rewired_edges(
    target: GraphWriteTarget,
    single_tenant_graph: GraphHandle,
    bare_edges: tuple[BareEdge, ...],
    canonical_id_by_incoming_id: dict[str, str],
    *,
    checkpoint_requested: bool = False,
) -> None:
    """Persist `bare_edges` (the ten Edge Catalog types) into `target`'s graph as one group.

    Rewrites each edge's endpoints through `canonical_id_by_incoming_id` (`incoming_id ->
    canonical_id`, Capability dedup's resolutions) instead of writing the baseline-local id
    verbatim. The rule is endpoint-agnostic: for both the source and target of every edge, an id
    with an entry in the map is rewritten to the canonical id, otherwise it passes through
    unchanged. Only Capability and Policy endpoints are canonically deduped, so in practice
    `REQUIRES`' Capability target, `GOVERNED_BY`'s two ends and the classification edges'
    Capability/Policy targets are rewritten; an Obligation redirect from a cleanup merge is in the
    map too. A dedupe-eligible endpoint (`_dedupe_eligible_endpoint_ids`) must have an entry.

    Each edge is an `UpsertEdge` (no properties) in one gateway group. "An endpoint that is missing
    writes nothing" is decided here, before submission, from one batched existing-id read per label
    on `single_tenant_graph` (`build_rewired_edge_primitives`). The nodes were written by the
    earlier groups of this run, which the gateway has applied.

    Idempotent: re-running with the same input leaves the same graph, and the gateway logs nothing
    for edges that already exist.

    Args:
        target: Submitter, graph name and audit event the group is written under.
        single_tenant_graph: Read handle on the graph `target` names.
        bare_edges: Edges to write, of the ten Edge Catalog types.
        canonical_id_by_incoming_id: `incoming_id -> canonical_id` map built from dedup.
        checkpoint_requested: Asks the gateway to record the graph's digest after this group; the
            merge sets it unless a canonical-embedding backfill follows and will request it instead.

    Returns:
        None.

    Side Effects:
        One read query per Edge Catalog label on `single_tenant_graph`; one gateway group submitted
        on `target` when some edge has both endpoints present.

    Raises:
        CompanyMergePersistenceError: a dedupe-eligible endpoint has no mapping entry (raised before
            any read or write, so a bad edge set writes nothing), an endpoint read failed
            (`FalkorDB query failed:`), or the gateway did not take the group
            (`FalkorDB write failed:`).
    """
    endpoint_ids = rewired_edge_endpoint_ids(bare_edges, canonical_id_by_incoming_id)
    existing = {
        label: read_existing_ids(single_tenant_graph, label, ids)
        for label, ids in endpoint_ids.items()
    }
    submit_group(
        target,
        build_rewired_edge_primitives(existing, bare_edges, canonical_id_by_incoming_id),
        checkpoint_requested=checkpoint_requested,
    )
