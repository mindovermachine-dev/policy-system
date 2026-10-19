"""Frozen copy of Company Merge's graph writer as it was before the gateway migration.

It issues the Cypher the pre-migration `company_merge/graph_writer.py` issued, in the same order,
against a `LegacyRecordingGraph`: a small interpreter that keeps the node and edge sets those
statements would have produced. Only the parameters are read; the statement text selects the
shape. A statement shape the interpreter does not know raises, so the reference can only claim
what it has been taught: it is grown family by family as each writer migrates.

Frozen shapes: the RegulatoryInstrument upsert (`SET`), the create-only Role/Requirement upserts
(`ON CREATE SET`), the DEFINES/EXPRESSES provenance edges, the create-only Obligation upsert and
the PracticeArea/RiskPath ones (same template, other labels).
Each statement text is pinned by a sha256 taken from `git show HEAD:` at freeze time
(`test_frozen_statements_match_their_pinned_hashes`), so an edit to a "verbatim" constant fails.

Deviations from the original, each deliberate: the `_execute_query` connectivity-health wrapper is
dropped (it records dependency health, not graph state), and generated values are drawn from a
`ValueSource` (`values.now()` / `values.new_id()` instead of `datetime.now` / `uuid4`; the canonical
node mint is the first user).

Met modulo edge identity; the live digest and live interpreter agreement are not verifiable here
(no FalkorDB in this sandbox): the `falkordb_live` tests stay as the live anchor.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

from graph_gateway._fakes import InMemoryGraph

from company_merge._seed_merge_graphs import SingleTenantStore, StoreResult

if TYPE_CHECKING:
    from ingestion.adapters.internal_seed._legacy_reference import EdgeFact, NodeFact

    from ps_service.company_merge.generated_values import ValueSource
    from ps_service.company_merge.models import (
        BareEdge,
        BaselineNode,
        CanonicalResolution,
        NearMissPair,
        ProvenanceEdge,
    )

__all__ = [
    "FROZEN_STATEMENT_HASHES",
    "LEGACY_EDGE_ENDPOINT_LABELS",
    "LegacyRecordingGraph",
    "LegacyWriteTarget",
    "write_legacy_canonical_nodes",
    "write_legacy_embedding_backfill",
    "write_legacy_instrument_roles_requirements",
    "write_legacy_obligations",
    "write_legacy_pending_reviews",
    "write_legacy_practice_areas_and_risk_paths",
    "write_legacy_resolve_keep_separate",
    "write_legacy_rewired_edges",
]

_INSTRUMENT = "RegulatoryInstrument"
_ROLE = "Role"
_REQUIREMENT = "Requirement"

# sha256 of each rendered statement text, taken from `git show HEAD:ps-service/src/ps_service/
# company_merge/graph_writer.py` (commit 006c08f) at freeze.
FROZEN_STATEMENT_HASHES: dict[str, str] = {
    "instrument": "cd8bdd981a3d84b5daebc84a4ca288ca26c01b59ed194f9ab3f835e3bfd39386",
    "role": "5d367a6927e15d1fa71082c96edcda7a1914356fe6dce94d6502188ae21dd2f6",
    "requirement": "b7be0d4860abbb095060da408f38347d260a00ae2ffd3342b6b7341c47981c74",
    "defines": "dfdd5d18f92d1477aa6c5b2c21dc10be2769a769dcb84845ce30e60857bb6b15",
    "expresses": "4e0f6a061990da4afe697d3efc77006e0c56d3a6f690f39dccf17c283bb1c148",
    # slice 4a: the same `_upsert_passthrough_node(..., preserve_existing_properties=True)` template
    # rendered for the Obligation label.
    "obligation": "f780943f4c5c64c88959bcdf427ae8ac1ebdef2233c1984f2b650a4b8377d565",
    # slice 4b: the same template for the classification-layer node labels.
    "practice_area": "012c4961314820f84aa0d09b3cc9c94a90af80413a404e9865fbc8e149399ed3",
    # slice 5a: the canonical-node mint, rendered for the Capability label.
    "capability": "33e62cab41da0f350963eeb6931565175462142c8696faf053f43cad8f8aa4e2",
    # slice 5b: the set-if-null embedding backfill (canonical Capability/Policy and the incoming
    # Capability on the baseline graph are the same text, differing only in the label).
    "embedding_backfill": "f86d3cf2fb438704310738df12a5eb1e3318b728a380706fb4e25ffe54da682b",
    "risk_path": "7489b67b08e115d4c8233460a56fe4ed1da2ae6bb287ab094ccf8815102ff17d",
    # slice 7a: the rewired-edge statement, rendered for `Role -[:HAS]-> Obligation` (every edge
    # type is the same template with its own labels and type).
    "rewired_edge": "39ce98d77b407c738859df4a976e1a3d645de12be015f0c7bcb745342957e64c",
    # slice 8a: the PendingReview `CREATE` (one statement per near-miss pair).
    "pending_review": "3161ef685ba50a875f76a2bb6562c5f957e85b19aed3522521e6a9991727a9ac",
    # slice 9b: the keep-separate resolution, from `pending_review.py` (`_DELETE_REVIEW_QUERY`).
    "delete_review": "7c7ee750ebf276b8a028c9721da719158585f2fcd51f65f2b75875088576945f",
}

_INSTRUMENT_QUERY = re.compile(
    r"^MERGE \(n:RegulatoryInstrument \{id: \$id\}\) SET n \+= \$properties$"
)
_CREATE_ONLY_QUERY = re.compile(
    r"^MERGE \(n:(\w+) \{id: \$id\}\) ON CREATE SET n \+= \$properties$"
)
_BACKFILL_QUERY = re.compile(
    r"^MATCH \(n:(\w+) \{id: \$id\}\) WHERE n\.embedding IS NULL SET n\.embedding = \$embedding$"
)
_PROVENANCE_QUERY = re.compile(
    r"^MATCH \(r:RegulatoryInstrument \{id: \$regulatory_instrument_id\}\), "
    r"\(n:(\w+) \{id: \$target_id\}\) "
    r"MERGE \(r\)-\[e:(\w+)\]->\(n\) SET e\.source_ref = \$source_ref$"
)


# The original `_EDGE_ENDPOINT_LABELS` (relationship type -> source label, target label).
LEGACY_EDGE_ENDPOINT_LABELS: dict[str, tuple[str, str]] = {
    "HAS": ("Role", "Obligation"),
    "SATISFIED_BY": ("Requirement", "Obligation"),
    "REQUIRES": ("Obligation", "Capability"),
    "GOVERNED_BY": ("Capability", "Policy"),
    "SUPPORTED_BY": ("Policy", "Standard"),
    "IMPLEMENTED_BY": ("Standard", "Control"),
    "COVERS": ("PracticeArea", "Capability"),
    "OWNS": ("PracticeArea", "Policy"),
    "MITIGATED_BY": ("RiskPath", "Capability"),
    "VERIFIED_BY": ("RiskPath", "Control"),
}

_CREATE_REVIEW_QUERY = re.compile(
    r"^CREATE \(r:PendingReview \{id: \$id, kind: \$kind, status: \$status, "
    r"incoming_id: \$incoming_id, incoming_text: \$incoming_text, "
    r"nearest_existing_id: \$nearest_existing_id, "
    r"nearest_existing_text: \$nearest_existing_text, "
    r"similarity: \$similarity, created_at: \$created_at\}\)$"
)
_DELETE_REVIEW_QUERY = re.compile(r"^MATCH \(r:PendingReview \{id: \$review_id\}\) DELETE r$")
_REWIRED_EDGE_QUERY = re.compile(
    r"^MATCH \(s:(\w+) \{id: \$source_id\}\), \(t:(\w+) \{id: \$target_id\}\) "
    r"MERGE \(s\)-\[:(\w+)\]->\(t\)$"
)


class LegacyWriteTarget(Protocol):
    """Where the frozen statements run: the recording graph here, a real FalkorDB graph live."""

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        """Run one legacy statement."""
        ...


def _facts(properties: dict[str, object]) -> frozenset[tuple[str, object]]:
    """Hashable property facts: a list value (an embedding) becomes a tuple."""
    return frozenset(
        (name, tuple(cast("list[object]", value)) if isinstance(value, list) else value)
        for name, value in properties.items()
    )


@dataclass
class _Result:
    result_set: list[object] = field(default_factory=list)


@dataclass
class LegacyRecordingGraph:
    """A legacy `GraphHandle`: records every write call and the graph those calls build."""

    node_calls: list[NodeFact] = field(default_factory=list)
    edge_calls: list[EdgeFact] = field(default_factory=list)
    nodes: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    edges: dict[tuple[str, str, str, str, str], dict[str, object]] = field(default_factory=dict)

    @classmethod
    def copy_of(cls, graph: InMemoryGraph) -> LegacyRecordingGraph:
        """The nodes and edges of `graph` as a legacy graph (an edge's identity is not kept)."""
        copy = cls()
        copy.nodes = {key: dict(props) for key, props in graph.nodes.items()}
        copy.edges = {
            (key[0], key[1], key[2], key[3], key[4]): {
                name: value for name, value in props.items() if name != "identity"
            }
            for key, props in graph.edges.items()
        }
        return copy

    def seed_into(self, graph: InMemoryGraph) -> None:
        """Put this graph's nodes and edges into `graph` as a pre-gateway writer left them."""
        for (label, node_id), props in self.nodes.items():
            graph.seed_node((label,), node_id, props)
        for key, props in self.edges.items():
            graph.seed_legacy_edge(key, props)

    @staticmethod
    def knows(q: str) -> bool:
        """Whether `q` is a frozen write shape this interpreter can apply."""
        return bool(
            _INSTRUMENT_QUERY.match(q)
            or _CREATE_ONLY_QUERY.match(q)
            or _PROVENANCE_QUERY.match(q)
            or _BACKFILL_QUERY.match(q)
            or _REWIRED_EDGE_QUERY.match(q)
            or _CREATE_REVIEW_QUERY.match(q)
            or _DELETE_REVIEW_QUERY.match(q)
        )

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        """Apply one frozen legacy statement; any other shape is a failure of the reference."""
        assert params is not None, f"a frozen write statement carries parameters: {q}"
        if _INSTRUMENT_QUERY.match(q):
            self._merge_node(_INSTRUMENT, params, create_only=False)
        elif create_match := _CREATE_ONLY_QUERY.match(q):
            self._merge_node(create_match.group(1), params, create_only=True)
        elif backfill_match := _BACKFILL_QUERY.match(q):
            self._backfill_embedding(backfill_match.group(1), params)
        elif _CREATE_REVIEW_QUERY.match(q):
            self._create_node("PendingReview", params)
        elif _DELETE_REVIEW_QUERY.match(q):
            self._delete_review(cast("str", params["review_id"]))
        elif rewired_match := _REWIRED_EDGE_QUERY.match(q):
            source_label, target_label, edge_type = rewired_match.groups()
            self._merge_edge(
                edge_type,
                (source_label, cast("str", params["source_id"])),
                (target_label, cast("str", params["target_id"])),
                {},
            )
        elif edge_match := _PROVENANCE_QUERY.match(q):
            target_label, edge_type = edge_match.groups()
            self._merge_edge(
                edge_type,
                (_INSTRUMENT, cast("str", params["regulatory_instrument_id"])),
                (target_label, cast("str", params["target_id"])),
                {"source_ref": params["source_ref"]},
            )
        else:
            raise AssertionError(f"not a frozen legacy statement: {q}")
        return _Result()

    def _merge_node(self, label: str, params: dict[str, object], *, create_only: bool) -> None:
        node_id = cast("str", params["id"])
        properties = cast("dict[str, object]", params["properties"])
        self.node_calls.append((label, node_id, _facts(properties)))
        key = (label, node_id)
        # `id` is the MERGE key, not a stored property: a `SET n += {id: <same id>}` is a no-op.
        stored = {
            name: value for name, value in properties.items() if (name, value) != ("id", node_id)
        }
        if create_only:
            # ON CREATE SET: an existing node keeps every property.
            self.nodes.setdefault(key, stored)
        else:
            self.nodes.setdefault(key, {}).update(stored)

    def _create_node(self, label: str, params: dict[str, object]) -> None:
        """`CREATE`: always a new node (the id is a fresh uuid); a clash would be a test bug."""
        node_id = cast("str", params["id"])
        key = (label, node_id)
        assert key not in self.nodes, f"CREATE of an existing node {key}"
        self.node_calls.append(
            (label, node_id, _facts({k: v for k, v in params.items() if k != "id"}))
        )
        self.nodes[key] = {k: v for k, v in params.items() if k != "id"}

    def _delete_review(self, review_id: str) -> None:
        """`MATCH (r:PendingReview {id}) DELETE r`: a missing review is a no-op; a plain `DELETE`
        of a node that still has edges would fail in FalkorDB, and a review has none.
        """
        key = ("PendingReview", review_id)
        if key not in self.nodes:
            return
        assert not any(key in {(edge[1], edge[2]), (edge[3], edge[4])} for edge in self.edges), (
            f"DELETE of a PendingReview that still has edges: {review_id}"
        )
        del self.nodes[key]

    def _backfill_embedding(self, label: str, params: dict[str, object]) -> None:
        """`WHERE n.embedding IS NULL SET`: an absent node or a held embedding is a no-op."""
        node = self.nodes.get((label, cast("str", params["id"])))
        if node is not None and node.get("embedding") is None:
            node["embedding"] = params["embedding"]

    def _merge_edge(
        self,
        edge_type: str,
        source: tuple[str, str],
        target: tuple[str, str],
        properties: dict[str, object],
    ) -> None:
        self.edge_calls.append(
            (edge_type, source[0], source[1], target[0], target[1], frozenset(properties.items()))
        )
        # MATCH finds no endpoint -> nothing is written (a silent no-op).
        if source in self.nodes and target in self.nodes:
            key = (edge_type, source[0], source[1], target[0], target[1])
            self.edges.setdefault(key, {}).update(properties)

    def as_gateway_graph(self) -> InMemoryGraph:
        """The graph the calls built, every edge stamped with the gateway's identity.

        The comparison is "modulo edge identity": the legacy graph carries no identity, the
        gateway stamps `TYPE:source->target` on every edge it writes.
        """
        graph = InMemoryGraph()
        for (label, node_id), properties in self.nodes.items():
            graph.seed_node((label,), node_id, properties)
        for (
            edge_type,
            source_label,
            source_id,
            target_label,
            target_id,
        ), props in self.edges.items():
            identity = f"{edge_type}:{source_id}->{target_id}"
            key = (edge_type, source_label, source_id, target_label, target_id, identity)
            graph.edges[key] = {"identity": identity, **props}
            graph.edge_internal_id(key)
        return graph

    def final_node_facts(self) -> Counter[NodeFact]:
        """The nodes the calls left, once each, with every folded property."""
        return Counter(
            (label, node_id, _facts(props)) for (label, node_id), props in self.nodes.items()
        )

    def final_edge_facts(self) -> Counter[EdgeFact]:
        """The edges the calls left (endpoints existed), once each, with their properties."""
        return Counter(
            (edge_type, source_label, source_id, target_label, target_id, frozenset(props.items()))
            for (
                edge_type,
                source_label,
                source_id,
                target_label,
                target_id,
            ), props in self.edges.items()
        )


def write_legacy_instrument_roles_requirements(
    graph: LegacyWriteTarget,
    regulatory_instrument_id: str,
    regulatory_instrument_properties: dict[str, object],
    role_nodes: tuple[BaselineNode, ...],
    requirement_nodes: tuple[BaselineNode, ...],
    provenance_edges: tuple[ProvenanceEdge, ...],
) -> None:
    """The frozen `persist_role_and_requirement_passthrough` (family A), statements verbatim.

    Nodes (instrument, every Role, every Requirement) are written before any edge, as the original
    did: an edge `MATCH`es both endpoints.
    """
    graph.query(
        f"MERGE (n:{_INSTRUMENT} {{id: $id}}) SET n += $properties",
        params={"id": regulatory_instrument_id, "properties": regulatory_instrument_properties},
    )
    for role in role_nodes:
        _upsert_create_only(graph, _ROLE, role.id, role.properties)
    for requirement in requirement_nodes:
        _upsert_create_only(graph, _REQUIREMENT, requirement.id, requirement.properties)
    for edge in provenance_edges:
        target_label = _ROLE if edge.relationship_type == "DEFINES" else _REQUIREMENT
        graph.query(
            f"MATCH (r:{_INSTRUMENT} {{id: $regulatory_instrument_id}}), "
            f"(n:{target_label} {{id: $target_id}}) "
            f"MERGE (r)-[e:{edge.relationship_type}]->(n) SET e.source_ref = $source_ref",
            params={
                "regulatory_instrument_id": regulatory_instrument_id,
                "target_id": edge.target_id,
                "source_ref": edge.source_ref,
            },
        )


def _upsert_create_only(
    graph: LegacyWriteTarget, label: str, node_id: str, properties: dict[str, str | float]
) -> None:
    graph.query(
        f"MERGE (n:{label} {{id: $id}}) ON CREATE SET n += $properties",
        params={"id": node_id, "properties": properties},
    )


def write_legacy_obligations(
    graph: LegacyWriteTarget, obligation_nodes: tuple[BaselineNode, ...]
) -> None:
    """The frozen `persist_obligation_passthrough` (slice 4a): one create-only upsert per node."""
    for obligation in obligation_nodes:
        _upsert_create_only(graph, "Obligation", obligation.id, obligation.properties)


def write_legacy_practice_areas_and_risk_paths(
    graph: LegacyWriteTarget,
    practice_area_nodes: tuple[BaselineNode, ...],
    risk_path_nodes: tuple[BaselineNode, ...],
) -> None:
    """The frozen `persist_practice_area_and_risk_path_passthrough` (slice 4b).

    Every PracticeArea, then every RiskPath, one create-only upsert each; nothing at all for an
    empty input.
    """
    for practice_area in practice_area_nodes:
        _upsert_create_only(graph, "PracticeArea", practice_area.id, practice_area.properties)
    for risk_path in risk_path_nodes:
        _upsert_create_only(graph, "RiskPath", risk_path.id, risk_path.properties)


def write_legacy_canonical_nodes(
    graph: LegacyWriteTarget,
    incoming_nodes: tuple[BaselineNode, ...],
    resolutions: tuple[CanonicalResolution, ...],
    *,
    kind: str,
    values: ValueSource,
) -> None:
    """The frozen `persist_canonical_nodes` (slice 5a): one create-only upsert per new resolution.

    Deviation from the original, commented: `created_at` is `values.now().isoformat()` where the
    original drew `datetime.now(UTC).isoformat()`. The embedding is a property here, as the
    original wrote it (`list(resolution.embedding)`).
    """
    nodes_by_id = {node.id: node for node in incoming_nodes}
    for resolution in resolutions:
        if resolution.match_kind != "new":
            continue
        node = nodes_by_id[resolution.incoming_id]
        properties: dict[str, object] = dict(node.properties)
        properties["created_at"] = values.now().isoformat()
        if resolution.embedding is not None:
            properties["embedding"] = list(resolution.embedding)
        graph.query(
            f"MERGE (n:{kind} {{id: $id}}) ON CREATE SET n += $properties",
            params={"id": resolution.canonical_id, "properties": properties},
        )


def write_legacy_embedding_backfill(
    graph: LegacyWriteTarget, embeddings: dict[str, tuple[float, ...]], *, kind: str
) -> None:
    """The frozen `backfill_canonical_embeddings` / `backfill_incoming_capability_embeddings`.

    One verbatim statement per id (slice 5b); the embedding is passed as the list the original
    passed (`list(embedding)`).
    """
    for node_id, embedding in embeddings.items():
        graph.query(
            f"MATCH (n:{kind} {{id: $id}}) WHERE n.embedding IS NULL SET n.embedding = $embedding",
            params={"id": node_id, "embedding": list(embedding)},
        )


def write_legacy_rewired_edges(
    graph: LegacyWriteTarget,
    bare_edges: tuple[BareEdge, ...],
    canonical_id_by_incoming_id: dict[str, str],
) -> None:
    """The frozen `persist_rewired_edges` (slice 7a): one verbatim `MATCH ... MERGE` per edge.

    Both endpoints go through `canonical_id_by_incoming_id` (a missing entry passes through). The
    original's validate-then-write pass is not repeated: the scenarios hand it a complete mapping.
    `LEGACY_EDGE_ENDPOINT_LABELS` is the original `_EDGE_ENDPOINT_LABELS` table, frozen here so
    this module does not import the code it freezes.
    """
    for edge in bare_edges:
        source_label, target_label = LEGACY_EDGE_ENDPOINT_LABELS[edge.relationship_type]
        graph.query(
            f"MATCH (s:{source_label} {{id: $source_id}}), "
            f"(t:{target_label} {{id: $target_id}}) "
            f"MERGE (s)-[:{edge.relationship_type}]->(t)",
            params={
                "source_id": canonical_id_by_incoming_id.get(edge.source_id, edge.source_id),
                "target_id": canonical_id_by_incoming_id.get(edge.target_id, edge.target_id),
            },
        )


def write_legacy_pending_reviews(
    graph: LegacyWriteTarget,
    near_misses: tuple[NearMissPair, ...],
    *,
    kind: str,
    values: ValueSource,
) -> None:
    """The frozen `persist_pending_reviews` (slice 8a): one verbatim `CREATE` per pair.

    Deviation from the original, commented: the id is `review_` + `values.new_id()` and
    `created_at` is `values.now().isoformat()`, where the original drew `uuid4().hex` and
    `datetime.now(UTC).isoformat()`; the draw order (id first, then the instant) is the original's.
    """
    for pair in near_misses:
        graph.query(
            "CREATE (r:PendingReview {id: $id, kind: $kind, status: $status, "
            "incoming_id: $incoming_id, incoming_text: $incoming_text, "
            "nearest_existing_id: $nearest_existing_id, "
            "nearest_existing_text: $nearest_existing_text, "
            "similarity: $similarity, created_at: $created_at})",
            params={
                "id": f"review_{values.new_id()}",
                "kind": kind,
                "status": "pending",
                "incoming_id": pair.incoming_id,
                "incoming_text": pair.incoming_text,
                "nearest_existing_id": pair.nearest_existing_id,
                "nearest_existing_text": pair.nearest_existing_text,
                "similarity": pair.similarity,
                "created_at": values.now().isoformat(),
            },
        )


def write_legacy_resolve_keep_separate(graph: LegacyWriteTarget, review_id: str) -> None:
    """The frozen `resolve_review` keep-separate write (slice 9b): the review is deleted, no more.

    Verbatim `_DELETE_REVIEW_QUERY`. The preceding reads of the original (the review's kind) are
    not statements that change the graph and are not replayed.
    """
    graph.query(
        "MATCH (r:PendingReview {id: $review_id}) DELETE r", params={"review_id": review_id}
    )


class SnapshotWriteStore(SingleTenantStore):
    """`SingleTenantStore` plus the old write interpreter: Restore's snapshot writer only.

    Company Merge writes no Cypher, so the merge-side double refuses a write. The
    interim Restore writer (`restore/snapshot_merge_writer.py`) and the frozen
    agreement test still issue the old statements, and this class applies them: node `MERGE`,
    `MERGE ... ON CREATE SET` (first wins) and edge `MERGE` (dropped when an endpoint is absent).
    """

    _NODE_WRITE = re.compile(
        r"^MERGE \(n:(\w+) \{id: \$id\}\) (ON CREATE SET|SET) n \+= \$properties$"
    )
    _EDGE_WRITE = re.compile(
        r"^MATCH \(s:(\w+) \{id: \$source_id\}\), \(t:(\w+) \{id: \$target_id\}\) "
        r"MERGE \(s\)-\[:(\w+)\]->\(t\)$"
    )

    def query(self, q: str, params: dict[str, object] | None = None) -> StoreResult:
        """Apply a write the old writer issues; record any other statement, as the old fake did.

        A statement of a shape that is neither of the two writes (a provenance edge `MERGE ... SET`,
        a read) was recorded and answered by the read half; it is not applied.
        """
        self.statements.append(q)
        self.params_seen.append(dict(params or {}))
        node_write = self._NODE_WRITE.match(q)
        edge_write = self._EDGE_WRITE.match(q)
        if node_write is not None and params is not None:
            self._write_node(node_write.group(2), params, node_write.group(1))
            return StoreResult([])
        if edge_write is not None and params is not None:
            self._write_edge(edge_write.group(3), params)
            return StoreResult([])
        return StoreResult(self._read(q, params))

    def _write_node(self, mode: str, params: dict[str, object], label: str) -> None:
        key = (label, cast("str", params["id"]))
        properties = dict(cast("dict[str, object]", params["properties"]))
        if mode == "SET":
            self.nodes.setdefault(key, {}).update(properties)
        elif key not in self.nodes:
            self.nodes[key] = properties

    def _write_edge(self, relationship: str, params: dict[str, object]) -> None:
        source, target = cast("str", params["source_id"]), cast("str", params["target_id"])
        known = {node_id for _, node_id in self.nodes}
        if source in known and target in known:
            self.edges.add((relationship, source, target))
