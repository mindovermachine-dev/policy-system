"""Verification of the first merge into the single-tenant graph, made hermetic.

`falkordb_live` + `postgres_live`, no LLM. The checks run the real `merge_baseline_graph`
(`_hermetic_policy_system.merged_graph_run`) into a uniquely named graph and assert on that run's
own before/after, so they pass on a clean instance.
"""

from __future__ import annotations

import json
from typing import cast

import pytest

from company_merge._hermetic_policy_system import (
    EXISTING_ID,
    NEW_ID,
    MergedGraphRun,
)
from company_merge._live_merge_assertions import (
    _assert_ac001_every_node_type_present,  # pyright: ignore[reportPrivateUsage]
    _assert_ac006_traversal_reachable,  # pyright: ignore[reportPrivateUsage]
    _assert_dedup_decisions_correlated,  # pyright: ignore[reportPrivateUsage]
    _assert_run_id_logged,  # pyright: ignore[reportPrivateUsage]
)


def _rows(run: MergedGraphRun, query: str, params: dict[str, object]) -> list[list[object]]:
    return cast("list[list[object]]", run.graph.query(query, params=params).result_set)


def _log_entries(run: MergedGraphRun) -> list[dict[str, object]]:
    return [
        cast("dict[str, object]", json.loads(line))
        for line in run.log_path.read_text(encoding="utf-8").splitlines()
        if line
    ]


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
@pytest.mark.parametrize("regulatory_instrument_id", [EXISTING_ID, NEW_ID])
def test_ac_bi_004_merged_baseline_data_is_queryable_end_to_end(
    merged_graph_run: MergedGraphRun, regulatory_instrument_id: str
) -> None:
    _assert_ac001_every_node_type_present(
        merged_graph_run.graph, regulatory_instrument_id, graph_name=merged_graph_run.graph_name
    )
    _assert_ac006_traversal_reachable(
        merged_graph_run.graph, regulatory_instrument_id, graph_name=merged_graph_run.graph_name
    )


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_ac_bi_005_exact_match_subcase_converges_on_the_shared_capability(
    merged_graph_run: MergedGraphRun,
) -> None:
    """Both regulations' Obligations REQUIRE the one shared Capability: exactly one node, with
    one REQUIRES edge from each regulation's Obligation (the new one re-pointed onto it).
    """
    shared = merged_graph_run.shared_capability_id
    nodes = _rows(
        merged_graph_run, "MATCH (n:Capability {id: $id}) RETURN count(n)", {"id": shared}
    )
    assert nodes[0][0] == 1
    before_edges = cast("dict[str, list[list[str]]]", merged_graph_run.before["edge_ids"])
    assert sum(1 for _s, target in before_edges["REQUIRES"] if target == shared) == 1
    after_count = _rows(
        merged_graph_run,
        "MATCH (:Obligation)-[:REQUIRES]->(c:Capability {id: $id}) RETURN count(*)",
        {"id": shared},
    )[0][0]
    assert after_count == 2


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_ac_bi_005_every_dedup_decision_resolves_to_exactly_one_capability_node(
    merged_graph_run: MergedGraphRun,
) -> None:
    run_ids = set(merged_graph_run.run_ids)
    new_ids = {
        cast("str", entry["entity_id"])
        for entry in _log_entries(merged_graph_run)
        if entry.get("action") == "dedupe_canonical_nodes"
        and entry.get("run_id") in run_ids
        and entry.get("outcome") in ("exact", "new")
    }
    assert new_ids, "no exact/new dedupe_canonical_nodes log entries found"
    counts = {
        cast("str", r[0]): cast("int", r[1])
        for r in _rows(
            merged_graph_run,
            "UNWIND $ids AS cid OPTIONAL MATCH (n:Capability {id: cid}) RETURN cid, count(n)",
            {"ids": sorted(new_ids)},
        )
    }
    assert {cid: n for cid, n in counts.items() if n != 1} == {}


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_ac_bi_006_every_pre_existing_node_id_survives(merged_graph_run: MergedGraphRun) -> None:
    before = cast("dict[str, list[str]]", merged_graph_run.before["node_ids"])
    after = cast("dict[str, list[str]]", merged_graph_run.after_pass_1["node_ids"])
    missing = {label: sorted(set(ids) - set(after[label])) for label, ids in before.items()}
    assert {label: ids for label, ids in missing.items() if ids} == {}


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_ac_bi_006_pre_existing_node_properties_unchanged(merged_graph_run: MergedGraphRun) -> None:
    """The create-only writes (`ON CREATE SET`) must leave every pre-existing Role, Requirement
    and Obligation exactly as it was (`graph_writer.py` must never overwrite them).
    """
    before = cast("dict[str, dict[str, dict[str, object]]]", merged_graph_run.before)
    after = cast("dict[str, dict[str, dict[str, object]]]", merged_graph_run.after_pass_1)
    before_props = cast("dict[str, dict[str, dict[str, object]]]", before["node_properties"])
    after_props = cast("dict[str, dict[str, dict[str, object]]]", after["node_properties"])
    assert before_props["Obligation"], "the pre-existing regulation must have Obligations"
    changed = {
        label: sorted(i for i, p in by_id.items() if after_props[label].get(i) != p)
        for label, by_id in before_props.items()
        if label in ("Role", "Requirement", "Obligation")
    }
    assert {label: ids for label, ids in changed.items() if ids} == {}


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_ac_bi_006_edge_properties_unchanged_for_defines_and_expresses(
    merged_graph_run: MergedGraphRun,
) -> None:
    before = cast(
        "dict[str, dict[str, dict[str, object]]]", merged_graph_run.before["edge_properties"]
    )
    after = cast(
        "dict[str, dict[str, dict[str, object]]]", merged_graph_run.after_pass_1["edge_properties"]
    )
    assert before["DEFINES"], "the pre-existing regulation must have DEFINES edges"
    changed = {
        rel: sorted(k for k, p in by_key.items() if after[rel].get(k) != p)
        for rel, by_key in before.items()
    }
    assert {rel: keys for rel, keys in changed.items() if keys} == {}


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_ac_bi_006_edge_existence_preserved_for_has_satisfied_by_requires(
    merged_graph_run: MergedGraphRun,
) -> None:
    before = cast("dict[str, list[list[str]]]", merged_graph_run.before["edge_ids"])
    after = cast("dict[str, list[list[str]]]", merged_graph_run.after_pass_1["edge_ids"])
    assert before["REQUIRES"], "the pre-existing regulation must have REQUIRES edges"
    missing = {
        rel: sorted({(p[0], p[1]) for p in pairs} - {(p[0], p[1]) for p in after[rel]})
        for rel, pairs in before.items()
    }
    assert {rel: pairs for rel, pairs in missing.items() if pairs} == {}


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_ac_bi_008_run_id_logged_and_correlated_to_dedup_decisions(
    merged_graph_run: MergedGraphRun,
) -> None:
    entries = _log_entries(merged_graph_run)
    for instrument_id, run_id in zip(
        (EXISTING_ID, NEW_ID),
        (merged_graph_run.run_ids[0], merged_graph_run.run_ids[1]),
        strict=True,
    ):
        _assert_run_id_logged(
            entries, action="merge_baseline_graph", run_id=run_id, entity_id=instrument_id
        )
        _assert_dedup_decisions_correlated(entries, run_id)
