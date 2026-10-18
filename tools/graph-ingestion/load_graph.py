#!/usr/bin/env python3
"""Load the Policy System domain graph (policy_system_graph.json) into FalkorDB.

Generic over the JSON's node/edge shape -- it needs no per-concept insert
functions. Each node is addressed by (label, id) and MERGEd on that key; each
edge is MERGEd between the two endpoint nodes it names. This mirrors
docs/artifacts/ps-domain-concepts.md directly: node properties in the JSON come
from each concept's Properties table, edge properties come from its
Relationships table (e.g. `source_ref` on DEFINES/EXPRESSES), so the
loader needs no per-label logic to place a fact on the right side of a node/edge
boundary -- the JSON already encodes it.

Options:
    --file        Graph JSON file to load (default:
                  test-data/eu-regulations/policy_system_graph.json).
    --host/--port FalkorDB location (default: env PS_FALKORDB_HOST / PS_FALKORDB_PORT,
                  else localhost:6379).
    --graph-name  Graph to load into (default: the `graph_name` field of the JSON file).
    --reset       Delete the target graph before loading (destructive).

After loading it prints node counts per label, edge counts per type and the
Capabilities required by more than one Obligation.

Exit codes: `0` after a successful load; `1` if FalkorDB cannot be reached (a message
goes to stderr).

Requires a running FalkorDB instance, e.g.:
    podman run --rm -d --name falkordb -p 6379:6379 falkordb/falkordb:v4.22.1
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from falkordb import FalkorDB

# Connection defaults, env-driven. In the dev container FalkorDB is a separate
# container reachable as `falkordb`, not localhost, so a hardcoded default would
# force a --host flag on every invocation. PS_FALKORDB_HOST/PS_FALKORDB_PORT are
# the same variables ps-service reads (see .env.example): one name repo-wide.
DEFAULT_HOST = os.environ.get("PS_FALKORDB_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("PS_FALKORDB_PORT", "6379"))


if TYPE_CHECKING:
    # falkordb ships no py.typed; these Protocols pin the slice of its surface
    # this script touches so the rest of the module stays precisely typed.
    class _QueryResult(Protocol):
        @property
        def result_set(self) -> list[list[Any]]: ...

    class _Graph(Protocol):
        def query(self, q: str, params: dict[str, Any] | None = None) -> _QueryResult: ...
        def delete(self) -> object: ...

    class _FalkorDB(Protocol):
        def select_graph(self, name: str) -> _Graph: ...


DEFAULT_DATA_FILE = (
    Path(__file__).resolve().parents[2]
    / "test-data"
    / "eu-regulations"
    / "policy_system_graph.json"
)


def load_data(path: Path) -> dict[str, Any]:
    """Read the graph JSON file.

    Args:
        path: Path to a JSON file with `graph_name`, `nodes` and `edges` keys.

    Returns:
        The parsed JSON object.

    Raises:
        OSError: If the file cannot be opened.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    with path.open() as f:
        return json.load(f)


def upsert_node(graph: _Graph, node: dict[str, Any]) -> None:
    """Create or update one node, keyed on its label and `id`.

    Args:
        graph: Target graph handle.
        node: Mapping with `label` (trusted to be a valid Cypher label, it is
            interpolated into the query), `id` and `properties`.

    Raises:
        KeyError: If `label`, `id` or `properties` is missing.
        Exception: Any error the FalkorDB client raises while running the query.

    Side Effects:
        Writes to the graph: merges the node and sets its properties.
    """
    label = node["label"]
    node_id = node["id"]
    properties = node["properties"]

    query = f"""
    MERGE (n:{label} {{id: $id}})
    SET n += $properties
    """
    graph.query(query, params={"id": node_id, "properties": properties})


def upsert_edge(graph: _Graph, edge: dict[str, Any]) -> None:
    """Create or update one edge between two existing nodes.

    Args:
        graph: Target graph handle.
        edge: Mapping with `type` (a Cypher relationship type, interpolated into the
            query), `from` and `to` (each with `label` and `id`), and optional
            `properties`.

    Raises:
        KeyError: If `type`, `from` or `to`, or their `label` or `id`, is missing.
        Exception: Any error the FalkorDB client raises while running the query.

    Side Effects:
        Writes to the graph: merges the edge and sets its properties. If either
        endpoint node does not exist the query matches nothing and no edge is created.
    """
    edge_type = edge["type"]
    from_label, from_id = edge["from"]["label"], edge["from"]["id"]
    to_label, to_id = edge["to"]["label"], edge["to"]["id"]
    properties: dict[str, Any] = edge.get("properties") or {}

    query = f"""
    MATCH (a:{from_label} {{id: $from_id}}), (b:{to_label} {{id: $to_id}})
    MERGE (a)-[r:{edge_type}]->(b)
    SET r += $properties
    """
    graph.query(
        query,
        params={"from_id": from_id, "to_id": to_id, "properties": properties},
    )


def load_graph(graph: _Graph, data: dict[str, Any]) -> None:
    """Load every node, then every edge, from the parsed JSON.

    Nodes go first so that each edge finds both of its endpoints.

    Args:
        graph: Target graph handle.
        data: Parsed graph JSON with `nodes` and `edges` lists.

    Raises:
        KeyError: If `nodes` or `edges` is missing, or an entry is malformed.
        Exception: Any error the FalkorDB client raises.

    Side Effects:
        Writes to the graph; safe to re-run because every write is a merge.
    """
    for node in data["nodes"]:
        upsert_node(graph, node)
    for edge in data["edges"]:
        upsert_edge(graph, edge)


def print_summary(graph: _Graph, data: dict[str, Any]) -> None:
    """Print node and edge counts and the Capability convergence check.

    Args:
        graph: Graph handle to count in.
        data: Parsed graph JSON; its labels and edge types decide which counts are
            printed.

    Raises:
        Exception: Any error the FalkorDB client raises while counting.

    Side Effects:
        Runs read-only queries and writes the summary to stdout.
    """
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
    """Parse arguments, connect to FalkorDB and load the graph file.

    Returns:
        `0` after loading and printing the summary; `1` if FalkorDB cannot be reached.

    Raises:
        SystemExit: Raised by argparse on invalid arguments or `--help`.
        OSError: If the graph JSON file cannot be read.
        json.JSONDecodeError: If the graph JSON file is not valid JSON.
        KeyError: If the JSON lacks `graph_name` (when `--graph-name` is not given),
            `nodes` or `edges`.

    Side Effects:
        With `--reset`, deletes the target graph first. Writes all nodes and edges to
        FalkorDB and prints progress to stdout and connection failures to stderr.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_DATA_FILE,
        help=f"Path to the graph JSON file (default: {DEFAULT_DATA_FILE.name})",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="FalkorDB host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="FalkorDB port")
    parser.add_argument(
        "--graph-name",
        default=None,
        help="Override the graph name (default: graph_name from the JSON file)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the graph before loading (destructive)",
    )
    args = parser.parse_args()

    data = load_data(args.file)
    graph_name = args.graph_name or data["graph_name"]

    try:
        db = cast("_FalkorDB", FalkorDB(host=args.host, port=args.port))
        graph = db.select_graph(graph_name)
        graph.query("RETURN 1")
    except Exception as e:  # noqa: BLE001 -- top-level connection guard: print a hint and exit non-zero
        print(
            f"FalkorDB connection failed at {args.host}:{args.port}. "
            f"Is FalkorDB running? Error: {e}",
            file=sys.stderr,
        )
        return 1

    if args.reset:
        print(f"Resetting graph '{graph_name}'...")
        # A delete of a graph that was never created raises from the client; that
        # is the expected no-op case here, so any error is safe to ignore.
        with contextlib.suppress(Exception):
            graph.delete()

    print(
        f"Loading {len(data['nodes'])} nodes and {len(data['edges'])} edges "
        f"into graph '{graph_name}' at {args.host}:{args.port}..."
    )
    load_graph(graph, data)

    print_summary(graph, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
