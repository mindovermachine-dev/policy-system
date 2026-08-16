# © 2026 Cartman ApS. All rights reserved.
"""Structural comparison between the current pipeline's graph (policy_system,
produced by tools/graph-ingestion) and this spike's CRA-native graph
(policy_system_graphrag_native, produced by ingest.py).

This covers ONLY the mechanizable comparison dimension (structural parity:
node/edge label vocabulary against ps-domain-concepts.md). Content fidelity
(spot-checking extracted Obligations/Capabilities against source text) is a
manual/qualitative step. Cross-regulation Capability convergence is not
checked here — this spike is CRA-only, so that dimension is not meaningful
until multiple regulations are loaded (see pipeline-rag4).

Usage (from repo root):
    python spikes/pipeline-rag3/compare.py
    python spikes/pipeline-rag3/compare.py --graphrag-graph policy_system_graphrag_native --baseline-graph policy_system
"""

import argparse

from falkordb import FalkorDB

EXPECTED_LABELS = {
    "Regulation", "Role", "Requirement", "Obligation", "PracticeArea",
    "RiskPath", "Capability", "Policy", "Standard", "Control",
}
EXPECTED_EDGE_TYPES = {
    "DEFINES", "EXPRESSES", "SUPERSEDED_BY", "HAS", "SATISFIED_BY",
    "REQUIRES", "COVERS", "OWNS", "MITIGATED_BY", "GOVERNED_BY",
    "SUPPORTED_BY", "IMPLEMENTED_BY", "VERIFIED_BY",
}


def node_counts(graph) -> dict[str, int]:
    # GraphRAG-SDK writes every extracted entity as (:<Type>:__Entity__) with
    # the type ALSO on an `n.type` property, but doesn't guarantee `<Type>`
    # is labels(n)[0] -- observed both `['Regulation', '__Entity__']` and
    # `['__Entity__', 'Role']` in the same graph. Gate on the `__Entity__`
    # marker specifically (not "does n.type exist") -- the baseline
    # pipeline's graph has no `__Entity__` label, but DOES use `type` as an
    # unrelated domain property on some node types (e.g. Capability.type =
    # "technical"/"organizational", Control.type = "manual"/"automated"),
    # which a bare `coalesce(n.type, labels(n)[0])` would wrongly prefer.
    result = graph.query(
        "MATCH (n) RETURN CASE WHEN '__Entity__' IN labels(n) THEN n.type "
        "ELSE labels(n)[0] END AS label, count(n) AS n"
    )
    return {row[0]: row[1] for row in result.result_set}


def edge_counts(graph) -> dict[str, int]:
    # Mirrors node_counts: GraphRAG-SDK writes every domain relationship as a
    # generic :RELATES edge with the real type on an `r.rel_type` property
    # (confirmed against graph_extraction.py's _relations_to_relationships,
    # which hardcodes type="RELATES"). Gate on type(r) = 'RELATES'
    # specifically, not "does r.rel_type exist", for the same reason as
    # node_counts above.
    result = graph.query(
        "MATCH ()-[r]->() RETURN CASE WHEN type(r) = 'RELATES' THEN r.rel_type "
        "ELSE type(r) END AS t, count(r) AS n"
    )
    return {row[0]: row[1] for row in result.result_set}


def report(db: FalkorDB, graph_name: str, label: str) -> None:
    print(f"\n=== {label}: graph '{graph_name}' ===")
    graph = db.select_graph(graph_name)

    nodes = node_counts(graph)
    edges = edge_counts(graph)

    print("Node counts:")
    for node_label in sorted(EXPECTED_LABELS):
        print(f"  {node_label:14s} {nodes.get(node_label, 0)}")
    unexpected_nodes = set(nodes) - EXPECTED_LABELS
    if unexpected_nodes:
        print(f"  UNEXPECTED LABELS (not in ps-domain-concepts.md): {sorted(unexpected_nodes)}")

    print("Edge counts:")
    for edge_type in sorted(EXPECTED_EDGE_TYPES):
        print(f"  {edge_type:14s} {edges.get(edge_type, 0)}")
    unexpected_edges = set(edges) - EXPECTED_EDGE_TYPES
    if unexpected_edges:
        print(f"  UNEXPECTED EDGE TYPES (not in ps-domain-concepts.md): {sorted(unexpected_edges)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--baseline-graph", default="policy_system", help="Current pipeline's graph (tools/graph-ingestion)")
    parser.add_argument("--graphrag-graph", default="policy_system_graphrag_native", help="This spike's graph (ingest.py)")
    args = parser.parse_args()

    db = FalkorDB(host=args.host, port=args.port)

    report(db, args.baseline_graph, "BASELINE (current pipeline)")
    report(db, args.graphrag_graph, "GRAPHRAG-SDK (this spike)")


if __name__ == "__main__":
    main()
