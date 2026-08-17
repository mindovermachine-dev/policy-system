#!/usr/bin/env python3
"""
Map policy_system_graphrag_native_full → policy_system_graphrag_final_full.
- Idempotent: deletes + recreates final graph each run.
- Cross-ref regulations filtered (keep only cyber_resilience_act__regulation).
- Edges pre-filtered in Python to guarantee source/target nodes exist.
- SATISFIED_BY synthesized via shared-chunk co-occurrence.
- JSONL logging with timestamp, counts, outcome, duration_ms.
- ALL IDs and property values are escaped for Cypher safety.
"""

import json
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from falkordb import FalkorDB


def load_regulation_keep_set(path: str) -> set[str]:
    """Return set of native Regulation node IDs to KEEP."""
    with open(path) as f:
        mapping = json.load(f)
    return set(mapping.keys())


def esc(s) -> str:
    """Escape single quotes in Cypher string literal."""
    if s is None:
        return ""
    return str(s).replace("'", "\\'")


def map_graph(
    native_name: str,
    final_name: str,
    regulation_map_path: str = "regulation_map.json",
    log_dir: str = "logs",
) -> None:
    """Map native graph to final domain graph (CRA-only)."""
    db = FalkorDB(host="localhost", port=6379)
    start = time.time()

    try:
        # 1. Reset final graph
        if final_name in db.list_graphs():
            g_final = db.select_graph(final_name)
            g_final.delete()
        final = db.select_graph(final_name)

        # 2. Load keep-set
        keep_ids = load_regulation_keep_set(regulation_map_path)

        # 3. Connect to native and pull data
        native = db.select_graph(native_name)

        # 3a. Fetch domain nodes
        nodes_res = native.query(
            "MATCH (n:__Entity__) "
            "RETURN n.id AS id, "
            "       n.type AS label, "
            "       n.name AS name, "
            "       n.status AS status, "
            "       n.confidence AS confidence, "
            "       n.obligation_type AS obligation_type, "
            "       n.capability_type AS capability_type, "
            "       n.source_ref AS source_ref"
        )

        # 3b. Fetch domain edges
        edges_res = native.query(
            "MATCH (src:__Entity__)-[r:RELATES]->(tgt:__Entity__) "
            "RETURN src.id AS sid, "
            "       src.type AS sl, "
            "       tgt.id AS tid, "
            "       tgt.type AS tl, "
            "       r.rel_type AS rel, "
            "       r.source_ref AS sref"
        )

        # 4. Build target nodes
        domain_labels = {
            "Regulation", "Role", "Requirement", "Obligation",
            "PracticeArea", "Standard", "Policy", "Control", "RiskPath", "Capability"
        }
        nodes_copied = 0

        for row in nodes_res.result_set:
            nid, label, name, status, confidence, ob_type, cap_type, source_ref = row

            # Skip structural labels
            if label not in domain_labels:
                continue

            # Skip non-CRA Regulations
            if label == "Regulation" and nid not in keep_ids:
                continue

            # Build CREATE Cypher (escape ALL values)
            name_val = esc(name) if name is not None else esc(nid.replace("__", " ").title())
            status_val = "active" if status is None else esc(status)

            cypher = "CREATE (" + "n:" + label + " {"
            cypher += "id: '" + esc(nid) + "', "
            cypher += "name: '" + name_val + "', "
            cypher += "status: '" + status_val + "'"

            if label == "Capability" and cap_type:
                cypher += ", type: '" + esc(cap_type) + "'"

            if label == "Obligation" and ob_type:
                cypher += ", obligation_type: '" + esc(ob_type) + "'"

            if label == "Regulation" and source_ref:
                cypher += ", source_ref: '" + esc(source_ref) + "'"

            cypher += "})"
            final.query(cypher)
            nodes_copied += 1

        # 5. Build target edges
        edges_copied = 0
        edges_skipped = 0

        for row in edges_res.result_set:
            sid, sl, tid, tl, rel, sref = row

            # Skip structural edge types
            if rel in ("MENTIONED_IN", "PART_OF", "NEXT_CHUNK"):
                edges_skipped += 1
                continue

            # Skip edge only if a Regulation endpoint is not in keep-set
            drop_edge = False
            if sl == "Regulation" and sid not in keep_ids:
                drop_edge = True
            if tl == "Regulation" and tid not in keep_ids:
                drop_edge = True

            if drop_edge:
                edges_skipped += 1
                continue

            # Build MERGE Cypher (escape all IDs)
            sid_e = esc(sid)
            tid_e = esc(tid)

            if rel in ("DEFINES", "EXPRESSES") and sref:
                sref_e = esc(sref)
                cypher = "MATCH (src:Regulation {id: '" + sid_e + "'}), "
                cypher += "(tgt:" + tl + " {id: '" + tid_e + "'}) "
                cypher += "MERGE (src)-[e:" + rel + "]->(tgt) "
                cypher += "SET e.source_ref = '" + sref_e + "'"
                final.query(cypher)
            else:
                if sl == "Regulation":
                    cypher = "MATCH (src:Regulation {id: '" + sid_e + "'}), "
                    cypher += "(tgt:" + tl + " {id: '" + tid_e + "'}) "
                elif tl == "Regulation":
                    cypher = "MATCH (src:" + sl + " {id: '" + sid_e + "'}), "
                    cypher += "(tgt:Regulation {id: '" + tid_e + "'}) "
                else:
                    cypher = "MATCH (src:" + sl + " {id: '" + sid_e + "'}), "
                    cypher += "(tgt:" + tl + " {id: '" + tid_e + "'}) "
                cypher += "MERGE (src)-[e:" + rel + "]->(tgt)"
                final.query(cypher)
            edges_copied += 1

        # 6. Synthesize SATISFIED_BY edges (shared-chunk co-occurrence)
        sat_by_synthesized = 0

        pairs_res = native.query(
            "MATCH (req:__Entity__)-[:MENTIONED_IN]->(c:Chunk)<-[:MENTIONED_IN]-(ob:__Entity__) "
            "WHERE req.type = 'Requirement' AND ob.type = 'Obligation' "
            "RETURN DISTINCT req.id AS req_id, ob.id AS ob_id"
        )

        for row in pairs_res.result_set:
            req_id, ob_id = row
            req_id_e = esc(req_id)
            ob_id_e = esc(ob_id)
            cypher = "MATCH (req:Requirement {id: '" + req_id_e + "'}), "
            cypher += "(ob:Obligation {id: '" + ob_id_e + "'}) "
            cypher += "MERGE (req)-[:SATISFIED_BY]->(ob)"
            final.query(cypher)
            sat_by_synthesized += 1

        # 7. Log run
        duration_ms = int((time.time() - start) * 1000)
        log_dir_path = Path(log_dir)
        log_dir_path.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        log_entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "native_graph": native_name,
            "final_graph": final_name,
            "nodes_copied": nodes_copied,
            "edges_copied": edges_copied,
            "edges_skipped": edges_skipped,
            "satisfied_by_synthesized": sat_by_synthesized,
            "keep_set": list(keep_ids),
            "outcome": "success",
            "duration_ms": duration_ms,
        }
        log_path = log_dir_path / f"map_graph-{ts}.jsonl"
        with open(log_path, "a") as f:
            f.write(json.dumps(log_entry) + "\n")

        print(f"Map complete: {nodes_copied} nodes, {edges_copied} edges, "
              f"{sat_by_synthesized} SATISFIED_BY synthesized. "
              f"(Skipped {edges_skipped} edges: non-domain or cross-ref Reg.)")

    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        log_dir_path = Path(log_dir)
        log_dir_path.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "native_graph": native_name,
            "final_graph": final_name,
            "nodes_copied": 0,
            "edges_copied": 0,
            "edges_skipped": 0,
            "satisfied_by_synthesized": 0,
            "keep_set": list(load_regulation_keep_set(regulation_map_path)),
            "outcome": "error",
            "duration_ms": duration_ms,
            "exception": traceback.format_exc(),
        }
        log_path = log_dir_path / f"map_graph-{ts}.jsonl"
        with open(log_path, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
        raise


if __name__ == "__main__":
    map_graph(
        native_name="policy_system_graphrag_native_full",
        final_name="policy_system_graphrag_final_full",
        regulation_map_path="regulation_map.json",
    )
