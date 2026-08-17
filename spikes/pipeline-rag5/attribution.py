#!/usr/bin/env python3
"""AC-4 (α/β/γ attribution): diff policy_system_cra vs policy_system_graphrag_final_full.

Loads pruned-*.jsonl sidecar, classify each missing element from A vs B,
and emits a verdict paragraph.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from falkordb import FalkorDB

from compare import edge_counts, node_counts


SPIKE_DIR = Path(__file__).resolve().parent
LOG_DIR = SPIKE_DIR / "logs"

# Governance labels the SDK cannot produce by design (CRA-only spike)
GOVERNANCE_LABELS = {"Policy", "Standard", "Control", "RiskPath", "PracticeArea"}

# Graph names
BASELINE_NAME = "policy_system_cra"
CANDIDATE_NAME = "policy_system_graphrag_final_full"

# Audit reason classification (per FLAW-007)
RECOVERABLE_PRUNE_REASONS = {"dangling"}
NON_RECOVERABLE_PRUNE_REASONS = {"label_undeclared", "rel_type_undeclared", "pattern_mismatch"}


def load_pruned_audit(log_dir: Path) -> list[dict]:
    """Load the most recent pruned-*.jsonl sidecar."""
    pattern = "pruned-*.jsonl"
    candidates = list(log_dir.glob(pattern))
    if not candidates:
        raise RuntimeError(f"No audit file found matching {log_dir / pattern}")
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    entries: list[dict] = []
    with latest.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                entries.append(json.loads(line))
    return entries


def _get_pruned_ids(audit: list[dict]) -> tuple[set[str], set[str]]:
    """Return (pruned_nodes_ids, pruned_rels_keys) from audit."""
    nodes = set()
    rels = set()
    for e in audit:
        if e.get("stage") == "prune" and e.get("kind") == "node":
            nid = e.get("id")
            if nid:
                nodes.add(nid)
        elif e.get("stage") == "prune" and e.get("kind") == "relationship":
            start = e.get("start")
            end = e.get("end")
            rel_type = e.get("rel_type")
            if start and end and rel_type:
                rels.add((start, end, rel_type))
    return nodes, rels


def _get_audit_entry_for_node(audit: list[dict], node_id: str) -> dict | None:
    """Return the first audit entry for this node in prune stage."""
    for e in audit:
        if e.get("stage") == "prune" and e.get("kind") == "node" and e.get("id") == node_id:
            return e
    return None


def _get_audit_entry_for_rel(audit: list[dict], start: str, end: str, rel_type: str) -> dict | None:
    """Return the first audit entry for this relationship in prune stage."""
    for e in audit:
        if (
            e.get("stage") == "prune"
            and e.get("kind") == "relationship"
            and e.get("start") == start
            and e.get("end") == end
            and e.get("rel_type") == rel_type
        ):
            return e
    return None


def _query_nodes_with_label(graph, label: str) -> list[dict]:
    """Return all nodes with this domain label (handles __Entity__ wrapper)."""
    query = f"""
    MATCH (n)
    WHERE CASE WHEN '__Entity__' IN labels(n) THEN n.type ELSE labels(n)[0] END = '{label}'
    RETURN n.id AS id, n.name AS name
    """
    result = graph.query(query)
    return [{"id": row[0], "name": row[1]} for row in result.result_set]


def _query_edges_with_type(graph, edge_type: str) -> list[dict]:
    """Return all edges with this domain edge type (handles RELATES wrapper)."""
    query = f"""
    MATCH ()-[r]->()
    WHERE CASE WHEN type(r) = 'RELATES' THEN r.rel_type ELSE type(r) END = '{edge_type}'
    RETURN id(r) AS internal_id, startNode(r).id AS src_id, endNode(r).id AS tgt_id
    """
    result = graph.query(query)
    return [{"internal_id": row[0], "src_id": row[1], "tgt_id": row[2]} for row in result.result_set]


def run_attribution(db: FalkorDB, baseline_name: str, candidate_name: str, log_dir: Path) -> dict[str, Any]:
    """Perform α/β/γ attribution and return results."""
    baseline = db.select_graph(baseline_name)
    candidate = db.select_graph(candidate_name)

    # Load counts per label/type
    baseline_nodes = node_counts(baseline)
    candidate_nodes = node_counts(candidate)
    baseline_edges = edge_counts(baseline)
    candidate_edges = edge_counts(candidate)

    # Load pruned audit sidecar
    audit = load_pruned_audit(log_dir)
    pruned_node_ids, pruned_rel_keys = _get_pruned_ids(audit)

    # Compute gaps
    gap_by_label: dict[str, dict[str, int]] = {}
    gap_by_edge_type: dict[str, dict[str, int]] = {}

    for label in baseline_nodes:
        gap = baseline_nodes[label] - candidate_nodes.get(label, 0)
        if gap > 0:
            gap_by_label[label] = {
                "baseline": baseline_nodes[label],
                "candidate": candidate_nodes.get(label, 0),
                "gap": gap,
            }

    for edge_type in baseline_edges:
        gap = baseline_edges[edge_type] - candidate_edges.get(edge_type, 0)
        if gap > 0:
            gap_by_edge_type[edge_type] = {
                "baseline": baseline_edges[edge_type],
                "candidate": candidate_edges.get(edge_type, 0),
                "gap": gap,
            }

    # Classify missing nodes (baseline has, candidate missing)
    alpha_missing: dict[str, int] = {}
    beta_missing: dict[str, int] = {}
    gamma_missing: dict[str, int] = {}

    for label, gap_info in gap_by_label.items():
        # If governance label → γ by definition (SDK cannot model it)
        if label in GOVERNANCE_LABELS:
            gamma_missing[label] = gap_info["gap"]
            continue

        # Get baseline node IDs for this label
        baseline_node_data = _query_nodes_with_label(baseline, label)
        candidate_node_data = _query_nodes_with_label(candidate, label)

        baseline_ids = {n["id"] for n in baseline_node_data}
        candidate_ids = {n["id"] for n in candidate_node_data}
        missing_ids = baseline_ids - candidate_ids

        # Classify missing nodes by audit data
        alpha_count = 0
        beta_count = 0

        for nid in missing_ids:
            entry = _get_audit_entry_for_node(audit, nid)
            if entry is not None:
                # Was pruned; reason determines β vs α
                reason = entry.get("reason")
                if reason in RECOVERABLE_PRUNE_REASONS:
                    beta_count += 1
                else:
                    alpha_count += 1
            else:
                # Not in audit → never extracted (α)
                alpha_count += 1

        alpha_missing[label] = alpha_count
        beta_missing[label] = beta_count

    # Edge attribution is approximate (no node IDs in audit for edges)
    alpha_edge_missing: dict[str, int] = {}
    beta_edge_missing: dict[str, int] = {}
    gamma_edge_missing: dict[str, int] = {}

    for edge_type, gap_info in gap_by_edge_type.items():
        # If governance label → γ by definition
        if edge_type in GOVERNANCE_LABELS:
            gamma_edge_missing[edge_type] = gap_info["gap"]
            continue

        # Get baseline edge data
        baseline_edges_data = _query_edges_with_type(baseline, edge_type)
        candidate_edges_data = _query_edges_with_type(candidate, edge_type)

        # Use internal IDs as proxy (not reliable across graphs, but best we can)
        baseline_eids = {e["internal_id"] for e in baseline_edges_data}
        candidate_eids = {e["internal_id"] for e in candidate_edges_data}
        missing_eids = baseline_eids - candidate_eids

        # Estimate: assume pruned edges (known reasons) → β, others → α
        # Note: this is approximate (no reliable way to get node IDs per edge in audit sidecar)
        edge_gap = gap_info["gap"]
        pruned_rels_for_type = [
            (s, t, rt)
            for (s, t, rt) in pruned_rel_keys
            if _get_audit_entry_for_rel(audit, s, t, rt) is not None
            and _get_audit_entry_for_rel(audit, s, t, rt).get("rel_type") == edge_type
        ]
        beta_estimate = min(len(pruned_rels_for_type), edge_gap)
        alpha_estimate = edge_gap - beta_estimate

        alpha_edge_missing[edge_type] = alpha_estimate
        beta_edge_missing[edge_type] = beta_estimate

    # Determine dominant mode
    total_alpha = sum(alpha_missing.values()) + sum(alpha_edge_missing.values())
    total_beta = sum(beta_missing.values()) + sum(beta_edge_missing.values())
    total_gamma = sum(gamma_missing.values()) + sum(gamma_edge_missing.values())
    total = total_alpha + total_beta + total_gamma

    if total == 0:
        verdict = "No gaps detected between the two graphs (all elements present in candidate)."
    elif total_beta > total_alpha and total_beta > total_gamma:
        verdict = (
            "SDK is a viable extractor: the gap is dominated by β (recoverable by pipeline config). "
            f"Found {total_beta} recoverable gaps vs {total_alpha} extractor-limited vs {total_gamma} governance-layer gaps."
        )
    elif total_alpha > total_beta and total_alpha > total_gamma:
        verdict = (
            "SDK is not a better extractor: the gap is dominated by α (extractor limitation). "
            f"Found {total_alpha} extractor-limited gaps vs {total_beta} recoverable vs {total_gamma} governance-layer gaps."
        )
    elif total_gamma > total_alpha and total_gamma > total_beta:
        verdict = (
            "SDK is not a drop-in for the governance layer: the gap is dominated by γ (structural ontology gap). "
            f"Found {total_gamma} governance-layer gaps vs {total_alpha} extractor-limited vs {total_beta} recoverable."
        )
    else:
        verdict = (
            "Attribution is mixed; no single mode dominates. "
            f"α={total_alpha}, β={total_beta}, γ={total_gamma}. "
            "Examine gap_by_label and gap_by_edge_type for details."
        )

    # Build result
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "baseline_graph": baseline_name,
        "candidate_graph": candidate_name,
        "baseline_node_counts": baseline_nodes,
        "candidate_node_counts": candidate_nodes,
        "baseline_edge_counts": baseline_edges,
        "candidate_edge_counts": candidate_edges,
        "gap_by_label": gap_by_label,
        "gap_by_edge_type": gap_by_edge_type,
        "pruned_reasons": {
            "pruned_node_count": len(pruned_node_ids),
            "pruned_rel_count": len(pruned_rel_keys),
        },
        "alpha": alpha_missing,
        "beta": beta_missing,
        "gamma": gamma_missing,
        "alpha_edges": alpha_edge_missing,
        "beta_edges": beta_edge_missing,
        "gamma_edges": gamma_edge_missing,
        "verdict": verdict,
    }

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AC-4 attribution: compare policy_system_cra vs policy_system_graphrag_final_full"
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--baseline-graph", default=BASELINE_NAME, help="Reference graph name")
    parser.add_argument("--candidate-graph", default=CANDIDATE_NAME, help="Candidate graph name")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output path (default: logs/attribution-<ts>.jsonl)",
    )
    parser.add_argument(
        "--audit-dir",
        type=str,
        default=None,
        help="Audit sidecar directory (default: logs/)",
    )
    args = parser.parse_args()

    # Use CLI args (mutable locals, no need for global)
    log_dir = Path(args.audit_dir) if args.audit_dir else LOG_DIR

    db = FalkorDB(host=args.host, port=args.port)

    # Run attribution
    result = run_attribution(db, args.baseline_graph, args.candidate_graph, log_dir)

    # Write output
    if args.output:
        out_path = Path(args.output)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_path = log_dir / f"attribution-{ts}.jsonl"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result) + "\n")

    print(f"Attribution complete. Results written to {out_path}")
    print(f"\nVerdict:\n{result['verdict']}")


if __name__ == "__main__":
    main()
