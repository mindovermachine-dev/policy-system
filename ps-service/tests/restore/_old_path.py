"""The pre-#214 Restore path, as far as it can run offline (issue #214, slices 7 and 8a).

`run_baseline_merge` is the frozen pre-#214 `restore_instrument._run_baseline_merge` (removed from
production in slice 8a), kept here verbatim as the equivalence reference over the interim
`snapshot_merge_writer`. It is test code: nothing in `src` calls it.

`PopulatedGraph` is a `_GraphQueryHandle` that interprets the two statement shapes the real
`restore.populate.populate_graph` issues, so the REAL old code builds the staged native and
baseline legs in an in-memory graph. Edges come out stamped with the gateway's identity (the
#210 convention: digests are compared modulo edge identity).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from graph_gateway._fakes import InMemoryGraph

from ps_service.company_merge import graph_reader, obligation_redirect
from ps_service.company_merge.dedup import resolve_capability_convergence_offline
from ps_service.company_merge.falkordb_client import select_graph as select_company_merge_graph
from ps_service.company_merge.governance_import import (
    GovernanceImport,
    governance_count_fields,
    import_draft_governance,
)
from ps_service.restore import snapshot_merge_writer
from ps_service.restore.errors import ArtifactContentRejectedError, RestoreOwnerRequiredError
from ps_service.restore.populate import populate_graph

if TYPE_CHECKING:
    from falkordb import FalkorDB

    from ps_service.export.models import SerializedGraph
    from ps_service.logging.emitter import LogEmitter

_CREATE = re.compile(r"^UNWIND \$rows AS row CREATE \(n:(\w+)\) SET n = row$")
_MERGE = re.compile(
    r"^UNWIND \$rows AS row MATCH \(s:(\w+) \{id: row\.source_id\}\), "
    r"\(t:(\w+) \{id: row\.target_id\}\) MERGE \(s\)-\[r:(\w+)\]->\(t\) SET r = row\.properties$"
)


@dataclass(frozen=True)
class _Result:
    result_set: list[object] = field(default_factory=list)


@dataclass
class PopulatedGraph:
    """The staged graph `populate_graph` builds, as an `InMemoryGraph`."""

    graph: InMemoryGraph = field(default_factory=InMemoryGraph)

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        """Apply one `populate_graph` statement."""
        rows = cast("list[dict[str, object]]", (params or {})["rows"])
        create = _CREATE.match(q)
        merge = _MERGE.match(q)
        if create is not None:
            for row in rows:
                properties = dict(row)
                node_id = cast("str", properties.pop("id"))
                self.graph.nodes[(create.group(1), node_id)] = properties
                self.graph.internal_id(create.group(1), node_id)
        elif merge is not None:
            self._merge_edges(merge.group(1), merge.group(2), merge.group(3), rows)
        else:
            raise AssertionError(f"populate_graph issued an unexpected statement: {q}")
        return _Result()

    def _merge_edges(
        self, source_label: str, target_label: str, edge_type: str, rows: list[dict[str, object]]
    ) -> None:
        for row in rows:
            source, target = cast("str", row["source_id"]), cast("str", row["target_id"])
            if (source_label, source) not in self.graph.nodes:
                continue
            if (target_label, target) not in self.graph.nodes:
                continue
            identity = f"{edge_type}:{source}->{target}"
            key = (edge_type, source_label, source, target_label, target, identity)
            self.graph.edges[key] = {
                "identity": identity,
                **cast("dict[str, object]", row["properties"]),
            }
            self.graph.edge_internal_id(key)


def populate_old_path(serialized: SerializedGraph) -> InMemoryGraph:
    """Run the real `populate_graph` over `serialized` and return the graph it built."""
    populated = PopulatedGraph()
    populate_graph(populated, serialized)
    return populated.graph


def run_baseline_merge(
    db: FalkorDB,
    baseline_staged_name: str,
    regulatory_instrument_id: str,
    incoming_embeddings: dict[str, tuple[float, ...]],
    similarity_threshold: float,
    snapshot_name: str,
    emitter: LogEmitter | None,
    governance: GovernanceImport | None = None,
) -> dict[str, int]:
    """D8 step 5 / D6: dedupe and merge the staged baseline graph into `snapshot_name`.

    Reads the already-staged baseline graph back via the EXISTING, unmodified
    `graph_reader.read_baseline_graph` (never reimplemented); dedupes its
    Capability nodes via `resolve_capability_convergence_offline` (Slices
    5.3/5.4, D6) against `snapshot_name`'s own existing canonical index; then
    persists via `snapshot_merge_writer.persist_*` (the verbatim interim copy of the
    pre-#210 Company Merge writers, retired by #214), in the same
    write order `merge.py::merge_baseline_graph` itself uses: role/
    requirement passthrough, obligation passthrough, canonical Capability
    mints, (issue #183/#227) the shared draft-governance import
    (`governance_import.import_draft_governance`) when the baseline carries a
    Policy/Standard/Control tree, rewired edges (regulatory-spine +
    governance), then Capability embedding backfill.

    `governance` (issue #183) carries the restoring caller's `owner` and the
    artifact-supplied Policy embeddings; it defaults to `None` (an empty
    `GovernanceImport`) so callers that never restore an internal-sourced
    instrument (no Policy content in the artifact) need not pass it.

    Issue #106: after the governance import, persists PracticeArea/RiskPath nodes
    (exact-identity passthrough, no dedup call of any kind) and validates
    every COVERS/OWNS/MITIGATED_BY/VERIFIED_BY edge endpoint exists before
    any classification edge is written (AC-BI-008), then folds
    `baseline.classification_edges` into the same `persist_rewired_edges`
    call -- mirroring `merge.py::merge_baseline_graph`'s live-path placement
    exactly (§5 of PLAN.md). Returns the six-key
    `snapshot_merge_writer.classification_write_counts(baseline)` dict (AC-BI-011)
    for the caller to attach to its own `"succeeded"` audit log entry.

    This function is passed as `stage_and_finalize_policy_system_leg`'s
    `run_offline_merge` argument -- it writes only into `snapshot_name`
    (a staged `GRAPH.COPY` of the live single-tenant graph), never the live
    graph itself, and may run more than once across that function's own
    WATCH-guarded retry attempts.
    """
    baseline_staged_graph = select_company_merge_graph(db, baseline_staged_name)
    baseline = graph_reader.read_baseline_graph(baseline_staged_graph, regulatory_instrument_id)
    governance = governance or GovernanceImport()
    if governance.owner is None and (
        baseline.policy_nodes or baseline.standard_nodes or baseline.control_nodes
    ):
        # Backstop for direct callers: refuse BEFORE any snapshot read or write.
        raise RestoreOwnerRequiredError
    if not baseline.regulatory_instrument_properties:
        # graph_reader._read_regulatory_instrument_properties looks up
        # `MATCH (n:RegulatoryInstrument {id: $regulatory_instrument_id})` and
        # returns {} rather than raising when nothing matches (that reader's
        # own documented contract leaves "should a miss abort the merge" to
        # its caller). A baseline graph with no RegulatoryInstrument node at
        # all is a legitimate no-op leg (D20 precedent: native-only test
        # fixtures use a wholly empty baseline) -- only a graph that HAS a
        # RegulatoryInstrument node, just not under the id the manifest
        # named, is the real problem. Confirmed empirically: a
        # manifest.instrument_id that doesn't equal the staged baseline
        # graph's own RegulatoryInstrument.id (e.g. the raw CELEX instead of
        # the real "{SHORT}-{VERSION}" id) used to merge silently with every
        # RegulatoryInstrument property (celex, title, ...) blanked out, with
        # no error anywhere in the pipeline -- export "succeeds", restore
        # reports all three stages "succeeded", and the corruption only
        # surfaces later to whoever queries the data. Fail loud here instead.
        any_regulatory_instrument = baseline_staged_graph.query(
            "MATCH (n:RegulatoryInstrument) RETURN count(n) AS c"
        )
        rows = cast("list[list[object]]", any_regulatory_instrument.result_set)
        if rows and cast("int", rows[0][0]) > 0:
            raise ArtifactContentRejectedError(
                f"no RegulatoryInstrument node with id {regulatory_instrument_id!r} found in "
                f"the staged baseline graph -- the manifest's instrument_id must match the "
                f"source graph's actual RegulatoryInstrument.id"
            )

    snapshot_graph = select_company_merge_graph(db, snapshot_name)
    dedup_result = resolve_capability_convergence_offline(
        baseline.capability_nodes,
        incoming_embeddings=incoming_embeddings,
        single_tenant_graph=snapshot_graph,
        threshold=similarity_threshold,
        emitter=emitter,
    )

    # issue #190 (H1): drop Obligations a cleanup merge absorbed, before any write, and map
    # them to their terminal survivor so the rewiring below re-targets their edges.
    baseline, obligation_redirects = obligation_redirect.resolve_obligation_redirects(
        snapshot_graph, baseline, emitter=emitter
    )

    snapshot_merge_writer.persist_role_and_requirement_passthrough(
        snapshot_graph,
        baseline.regulatory_instrument_id,
        baseline.regulatory_instrument_properties,
        baseline.role_nodes,
        baseline.requirement_nodes,
        baseline.provenance_edges,
    )
    snapshot_merge_writer.persist_obligation_passthrough(snapshot_graph, baseline.obligation_nodes)
    snapshot_merge_writer.persist_canonical_nodes(
        snapshot_graph, baseline.capability_nodes, dedup_result.resolutions, kind="Capability"
    )
    canonical_id_by_incoming_id = {
        resolution.incoming_id: resolution.canonical_id for resolution in dedup_result.resolutions
    }
    canonical_id_by_incoming_id.update(obligation_redirects)

    # issue #183 -- the draft-governance import: Policy/Standard/Control written `draft`,
    # owned by the restoring caller, never converged (replaces issue #54 S6/B6's
    # Policy convergence pass for restore). A structural no-op for an external-sourced
    # restore (no governance content).
    governance_counts = governance_count_fields(
        import_draft_governance(
            baseline,
            single_tenant_graph=snapshot_graph,
            governance=governance,
            canonical_id_by_incoming_id=canonical_id_by_incoming_id,
            write_drafts=snapshot_merge_writer.persist_governance_drafts,
        )
    )

    # issue #106 -- PracticeArea/RiskPath node passthrough, a structural
    # no-op for a baseline with no classification-layer content. Runs after
    # Capability dedup and the governance import complete and before the edge rewiring
    # call below, mirroring merge.py's own live-path placement exactly: no
    # dedup, no embedding call (AC-BI-006) -- identity convergence is
    # structural (content-hashed ids minted upstream by `internal_seed`).
    snapshot_merge_writer.persist_practice_area_and_risk_path_passthrough(
        snapshot_graph, baseline.practice_area_nodes, baseline.risk_path_nodes
    )
    snapshot_merge_writer.validate_classification_edge_endpoints(
        snapshot_graph, baseline.classification_edges, canonical_id_by_incoming_id
    )

    # One rewiring call over the regulatory-spine edges, the governance
    # edges, and the classification edges -- governance_edges/
    # classification_edges are empty for an external-sourced restore, so
    # this is unchanged from before S6/#106 in that case.
    snapshot_merge_writer.persist_rewired_edges(
        snapshot_graph,
        baseline.bare_edges + baseline.governance_edges + baseline.classification_edges,
        canonical_id_by_incoming_id,
    )
    snapshot_merge_writer.backfill_canonical_embeddings(
        snapshot_graph, kind="Capability", embeddings=dedup_result.embedding_backfills
    )

    return {**snapshot_merge_writer.classification_write_counts(baseline), **governance_counts}
