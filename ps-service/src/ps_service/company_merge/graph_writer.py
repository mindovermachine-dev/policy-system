"""FalkorDB persistence for `ps_service.company_merge`'s single-tenant graph.

Since issue #210 every write this module makes is a Graph Write Gateway group: each `persist_*` /
`backfill_*` function builds gateway primitives (`build_*_primitives`) and submits them as one
group through `graph_submission.submit_group`. The module issues no Cypher write. The few reads it
makes (existing ids, embedding state) go through `graph_submission.read_rows`, which records
FalkorDB reachability.

Decisions that depend on graph state are made BEFORE submission, from a batched read, because a
gateway upsert merges over an existing node and so cannot express them:

- Create-only (RegulatoryInstrument excepted): a Role, Requirement, Obligation, Capability,
  PracticeArea, RiskPath or draft governance node whose id the graph already holds is left out of
  the group, so an existing node's properties are never overwritten and nothing is logged for it.
  For an id repeated in one input the first occurrence is kept. Role/Requirement/Obligation ids
  can legitimately recur across merge runs with drifted content (issue #28 AC-BI-006), exactly like
  a Capability's exact-key match.
- Set-if-null: an embedding backfill is submitted only for a node that exists and has no
  embedding; an already-embedded node is untouched. `persist_canonical_nodes` mints only for a
  `match_kind="new"` resolution; an exact or semantic match writes nothing onto the node it
  resolved to.
- Edge rewiring (`persist_rewired_edges`): one `UpsertEdge` per edge, identity
  `TYPE:source->target`. The rewrite rule is endpoint-agnostic: for BOTH ends, an id with an entry
  in `canonical_id_by_incoming_id` (`incoming_id -> canonical_id`, built by `merge.py` from
  Capability dedup) is rewritten to the canonical id, otherwise it passes through. In practice
  only Capability endpoints are mapped, so `REQUIRES`' Capability target is the one rewritten (a
  matched Capability is never minted, so its baseline-local id would dangle). An edge whose
  endpoint the graph does not hold is dropped before submission.
- Values generated at write time (`created_at`, embeddings, uuids) are fixed before submission
  from a `ValueSource` and carried in the group, so a replay reproduces them.

Obligation is a passthrough node (issue #42): its identity is Role-scoped, so it is never deduped
across sources. Only Capability (and internal-SoP-derived Policy) is canonically deduped by
Company Merge.
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
    "backfill_canonical_embeddings",
    "classification_write_counts",
    "persist_canonical_nodes",
    "persist_governance_drafts",
    "persist_obligation_passthrough",
    "persist_practice_area_and_risk_path_passthrough",
    "persist_rewired_edges",
    "persist_role_and_requirement_passthrough",
    "validate_classification_edge_endpoints",
]

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

# source_label, target_label per relationship_type -- the Edge Catalog shape
# mirrored from domain_mapper.graph_writer.persist_obligation_and_capability_graph
# (HAS/SATISFIED_BY/REQUIRES), .persist_governance_graph (GOVERNED_BY/
# SUPPORTED_BY/IMPLEMENTED_BY, issue #54 S4), and issue #106's
# classification-layer edges (COVERS/OWNS/MITIGATED_BY/VERIFIED_BY). Adding
# these four entries is the whole mechanism behind AC-BI-005: every function
# below (`_dedupe_eligible_endpoint_ids`, `_validate_rewired_edge_endpoints`,
# `persist_rewired_edges`) is already endpoint-label-generic, driven entirely
# off this one table -- no other code change is needed to make a
# `COVERS`/`MITIGATED_BY` edge's Capability target or an `OWNS` edge's Policy
# target get rewritten onto its dedup-resolved canonical id.
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


def _read_existing_ids(graph: GraphHandle, label: str, node_ids: Iterable[str]) -> set[str]:
    """Which of `node_ids` are `label` nodes of `graph`: one batched read, ids via `params`.

    `label` is always one of this module's fixed label literals, never baseline- or LLM-sourced,
    so interpolating it is safe; the ids travel as a parameter. Nothing to ask reads nothing.

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
    """One node primitive per id the graph does not hold, the FIRST occurrence's properties kept.

    Create-only: an id repeated in one input keeps its first occurrence's properties and the rest
    change nothing (the opposite of the Domain Mapper's last-wins fold). An id in `existing` is
    left out entirely: nothing is logged for it.

    Raises:
        CompanyMergePersistenceError: a node carries a property the gateway cannot store (a
            reserved key, a non-finite number); nothing has been submitted by then.
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
    """The group's mutations: instrument, Roles, Requirements, `DEFINES`, `EXPRESSES`.

    Pure: nothing is read or submitted. The instrument is refreshed on every run (an upsert that
    merges the properties over the node, as the old unconditional `SET` did) with `id` left out of
    the bag (a whole-bag copy of the NATIVE node, whose `id` may differ in casing and must never
    rewrite the match key). A Role or Requirement is create-only (`_create_only_nodes`): one
    already in `existing_*` is left out of the group, so it keeps every property and nothing is
    logged for it. A provenance edge carries the identity `{type}:{instrument}->{target}` and its
    `source_ref`; one whose target is in neither the graph nor the group is dropped, silently, as
    a missing endpoint writes nothing (a group edge with a missing endpoint would fail the whole
    group); a repeated edge folds to one primitive, the last `source_ref` winning.

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

    One gateway group on `target`'s graph: the instrument (refreshed every run), then every
    Role and Requirement the graph does not hold yet, then the `DEFINES`/`EXPRESSES` edges. Role
    and Requirement ids can legitimately recur across merge runs with different properties; since
    issue #28's AC-BI-006 fix an existing node keeps every property, and since #210 that is
    decided here, before submission, from one batched read per label on `graph` (a gateway upsert
    would merge over the existing node). Obligation is in the same passthrough category since #42
    -- see `persist_obligation_passthrough`.

    Idempotent: re-running with identical input leaves the same graph and logs nothing new (the
    gateway drops the mutations that change nothing).

    Raises:
        CompanyMergePersistenceError: the existing-node read failed (`FalkorDB query failed:`), or
            the gateway did not take the group (`FalkorDB write failed:`).
    """
    existing_roles = _read_existing_ids(
        graph,
        _ROLE_LABEL,
        (
            *(node.id for node in role_nodes),
            *(e.target_id for e in provenance_edges if e.relationship_type == "DEFINES"),
        ),
    )
    existing_requirements = _read_existing_ids(
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
    """Count how many of `obligation_ids` are NOT yet Obligation nodes in `single_tenant_graph`.

    One parameterized read (`$ids`, never interpolated: Level 2 Query Safety), issued BEFORE
    `persist_obligation_passthrough` so the answer is the delta that write will make. Net-new is
    decided by this read, not driver statistics: `GraphQueryResult` exposes only `result_set`.
    Duplicate ids count once; an empty tuple issues no query.

    Raises:
        CompanyMergePersistenceError: `FalkorDB query failed: <error>` if the read could not be
            made.
    """
    unique_ids = sorted(set(obligation_ids))
    if not unique_ids:
        return 0
    rows = read_rows(single_tenant_graph, _EXISTING_OBLIGATIONS_QUERY, {"ids": unique_ids})
    return len(set(unique_ids) - {cast("str", row[0]) for row in rows})


def persist_obligation_passthrough(
    target: GraphWriteTarget,
    single_tenant_graph: GraphHandle,
    obligation_nodes: tuple[BaselineNode, ...],
) -> None:
    """Persist one regulation's Obligation nodes as one gateway group on `target`'s graph.

    Create-only, like Role/Requirement since issue #28's AC-BI-006 fix (issue #42: Obligation is
    Role-scoped, a weak entity of exactly one Role, never deduped across sources). Re-merging the
    same regulation against a baseline graph whose content has since drifted is a real, observed
    way the same Obligation id recurs with DIFFERENT properties (IMPL_SLICE_7.md's live finding:
    199 pre-existing Obligation nodes had `confidence` drift after a second live merge run), so an
    existing Obligation keeps every property. Since #210 that is decided here, before submission,
    from one batched read on `single_tenant_graph` (a gateway upsert would merge over the
    existing node): an Obligation the graph holds is left out of the group and nothing is logged
    for it. For an id repeated in the input the first occurrence wins.

    Called by `merge.py` after `persist_role_and_requirement_passthrough` and before
    `persist_rewired_edges`, so the `HAS`/`SATISFIED_BY`/`REQUIRES` edge writes find these nodes.

    Raises:
        CompanyMergePersistenceError: the existing-node read failed (`FalkorDB query failed:`), a
            node carries a property the gateway cannot store, or the gateway did not take the
            group (`FalkorDB write failed:`).
    """
    existing = _read_existing_ids(
        single_tenant_graph, _OBLIGATION_LABEL, (node.id for node in obligation_nodes)
    )
    submit_group(target, _create_only_nodes(_OBLIGATION_LABEL, obligation_nodes, existing))


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
    """The create-only draft nodes of a governance tree, and how many authored a non-draft status.

    Pure. Returns one `UpsertNode` per Policy, Standard and Control id that `existing` (label ->
    ids the graph already holds) does not hold, Policies first, the FIRST occurrence of a repeated
    id kept (create-only, in Policy, Standard, Control order). `status` is forced to
    `"draft"` after the artifact's own properties are spread, so an artifact-authored `approved`
    never wins; `version` defaults to the lifecycle's string `"1"`. A Policy also carries the
    owner pair and `created_at` (fixed by the caller, the same for every Policy of the call); its
    artifact-supplied embedding travels in the primitive's `embedding` field. The override count
    describes the INPUT nodes, whether or not the graph already holds them.

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

    The write behind `governance_import.import_draft_governance`, used by live internal ingest
    (issues #183, #227): an imported tree has not been through the customer's governance, so it
    arrives `draft`, owned by the importing caller (`owner` is their verified `(sub, iss)` pair,
    stored as `owner_subject`/`owner_issuer` exactly as `policy_lifecycle.create_policy_draft`
    stores them), and can only become `approved` through the normal propose/approve flow.

    No dedup of any kind: each node is keyed on its own content-hashed id. A draft must never
    converge onto an existing canonical Policy, since the draft Standards/Controls would then
    hang under a possibly-approved parent and the whole-tree-moves-together rule would break.
    Near-duplicate detection is the near-miss review flow's job.

    Since #210 this is a Graph Write Gateway group. Create-only is decided here, before
    submission, from one batched existing-id read per label (a gateway upsert would merge over
    the existing node), so a node that already exists -- including one a user has since approved
    -- keeps its status, owner and content, and nothing is logged for it. `created_at` is drawn
    from `values` ONCE, before the group is built, so the group carries it and a replay
    reproduces it (AC-BI-007). `policy_embeddings` carries the artifact-supplied Policy vectors
    (by id) so a later cleanup flow still has them; a Policy without one gets no embedding.

    Returns:
        `GovernanceDraftCounts` describing the INPUT (not the nodes actually created).

    Raises:
        CompanyMergePersistenceError: an existing-id read failed (`FalkorDB query failed:`), a
            node carries a property the gateway cannot store, or the gateway did not take the
            group (`FalkorDB write failed:`).
    """
    created_at = values.now().isoformat()
    existing = {
        label: _read_existing_ids(single_tenant_graph, label, (node.id for node in nodes))
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


def persist_practice_area_and_risk_path_passthrough(
    target: GraphWriteTarget,
    single_tenant_graph: GraphHandle,
    practice_area_nodes: tuple[BaselineNode, ...],
    risk_path_nodes: tuple[BaselineNode, ...],
) -> None:
    """Persist PracticeArea/RiskPath nodes (issue #106): exact-identity passthrough.

    PracticeArea/RiskPath ids are content-hashed from `name` alone and are
    EXPECTED to recur across separate baselines
    (`ps-domain-concepts.md`'s identity notes) -- so they are create-only, like
    Capability/Policy (`persist_canonical_nodes`) and Role/Requirement/Obligation,
    making AC-BI-007's "an existing node's properties are never overwritten"
    hold. Since #210 that is decided here, before submission, from one batched
    existing-node read per label on `single_tenant_graph` (a gateway upsert would
    merge over the existing node): a node the graph holds is left out of the
    group and nothing is logged for it. Every PracticeArea precedes every
    RiskPath in the one group; for an id repeated in the input the first
    occurrence wins.

    No dedup call of any kind precedes this -- identity convergence is
    structural (identical `name` -> identical hash -> identical node id), so
    there is nothing to compute (AC-BI-006).

    A baseline with `practice_area_nodes == () and risk_path_nodes == ()`
    issues no read and submits no group (AC-BI-009's structural no-op for the
    node half).

    Raises:
        CompanyMergePersistenceError: an existing-node read failed (`FalkorDB query failed:`), a
            node carries a property the gateway cannot store, or the gateway did not take the
            group (`FalkorDB write failed:`).
    """
    existing_practice_areas = _read_existing_ids(
        single_tenant_graph, _PRACTICE_AREA_LABEL, (node.id for node in practice_area_nodes)
    )
    existing_risk_paths = _read_existing_ids(
        single_tenant_graph, _RISK_PATH_LABEL, (node.id for node in risk_path_nodes)
    )
    submit_group(
        target,
        (
            *_create_only_nodes(_PRACTICE_AREA_LABEL, practice_area_nodes, existing_practice_areas),
            *_create_only_nodes(_RISK_PATH_LABEL, risk_path_nodes, existing_risk_paths),
        ),
    )


def validate_classification_edge_endpoints(
    single_tenant_graph: GraphHandle,
    classification_edges: tuple[BareEdge, ...],
    canonical_id_by_incoming_id: dict[str, str],
) -> None:
    """AC-BI-008: every classification edge endpoint must already exist.

    Every `COVERS`/`OWNS`/`MITIGATED_BY`/`VERIFIED_BY` endpoint must already
    exist, before any of these four edges is written.

    Capability/Policy targets are covered by `canonical_id_by_incoming_id`
    (built by Capability/Policy dedup, same as
    `_validate_rewired_edge_endpoints`). PracticeArea/RiskPath sources and
    `VERIFIED_BY`'s Control target are passthrough nodes with NO entry in
    that dict by design -- but by the time this runs, `merge.py`/
    `restore_instrument.py` has already persisted every passthrough node for
    this baseline (Role/Requirement/Obligation/Capability/Policy/Standard/
    Control/PracticeArea/RiskPath all write before any edge write -- the
    same ordering invariant `persist_role_and_requirement_passthrough`'s own
    docstring already states). So "absent from both the baseline and the
    single-tenant graph" (AC-BI-008's wording) reduces, at this point in the
    call sequence, to a single live existence check against
    `single_tenant_graph` -- one batched read, before any classification
    edge write.

    This is a NEW, additional guarantee scoped explicitly to these four edge
    types -- it deliberately does not reuse `_validate_rewired_edge_endpoints`
    (which only ever checks dict membership, never queries the database):
    for the six pre-existing edge types, a passthrough endpoint missing from
    the graph has never been treated as an error (a zero-row `MATCH` there
    silently produces no write, long-standing out-of-scope behavior this
    issue does not change). Raises `CompanyMergePersistenceError` if any
    endpoint absent from `canonical_id_by_incoming_id` is also absent from
    `single_tenant_graph`.
    """
    unresolved_ids: set[str] = set()
    for edge in classification_edges:
        for endpoint_id in (edge.source_id, edge.target_id):
            if endpoint_id not in canonical_id_by_incoming_id:
                unresolved_ids.add(endpoint_id)
    if not unresolved_ids:
        return
    rows = read_rows(
        single_tenant_graph,
        "UNWIND $ids AS id MATCH (n {id: id}) RETURN id",
        {"ids": sorted(unresolved_ids)},
    )
    existing_ids = {cast("str", row[0]) for row in rows}
    missing = unresolved_ids - existing_ids
    if missing:
        raise CompanyMergePersistenceError(
            f"classification edge endpoint(s) {sorted(missing)!r} not found in "
            "the single-tenant graph"
        )


def classification_write_counts(graph: BaselineGraph) -> dict[str, int]:
    """AC-BI-011: PracticeArea/RiskPath node and classification-edge counts.

    Counts of PracticeArea/RiskPath nodes and the four classification edge
    types a `merge_baseline_graph`/`_run_baseline_merge` call writes for
    `graph` -- attached to the caller's own `outcome="succeeded"` audit log
    entry via `extra=`.

    A public function in `graph_writer.py` (not a private helper in
    `merge.py`) so both `merge.py` (live path) and, in a later slice,
    `restore_instrument.py` (offline path) can compute the identical six
    counts via a normal public import, without a cross-module private
    import (`ruff`'s `SLF001` is not exempted for `src/`).
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
    """One create-only `kind` node per `match_kind="new"` resolution the graph does not hold.

    Pure apart from `values`: nothing is read or submitted. `created_at` is drawn from `values`
    once per new resolution, in resolution order, HERE, so the primitive carries a literal (the
    Cypher writer drew `datetime.now` at apply time). The embedding travels in the primitive's
    `embedding` field, present only when the resolution has one; a mint that never triggered a
    comparison (an empty working index) carries none and is backfilled later. A canonical id in
    `existing_ids` is left out of the group, so the node keeps every property and nothing is
    logged for it.

    `incoming_nodes`/`resolutions` are guaranteed 1:1 by construction (`dedup.dedupe_canonical_
    nodes` builds exactly one `CanonicalResolution` per incoming node, `incoming_id` set to that
    node's own `id`), so there is no validate-then-write pass.

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

    Writes into the graph `target` names (PLAN_REVIEWED.md §6.1, Increment 11's mint half). A
    `match_kind="exact"`/`"semantic"` resolution gets NO write at all here -- it already resolved
    onto an existing canonical node, and this function's whole point is to never touch one.

    `kind` is `"Capability"` since #42 -- Obligation is no longer canonically deduped
    (`persist_obligation_passthrough` above).

    Create-only is the load-bearing invariant that makes "existing canonical node's properties
    are never overwritten" hold. Since #210 it is decided here, before submission, from one
    batched existing-id read on `single_tenant_graph` (a gateway upsert would merge over the
    existing node); see `build_canonical_node_primitives` for what a new node carries. The
    properties are the underlying `BaselineNode.properties` plus `created_at` (issue #35, the
    near-miss review workflow: the deterministic "earlier-created wins" winner selection of
    `resolve --decision=merge`), set once at mint and never touched again.

    Raises:
        CompanyMergePersistenceError: the existing-node read failed (`FalkorDB query failed:`), a
            node carries a property the gateway cannot store, or the gateway did not take the
            group (`FalkorDB write failed:`).
    """
    existing = _read_existing_ids(
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
    """Which of `node_ids` are `label` nodes of `graph`, and whether each still has no embedding.

    One batched read, ids via `params`; the vector itself is never returned. An id the graph does
    not hold is absent from the result. `label` is always one of this module's fixed literals.

    Raises:
        CompanyMergePersistenceError: `FalkorDB query failed: <error>` if the read could not be
            made; nothing has been submitted by then.
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
    """One `UpsertNode(label, id, embedding=...)` per id that exists and has no embedding yet.

    Pure. `embedding_is_null` maps each id the graph holds to whether it has no embedding; an id
    missing from it is absent (a gateway upsert would create it, which `MATCH` never did), and an
    id mapped to `False` already has an embedding (a gateway upsert would replace it). Neither is
    submitted, so nothing is logged for them. Input order is kept. `embedding` is a reserved
    property key, so it travels in the primitive's `embedding` field and no other property is
    named: the node keeps everything else it has.

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
) -> None:
    """Set each `embeddings` entry on an already-existing `kind` canonical node that has none.

    PLAN_REVIEWED.md §6.2, B2's fix -- distinct from `persist_canonical_nodes`' create-only mint,
    and never touching a node's other properties. Since #210 set-if-null is decided here, before
    submission: one batched read of which ids exist and have no embedding
    (`_read_embedding_state`), then ONE group of `build_embedding_backfill_primitives`. A node
    that already has an embedding, or is absent, is left out; a re-run therefore submits and logs
    nothing (AC-BI-006). Every id is guaranteed (by construction -- `dedup.dedupe_canonical_nodes`'
    `original_existing_ids` filter, §5.4) to exist already; `merge.py` calls this after the mint
    and the edge writes.

    Raises:
        CompanyMergePersistenceError: the state read failed (`FalkorDB query failed:`), an
            embedding cannot be stored, or the gateway did not take the group
            (`FalkorDB write failed:`).
    """
    state = _read_embedding_state(single_tenant_graph, kind, embeddings)
    submit_group(target, build_embedding_backfill_primitives(state, embeddings, label=kind))


def backfill_incoming_capability_embeddings(
    target: GraphWriteTarget,
    baseline_graph: GraphHandle,
    *,
    embeddings: dict[str, tuple[float, ...]],
) -> None:
    """Set each `embeddings` entry on its own INCOMING `{short}_baseline` Capability node, if null.

    Issue #31 -- the incoming-side twin of `backfill_canonical_embeddings`: same set-if-null
    contract (AC-BI-006) and the same "only `embedding` is ever touched" contract, but `target`
    names the regulation's OWN `{short}_baseline` graph, `baseline_graph` is its read handle, and
    the kind is always Capability (there is no Policy-side incoming embedding to cache, see
    `DedupResult.incoming_embedding_backfills`).

    Every `(incoming_id, embedding)` pair comes from `DedupResult.incoming_embedding_backfills`
    (`dedup.dedupe_canonical_nodes`, AC-BI-005); a later `graph_reader.read_baseline_graph` of the
    same, unchanged baseline reads the cached value back onto `BaselineNode.embedding`, letting
    `dedup.find_best_semantic_match` skip the `route_embedding` call for it (AC-BI-002/003).
    """
    state = _read_embedding_state(baseline_graph, _CAPABILITY_LABEL, embeddings)
    submit_group(
        target, build_embedding_backfill_primitives(state, embeddings, label=_CAPABILITY_LABEL)
    )


def _dedupe_eligible_endpoint_ids(edge: BareEdge) -> tuple[str, ...]:
    """Return `edge`'s endpoints whose label (per `_EDGE_ENDPOINT_LABELS`) is canonically deduped.

    Capability and (since issue #54's S4) Policy endpoints are the only ones
    that went through `dedup.dedupe_canonical_nodes` and are therefore
    guaranteed, by construction, to carry an entry in a correctly-built
    `canonical_id_by_incoming_id`. Since #42, Obligation is a passthrough
    node (Role-scoped, never deduped), so `HAS`/`SATISFIED_BY` yield nothing
    (Role/Requirement source, Obligation target -- all passthrough) and
    `REQUIRES` yields only its target (Capability); its Obligation source
    passes through. Since #54, `GOVERNED_BY` yields both endpoints
    (Capability source, Policy target -- both canonically deduped);
    `SUPPORTED_BY`/`IMPLEMENTED_BY` yield nothing (Standard/Control are weak
    entities, passthrough, never deduped). Order in the returned tuple is
    source-before-target when both are eligible; callers should not
    otherwise rely on it.
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
    """Whole-collection validation pass over every edge's dedupe-eligible endpoints.

    Mirrors `domain_mapper.graph_writer._validate_role_references`'s B3
    shape: EVERY one of an edge's dedupe-eligible endpoints (see
    `_dedupe_eligible_endpoint_ids` -- since #42 that is only a `REQUIRES`
    edge's Capability target) must resolve within `canonical_id_by_incoming_id`
    before `persist_rewired_edges` issues a single `graph.query` call for ANY
    edge -- not interleaved validate-then-write per edge. Raises
    `CompanyMergePersistenceError` on the first violation found, with zero
    `graph.query` calls having been made by the time this raises.

    Every Capability id that appears as a `REQUIRES` target is guaranteed by
    construction to have gone through `dedup.dedupe_canonical_nodes` and
    therefore have a `CanonicalResolution` entry in a correctly-built
    `canonical_id_by_incoming_id`. An absent entry means the caller-supplied
    mapping itself is incomplete (e.g. `merge.py` failed to pass Capability
    dedup's results), not that the endpoint "hasn't been resolved yet" -- so
    it is a hard pre-write error, not a pass-through.
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
    """One `UpsertEdge` per distinct edge whose two endpoints (after the rewrite) both exist.

    Pure: nothing is read or submitted. Each endpoint goes through `canonical_id_by_incoming_id`
    (a missing entry passes through). `existing` maps a node label to the ids the graph holds. An
    edge with a missing endpoint writes nothing, and a group edge with a missing endpoint would
    fail the WHOLE group, so such an edge is dropped here, silently. The identity is
    `{type}:{source}->{target}` (`_edge_identity`): a repeated edge, or two that rewrite to the
    same ends, fold to one primitive (first position kept). The edge carries no properties (Edge
    Catalog).
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


def persist_rewired_edges(
    target: GraphWriteTarget,
    single_tenant_graph: GraphHandle,
    bare_edges: tuple[BareEdge, ...],
    canonical_id_by_incoming_id: dict[str, str],
) -> None:
    """Persist `bare_edges` (the ten Edge Catalog types) into `target`'s graph as ONE group.

    Rewrites each edge's endpoints through `canonical_id_by_incoming_id`
    (`incoming_id -> canonical_id`, Capability dedup's resolutions -- see module docstring)
    instead of writing the baseline-local id verbatim (PLAN_REVIEWED.md §6.2/§10 Increment 12).

    The rule is endpoint-agnostic, not edge-type-specific: for BOTH the source and target of every
    edge, if that endpoint's baseline-local id has an entry in `canonical_id_by_incoming_id`, it is
    rewritten to the canonical id; otherwise it passes through unchanged. Only Capability (and
    Policy) endpoints are ever canonically deduped, so in practice `REQUIRES`'s Capability target,
    `GOVERNED_BY`'s two ends and the classification edges' Capability/Policy targets are the ones
    rewritten; an Obligation redirect (issue #190) is in the mapping too. A dedupe-eligible
    endpoint (`_dedupe_eligible_endpoint_ids`) MUST have an entry.

    Since #210 each edge is an `UpsertEdge` (no properties) in one gateway group, and "an endpoint
    that is missing writes nothing" is decided here, before submission, from one batched
    existing-id read per label on `single_tenant_graph` (`build_rewired_edge_primitives`). The
    nodes were written by the earlier groups of this run, which the gateway has applied.

    Idempotent: re-running with the same input leaves the same graph, and the gateway logs nothing
    for edges that already exist.

    Raises:
        CompanyMergePersistenceError: a dedupe-eligible endpoint has no mapping entry (before any
            read or write: validate-then-write over the whole collection, so a bad edge set
            writes nothing), an endpoint read failed (`FalkorDB query failed:`), or the gateway
            did not take the group (`FalkorDB write failed:`).
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
    existing = {
        label: _read_existing_ids(single_tenant_graph, label, ids)
        for label, ids in endpoint_ids.items()
    }
    submit_group(
        target, build_rewired_edge_primitives(existing, bare_edges, canonical_id_by_incoming_id)
    )
