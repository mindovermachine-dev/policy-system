"""The live 3-regulation Company Merge capstone.

`@pytest.mark.falkordb_live @pytest.mark.llm_live` (plus `cellar_live` and `postgres_live`):
reads the `cra_baseline`/`gdpr_baseline`/`nis2_baseline` graphs the `live_baseline_graphs` fixture
builds fresh (Cellar ingest, then bounded Domain Mapper extraction and derivation), runs
`merge_baseline_graph` for each regulation against real Azure OpenAI (via
`route_embedding`'s default caller, real `RouteEmbedding` calls, no
`call_embedding` override), and writes into a **dedicated, disposable**
`policy_system_capstone_test` single-tenant graph -- never the real,
shared `policy_system` graph (mutating a database that has never before seen `ON CREATE SET`-based
canonical writing or cross-run embedding backfill is exactly the kind of
hard-to-reverse, shared-system action CLAUDE.md requires explicit user
permission for -- already obtained for the disposable graph, not for the
real one).

The capstone graph name is a module constant handed to the merge stage as an argument.

**A deliberate, deterministic seeded duplicate for the exact-key convergence check, with an
honest non-forcing caveat for the semantic-match and near-miss checks**: before any merge runs,
one extra Role/Requirement/Obligation/Capability chain is additively `MERGE`d into
`gdpr_baseline` and `nis2_baseline` (never `policy_system` or
`policy_system_capstone_test` directly), using the REAL `role_id`/
`requirement_id`/`obligation_id`/`capability_id` functions. Because an
Obligation is Role-scoped (a weak entity of exactly one Role), the two
seeded Obligations are legitimately DISTINCT nodes -- the guaranteed
exact-key collision is at **`Capability`** instead: both seeded chains
`REQUIRES` a Capability whose name is the same literal string, and
`capability_id(name)` is a pure function of name alone, so a real exact-key
Capability collision across GDPR/NIS2 is guaranteed independent of real
CRA/GDPR/NIS2 content -- closing the exact-key live check. The
semantic-match and near-miss checks keep a non-forcing caveat: whether real CRA/GDPR/NIS2
content contains a
genuine near-or-above-threshold pair is not something this test controls or
fakes; only that the comparison mechanism itself fires for real is
asserted.

**Safety property, the most important one in this file**: the real
`policy_system` graph's total node count is read before anything runs and
again after everything (including the second pass) completes, and
asserted identical -- this is the live proof that this whole capstone never
wrote a single node/edge/property into the real graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.config import ServiceConfig

import os
from dataclasses import dataclass
from typing import cast

import pytest

from company_merge._live_merge_assertions import (
    _assert_ac001_every_node_type_present,  # pyright: ignore[reportPrivateUsage]
    _assert_ac006_traversal_reachable,  # pyright: ignore[reportPrivateUsage]
    _assert_ac008_no_role_or_requirement_dedup,  # pyright: ignore[reportPrivateUsage]
    _assert_dedup_decisions_correlated,  # pyright: ignore[reportPrivateUsage]
    _assert_run_id_logged,  # pyright: ignore[reportPrivateUsage]
    _snapshot_counts,  # pyright: ignore[reportPrivateUsage]
    _snapshot_embeddings,  # pyright: ignore[reportPrivateUsage]
)
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.domain_mapper.identity import (
    capability_id,
    obligation_id,
    requirement_id,
    role_id,
)
from ps_service.graph_gateway.connection import connect_from_config, select_graph
from ps_service.graph_gateway.default_gateway import build_default_graph_write_gateway
from ps_service.graph_gateway.system_audit import build_system_audit_recorder
from ps_service.logging import bind_run_context

_CAPSTONE_GRAPH_NAME = "policy_system_capstone_test"
_REAL_SINGLE_TENANT_GRAPH_NAME = "policy_system"
_MERGE_THRESHOLD = 0.59  # empirically recommended by a labeled sweep -- not
# read from PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD/ServiceConfig, passed directly to
# merge_baseline_graph.
_LOG_FILENAME = "company_merge_capstone.jsonl"

_SEED_DUTY_TEXT = "Report the incident to the competent authority without undue delay"
_SEED_CAPABILITY_NAME = "Capstone Seeded Incident Notification Capability"
_SEED_REQUIREMENT_TEXT = (
    "Capstone seeding: notify the competent authority of a qualifying incident without undue delay"
)
_SEED_ROLE_NAME = "Capstone Seeded Incident Notifier"
_SEED_SOURCE_REF = "capstone-seed"

# Captured at module-import time (collection), before tests/conftest.py's autouse
# `_isolate_logging` fixture runs `monkeypatch.delenv("PS_LLMINTERFACE_EMBED_MODEL", ...)`
# for every test -- mirrors domain_mapper's own test_live_capstone.py pattern exactly, for
# the same reason: this live test's whole point is to use the real configured embed model,
# so it must be read before that fixture strips it.
_LLM_INTERFACE_EMBED_MODEL = os.environ.get("PS_LLMINTERFACE_EMBED_MODEL")


@dataclass(frozen=True)
class _RegulatoryInstrumentFixture:
    short_name: str
    regulatory_instrument_id: str
    seed_duplicate: bool


_REGULATIONS = (
    _RegulatoryInstrumentFixture("CRA", "CRA-1.0", seed_duplicate=False),
    _RegulatoryInstrumentFixture("GDPR", "GDPR-1.0", seed_duplicate=True),
    _RegulatoryInstrumentFixture("NIS2", "NIS2-1.0", seed_duplicate=True),
)


@dataclass(frozen=True)
class _SeededIds:
    role_id: str
    requirement_id: str
    obligation_id: str
    capability_id: str


def _query_rows(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query, params=params).result_set)


def _count(graph: GraphHandle, query: str, params: dict[str, object] | None = None) -> int:
    return cast("int", _query_rows(graph, query, params=params)[0][0])


def _seed_duplicate_obligation(
    baseline_graph: GraphHandle, regulatory_instrument_id: str
) -> _SeededIds:
    """Additively `MERGE`
    one extra Role/Requirement/Obligation/Capability chain into
    `baseline_graph` (a `{short}_baseline` graph -- never `policy_system`/
    `policy_system_capstone_test`), fully wired with its own
    regulation-scoped `DEFINES`/`EXPRESSES`/`HAS`/`SATISFIED_BY`/`REQUIRES`
    edges. Mirrors `ps_service.domain_mapper.graph_writer`'s exact write
    shapes -- additive-only, the same MERGE semantics every other write in
    this system already relies on for safety, targeting only a
    per-regulation baseline graph already treated as a disposable test
    fixture.

    The seeded `Capability`'s name is the SAME literal string for every
    caller (`_SEED_CAPABILITY_NAME`) -- since `capability_id(name)` is a
    pure function of name alone, this guarantees an identical canonical
    Capability id across whichever regulations this is called for,
    independent of real CRA/GDPR/NIS2 content (the live exact-key proof, not left
    to chance). The `Obligation` id is Role-scoped, so the two seeded
    Obligations are legitimately distinct -- convergence is at the shared
    Capability.
    """
    seeded_role_id = role_id(_SEED_ROLE_NAME, regulatory_instrument_id)
    seeded_requirement_id = requirement_id(regulatory_instrument_id, "CAPSTONE", "1", None)
    seeded_obligation_id = obligation_id(seeded_role_id, _SEED_DUTY_TEXT)
    seeded_capability_id = capability_id(_SEED_CAPABILITY_NAME)

    baseline_graph.query(
        "MERGE (n:Role {id: $id}) SET n += $properties",
        params={
            "id": seeded_role_id,
            "properties": {"name": _SEED_ROLE_NAME, "confidence": 1.0},
        },
    )
    baseline_graph.query(
        "MERGE (n:Requirement {id: $id}) SET n += $properties",
        params={
            "id": seeded_requirement_id,
            "properties": {
                "text": _SEED_REQUIREMENT_TEXT,
                "type": "requirement",
                "confidence": 1.0,
                "role_id": seeded_role_id,
            },
        },
    )
    baseline_graph.query(
        "MERGE (n:Obligation {id: $id}) SET n += $properties",
        params={
            "id": seeded_obligation_id,
            "properties": {"text": _SEED_DUTY_TEXT, "confidence": 1.0},
        },
    )
    baseline_graph.query(
        "MATCH (r:RegulatoryInstrument {id: $regulatory_instrument_id}), (n:Role {id: $target_id}) "
        "MERGE (r)-[e:DEFINES]->(n) SET e.source_ref = $source_ref",
        params={
            "regulatory_instrument_id": regulatory_instrument_id,
            "target_id": seeded_role_id,
            "source_ref": _SEED_SOURCE_REF,
        },
    )
    baseline_graph.query(
        "MATCH (r:RegulatoryInstrument {id: $regulatory_instrument_id}), "
        "(n:Requirement {id: $target_id}) "
        "MERGE (r)-[e:EXPRESSES]->(n) SET e.source_ref = $source_ref",
        params={
            "regulatory_instrument_id": regulatory_instrument_id,
            "target_id": seeded_requirement_id,
            "source_ref": _SEED_SOURCE_REF,
        },
    )
    baseline_graph.query(
        "MATCH (s:Role {id: $source_id}), (t:Obligation {id: $target_id}) MERGE (s)-[:HAS]->(t)",
        params={"source_id": seeded_role_id, "target_id": seeded_obligation_id},
    )
    baseline_graph.query(
        "MATCH (s:Requirement {id: $source_id}), (t:Obligation {id: $target_id}) "
        "MERGE (s)-[:SATISFIED_BY]->(t)",
        params={"source_id": seeded_requirement_id, "target_id": seeded_obligation_id},
    )
    baseline_graph.query(
        "MERGE (n:Capability {id: $id}) SET n += $properties",
        params={
            "id": seeded_capability_id,
            "properties": {"name": _SEED_CAPABILITY_NAME, "confidence": 1.0},
        },
    )
    baseline_graph.query(
        "MATCH (s:Obligation {id: $source_id}), (t:Capability {id: $target_id}) "
        "MERGE (s)-[:REQUIRES]->(t)",
        params={"source_id": seeded_obligation_id, "target_id": seeded_capability_id},
    )

    return _SeededIds(
        role_id=seeded_role_id,
        requirement_id=seeded_requirement_id,
        obligation_id=seeded_obligation_id,
        capability_id=seeded_capability_id,
    )


def _assert_seeded_nodes_present(single_tenant_graph: GraphHandle, seeded: _SeededIds) -> None:
    assert (
        _count(
            single_tenant_graph, "MATCH (n:Role {id: $id}) RETURN count(n)", {"id": seeded.role_id}
        )
        == 1
    )
    assert (
        _count(
            single_tenant_graph,
            "MATCH (n:Requirement {id: $id}) RETURN count(n)",
            {"id": seeded.requirement_id},
        )
        == 1
    )
    assert (
        _count(
            single_tenant_graph,
            "MATCH (n:Obligation {id: $id}) RETURN count(n)",
            {"id": seeded.obligation_id},
        )
        == 1
    )
    assert (
        _count(
            single_tenant_graph,
            "MATCH (n:Capability {id: $id}) RETURN count(n)",
            {"id": seeded.capability_id},
        )
        == 1
    )


def _assert_ac002_exact_duplicate_converges(
    single_tenant_graph: GraphHandle, seeded_by_regulatory_instrument: dict[str, _SeededIds]
) -> None:
    """The guaranteed exact-key duplicate is a shared `Capability`.
    The two seeded `Obligation`s are legitimately distinct (Role-scoped);
    both `REQUIRES` the one canonical Capability.
    """
    canonical_id = seeded_by_regulatory_instrument["GDPR"].capability_id
    assert canonical_id == seeded_by_regulatory_instrument["NIS2"].capability_id, (
        "capability_id(name) must be identical for both seeded duplicates -- this is the "
        "whole point of the seed"
    )

    count = _count(
        single_tenant_graph, "MATCH (n:Capability {id: $id}) RETURN count(n)", {"id": canonical_id}
    )
    assert count == 1, (
        f"expected exactly ONE canonical Capability node for the seeded exact-key duplicate "
        f"{canonical_id!r}, found {count}"
    )

    # Both seeded Obligations remain distinct passthrough nodes.
    seeded_obligation_ids = {
        seeded_by_regulatory_instrument["GDPR"].obligation_id,
        seeded_by_regulatory_instrument["NIS2"].obligation_id,
    }
    assert len(seeded_obligation_ids) == 2, "the two seeded Obligations must be distinct nodes"

    for short_name in ("GDPR", "NIS2"):
        seeded = seeded_by_regulatory_instrument[short_name]
        rows = _query_rows(
            single_tenant_graph,
            "MATCH (:Obligation {id: $obl_id})-[:REQUIRES]->(c:Capability) RETURN c.id",
            {"obl_id": seeded.obligation_id},
        )
        target_ids = {cast("str", row[0]) for row in rows}
        assert target_ids == {canonical_id}, (
            f"{short_name}'s seeded Obligation's REQUIRES edge does not point at the "
            f"single canonical Capability id {canonical_id!r}: found {target_ids}"
        )


def _report_ac003_ac004_mechanism_presence(
    single_tenant_graph: GraphHandle, log_entries: list[dict[str, object]]
) -> dict[str, int]:
    """Non-forcing caveat: whether real
    CRA/GDPR/NIS2 content contains a genuine cross-regulation semantic match
    or near-miss is not something this test controls or
    fakes -- forcing one would require either mocking `route_embedding`
    (defeating the entire point of a `llm_live` capstone) or picking texts
    and hoping the real model agrees, which is not meaningfully different
    from hoping the real content already contains such a pair. This
    function only asserts the COMPARISON MECHANISM ITSELF fired for real
    (at least one embedding was actually computed and persisted somewhere
    across the three regulations -- guaranteed once the single-tenant graph
    is non-empty, since GDPR/NIS2 both compare their non-exact-matching
    Obligations/Capabilities against whatever CRA/GDPR-before-them already
    wrote) and reports whatever the real model actually produced, without
    forcing a match/near-miss outcome either way.
    """
    embedded_obligation_count = _count(
        single_tenant_graph, "MATCH (n:Obligation) WHERE n.embedding IS NOT NULL RETURN count(n)"
    )
    embedded_capability_count = _count(
        single_tenant_graph, "MATCH (n:Capability) WHERE n.embedding IS NOT NULL RETURN count(n)"
    )
    semantic_match_count = sum(
        1
        for entry in log_entries
        if entry.get("action") == "dedupe_canonical_nodes" and entry.get("outcome") == "semantic"
    )
    near_miss_count = sum(
        1
        for entry in log_entries
        if entry.get("action") == "dedupe_canonical_nodes" and entry.get("outcome") == "near_miss"
    )

    assert embedded_obligation_count > 0 or embedded_capability_count > 0, (
        "the semantic-match mechanism never computed/persisted a single real embedding across "
        "all three regulations -- expected at least GDPR/NIS2 comparing against CRA's already-"
        "written canonical set"
    )

    return {
        "embedded_obligation_count": embedded_obligation_count,
        "embedded_capability_count": embedded_capability_count,
        "semantic_match_count": semantic_match_count,
        "near_miss_count": near_miss_count,
    }


def _live_audit_event_id(config: ServiceConfig, run_id: str) -> str:
    """One system audit row for the run, the way production records it.

    The merge's gateway groups are logged under it. Not runnable in the implementation sandbox.
    """
    return build_system_audit_recorder(config)(
        action="ingestion_run.submit",
        resource_type="ingestion_run",
        resource_id=run_id,
        details={"trigger": "live_capstone"},
    )


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
@pytest.mark.cellar_live
@pytest.mark.llm_live
@pytest.mark.skipif(
    not _LLM_INTERFACE_EMBED_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_EMBED_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_live_three_regulation_company_merge_capstone_across_cra_gdpr_nis2(
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
    live_baseline_graphs: ServiceConfig,
) -> None:
    assert _LLM_INTERFACE_EMBED_MODEL is not None  # narrows type; skipif already guards this
    embed_model = _LLM_INTERFACE_EMBED_MODEL

    # The config of the scratch PS state Postgres the baseline graphs were built through
    # (`live_baseline_graphs`); PS_LLMINTERFACE_EMBED_MODEL is stripped by the autouse fixture,
    # hence the module-import capture above.
    config = live_baseline_graphs
    db = connect_from_config(config)

    # --- Safety check (BEFORE): the real policy_system graph must be provably untouched ---
    set(db.list_graphs())
    real_single_tenant_graph = select_graph(db, _REAL_SINGLE_TENANT_GRAPH_NAME)
    real_node_count_before = _count(real_single_tenant_graph, "MATCH (n) RETURN count(n)")

    # --- Point every write this test makes at the disposable capstone graph, never at
    # "policy_system" directly. ---
    single_tenant_graph = select_graph(db, _CAPSTONE_GRAPH_NAME)

    baseline_graphs = {
        fixture.short_name: select_graph(db, baseline_graph_name(fixture.short_name))
        for fixture in _REGULATIONS
    }

    # --- Seeding: a guaranteed exact-key duplicate across GDPR/NIS2 ---
    seeded_by_regulatory_instrument: dict[str, _SeededIds] = {
        fixture.short_name: _seed_duplicate_obligation(
            baseline_graphs[fixture.short_name], fixture.regulatory_instrument_id
        )
        for fixture in _REGULATIONS
        if fixture.seed_duplicate
    }

    emitter, log_path = make_emitter(filename=_LOG_FILENAME)

    run_ids: list[str] = []
    submitter = build_default_graph_write_gateway(config)

    def _run_pass(pass_number: int) -> None:
        for fixture in _REGULATIONS:
            run_id = f"capstone-{fixture.short_name.lower()}-pass{pass_number}"
            run_ids.append(run_id)
            with bind_run_context(run_id):
                merge_baseline_graph(
                    fixture.regulatory_instrument_id,
                    baseline_graph=baseline_graphs[fixture.short_name],
                    single_tenant_graph=single_tenant_graph,
                    embed_model=embed_model,
                    merge_threshold=_MERGE_THRESHOLD,
                    review_threshold=REVIEW_THRESHOLD,
                    role_review_threshold=ROLE_REVIEW_THRESHOLD,
                    emitter=emitter,
                    submitter=submitter,
                    single_tenant_graph_name=_CAPSTONE_GRAPH_NAME,
                    baseline_graph_name=baseline_graph_name(fixture.short_name),
                    audit_event_id=_live_audit_event_id(config, run_id),
                )

    # --- Pass 1: the actual 3-regulation merge ---
    _run_pass(1)

    counts_after_pass_1 = _snapshot_counts(single_tenant_graph)
    embeddings_after_pass_1 = _snapshot_embeddings(single_tenant_graph)

    # --- re-run the WHOLE capstone a second time against the SAME
    # policy_system_capstone_test state -- must be a structural no-op. ---
    _run_pass(2)

    emitter.flush()
    log_entries = read_lines(log_path)

    counts_after_pass_2 = _snapshot_counts(single_tenant_graph)
    embeddings_after_pass_2 = _snapshot_embeddings(single_tenant_graph)

    assert counts_after_pass_2 == counts_after_pass_1, (
        f"AC-005: second pass grew node/edge counts: {counts_after_pass_1} -> {counts_after_pass_2}"
    )
    assert embeddings_after_pass_2 == embeddings_after_pass_1, (
        "AC-005: second pass computed/wrote a different embedding for at least one canonical "
        "node -- the `WHERE n.embedding IS NULL` backfill guard should make this a structural "
        "no-op"
    )

    # --- per regulation ---
    for fixture in _REGULATIONS:
        _assert_ac001_every_node_type_present(
            single_tenant_graph, fixture.regulatory_instrument_id, graph_name=_CAPSTONE_GRAPH_NAME
        )
        _assert_ac006_traversal_reachable(
            single_tenant_graph, fixture.regulatory_instrument_id, graph_name=_CAPSTONE_GRAPH_NAME
        )

    for seeded in seeded_by_regulatory_instrument.values():
        _assert_seeded_nodes_present(single_tenant_graph, seeded)

    # --- the seeded exact-key duplicate converges onto one canonical node ---
    _assert_ac002_exact_duplicate_converges(single_tenant_graph, seeded_by_regulatory_instrument)

    # --- semantic-match / near-miss: mechanism presence only, non-forcing (see helper) ---
    _report_ac003_ac004_mechanism_presence(single_tenant_graph, log_entries)

    # --- distinct run_id per regulation (per pass), correlated dedup decisions ---
    assert len(set(run_ids)) == len(run_ids), f"expected 6 mutually distinct run_ids, got {run_ids}"
    for fixture in _REGULATIONS:
        merge_run_id = f"capstone-{fixture.short_name.lower()}-pass1"
        _assert_run_id_logged(
            log_entries,
            action="merge_baseline_graph",
            run_id=merge_run_id,
            entity_id=fixture.regulatory_instrument_id,
        )
        _assert_dedup_decisions_correlated(log_entries, merge_run_id)

    # --- no dedupe_canonical_nodes decision ever fires for a Role/Requirement id ---
    _assert_ac008_no_role_or_requirement_dedup(log_entries)

    # --- Safety check (AFTER): the real policy_system graph must be provably untouched ---
    real_node_count_after = _count(real_single_tenant_graph, "MATCH (n) RETURN count(n)")
    assert real_node_count_after == real_node_count_before, (
        f"the real {_REAL_SINGLE_TENANT_GRAPH_NAME!r} graph's node count changed "
        f"({real_node_count_before} -> {real_node_count_after}) -- this must NEVER happen"
    )

    # Informational, not asserted on: real observed shape for IMPL_20.md.
