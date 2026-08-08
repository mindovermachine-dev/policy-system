#!/usr/bin/env python3
"""Compute a golden answer through an INDEPENDENT path.

This is the only sanctioned way to produce a golden answer. It exists to
enforce the M7/H1 lesson: a golden answer authored by running (or
hand-mimicking) the candidate query mechanism can inherit that mechanism's
bugs -- concretely, FalkorDB's projection-dependent row-dropping on 5+ hop
chains, which made M7's and H1's original golden counts wrong until an
independent Python-side join caught them.

Contract for every computation here:
  1. Hand-written Cypher, written FOR THIS PURPOSE, not copied from a
     mechanism's internals.
  2. For any chain of 5+ hops, project ALL matched node ids (or wrap in
     RETURN DISTINCT over all of them) -- the M7 mitigation.
  3. Cross-check long-chain aggregate counts against an independently
     computed Python-side join of each hop's edges, pulled separately.
     If the Cypher row count and the join disagree, the answer is WRONG
     until the discrepancy is understood -- do not ship the Cypher number.

This is a scaffold. It wires the graph connection and the cross-check
skeleton; each question's independent computation is filled in when that
answer is (re)derived. Questions flagged independent_recompute_needed in
the dev set (M7, H1) are the first priority.

Usage:
    python3 compute_answer.py <QUESTION_ID>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "query1"))

from falkordb import FalkorDB  # noqa: E402

GRAPH_NAME = "policy_system"


def connect():
    db = FalkorDB(host="localhost", port=6379)
    return db.select_graph(GRAPH_NAME)


def cross_check_chain(g, cypher: str, join_check) -> list[list]:
    """Run hand-written Cypher AND an independent Python-side join; refuse to
    return a count the two disagree on. `join_check` is a callable taking the
    graph and returning the same row set computed hop-by-hop in Python.

    The disagreement-is-an-error stance is the whole point: silently trusting
    the Cypher number is exactly the failure this function exists to catch.
    """
    cypher_rows = g.query(cypher).result_set
    join_rows = join_check(g)
    if len(cypher_rows) != len(join_rows):
        raise AssertionError(
            f"CROSS-CHECK FAILED: cypher returned {len(cypher_rows)} rows, "
            f"independent join returned {len(join_rows)}. Do not ship either "
            "number until the discrepancy is understood (see M7's FalkorDB "
            "projection bug in query1/golden-answers.md)."
        )
    return cypher_rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question_id", help="e.g. M7, H1")
    args = ap.parse_args()

    connect()  # validates the graph is reachable; per-question compute is filled in per answer
    print(f"compute_answer scaffold: no independent computation is registered "
          f"for {args.question_id} yet.")
    print("Add a hand-written Cypher + Python-join cross-check for this "
          "question here before deriving its golden answer.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
