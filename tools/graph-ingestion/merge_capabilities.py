#!/usr/bin/env python3
"""Apply human-reviewed Capability merge decisions to a FalkorDB graph.

Reads a decisions file (see capability_merges.json) produced after reviewing
find_capability_duplicates.py's output, and for each {keep, drop} pair:
rewires every edge (any type, any direction -- REQUIRES today, GOVERNED_BY /
future Policy-Standard-Control edges tomorrow) touching a "drop" node onto
the "keep" node, records the retired id(s) on the kept node's `merged_from`
property for traceability, then deletes the drop node.

The decisions file is the durable, version-controlled record of these
judgment calls -- re-running this script against a freshly reloaded graph
(from load_graph.py) reproduces the same merged state, so it's safe to keep
appending to it as more regulations are loaded.

Usage (a single command line, wrapped here for width):
    python tools/graph-ingestion/merge_capabilities.py
      --decisions docs/test-data/eu-regulations/capability_merges.json
      --graph-name policy_system
"""

from __future__ import annotations

import argparse
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

    class _FalkorDB(Protocol):
        def select_graph(self, name: str) -> _Graph: ...


def load_decisions(path: Path) -> list[dict[str, Any]]:
    with path.open() as f:
        return json.load(f)


def node_exists(graph: _Graph, cap_id: str) -> bool:
    result = graph.query("MATCH (n:Capability {id: $id}) RETURN n.id", params={"id": cap_id})
    return bool(result.result_set)


def rewire_and_delete(graph: _Graph, keep_id: str, drop_id: str) -> None:
    outgoing = graph.query(
        "MATCH (d:Capability {id: $id})-[r]->(o) RETURN type(r), labels(o)[0], o.id, properties(r)",
        params={"id": drop_id},
    ).result_set
    for rel_type, other_label, other_id, props in outgoing:
        graph.query(
            f"""
            MATCH (k:Capability {{id: $keep_id}}), (o:{other_label} {{id: $other_id}})
            MERGE (k)-[r:{rel_type}]->(o)
            SET r += $props
            """,
            params={"keep_id": keep_id, "other_id": other_id, "props": props},
        )

    incoming = graph.query(
        "MATCH (o)-[r]->(d:Capability {id: $id}) RETURN type(r), labels(o)[0], o.id, properties(r)",
        params={"id": drop_id},
    ).result_set
    for rel_type, other_label, other_id, props in incoming:
        graph.query(
            f"""
            MATCH (o:{other_label} {{id: $other_id}}), (k:Capability {{id: $keep_id}})
            MERGE (o)-[r:{rel_type}]->(k)
            SET r += $props
            """,
            params={"other_id": other_id, "keep_id": keep_id, "props": props},
        )

    graph.query(
        """
        MATCH (k:Capability {id: $keep_id}), (d:Capability {id: $drop_id})
        SET k.merged_from = CASE
            WHEN k.merged_from IS NULL THEN [$drop_id]
            ELSE k.merged_from + [$drop_id]
        END
        WITH d
        DETACH DELETE d
        """,
        params={"keep_id": keep_id, "drop_id": drop_id},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--graph-name", default="policy_system")
    parser.add_argument(
        "--decisions",
        type=Path,
        required=True,
        help="Path to the merge decisions JSON file",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what would happen without writing"
    )
    args = parser.parse_args()

    decisions = load_decisions(args.decisions)

    db = cast("_FalkorDB", FalkorDB(host=args.host, port=args.port))
    graph = db.select_graph(args.graph_name)

    applied, skipped = 0, 0
    for decision in decisions:
        keep_id = decision["keep"]
        drop_ids = decision["drop"]
        note = decision.get("note", "")

        if not node_exists(graph, keep_id):
            print(f"SKIP: keep id '{keep_id}' not found in '{args.graph_name}' (note: {note})")
            skipped += len(drop_ids)
            continue

        for drop_id in drop_ids:
            if not node_exists(graph, drop_id):
                print(f"  already merged/missing, skipping: {drop_id} -> {keep_id}")
                continue
            if args.dry_run:
                print(f"  [dry-run] would merge {drop_id} -> {keep_id} ({note})")
                continue
            rewire_and_delete(graph, keep_id, drop_id)
            print(f"  merged {drop_id} -> {keep_id} ({note})")
            applied += 1

    print(f"\nApplied {applied} merge(s), skipped {skipped}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
