"""Issue #28, slice 9 -- read-only live verification of AC-BI-007 (the second live
merge, slice 8) against the real `policy_system` graph.

`@pytest.mark.falkordb_live` ONLY -- no `llm_live`, zero writes. Every check here
either reads the two already-on-disk JSON reports slice 6/slice 8 (`run_live_merge.py`)
wrote (`live_merge_report_20260913T125204Z.json` = run 1,
`live_merge_report_20260913T144013Z.json` = run 2), or issues plain read-only Cypher
(`MATCH`/`OPTIONAL MATCH`/`RETURN`, never `MERGE`/`SET`/`DELETE`) against the real,
already-twice-merged `policy_system` graph.

**Why this file does NOT do PLAN.md §5.2's originally-designed byte-identical
edge-count comparison, and why that is correct, not a weakening:**

PLAN.md §5.2 originally called for comparing run 2's live-read
`_snapshot_counts`/`_snapshot_embeddings` against run 1's `after` snapshot for exact
equality across the board, mirroring #16's disposable-graph two-pass proof
(`test_live_capstone.py`). Node counts and DEFINES/EXPRESSES/HAS/SATISFIED_BY edge
counts DID come back byte-identical between the two real reports -- but `REQUIRES`
legitimately grew (6893 -> 6903, +10) on the real re-run, and a byte-identical
comparison would fail on that growth despite it not being a duplication bug.

`.orchestrator/tracker/issue-28-live-merge-verification/INVESTIGATION_REQUIRES_DELTA.md`
(read in full before touching this file again) root-caused the +10 exhaustively:
`graph_writer.persist_rewired_edges` writes every bare edge via
`MATCH (s), (t) MERGE (s)-[:TYPE]->(t)` -- a `MERGE` on the FULL
`(source_id, type, target_id)` triple, making a literal duplicate edge between the
same two nodes structurally impossible at the FalkorDB engine level. All 10 new
`REQUIRES` pairs are genuinely distinct `(source_id, target_id)` combinations, never
seen before either run, and none of run 1's edges were lost. The mechanism is a
confirmed, deterministic, order-dependent property of `dedupe_canonical_nodes`'s
in-run working-index growth (not LLM/embedding non-determinism, not a duplicate, not
a lost edge): a second run's working index already contains every canonical node
minted during the first run, so an incoming id can legitimately resolve onto a
better-scoring candidate that simply did not exist yet, in the index, during the
first run. This is real but NOT what AC-BI-007 ("no duplicate canonical nodes or
edges are created") is actually testing on its literal wording -- the user has
explicitly accepted AC-BI-007 as MET on that literal wording given this
understanding, and the deeper non-strict-idempotency question (should the merge be
strictly idempotent on re-run?) is tracked separately as issue #88. This file tests
AC-BI-007's literal wording correctly instead of re-litigating #88 here:

1. `test_ac_bi_007_run_two_node_counts_match_before_and_after` -- run 2's own
   before/after node counts are identical (hard assertion; this part of the
   original design was correct and DID hold).
2. `test_ac_bi_007_no_duplicate_parallel_edges_for_any_relationship_type` -- the real
   substance of "no duplicate ... edges": a LIVE, current-state query proving no
   `(source_id, target_id)` pair appears more than once for any relationship type,
   for every relationship type, regardless of how many distinct pairs exist.
3. `test_ac_bi_007_edges_grow_additively_never_lost_between_runs` -- explains, rather
   than hides, the REQUIRES growth: every pair present in run 1's `after` is still
   present in run 2's `after` (monotonic growth only, nothing lost) -- named
   specifically to distinguish "no loss" (this test) from "no duplication" (test #2
   above), since they are different properties and this file must not conflate them.
4. `test_ac_bi_007_capability_embeddings_stable_across_both_runs` -- Capability
   embeddings present after run 1 are unchanged after run 2 (unaffected by the
   REQUIRES finding; this part of the original design also holds as intended).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import cast

import pytest

from ps_service.company_merge.falkordb_client import (
    GraphHandle,
    connect_from_config,
    select_graph,
)
from ps_service.config import load_config

_REAL_SINGLE_TENANT_GRAPH_NAME = "policy_system"
_EDGE_TYPES = ("DEFINES", "EXPRESSES", "HAS", "SATISFIED_BY", "REQUIRES")

_TRACKER_DIR = (
    Path(__file__).resolve().parents[3]
    / ".orchestrator"
    / "tracker"
    / "issue-28-live-merge-verification"
)
_REPORT_PATH_RUN_1 = _TRACKER_DIR / "live_merge_report_20260913T125204Z.json"
_REPORT_PATH_RUN_2 = _TRACKER_DIR / "live_merge_report_20260913T144013Z.json"


def _load_report(path: Path) -> dict[str, object]:
    return cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))


def _before_after(report: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    return cast("dict[str, object]", report["before"]), cast("dict[str, object]", report["after"])


def _query_rows(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query, params=params).result_set)


def _connect_single_tenant_graph() -> GraphHandle:
    config = load_config()
    db = connect_from_config(config)
    return select_graph(db, _REAL_SINGLE_TENANT_GRAPH_NAME)


# --- AC-BI-007 -----------------------------------------------------------------------


@pytest.mark.falkordb_live
def test_ac_bi_007_run_two_node_counts_match_before_and_after() -> None:
    """Hard assertion: run 2's own before/after node counts, per label, are exactly
    equal -- proving the re-run created no duplicate canonical NODE for
    RegulatoryInstrument/Role/Requirement/Obligation/Capability. This is the part of
    PLAN.md §5.2's original design that is still correct as originally stated.
    """
    report = _load_report(_REPORT_PATH_RUN_2)
    before, after = _before_after(report)
    before_node_counts = cast("dict[str, int]", before["node_counts"])
    after_node_counts = cast("dict[str, int]", after["node_counts"])

    assert before_node_counts == after_node_counts, (
        "AC-BI-007: run 2's before/after node counts differ -- the re-run created "
        f"or removed canonical node(s): before={before_node_counts}, after={after_node_counts}"
    )


@pytest.mark.falkordb_live
@pytest.mark.parametrize("relationship_type", _EDGE_TYPES)
def test_ac_bi_007_no_duplicate_parallel_edges_for_any_relationship_type(
    relationship_type: str,
) -> None:
    """The real substance of AC-BI-007's "no duplicate ... edges" wording: a LIVE,
    current-state query against the real, now-twice-merged `policy_system` graph,
    proving no `(source_id, target_id)` pair has more than one parallel edge of this
    relationship type between them. This holds regardless of whether new, distinct
    pairs were added across runs (REQUIRES legitimately has more distinct pairs now
    than after run 1 -- see module docstring) -- a true set of edges has no duplicate
    parallel edge between the same two nodes, which is exactly what this checks,
    live, right now.
    """
    single_tenant_graph = _connect_single_tenant_graph()
    rows = _query_rows(
        single_tenant_graph,
        f"MATCH (a)-[:{relationship_type}]->(b) RETURN a.id, b.id",
    )
    pairs = [(cast("str", row[0]), cast("str", row[1])) for row in rows]
    assert pairs, (
        f"expected at least one live {relationship_type} edge in {_REAL_SINGLE_TENANT_GRAPH_NAME}"
    )

    duplicated = {pair: count for pair, count in Counter(pairs).items() if count > 1}
    assert not duplicated, (
        f"AC-BI-007: {relationship_type} has duplicate parallel edge(s) for the same "
        f"(source_id, target_id) pair in {_REAL_SINGLE_TENANT_GRAPH_NAME}: {duplicated}"
    )


@pytest.mark.falkordb_live
def test_ac_bi_007_edges_grow_additively_never_lost_between_runs() -> None:
    """Distinct from the "no duplication" test above: this tests "no loss" -- every
    (source_id, target_id) pair present in run 1's `after.edge_ids` is still present
    in run 2's `after.edge_ids`. `edge_ids` only carries the three propertyless
    relationship types (HAS/SATISFIED_BY/REQUIRES -- see
    `_live_merge_assertions._snapshot_edge_ids`'s docstring: DEFINES/EXPRESSES carry
    a `source_ref` property instead and live in `edge_properties`, whose own
    before/after equality is already asserted by
    `test_live_policy_system_verification_pass1.py`'s
    `test_ac_bi_006_edge_properties_unchanged_for_defines_and_expresses`). REQUIRES
    legitimately grows (10 new pairs -- see module docstring and
    INVESTIGATION_REQUIRES_DELTA.md); HAS/SATISFIED_BY are expected to be identical
    sets. Either way, `run1_after subset-of run2_after` must hold for all three:
    monotonic growth only, nothing ever dropped by the re-run.
    """
    report_1 = _load_report(_REPORT_PATH_RUN_1)
    report_2 = _load_report(_REPORT_PATH_RUN_2)
    _before_1, after_1 = _before_after(report_1)
    _before_2, after_2 = _before_after(report_2)
    run1_after_edge_ids = cast("dict[str, list[list[str]]]", after_1["edge_ids"])
    run2_after_edge_ids = cast("dict[str, list[list[str]]]", after_2["edge_ids"])

    lost_by_relationship_type: dict[str, list[tuple[str, str]]] = {}
    growth_by_relationship_type: dict[str, int] = {}
    for relationship_type, pairs in run1_after_edge_ids.items():
        run1_pairs = {(pair[0], pair[1]) for pair in pairs}
        run2_pairs = {(pair[0], pair[1]) for pair in run2_after_edge_ids[relationship_type]}
        lost = sorted(run1_pairs - run2_pairs)
        if lost:
            lost_by_relationship_type[relationship_type] = lost
        growth_by_relationship_type[relationship_type] = len(run2_pairs) - len(run1_pairs)

    assert not lost_by_relationship_type, (
        "AC-BI-007: edge pair(s) present after run 1 are missing after run 2 (a real "
        f"loss, not the known additive-only REQUIRES growth): {lost_by_relationship_type}"
    )
    # Documented, understood growth (INVESTIGATION_REQUIRES_DELTA.md): REQUIRES gains
    # exactly 10 new pairs; HAS/SATISFIED_BY (the other two edge_ids-tracked
    # relationship types) are unchanged. A different shape here means the known
    # finding has drifted and needs re-investigation, not silent re-acceptance.
    assert growth_by_relationship_type == {
        "HAS": 0,
        "SATISFIED_BY": 0,
        "REQUIRES": 10,
    }, (
        "AC-BI-007: edge-count growth between run 1's after and run 2's after no "
        "longer matches the documented, investigated finding (REQUIRES +10, all "
        f"others +0) -- got {growth_by_relationship_type}; see "
        "INVESTIGATION_REQUIRES_DELTA.md and issue #88 before changing this assertion"
    )


@pytest.mark.falkordb_live
def test_ac_bi_007_capability_embeddings_stable_across_both_runs() -> None:
    """Embedding stability, proved without needing a raw run-1 embedding snapshot
    (neither report stores one -- `_snapshot_node_properties` strips `embedding` from
    `Capability` at CAPTURE time by design, see its docstring, so there is no
    byte-for-byte run-1 embedding value on disk to diff against).

    What IS on disk and decisive: PLAN.md section 0.3 confirms all 563 pre-existing
    Capability nodes already carried a non-null `embedding` before run 1 ever
    started, and `graph_writer.backfill_canonical_embeddings` only ever writes an
    embedding under a `WHERE n.embedding IS NULL` guard (never overwrites a present
    one). `test_ac_bi_007_run_two_node_counts_match_before_and_after` above already
    proves run 2 minted zero new Capability nodes. Combined, this means run 2's
    backfill step had zero eligible (null-embedding) targets to act on at all --
    so if every live Capability node right now has a non-null embedding, and every
    Capability id known after run 1 still exists live, no embedding could have been
    added, removed, or changed by run 2. This is proved live, right now, against the
    real, twice-merged graph.
    """
    report_1 = _load_report(_REPORT_PATH_RUN_1)
    _before_1, after_1 = _before_after(report_1)
    run1_capability_ids = cast(
        "list[str]", cast("dict[str, object]", after_1["node_ids"])["Capability"]
    )
    assert run1_capability_ids, "expected at least one Capability id in run 1's after snapshot"

    single_tenant_graph = _connect_single_tenant_graph()
    rows = _query_rows(
        single_tenant_graph,
        "MATCH (n:Capability) RETURN n.id, n.embedding",
    )
    live_embeddings: dict[str, tuple[float, ...] | None] = {
        cast("str", node_id): (
            tuple(cast("list[float]", embedding)) if embedding is not None else None
        )
        for node_id, embedding in rows
    }

    missing = sorted(set(run1_capability_ids) - set(live_embeddings))
    assert not missing, (
        f"AC-BI-007: Capability id(s) present after run 1 no longer exist live: {missing}"
    )

    null_embeddings = sorted(
        node_id for node_id, embedding in live_embeddings.items() if embedding is None
    )
    assert not null_embeddings, (
        "AC-BI-007: Capability node(s) with a null embedding found live -- combined "
        "with zero new Capability nodes minted by run 2 (see node-count test above), "
        "this would mean run 2's backfill either skipped a pre-existing gap or "
        "something else changed embedding state: "
        f"{null_embeddings[:10]}{'...' if len(null_embeddings) > 10 else ''}"
    )
