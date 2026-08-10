#!/usr/bin/env python3
"""Shortlist candidate duplicate Capability nodes for human review.

Regulation extraction runs independently per regulation, so the same
underlying capacity (e.g. "Vulnerability Management") can get minted as
distinct Capability nodes with different ids/phrasing across regulations.
This does not try to auto-merge them -- that's a judgment call for someone
with domain knowledge (see merge_capabilities.py) -- it just narrows a full
N^2 comparison down to the pairs actually worth looking at.

Scoring is corpus-relative TF-IDF cosine similarity over name+description
tokens, not a fixed stopword list: words nearly every Capability in this
graph shares (e.g. "technical", "capacity", "security") get automatically
down-weighted, so the signal self-calibrates as more regulations are added
rather than being tuned to any one regulation's phrasing.

Results are reported as ranked pairs, not transitively-merged clusters --
single-linkage clustering here would chain unrelated capabilities together
through weak intermediate matches (verified empirically: it happily grouped
"Security Risk Assessment", "Vulnerability Management", "Secure
Configuration Management" and "Incident & Vulnerability Reporting" into one
"cluster" on nothing but shared generic words). A human reviewing a flat
ranked list can still merge a 3-way duplicate manually if all its pairs show
up near the top.

Usage:
    python tools/graph-ingestion/find_capability_duplicates.py --graph-name policy_system
"""

import argparse
import math
import re
import sys
from collections import Counter
from itertools import combinations

from falkordb import FalkorDB

STOPWORDS = {"and", "the", "of", "a", "an", "to", "for", "in", "on", "&", "or", "is", "as"}


def tokens(cap: dict) -> set:
    text = f"{cap['name']} {cap.get('description', '')}"
    words = re.sub(r"[^a-z0-9\s]", " ", text.lower()).split()
    return {w for w in words if w not in STOPWORDS}


def build_idf(cap_tokens: dict) -> dict:
    n = len(cap_tokens)
    df = Counter()
    for toks in cap_tokens.values():
        df.update(toks)
    return {word: math.log((n + 1) / (count + 1)) + 1 for word, count in df.items()}


def cosine(toks_a: set, toks_b: set, idf: dict) -> float:
    if not toks_a or not toks_b:
        return 0.0
    weights_a = {w: idf[w] for w in toks_a}
    weights_b = {w: idf[w] for w in toks_b}
    shared = toks_a & toks_b
    dot = sum(weights_a[w] * weights_b[w] for w in shared)
    norm_a = math.sqrt(sum(v * v for v in weights_a.values()))
    norm_b = math.sqrt(sum(v * v for v in weights_b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def fetch_capabilities(graph) -> list:
    result = graph.query("MATCH (n:Capability) RETURN n")
    return [row[0].properties for row in result.result_set]


def fetch_source_regulations(graph, cap_id: str) -> list:
    result = graph.query(
        """
        MATCH (reg:Regulation)-[:DEFINES]->(:Role)-[:HAS]->(:Obligation)-[:REQUIRES]->(c:Capability {id: $cap_id})
        RETURN DISTINCT reg.id
        """,
        params={"cap_id": cap_id},
    )
    return [row[0] for row in result.result_set]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--graph-name", default="policy_system")
    parser.add_argument(
        "--threshold", type=float, default=0.35,
        help="Minimum TF-IDF cosine similarity [0-1] to report a pair (default: 0.35)",
    )
    parser.add_argument(
        "--top", type=int, default=30,
        help="Max number of pairs to print (default: 30)",
    )
    args = parser.parse_args()

    db = FalkorDB(host=args.host, port=args.port)
    graph = db.select_graph(args.graph_name)

    capabilities = fetch_capabilities(graph)
    if len(capabilities) < 2:
        print(f"Only {len(capabilities)} Capability node(s) in '{args.graph_name}' -- nothing to compare.")
        return 0

    by_id = {c["id"]: c for c in capabilities}
    cap_tokens = {c["id"]: tokens(c) for c in capabilities}
    idf = build_idf(cap_tokens)

    scored_pairs = []
    for a, b in combinations(capabilities, 2):
        score = cosine(cap_tokens[a["id"]], cap_tokens[b["id"]], idf)
        if score >= args.threshold:
            scored_pairs.append((score, a["id"], b["id"]))

    scored_pairs.sort(reverse=True)

    print(f"Graph: {args.graph_name}  |  {len(capabilities)} Capability nodes  |  threshold: {args.threshold}")
    print(f"{len(scored_pairs)} candidate pair(s) found\n")

    if not scored_pairs:
        print("No candidate duplicates above threshold. Try lowering --threshold if you expect more.")
        return 0

    reg_cache = {}

    def regs_for(cap_id):
        if cap_id not in reg_cache:
            reg_cache[cap_id] = fetch_source_regulations(graph, cap_id)
        return reg_cache[cap_id]

    for score, a_id, b_id in scored_pairs[: args.top]:
        cap_a, cap_b = by_id[a_id], by_id[b_id]
        print(f"--- score {score:.2f} ---")
        for cap_id, cap in ((a_id, cap_a), (b_id, cap_b)):
            regs = regs_for(cap_id)
            reg_str = ", ".join(regs) if regs else "(no regulation path found)"
            print(f"  [{cap_id}]")
            print(f"    name:        {cap['name']}")
            print(f"    description: {cap.get('description', '')}")
            print(f"    regulations: {reg_str}")
        print()

    if len(scored_pairs) > args.top:
        print(f"... {len(scored_pairs) - args.top} more pair(s) below the --top cutoff\n")

    print("Review each pair above and record merge decisions in a decisions file, e.g.:")
    print('  [{"keep": "<id to keep>", "drop": ["<id(s) to retire>"], "note": "why"}]')
    print("Then apply with: python merge_capabilities.py --decisions capability_merges.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
