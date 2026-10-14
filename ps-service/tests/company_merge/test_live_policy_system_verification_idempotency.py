"""A second merge creates no duplicate canonical nodes or edges (hermetic).

`falkordb_live` + `postgres_live`, no LLM. The real `merge_baseline_graph` is run twice for the
same regulation into a uniquely named graph (`_hermetic_policy_system.merged_graph_run`) and the
two resulting snapshots are compared. Non-strict idempotency (edges may legitimately grow
additively on a re-run) is not asserted away: no loss and no duplication are.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from company_merge._hermetic_policy_system import MergedGraphRun


_EDGE_TYPES = ("DEFINES", "EXPRESSES", "HAS", "SATISFIED_BY", "REQUIRES")


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_ac_bi_007_run_two_node_counts_match_before_and_after(
    merged_graph_run: MergedGraphRun,
) -> None:
    first = cast("dict[str, int]", merged_graph_run.after_pass_1["node_counts"])
    second = cast("dict[str, int]", merged_graph_run.after_pass_2["node_counts"])
    assert first["Capability"] > 0
    assert first == second


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
@pytest.mark.parametrize("relationship_type", _EDGE_TYPES)
def test_ac_bi_007_no_duplicate_parallel_edges_for_any_relationship_type(
    merged_graph_run: MergedGraphRun, relationship_type: str
) -> None:
    rows = cast(
        "list[list[object]]",
        merged_graph_run.graph.query(
            f"MATCH (a)-[:{relationship_type}]->(b) RETURN a.id, b.id"
        ).result_set,
    )
    pairs = [(cast("str", r[0]), cast("str", r[1])) for r in rows]
    assert pairs, f"expected at least one live {relationship_type} edge"
    assert {p: n for p, n in Counter(pairs).items() if n > 1} == {}


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_ac_bi_007_edges_grow_additively_never_lost_between_runs(
    merged_graph_run: MergedGraphRun,
) -> None:
    first = cast("dict[str, list[list[str]]]", merged_graph_run.after_pass_1["edge_ids"])
    second = cast("dict[str, list[list[str]]]", merged_graph_run.after_pass_2["edge_ids"])
    lost = {
        rel: sorted({(p[0], p[1]) for p in pairs} - {(p[0], p[1]) for p in second[rel]})
        for rel, pairs in first.items()
    }
    assert {rel: pairs for rel, pairs in lost.items() if pairs} == {}


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_ac_bi_007_capability_embeddings_stable_across_both_runs(
    merged_graph_run: MergedGraphRun,
) -> None:
    ids = cast("dict[str, list[str]]", merged_graph_run.after_pass_1["node_ids"])["Capability"]
    assert ids
    rows = cast(
        "list[list[object]]",
        merged_graph_run.graph.query("MATCH (n:Capability) RETURN n.id, n.embedding").result_set,
    )
    live = {cast("str", r[0]): r[1] for r in rows}
    assert set(ids) <= set(live)
    assert [i for i, e in live.items() if e is None] == []
