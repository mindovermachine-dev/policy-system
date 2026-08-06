#!/usr/bin/env python3
"""Expand helvex_source.json (intermediate authoring format) into the same
{graph_name, nodes, edges} shape as cra.json/nis2.json/gdpr.json, and load it
into FalkorDB alongside them -- adding the fictional Helvex Biotech ApS
Policy/Standard/Control layer (and one internal Regulation) on top of the
real regulation graph. See synthetic-data-spec.md for the design rationale.

Why an intermediate format at all: hand-typing content-derived hash ids
(cap_{slug}_{hash}, pol_{slug}_{hash}, ...) and wiring every edge by id is
exactly the kind of thing that invites silent typos in a hand-authored
fixture. helvex_source.json instead references capabilities/roles/policies/
standards by short human-readable 'key's; this script resolves those to
ids computed per docs/artifacts/ps-domain-concepts.md's identity scheme,
and validates every cross-reference (including existing capability ids
copied from the real regulation files) instead of silently producing a
dangling edge.

Reuses graph-ingestion3/load_graph.py's upsert_node/upsert_edge/print_summary
directly rather than re-implementing the same generic MERGE logic here --
that loader is already fully generic over label/edge type, so nothing about
loading a Policy/Standard/Control layer requires new Cypher.

This is a SPIKE -- technology test, not production code.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

QUERY1_DIR = Path(__file__).parent
GRAPH_INGESTION3_DIR = QUERY1_DIR.parent / "graph-ingestion3"
sys.path.insert(0, str(GRAPH_INGESTION3_DIR))
from load_graph import load_graph, print_summary  # noqa: E402

DEFAULT_SOURCE_FILE = QUERY1_DIR / "helvex_source.json"
DEFAULT_OUTPUT_FILE = QUERY1_DIR / "helvex.json"
DEFAULT_CAPABILITY_SOURCES = [
    GRAPH_INGESTION3_DIR / "cra.json",
    GRAPH_INGESTION3_DIR / "nis2.json",
    GRAPH_INGESTION3_DIR / "gdpr.json",
]


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return re.sub(r"_+", "_", slug)


def content_hash(*parts: str) -> str:
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:6]


def load_capability_registry(paths: list[Path]) -> dict:
    """id -> name, for every real Capability already extracted from a regulation file."""
    registry = {}
    for path in paths:
        with open(path) as f:
            data = json.load(f)
        for node in data["nodes"]:
            if node["label"] == "Capability":
                registry[node["id"]] = node["properties"].get("name", node["id"])
    return registry


def build(source: dict, capability_registry: dict) -> dict:
    nodes = []
    edges = []

    def add_node(label, node_id, properties):
        nodes.append({"label": label, "id": node_id, "properties": {"id": node_id, **properties}})

    def add_edge(edge_type, from_label, from_id, to_label, to_id, properties=None):
        edges.append({
            "type": edge_type,
            "from": {"label": from_label, "id": from_id},
            "to": {"label": to_label, "id": to_id},
            "properties": properties or {},
        })

    # -- Regulations --
    for reg in source["regulations"]:
        add_node("Regulation", reg["id"], {
            "title": reg["title"],
            "source_type": reg["source_type"],
            "effective_date": reg["effective_date"],
            "version": reg["version"],
            "status": reg["status"],
        })
    for reg in source["regulations"]:
        if reg.get("superseded_by"):
            add_edge("SUPERSEDED_BY", "Regulation", reg["id"], "Regulation", reg["superseded_by"])

    # -- Roles --
    role_ids = {}
    for role in source["roles"]:
        role_id = f"role_{slugify(role['name'])}_{content_hash(role['name'], role['defined_by'])}"
        role_ids[role["key"]] = role_id
        add_node("Role", role_id, {"name": role["name"], "description": role["description"]})
        add_edge("DEFINES", "Regulation", role["defined_by"], "Role", role_id,
                  {"source_ref": role["source_ref"]})

    # -- New capabilities (minted here; real ones referenced directly by id) --
    capability_ids = {}  # key -> id, for capabilities_new only
    for cap in source["capabilities_new"]:
        cap_id = f"cap_{slugify(cap['name'])}_{content_hash(cap['name'])}"
        capability_ids[cap["key"]] = cap_id
        add_node("Capability", cap_id, {
            "name": cap["name"], "description": cap["description"],
            "type": cap["type"], "status": cap["status"],
        })

    def resolve_capability(ref: str) -> str:
        if ref in capability_ids:
            return capability_ids[ref]
        if ref in capability_registry:
            return ref
        raise ValueError(
            f"Unknown capability reference '{ref}' -- not a capabilities_new key "
            f"and not a real Capability id found in {[p.name for p in DEFAULT_CAPABILITY_SOURCES]}"
        )

    # -- Obligations --
    obligation_ids = {}  # key -> id
    for obl in source["obligations"]:
        obl_id = f"obl_{slugify(obl['text'])}_{content_hash(obl['text'])}"
        obligation_ids[obl["key"]] = obl_id
        add_node("Obligation", obl_id, {
            "text": obl["text"], "confidence": obl["confidence"],
            "obligation_type": obl["obligation_type"],
        })
        role_id = role_ids[obl["role"]]
        add_edge("HAS", "Role", role_id, "Obligation", obl_id)
        for cap_ref in obl["requires"]:
            add_edge("REQUIRES", "Obligation", obl_id, "Capability", resolve_capability(cap_ref))

    # -- Requirements --
    for req in source["requirements"]:
        req_id = f"{req['regulation']}_req_art_{req['article']}"
        add_node("Requirement", req_id, {"text": req["text"], "type": req["type"], "status": "active"})
        add_edge("EXPRESSES", "Regulation", req["regulation"], "Requirement", req_id,
                  {"source_ref": req["source_ref"]})
        for obl_key in req["satisfies"]:
            add_edge("SATISFIED_BY", "Requirement", req_id, "Obligation", obligation_ids[obl_key])

    # -- Policies --
    policy_ids = {}  # key -> id
    for pol in source["policies"]:
        pol_id = f"pol_{slugify(pol['title'])}_{content_hash(pol['title'])}"
        policy_ids[pol["key"]] = pol_id
        add_node("Policy", pol_id, {
            "title": pol["title"], "description": pol["description"],
            "owner_id": pol["owner_id"], "status": pol["status"], "version": pol["version"],
        })
        for cap_ref in pol["governs"]:
            add_edge("GOVERNED_BY", "Capability", resolve_capability(cap_ref), "Policy", pol_id)

    # -- Standards --
    standard_ids = {}  # key -> id
    for std in source["standards"]:
        pol_id = policy_ids[std["policy"]]
        std_id = f"std_{pol_id}_{std['slot']}"
        standard_ids[std["key"]] = std_id
        add_node("Standard", std_id, {
            "title": std["title"], "description": std["description"],
            "implementation_status": std["implementation_status"], "version": std["version"],
        })
        add_edge("SUPPORTED_BY", "Policy", pol_id, "Standard", std_id)

    # -- Controls --
    for ctrl in source["controls"]:
        std_id = standard_ids[ctrl["standard"]]
        ctrl_id = f"ctrl_{std_id}_{ctrl['type']}"
        properties = {
            "type": ctrl["type"], "title": ctrl["title"], "description": ctrl["description"],
            "implementation_status": ctrl["implementation_status"],
        }
        for optional in ("execution_frequency", "last_test_date", "next_review_date", "evidence_ref"):
            if ctrl.get(optional) is not None:
                properties[optional] = ctrl[optional]
        add_node("Control", ctrl_id, properties)
        add_edge("IMPLEMENTED_BY", "Standard", std_id, "Control", ctrl_id)

    return {"graph_name": source["graph_name"], "nodes": nodes, "edges": edges}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_FILE,
                         help=f"Intermediate authoring JSON (default: {DEFAULT_SOURCE_FILE.name})")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE,
                         help=f"Where to write the expanded graph JSON (default: {DEFAULT_OUTPUT_FILE.name})")
    parser.add_argument("--capability-source", type=Path, action="append",
                         help="Regulation file(s) to resolve real Capability ids against "
                              "(default: cra.json, nis2.json, gdpr.json in graph-ingestion3)")
    parser.add_argument("--no-load", action="store_true",
                         help="Only write the expanded JSON; skip loading into FalkorDB")
    parser.add_argument("--host", default="localhost", help="FalkorDB host")
    parser.add_argument("--port", type=int, default=6379, help="FalkorDB port")
    parser.add_argument("--graph-name", default=None,
                         help="Override the graph name (default: graph_name from the source JSON)")
    args = parser.parse_args()

    capability_sources = args.capability_source or DEFAULT_CAPABILITY_SOURCES
    with open(args.source) as f:
        source = json.load(f)

    capability_registry = load_capability_registry(capability_sources)
    print(f"Loaded {len(capability_registry)} real Capability ids from "
          f"{[p.name for p in capability_sources]} for reference validation.")

    try:
        expanded = build(source, capability_registry)
    except ValueError as e:
        print(f"Error expanding {args.source.name}: {e}", file=sys.stderr)
        return 1

    with open(args.output, "w") as f:
        json.dump(expanded, f, indent=2)
    print(f"Wrote {len(expanded['nodes'])} nodes and {len(expanded['edges'])} edges to {args.output}")

    if args.no_load:
        return 0

    graph_name = args.graph_name or expanded["graph_name"]
    try:
        from falkordb import FalkorDB
        db = FalkorDB(host=args.host, port=args.port)
        graph = db.select_graph(graph_name)
        graph.query("RETURN 1")
    except Exception as e:
        print(
            f"FalkorDB connection failed at {args.host}:{args.port}. Is FalkorDB running? "
            f"Error: {e}\n({args.output} was still written -- load it manually with "
            f"graph-ingestion3/load_graph.py once FalkorDB is up.)",
            file=sys.stderr,
        )
        return 1

    print(f"Loading into graph '{graph_name}' at {args.host}:{args.port}...")
    load_graph(graph, expanded)
    print_summary(graph, expanded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
