#!/usr/bin/env python3
"""AC-3: Structural comparison — CRA-vs-CRA.

Compares policy_system_cra (reference, 188n/335e) against
policy_system_graphrag_final_full (candidate, from GraphRAG-SDK).
Reports per-label + per-edge-type counts and RATIOS.
Checks DEFECT-1, unknown labels, convergence. No cross-ref gate.
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


def node_counts(graph) -> dict:
    """Count nodes per domain label, handling both __Entity__ and plain shapes."""
    result = graph.query(
        "MATCH (n) RETURN "
        "CASE WHEN '__Entity__' IN labels(n) THEN n.type ELSE labels(n)[0] "
        "END AS label, count(n) AS n"
    )
    return {row[0]: row[1] for row in result.result_set}


def edge_counts(graph) -> dict:
    """Count edges per domain type, handling both RELATES and plain shapes."""
    result = graph.query(
        "MATCH ()-[r]->() RETURN "
        "CASE WHEN type(r) = 'RELATES' THEN r.rel_type ELSE type(r) "
        "END AS t, count(r) AS n"
    )
    return {row[0]: row[1] for row in result.result_set}


def capability_convergence(graph) -> list:
    """CAPs required by OBLs of >1 distinct Regulation."""
    query = (
        "MATCH (reg)-[e1]->(req)-[e2]->(ob)-[e3]->(cap)\n"
        "WHERE CASE WHEN '__Entity__' IN labels(reg) THEN reg.type ELSE labels(reg)[0] END = 'Regulation'\n"
        "  AND CASE WHEN '__Entity__' IN labels(req) THEN req.type ELSE labels(req)[0] END = 'Requirement'\n"
        "  AND CASE WHEN '__Entity__' IN labels(ob) THEN ob.type ELSE labels(ob)[0] END = 'Obligation'\n"
        "  AND CASE WHEN '__Entity__' IN labels(cap) THEN cap.type ELSE labels(cap)[0] END = 'Capability'\n"
        "  AND CASE WHEN type(e1) = 'RELATES' THEN e1.rel_type ELSE type(e1) END = 'EXPRESSES'\n"
        "  AND CASE WHEN type(e2) = 'RELATES' THEN e2.rel_type ELSE type(e2) END = 'SATISFIED_BY'\n"
        "  AND CASE WHEN type(e3) = 'RELATES' THEN e3.rel_type ELSE type(e3) END = 'REQUIRES'\n"
        "WITH cap, collect(DISTINCT coalesce(reg.id, reg.name)) AS regs\n"
        "WHERE size(regs) > 1\n"
        "RETURN cap.name AS name, size(regs) AS n_regulations\n"
        "ORDER BY n_regulations DESC"
    )
    result = graph.query(query)
    return [(row[0], row[1]) for row in result.result_set]


def fmt_ratio(cand: int, base: int) -> str:
    """Format candidate/baseline ratio."""
    if base == 0:
        return "inf" if cand > 0 else "0.00"
    return f"{cand / base:.2f}"


def report(db, graph_name, label, baseline_counts=None):
    """Print node/edge counts and ratios, return raw counts."""
    print(f"\n=== {label}: graph '{graph_name}' ===")
    graph = db.select_graph(graph_name)
    nodes = node_counts(graph)
    edges = edge_counts(graph)

    print("Node counts:")
    for lbl in sorted(EXPECTED_LABELS):
        count = nodes.get(lbl, 0)
        if baseline_counts and "nodes" in baseline_counts:
            base = baseline_counts["nodes"].get(lbl, 0)
            print(f"     {lbl:14s} {count:3d}  (baseline {base:3d}, ratio {fmt_ratio(count, base)})")
        else:
            print(f"     {lbl:14s} {count}")
    unexpected = sorted(set(nodes) - EXPECTED_LABELS)
    if unexpected:
        print(f"  UNEXPECTED LABELS: {unexpected}")

    print("Edge counts:")
    for et in sorted(EXPECTED_EDGE_TYPES):
        count = edges.get(et, 0)
        if baseline_counts and "edges" in baseline_counts:
            base = baseline_counts["edges"].get(et, 0)
            print(f"     {et:14s} {count:3d}  (baseline {base:3d}, ratio {fmt_ratio(count, base)})")
        else:
            print(f"     {et:14s} {count}")
    unexpected_e = sorted(set(edges) - EXPECTED_EDGE_TYPES)
    if unexpected_e:
        print(f"  UNEXPECTED EDGE TYPES: {unexpected_e}")

    # Governance flag
    gov_labels = ["Policy", "Standard", "Control", "RiskPath"]
    gov_absent = all(nodes.get(l, 0) == 0 for l in gov_labels)
    if gov_absent:
        print("  WARN: GOVERNANCE LAYER = 0 (Policy/Standard/Control/RiskPath)")
    else:
        present = {l: nodes.get(l, 0) for l in gov_labels if nodes.get(l, 0) > 0}
        print(f"  WARN: GOVERNANCE LAYER present: {present}")

    # Convergence
    try:
        converged = capability_convergence(graph)
        print(f"Capabilities converged >1 Reg: {len(converged)}")
        for name, n in converged:
            print(f"     {name!r} <- {n} regs")
    except Exception as e:
        converged = None
        print(f"cap_convergence FAILED: {e}")

    return {"nodes": nodes, "edges": edges, "conv_ok": converged is not None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--baseline-graph", default="policy_system_cra")
    parser.add_argument("--graphrag-graph", default="policy_system_graphrag_final_full")
    args = parser.parse_args()

    db = FalkorDB(host=args.host, port=args.port)

    # Baseline
    baseline = report(db, args.baseline_graph, "BASELINE (reference)")
    # Candidate with ratios
    candidate = report(db, args.graphrag_graph, "CANDIDATE (GraphRAG-SDK)", baseline)

    # RATIOS
    cn = candidate["nodes"]
    bn = baseline["nodes"]
    ce = candidate["edges"]
    be = baseline["edges"]

    print("\n=== RATIOS (candidate / baseline) ===")
    print("Nodes:")
    for lbl in sorted(EXPECTED_LABELS):
        c = cn.get(lbl, 0)
        b = bn.get(lbl, 0)
        print(f"     {lbl:14s} {c:5d} / {b:5d} = {fmt_ratio(c, b)}")
    print("Edges:")
    for et in sorted(EXPECTED_EDGE_TYPES):
        c = ce.get(et, 0)
        b = be.get(et, 0)
        print(f"     {et:14s} {c:5d} / {b:5d} = {fmt_ratio(c, b)}")

    # AC-3 VERDICT
    print("\n=== AC-3 VERDICT ===")

    total = sum(cn.get(l, 0) for l in EXPECTED_LABELS)
    domain_ok = total >= 80
    print(f"Domain entities: {total} (>80) {'PASS' if domain_ok else 'FAIL'}")

    core = {
        "HAS": ce.get("HAS", 0),
        "REQUIRES": ce.get("REQUIRES", 0),
        "SATISFIED_BY": ce.get("SATISFIED_BY", 0),
    }
    core_ok = all(v > 0 for v in core.values())
    has_e = ce.get("HAS", 0)
    req_e = ce.get("REQUIRES", 0)
    sat_e = ce.get("SATISFIED_BY", 0)
    core = {"HAS": has_e, "REQUIRES": req_e, "SATISFIED_BY": sat_e}

    expr = ce.get("EXPRESSES", 0)
    print(f"  EXPRESSES={expr} (REPORTED, not graded)")

    unknown = sorted(set(cn) - EXPECTED_LABELS)
    unknown_ok = len(unknown) == 0
    print(f"Unknown labels: {unknown} {'PASS' if unknown_ok else 'FAIL'}")

    # DEFECT-1
    graph = db.select_graph(args.graphrag_graph)
    r = graph.query(
        "MATCH (n:Capability) WHERE n.type = 'Capability' OR n.type IS NULL "
        "RETURN count(*) AS c"
    )
    defect1 = r.result_set[0][0]
    defect1_ok = defect1 == 0
    print(f"DEFECT-1 (Capability type collision): {defect1} {'PASS' if defect1_ok else 'FAIL'}")

    # Convergence
    conv_ok = candidate["conv_ok"]
    print(f"cap_convergence ran: {'PASS' if conv_ok else 'FAIL'}")

    print("Cross-ref gate: SKIPPED (CRA-vs-CRA)")

    all_pass = domain_ok and core_ok and unknown_ok and defect1_ok and conv_ok
    print(f"\nFINAL AC-3: {'PASS' if all_pass else 'FAIL'}")


if __name__ == "__main__":
    main()
