#!/usr/bin/env python3
"""Load the Policy System domain graph (policy_system_graph.json) into FalkorDB.

Generic over the JSON's node/edge shape -- it does not hardcode per-concept
insert functions the way earlier spikes did. Each node is addressed by
(label, id) and MERGEd on that key; each edge is MERGEd between the two
endpoint nodes it names. This mirrors docs/artifacts/ps-domain-concepts.md
directly: node properties in the JSON come from each concept's Properties
table, edge properties come from its Relationships table (e.g. `source_ref`
on DEFINES/EXPRESSES), so the loader needs no per-label logic to place a
fact on the right side of a node/edge boundary -- the JSON already encodes it.

Requires a running FalkorDB instance, e.g.:
    podman run --rm -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
"""

import argparse
import json
import sys
from pathlib import Path

from falkordb import FalkorDB

DEFAULT_DATA_FILE = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "test-data"
    / "eu-regulations"
    / "policy_system_graph.json"
)


def load_data(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def upsert_node(graph, node: dict) -> None:
    label = node["label"]
    node_id = node["id"]
    properties = node["properties"]

    query = f"""
    MERGE (n:{label} {{id: $id}})
    SET n += $properties
    """
    graph.query(query, params={"id": node_id, "properties": properties})


def upsert_edge(graph, edge: dict) -> None:
    edge_type = edge["type"]
    from_label, from_id = edge["from"]["label"], edge["from"]["id"]
    to_label, to_id = edge["to"]["label"], edge["to"]["id"]
    properties = edge.get("properties") or {}

    query = f"""
    MATCH (a:{from_label} {{id: $from_id}}), (b:{to_label} {{id: $to_id}})
    MERGE (a)-[r:{edge_type}]->(b)
    SET r += $properties
    """
    graph.query(
        query,
        params={"from_id": from_id, "to_id": to_id, "properties": properties},
    )


def load_graph(graph, data: dict) -> None:
    for node in data["nodes"]:
        upsert_node(graph, node)
    for edge in data["edges"]:
        upsert_edge(graph, edge)


def print_summary(graph, data: dict) -> None:
    node_labels = sorted({n["label"] for n in data["nodes"]})
    edge_types = sorted({e["type"] for e in data["edges"]})

    print("\nNode counts:")
    for label in node_labels:
        result = graph.query(f"MATCH (n:{label}) RETURN count(n)")
        print(f"  {label}: {result.result_set[0][0]}")

    print("\nEdge counts:")
    for edge_type in edge_types:
        result = graph.query(f"MATCH ()-[r:{edge_type}]->() RETURN count(r)")
        print(f"  {edge_type}: {result.result_set[0][0]}")

    print("\nConvergence check (Capabilities required by >1 Obligation):")
    result = graph.query(
        """
        MATCH (o:Obligation)-[:REQUIRES]->(c:Capability)
        WITH c, count(o) AS obligation_count
        WHERE obligation_count > 1
        RETURN c.name, obligation_count
        """
    )
    if result.result_set:
        for name, count in result.result_set:
            print(f"  {name}: required by {count} Obligations")
    else:
        print("  none")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file", type=Path, default=DEFAULT_DATA_FILE,
        help=f"Path to the graph JSON file (default: {DEFAULT_DATA_FILE.name})",
    )
    parser.add_argument("--host", default="localhost", help="FalkorDB host")
    parser.add_argument("--port", type=int, default=6379, help="FalkorDB port")
    parser.add_argument(
        "--graph-name", default=None,
        help="Override the graph name (default: graph_name from the JSON file)",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Delete the graph before loading (destructive)",
    )
    args = parser.parse_args()

    data = load_data(args.file)
    graph_name = args.graph_name or data["graph_name"]

    try:
        db = FalkorDB(host=args.host, port=args.port)
        graph = db.select_graph(graph_name)
        graph.query("RETURN 1")
    except Exception as e:
        print(
            f"FalkorDB connection failed at {args.host}:{args.port}. "
            f"Is FalkorDB running? Error: {e}",
            file=sys.stderr,
        )
        return 1

    if args.reset:
        print(f"Resetting graph '{graph_name}'...")
        try:
            graph.delete()
        except Exception:
            pass  # graph did not exist yet

    print(f"Loading {len(data['nodes'])} nodes and {len(data['edges'])} edges "
          f"into graph '{graph_name}' at {args.host}:{args.port}...")
    load_graph(graph, data)

    print_summary(graph, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
