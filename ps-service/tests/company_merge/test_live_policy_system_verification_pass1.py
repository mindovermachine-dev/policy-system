"""Issue #28, slice 7 -- read-only live verification of slice 6's REAL merge into `policy_system`.

`@pytest.mark.falkordb_live` ONLY -- no `llm_live`. This module issues ZERO
new LLM calls (CONTEXT.md's explicit design constraint, and issue #45's
flakiness precedent): every check here either reads the JSON report slice 6
(`run_live_merge.py`) already wrote
(`.orchestrator/tracker/issue-28-live-merge-verification/
live_merge_report_20260913T125204Z.json`), reads the structured JSONL log
that report points to (`log_file_path`), or issues plain read-only Cypher
(`MATCH`/`OPTIONAL MATCH`/`RETURN`, never `MERGE`/`SET`/`DELETE`) against the
real, already-merged `policy_system` graph. Slice 6 already ran for real and
was approved by the user; this file proves AC-BI-004/005/006/008 hold for
that result, per PLAN.md Section 5.1 as amended by CHANGES.md rows #1 and
#4 (both supersede PLAN.md's original §5.1 text).

**AC-BI-006 is compared against the report's own before/after snapshot, not
a fresh "live re-read of before"** (CHANGES.md #1): a live before-state no
longer exists post-hoc -- the merge already happened. `before`/`after` were
both captured live by the runner itself (`run_live_merge.py`'s
`_snapshot_graph`), never re-derived here.

**AC-BI-005's exact-match sub-case gets a real read-only pre-check**
(CHANGES.md #4): GDPR baseline's Capability id-set is compared, read-only,
against the report's own `before.node_ids["Capability"]` (the 563
pre-existing canonical Capability ids CRA/NIS2 already held). A natural
overlap is hard-asserted to converge onto one canonical node with edges
re-pointed from both sources; a genuine absence of overlap would be an
explicit, non-forcing, named finding instead (this real run DOES have a
7-id overlap -- see the test body).

**A load-bearing discovery about the structured log's `entity_id` field**,
confirmed by direct reading of `ps_service/company_merge/merge.py` (the
`emit_log_entry(..., entity_id=resolution.incoming_id, ...)` call site) and
`dedup.py`'s `CanonicalResolution` construction: `entity_id` is ALWAYS
`resolution.incoming_id`, never `resolution.canonical_id`. For match_kind
`"exact"`/`"new"` (and the `"near_miss"` entries paired with a `"new"`
resolution), `incoming_id == canonical_id` by construction, so the logged id
IS the graph node id. For match_kind `"semantic"`, `canonical_id` is a
DIFFERENT, pre-existing id (`result.best_existing_id`) that the log never
records at all -- and `graph_writer.py`'s own docstring confirms "a
semantically ... matched Capability is never minted as its own graph node".
So "every resolved canonical Capability id has exactly one node" is checked
per match_kind using what each kind's logged id actually means: exactly-one
for `exact`/`new`/`near_miss`, exactly-ZERO for `semantic` (proving the
merged-away id was never separately minted as a duplicate either). This is
not a weakened assertion -- it is the correct, evidence-based reading of
what this log schema can and cannot prove per match_kind, verified against
real log data before being written into an assertion.

**Post-repair addendum (issue #28, `REPAIR_AC_BI_006.md`):** IMPL_SLICE_7's
own AC-BI-006 property check found 202 pre-existing Role/Requirement/
Obligation nodes whose properties had been overwritten by a re-run merge
(root cause fixed in `graph_writer.py`, see `IMPL_SLICE_BASELINE_FIX_006.md`
-- `ON CREATE SET` now protects these three labels). Those 202 nodes were
then live-restored to their exact pre-merge `before` values and independently
re-verified (0 mismatches across all 202, plus a clean 5-node unaffected
spot-check); see `REPAIR_AC_BI_006.md`'s Addendum for the full record. Two
consequences here:

1. `test_ac_bi_006_repaired_properties_match_before_snapshot_live` (new) is
   the TRUE, current, meaningful proof that the repair succeeded and holds
   right now -- it live-reads `properties(n)` for every one of the 202
   previously-corrupted ids (recomputed from the report's own before/after,
   never hardcoded) and asserts equality with `before`.
2. `test_ac_bi_006_node_properties_unchanged_for_pre_existing_nodes` compared
   two frozen historical JSON blobs from the report and could therefore never
   pass again once that drift was captured -- a permanent, un-fixable red
   that no amount of live repair can change, since it issues zero live
   queries. Repurposing a whole-suite-blocking permanent failure into a
   silent deletion would erase the historical record; deleting the file's
   only reference to the incident would too. Instead it is kept and
   repurposed below into an intentional regression-witness that documents
   the known, already-fixed-and-repaired incident and asserts its exact,
   frozen shape forever (see that test's own docstring for the full
   reasoning) -- turning a confusing permanent failure into a documented,
   permanently-green historical record, corroborating (not duplicating)
   `REPAIR_AC_BI_006.md`/`IMPL_SLICE_7.md`/`IMPL_SLICE_BASELINE_FIX_006.md`.
"""

from __future__ import annotations

import json
import warnings
from collections import Counter
from pathlib import Path
from typing import cast

import pytest

from company_merge._live_merge_assertions import (
    _assert_ac001_every_node_type_present,  # pyright: ignore[reportPrivateUsage]
    _assert_ac006_traversal_reachable,  # pyright: ignore[reportPrivateUsage]
    _assert_dedup_decisions_correlated,  # pyright: ignore[reportPrivateUsage]
    _assert_run_id_logged,  # pyright: ignore[reportPrivateUsage]
    _snapshot_node_properties,  # pyright: ignore[reportPrivateUsage]
)
from ps_service.company_merge.falkordb_client import (
    GraphHandle,
    connect_from_config,
    select_graph,
)
from ps_service.config import load_config
from ps_service.domain_mapper.falkordb_client import baseline_graph_name

_REAL_SINGLE_TENANT_GRAPH_NAME = "policy_system"

_REPORT_PATH = (
    Path(__file__).resolve().parents[3]
    / ".orchestrator"
    / "tracker"
    / "issue-28-live-merge-verification"
    / "live_merge_report_20260913T125204Z.json"
)


def _load_report() -> dict[str, object]:
    return cast("dict[str, object]", json.loads(_REPORT_PATH.read_text(encoding="utf-8")))


def _before_after(report: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    return cast("dict[str, object]", report["before"]), cast("dict[str, object]", report["after"])


def _load_log_entries(log_path: Path) -> list[dict[str, object]]:
    """Mirrors `tests/conftest.py`'s own `read_lines` body exactly (own copy: that
    fixture is `tmp_path`-scoped for test-written logs, this reads a fixed,
    already-on-disk report-pointed path instead -- not a fixture-shape match).
    """
    if not log_path.exists():
        return []
    return [
        cast("dict[str, object]", json.loads(line))
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _query_rows(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query, params=params).result_set)


def _capability_node_counts(graph: GraphHandle, ids: list[str]) -> dict[str, int]:
    """Per-id live Capability node count in `policy_system` -- 0 when absent, >1 would be
    a real duplicate. `OPTIONAL MATCH` (not `MATCH`) so every requested id gets a row,
    including ids with zero matching nodes.
    """
    if not ids:
        return {}
    rows = _query_rows(
        graph,
        "UNWIND $ids AS cid OPTIONAL MATCH (n:Capability {id: cid}) RETURN cid, count(n)",
        {"ids": ids},
    )
    return {cast("str", row[0]): cast("int", row[1]) for row in rows}


def _connect_single_tenant_graph() -> GraphHandle:
    config = load_config()
    db = connect_from_config(config)
    return select_graph(db, _REAL_SINGLE_TENANT_GRAPH_NAME)


# --- AC-BI-004 --------------------------------------------------------------------------


@pytest.mark.falkordb_live
@pytest.mark.parametrize("regulatory_instrument_id", ["CRA-1.0", "GDPR-1.0", "NIS2-1.0"])
def test_ac_bi_004_merged_baseline_data_is_queryable_end_to_end(
    regulatory_instrument_id: str,
) -> None:
    """Real live Regulation->Role/Requirement->Obligation->Capability traversal for each
    currently-mapped regulation, against the real `policy_system` -- not just GDPR (the
    genuinely new one), reconfirming CRA/NIS2's already-merged data too.
    """
    single_tenant_graph = _connect_single_tenant_graph()

    _assert_ac001_every_node_type_present(
        single_tenant_graph, regulatory_instrument_id, graph_name=_REAL_SINGLE_TENANT_GRAPH_NAME
    )
    _assert_ac006_traversal_reachable(
        single_tenant_graph, regulatory_instrument_id, graph_name=_REAL_SINGLE_TENANT_GRAPH_NAME
    )


# --- AC-BI-005 ---------------------------------------------------------------------------


@pytest.mark.falkordb_live
def test_ac_bi_005_exact_match_subcase_converges_on_natural_overlap() -> None:
    """CHANGES.md #4: read-only pre-check comparing GDPR baseline's Capability id-set
    against the report's `before.node_ids["Capability"]` (CRA/NIS2's 563 pre-existing
    canonical ids). Non-forcing when there is no natural overlap (an explicit, named
    finding, never a failure); a hard assertion when there is one.
    """
    report = _load_report()
    before, _after = _before_after(report)
    before_capability_ids = frozenset(
        cast("list[str]", cast("dict[str, object]", before["node_ids"])["Capability"])
    )

    config = load_config()
    db = connect_from_config(config)
    gdpr_baseline_graph = select_graph(db, baseline_graph_name("GDPR"))
    single_tenant_graph = select_graph(db, _REAL_SINGLE_TENANT_GRAPH_NAME)

    gdpr_capability_ids = {
        cast("str", row[0])
        for row in _query_rows(gdpr_baseline_graph, "MATCH (n:Capability) RETURN n.id")
    }

    overlap = sorted(gdpr_capability_ids & before_capability_ids)

    if not overlap:
        # CHANGES.md #4: a genuine absence of overlap is a legitimate real-data outcome,
        # reported as an explicit, named finding -- never asserted as a failure.
        warnings.warn(
            "AC-BI-005 exact-match sub-case FINDING: GDPR baseline has zero natural "
            "exact-key Capability overlap with the 563 pre-existing canonical ids -- the "
            "exact-match sub-case is not exercised by real data this run (non-forcing, "
            "per CHANGES.md #4).",
            stacklevel=2,
        )
        return

    before_requires_pairs = cast(
        "list[list[str]]", cast("dict[str, object]", before["edge_ids"])["REQUIRES"]
    )
    pre_existing_requires_count_by_capability = Counter(
        target_id for _source_id, target_id in before_requires_pairs
    )
    after_requires_rows = _query_rows(
        single_tenant_graph,
        "MATCH (:Obligation)-[:REQUIRES]->(c:Capability) WHERE c.id IN $ids RETURN c.id, count(*)",
        {"ids": overlap},
    )
    after_requires_count_by_capability = {
        cast("str", row[0]): cast("int", row[1]) for row in after_requires_rows
    }
    node_counts = _capability_node_counts(single_tenant_graph, overlap)

    for capability_id in overlap:
        assert node_counts[capability_id] == 1, (
            f"exact-match overlap {capability_id!r} has {node_counts[capability_id]} "
            f"Capability node(s) in {_REAL_SINGLE_TENANT_GRAPH_NAME} -- expected exactly "
            "one canonical node (never duplicated)"
        )
        pre_count = pre_existing_requires_count_by_capability.get(capability_id, 0)
        post_count = after_requires_count_by_capability.get(capability_id, 0)
        assert post_count > pre_count, (
            f"exact-match overlap {capability_id!r}: REQUIRES edge count did not grow "
            f"({pre_count} -> {post_count}) -- expected GDPR's own Obligations to be "
            "re-pointed onto this pre-existing canonical node in addition to the edges "
            "already pointing to it before the merge"
        )


@pytest.mark.falkordb_live
def test_ac_bi_005_every_dedup_decision_resolves_to_exactly_one_capability_node() -> None:
    """Unconditional, hard assertion (CONTEXT.md AC-BI-005's "never duplicated" half):
    every `dedupe_canonical_nodes` log entry across all three real run_ids, checked live,
    per match_kind (see module docstring for why the expected count differs by kind).
    """
    report = _load_report()
    run_ids = frozenset(cast("list[str]", report["run_ids"]))
    log_entries = _load_log_entries(Path(cast("str", report["log_file_path"])))

    ids_by_outcome: dict[str, set[str]] = {}
    for entry in log_entries:
        if entry.get("action") != "dedupe_canonical_nodes" or entry.get("run_id") not in run_ids:
            continue
        outcome = cast("str", entry["outcome"])
        ids_by_outcome.setdefault(outcome, set()).add(cast("str", entry["entity_id"]))

    assert ids_by_outcome, (
        f"no dedupe_canonical_nodes log entries found for run_ids={sorted(run_ids)!r} in "
        f"{report['log_file_path']!r}"
    )

    single_tenant_graph = _connect_single_tenant_graph()

    # exact/new/near_miss: logged entity_id == canonical_id by construction -> exactly one
    # node must exist. semantic: logged entity_id is the merged-away incoming id, which
    # `graph_writer.persist_canonical_nodes` never mints as its own node -> exactly zero.
    for outcome, expected_count in (("exact", 1), ("new", 1), ("near_miss", 1), ("semantic", 0)):
        ids = sorted(ids_by_outcome.get(outcome, set()))
        if not ids:
            continue
        counts = _capability_node_counts(single_tenant_graph, ids)
        bad = {capability_id: n for capability_id, n in counts.items() if n != expected_count}
        assert not bad, (
            f"match_kind={outcome!r}: expected every logged id to resolve to exactly "
            f"{expected_count} Capability node(s) in {_REAL_SINGLE_TENANT_GRAPH_NAME}, "
            f"got: {bad}"
        )

    # Independent corroborating check that does not depend on entity_id semantics at all:
    # if any match_kind had silently minted an extra node, total Capability growth would
    # exceed the number of genuinely new mints.
    before, after = _before_after(report)
    before_node_counts = cast("dict[str, object]", before["node_counts"])
    after_node_counts = cast("dict[str, object]", after["node_counts"])
    before_capability_count = cast("int", before_node_counts["Capability"])
    after_capability_count = cast("int", after_node_counts["Capability"])
    new_mint_count = len(ids_by_outcome.get("new", set()))
    assert after_capability_count - before_capability_count == new_mint_count, (
        f"Capability count grew by {after_capability_count - before_capability_count}, but "
        f"only {new_mint_count} distinct id(s) were minted as match_kind='new' -- any "
        "mismatch would mean a node was created outside the mint path"
    )


# --- AC-BI-006 ---------------------------------------------------------------------------


@pytest.mark.falkordb_live
def test_ac_bi_006_every_pre_existing_node_id_survives() -> None:
    report = _load_report()
    before, after = _before_after(report)
    before_node_ids = cast("dict[str, list[str]]", before["node_ids"])
    after_node_ids = cast("dict[str, list[str]]", after["node_ids"])

    missing_by_label = {
        label: sorted(set(ids) - set(after_node_ids[label]))
        for label, ids in before_node_ids.items()
    }
    missing_by_label = {label: ids for label, ids in missing_by_label.items() if ids}

    assert not missing_by_label, (
        f"AC-BI-006: node id(s) present before the merge are missing after: {missing_by_label}"
    )


def _corrupted_ids_by_label(
    before_props: dict[str, dict[str, dict[str, object]]],
    after_props: dict[str, dict[str, dict[str, object]]],
) -> dict[str, list[str]]:
    """Recomputes, from the report's own `before`/`after` node-property snapshots, which
    node ids' properties differ between the two -- the same diff IMPL_SLICE_7.md
    originally found (1 Role, 2 Requirement, 199 Obligation), recomputed here rather than
    hardcoded so both tests below stay honest if the report's content were ever read
    differently.
    """
    mismatches: dict[str, list[str]] = {}
    for label, by_id in before_props.items():
        after_by_id = after_props[label]
        label_mismatches = sorted(
            node_id for node_id, props in by_id.items() if after_by_id.get(node_id) != props
        )
        if label_mismatches:
            mismatches[label] = label_mismatches
    return mismatches


@pytest.mark.falkordb_live
def test_ac_bi_006_historical_report_drift_matches_the_documented_repaired_incident() -> None:
    """Regression-witness, NOT a current-bug check (issue #28 disposition, see module
    docstring's "Post-repair addendum").

    This test replaces `test_ac_bi_006_node_properties_unchanged_for_pre_existing_nodes`,
    which compared the report's frozen `before.node_properties`/`after.node_properties`
    blobs directly and asserted equality. That JSON was captured once, live, at merge
    time (`live_merge_report_20260913T125204Z.json`) and can never change again -- so once
    IMPL_SLICE_7.md found real before/after drift, that assertion became permanently,
    un-fixably red. It issues zero live queries, so no amount of live repair (which did
    happen -- see `REPAIR_AC_BI_006.md`) could ever turn it green again. Leaving a
    permanently-failing test in the suite is bad hygiene: a future reader cannot tell
    "known, already-fixed historical incident" from "something is still broken right
    now" just by seeing red.

    Rather than delete the file's only automated tie to the incident, this test is
    repurposed into an explicit, intentional regression-witness: it asserts the frozen
    report DOES document exactly the known, already-repaired drift (1 Role, 2
    Requirement, 199 Obligation -- root-caused and code-fixed via `graph_writer.py`'s
    `ON CREATE SET` change in `IMPL_SLICE_BASELINE_FIX_006.md`, then the 202 affected
    nodes live-restored per `REPAIR_AC_BI_006.md`'s Addendum). Because the report file is
    static, this stays permanently green -- it is a documented historical record, not a
    live health check. The live, current, meaningful proof that the repair holds *now*
    is `test_ac_bi_006_repaired_properties_match_before_snapshot_live` below; this test's
    only job is to keep that documented incident from silently disappearing or drifting
    if the report file were ever mis-edited.
    """
    report = _load_report()
    before, after = _before_after(report)
    before_props = cast("dict[str, dict[str, dict[str, object]]]", before["node_properties"])
    after_props = cast("dict[str, dict[str, dict[str, object]]]", after["node_properties"])

    mismatches = _corrupted_ids_by_label(before_props, after_props)
    counts = {label: len(ids) for label, ids in mismatches.items()}

    assert counts == {"Role": 1, "Requirement": 2, "Obligation": 199}, (
        "expected the frozen report to document exactly the known, already-repaired "
        "202-node historical incident (1 Role, 2 Requirement, 199 Obligation -- see "
        "IMPL_SLICE_7.md/REPAIR_AC_BI_006.md), got per-label counts: "
        f"{counts}. This test reads a static historical report file that should never "
        "change; a different count here means the report file itself changed, not that "
        "a new bug appeared."
    )


@pytest.mark.falkordb_live
def test_ac_bi_006_repaired_properties_match_before_snapshot_live() -> None:
    """The TRUE, current, meaningful verification that the AC-BI-006 repair succeeded and
    is durable (issue #28, `REPAIR_AC_BI_006.md`'s Addendum) -- unlike the historical
    witness test above, this issues real live queries against `policy_system` right now.

    Recomputes, from the report's own `before`/`after` node-property snapshots (never
    hardcoded), the exact set of previously-corrupted Role/Requirement/Obligation ids
    (IMPL_SLICE_7.md's 202-node finding: 1 Role, 2 Requirement, 199 Obligation), then
    live-reads `properties(n)` for every one of those ids and asserts each still exactly
    equals its `before` snapshot -- proving the live restore
    (`REPAIR_AC_BI_006.md`'s Addendum: 202/202 restored, 0 mismatches) both succeeded and
    has not drifted again since.
    """
    report = _load_report()
    before, after = _before_after(report)
    before_props = cast("dict[str, dict[str, dict[str, object]]]", before["node_properties"])
    after_props = cast("dict[str, dict[str, dict[str, object]]]", after["node_properties"])

    corrupted_ids_by_label = {
        label: ids
        for label, ids in _corrupted_ids_by_label(before_props, after_props).items()
        if label in ("Role", "Requirement", "Obligation")
    }
    total_corrupted = sum(len(ids) for ids in corrupted_ids_by_label.values())
    assert total_corrupted > 0, (
        "expected the report to document at least one previously-corrupted "
        "Role/Requirement/Obligation node (IMPL_SLICE_7.md's 202-node finding) -- a zero "
        "count here means the report was read differently than expected, which would "
        "make the check below vacuous"
    )

    single_tenant_graph = _connect_single_tenant_graph()
    live_props = _snapshot_node_properties(single_tenant_graph)

    still_mismatched: dict[str, list[str]] = {}
    for label, ids in corrupted_ids_by_label.items():
        expected_by_id = before_props[label]
        live_by_id = live_props[label]
        label_mismatches = sorted(
            node_id for node_id in ids if live_by_id.get(node_id) != expected_by_id[node_id]
        )
        if label_mismatches:
            still_mismatched[label] = label_mismatches

    counts_checked = {label: len(ids) for label, ids in corrupted_ids_by_label.items()}
    assert not still_mismatched, (
        f"AC-BI-006 repair verification: live properties right now do not match the "
        f"report's `before` snapshot for previously-corrupted node(s) -- checked "
        f"{counts_checked} (total {total_corrupted}); still-mismatched (sample): "
        f"{ {label: ids[:5] for label, ids in still_mismatched.items()} }"
    )


@pytest.mark.falkordb_live
def test_ac_bi_006_edge_properties_unchanged_for_defines_and_expresses() -> None:
    report = _load_report()
    before, after = _before_after(report)
    before_edge_props = cast("dict[str, dict[str, dict[str, object]]]", before["edge_properties"])
    after_edge_props = cast("dict[str, dict[str, dict[str, object]]]", after["edge_properties"])

    mismatches: dict[str, list[str]] = {}
    for relationship_type, by_key in before_edge_props.items():
        after_by_key = after_edge_props[relationship_type]
        relationship_mismatches = sorted(
            key for key, props in by_key.items() if after_by_key.get(key) != props
        )
        if relationship_mismatches:
            mismatches[relationship_type] = relationship_mismatches

    assert not mismatches, (
        f"AC-BI-006: DEFINES/EXPRESSES edge properties changed for pre-existing edge(s): "
        f"{mismatches}"
    )


@pytest.mark.falkordb_live
def test_ac_bi_006_edge_existence_preserved_for_has_satisfied_by_requires() -> None:
    report = _load_report()
    before, after = _before_after(report)
    before_edge_ids = cast("dict[str, list[list[str]]]", before["edge_ids"])
    after_edge_ids = cast("dict[str, list[list[str]]]", after["edge_ids"])

    missing_by_relationship_type: dict[str, list[tuple[str, str]]] = {}
    for relationship_type, pairs in before_edge_ids.items():
        before_pairs = {(pair[0], pair[1]) for pair in pairs}
        after_pairs = {(pair[0], pair[1]) for pair in after_edge_ids[relationship_type]}
        missing = sorted(before_pairs - after_pairs)
        if missing:
            missing_by_relationship_type[relationship_type] = missing

    assert not missing_by_relationship_type, (
        f"AC-BI-006: HAS/SATISFIED_BY/REQUIRES edge(s) present before the merge are missing "
        f"after: {missing_by_relationship_type}"
    )


@pytest.mark.falkordb_live
def test_ac_bi_006_live_capability_properties_match_the_reports_after_snapshot() -> None:
    """One live sanity cross-check (CHANGES.md #1): re-read all current `Capability`
    nodes' properties right now and assert equality against the report's own
    `after.node_properties.Capability` -- guards against any intervening write between
    slice 6 (the live merge) and this test.
    """
    report = _load_report()
    _before, after = _before_after(report)
    report_capability_properties = cast(
        "dict[str, dict[str, object]]",
        cast("dict[str, object]", after["node_properties"])["Capability"],
    )

    single_tenant_graph = _connect_single_tenant_graph()
    live_capability_properties = _snapshot_node_properties(single_tenant_graph)["Capability"]

    assert live_capability_properties == report_capability_properties, (
        "live Capability properties right now differ from the report's own `after` "
        "snapshot -- an intervening write may have occurred between slice 6 and this test"
    )


# --- AC-BI-008 ---------------------------------------------------------------------------


@pytest.mark.falkordb_live
def test_ac_bi_008_run_id_logged_and_correlated_to_dedup_decisions() -> None:
    report = _load_report()
    regulatory_instrument_ids = cast("list[str]", report["regulations_attempted"])
    run_ids = cast("list[str]", report["run_ids"])
    log_entries = _load_log_entries(Path(cast("str", report["log_file_path"])))

    assert len(regulatory_instrument_ids) == len(run_ids), (
        "report's regulations_attempted/run_ids lists are not the same length"
    )
    for regulatory_instrument_id, run_id in zip(regulatory_instrument_ids, run_ids, strict=True):
        _assert_run_id_logged(
            log_entries,
            action="merge_baseline_graph",
            run_id=run_id,
            entity_id=regulatory_instrument_id,
        )
        _assert_dedup_decisions_correlated(log_entries, run_id)
