#!/usr/bin/env python3
"""One-off migration: seed `engprac_baseline`/`engprac_native` from the fixture (issue #66, D15).

D15's own rationale: AC-BI-003's internal curated content is produced by
hand-migrating `test-data/engineering-practices/engineering-practices-
seed.json` into today's real Role/Requirement/Obligation/Capability/Policy/
Standard/Control domain shapes (`docs/artifacts/ps-domain-concepts.md`) --
NOT by first building the not-yet-existing internal-source Ingestion/Domain
Mapping adapter pair. This script is a one-off, hand-mapped transform, not a
generic loader:

- Only nodes/edges belonging to the real compliance spine are kept. The
  fixture also carries `PracticeArea`/`RiskPath` nodes and their `COVERS`/
  `OWNS`/`MITIGATED_BY`/`VERIFIED_BY` edges -- organizational-classification
  concepts that sit outside the spine entirely (ps-domain-concepts.md's own
  Document Purpose section) and are not part of what Slice 3.6/D15 asks for
  ("today's real Role/Requirement/Obligation/Capability/Policy/Standard/
  Control shapes"). Dropping those four edge types automatically drops
  every edge touching a `PracticeArea`/`RiskPath` node; the nodes themselves
  are separately excluded from the allowed-label set.
- `Role`/`Requirement`/`Obligation`/`Capability` carry a required
  `confidence` property in the real domain model (the extracting LLM's own
  certainty) that this hand-authored fixture has no LLM run to source from.
  This migration is not an LLM extraction, so there is no genuine certainty
  score to record -- `confidence: 1.0` is written by fixed convention (full
  certainty, since a human directly authored the content), satisfying the
  required field without fabricating a fractional value. `Obligation`'s
  fixture-only `obligation_type` property (not part of the real Obligation
  schema -- see ps-domain-concepts.md's Obligation Properties table) is
  dropped, not carried through.
- The fixture's `GOVERNED_BY`/`COVERS`/`MITIGATED_BY`/`REQUIRES` edges
  reference several Capability ids that have no corresponding `Capability`
  node object in the fixture's own `nodes` array (a data-quality gap in the
  fixture, confirmed by inspection: 10 distinct Capability ids are
  referenced by edges, only 4 have an explicit node). Real Capability
  lifecycle already covers this ("minted when an Obligation requires a
  capability type that doesn't yet exist," ps-domain-concepts.md) -- this
  migration mints a minimal node (`id` + a title-cased `name` derived from
  the id's own slug + `confidence: 1.0`) for every edge-referenced
  Capability id missing a node, so no edge is silently dropped for want of
  an endpoint.
- `engprac_native` is written with only its `RegulatoryInstrument` node --
  the fixture carries no document-structural markup (`TITLE`/`CHAPTER`/
  `SECTION`/`ARTICLE`/...) to derive a native structural graph from, since
  D15 explicitly does not run the Ingestion pipeline. A single-node native
  graph is a legitimate, minimal native graph for an internal source with
  no formal document structure to extract from.

Requires a running FalkorDB instance, e.g.:
    podman run --rm -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from falkordb import FalkorDB

DEFAULT_HOST = os.environ.get("PS_FALKORDB_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("PS_FALKORDB_PORT", "6379"))

BASELINE_GRAPH_NAME = "engprac_baseline"
NATIVE_GRAPH_NAME = "engprac_native"

DEFAULT_SEED_FILE = (
    Path(__file__).resolve().parents[2]
    / "test-data"
    / "engineering-practices"
    / "engineering-practices-seed.json"
)

# The real compliance-spine shape (ps-domain-concepts.md) -- PracticeArea/RiskPath are
# organizational classification nodes outside the spine and are deliberately excluded.
_ALLOWED_NODE_LABELS = frozenset(
    {
        "RegulatoryInstrument",
        "Role",
        "Requirement",
        "Obligation",
        "Capability",
        "Policy",
        "Standard",
        "Control",
    }
)
_ALLOWED_RELATIONSHIP_TYPES = frozenset(
    {
        "DEFINES",
        "EXPRESSES",
        "HAS",
        "SATISFIED_BY",
        "REQUIRES",
        "GOVERNED_BY",
        "SUPPORTED_BY",
        "IMPLEMENTED_BY",
    }
)
# Node kinds that carry a required `confidence` property in the real domain model but
# that this hand-authored fixture (no LLM extraction run) has no genuine score for.
_LABELS_NEEDING_A_MINTED_CONFIDENCE = frozenset({"Role", "Requirement", "Obligation", "Capability"})
_MINTED_CONFIDENCE = 1.0
_CAPABILITY_LABEL = "Capability"
_CAPABILITY_ID_HASH_SUFFIX = re.compile(r"^[0-9a-f]{4,8}$")


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


def load_seed(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return cast("dict[str, Any]", json.load(f))


def _capability_name_from_id(capability_id: str) -> str:
    """Derive a human-readable name from a `cap_{slug}_{hash}` id.

    Used only to mint a `Capability` node for an edge-referenced id that has
    no explicit node object in the fixture (module docstring). Strips the
    `cap_` prefix and a trailing hex-hash segment, title-cases what's left.
    """
    segments = capability_id.removeprefix("cap_").split("_")
    if segments and _CAPABILITY_ID_HASH_SUFFIX.match(segments[-1]):
        segments = segments[:-1]
    return " ".join(segment.capitalize() for segment in segments)


def _spine_nodes(seed: dict[str, Any]) -> list[dict[str, Any]]:
    """Every fixture node on the real compliance spine, confidence-backfilled.

    Adds `confidence: 1.0` to Role/Requirement/Obligation/Capability nodes
    (module docstring) and drops Obligation's fixture-only, non-schema
    `obligation_type` property.
    """
    nodes: list[dict[str, Any]] = []
    for node in seed["nodes"]:
        if node["label"] not in _ALLOWED_NODE_LABELS:
            continue
        properties = dict(node["properties"])
        properties.pop("obligation_type", None)
        if node["label"] in _LABELS_NEEDING_A_MINTED_CONFIDENCE:
            properties.setdefault("confidence", _MINTED_CONFIDENCE)
        nodes.append({"label": node["label"], "id": node["id"], "properties": properties})
    return nodes


def _spine_edges(seed: dict[str, Any]) -> list[dict[str, Any]]:
    return [edge for edge in seed["edges"] if edge["type"] in _ALLOWED_RELATIONSHIP_TYPES]


def _minted_capability_nodes(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Mint a minimal `Capability` node for every edge-referenced id missing one.

    Module docstring's third point: the fixture's edges reference more
    Capability ids than it has explicit Capability node objects for.
    """
    existing_ids = {node["id"] for node in nodes if node["label"] == _CAPABILITY_LABEL}
    referenced_ids: set[str] = set()
    for edge in edges:
        if edge["from"]["label"] == _CAPABILITY_LABEL:
            referenced_ids.add(edge["from"]["id"])
        if edge["to"]["label"] == _CAPABILITY_LABEL:
            referenced_ids.add(edge["to"]["id"])
    missing_ids = sorted(referenced_ids - existing_ids)
    return [
        {
            "label": _CAPABILITY_LABEL,
            "id": capability_id,
            "properties": {
                "id": capability_id,
                "name": _capability_name_from_id(capability_id),
                "confidence": _MINTED_CONFIDENCE,
            },
        }
        for capability_id in missing_ids
    ]


def build_baseline_dataset(seed: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The filtered, confidence-backfilled, referentially-complete baseline dataset."""
    nodes = _spine_nodes(seed)
    edges = _spine_edges(seed)
    nodes.extend(_minted_capability_nodes(nodes, edges))
    return {"nodes": nodes, "edges": edges}


def build_native_dataset(seed: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The native dataset: only the `RegulatoryInstrument` node (module docstring)."""
    instrument_nodes = [n for n in seed["nodes"] if n["label"] == "RegulatoryInstrument"]
    return {"nodes": instrument_nodes, "edges": []}


def upsert_node(graph: _Graph, node: dict[str, Any]) -> None:
    label = node["label"]
    query = f"MERGE (n:{label} {{id: $id}}) SET n += $properties"
    graph.query(query, params={"id": node["id"], "properties": node["properties"]})


def upsert_edge(graph: _Graph, edge: dict[str, Any]) -> None:
    edge_type = edge["type"]
    from_label, from_id = edge["from"]["label"], edge["from"]["id"]
    to_label, to_id = edge["to"]["label"], edge["to"]["id"]
    properties: dict[str, Any] = edge.get("properties") or {}
    query = (
        f"MATCH (a:{from_label} {{id: $from_id}}), (b:{to_label} {{id: $to_id}}) "
        f"MERGE (a)-[r:{edge_type}]->(b) SET r += $properties"
    )
    graph.query(query, params={"from_id": from_id, "to_id": to_id, "properties": properties})


def load_dataset(graph: _Graph, dataset: dict[str, list[dict[str, Any]]]) -> None:
    for node in dataset["nodes"]:
        upsert_node(graph, node)
    for edge in dataset["edges"]:
        upsert_edge(graph, edge)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file", type=Path, default=DEFAULT_SEED_FILE, help="Path to the seed JSON"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="FalkorDB host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="FalkorDB port")
    args = parser.parse_args()

    seed = load_seed(args.file)
    baseline_dataset = build_baseline_dataset(seed)
    native_dataset = build_native_dataset(seed)

    try:
        db = cast("_FalkorDB", FalkorDB(host=args.host, port=args.port))
        db.select_graph(BASELINE_GRAPH_NAME).query("RETURN 1")
    except Exception as exc:  # noqa: BLE001 -- top-level connection guard: print a hint and exit non-zero
        print(
            f"FalkorDB connection failed at {args.host}:{args.port}. "
            f"Is FalkorDB running? Error: {exc}",
            file=sys.stderr,
        )
        return 1

    print(
        f"Loading {len(baseline_dataset['nodes'])} nodes and {len(baseline_dataset['edges'])} "
        f"edges into '{BASELINE_GRAPH_NAME}' at {args.host}:{args.port}..."
    )
    load_dataset(db.select_graph(BASELINE_GRAPH_NAME), baseline_dataset)

    print(f"Loading {len(native_dataset['nodes'])} node(s) into '{NATIVE_GRAPH_NAME}'...")
    load_dataset(db.select_graph(NATIVE_GRAPH_NAME), native_dataset)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
