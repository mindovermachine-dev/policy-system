"""Increment 20 -- the live 3-regulation Company Merge capstone
(PLAN_REVIEWED.md §10 Batch 11, §12).

`@pytest.mark.falkordb_live @pytest.mark.llm_live`: reads #15's own
live-populated `cra_baseline`/`gdpr_baseline`/`nis2_baseline` graphs, runs
`merge_baseline_graph` for each regulation against real Azure OpenAI (via
`route_embedding`'s default caller, real `RouteEmbedding` calls, no
`call_embedding` override), and writes into a **dedicated, disposable**
`policy_system_capstone_test` single-tenant graph -- never the real,
shared `policy_system` graph (PLAN_REVIEWED.md §12's target-graph decision;
mutating a database that has never before seen `ON CREATE SET`-based
canonical writing or cross-run embedding backfill is exactly the kind of
hard-to-reverse, shared-system action CLAUDE.md requires explicit user
permission for -- already obtained for the disposable graph, not for the
real one).

Reached via `ps_service.company_merge.falkordb_client.single_tenant_graph_name()`'s
existing `PS_FALKORDB_GRAPH` env var override (`monkeypatch`), exactly as
that function already supports -- no new mechanism.

**S2's fix (§12), adapted for issue #42 -- a deliberate, deterministic
seeded duplicate for AC-002, kept as an honest non-forcing caveat for
AC-003/AC-004**: before any merge runs, one extra
Role/Requirement/Obligation/Capability chain is additively `MERGE`d into
`gdpr_baseline` and `nis2_baseline` (never `policy_system` or
`policy_system_capstone_test` directly), using the REAL `role_id`/
`requirement_id`/`obligation_id`/`capability_id` functions. Since #42 an
Obligation is Role-scoped (a weak entity of exactly one Role), the two
seeded Obligations are legitimately DISTINCT nodes -- the guaranteed
exact-key collision is at **`Capability`** instead: both seeded chains
`REQUIRES` a Capability whose name is the same literal string, and
`capability_id(name)` is a pure function of name alone, so a real exact-key
Capability collision across GDPR/NIS2 is guaranteed independent of real
CRA/GDPR/NIS2 content -- closing AC-002's live check. AC-003/AC-004 keep the
identical non-forcing caveat: whether real CRA/GDPR/NIS2 content contains a
genuine near-or-above-threshold pair is not something this test controls or
fakes; only that the comparison mechanism itself fires for real is
asserted.

**Safety property, the most important one in this file**: the real
`policy_system` graph's total node count is read before anything runs and
again after everything (including the AC-005 second pass) completes, and
asserted identical -- this is the live proof that this whole capstone never
wrote a single node/edge/property into the real graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines

import os
from dataclasses import dataclass
from typing import cast

import pytest

from ps_service.company_merge.falkordb_client import (
    GraphHandle,
    connect_from_config,
    select_graph,
    single_tenant_graph_name,
)
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.config import load_config
from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.domain_mapper.identity import (
    capability_id,
    obligation_id,
    requirement_id,
    role_id,
)
from ps_service.logging import bind_run_context

_CAPSTONE_GRAPH_NAME = "policy_system_capstone_test"
_REAL_SINGLE_TENANT_GRAPH_NAME = "policy_system"
_SIMILARITY_THRESHOLD = 0.90  # user-approved for this run, see task brief -- not read from
# PS_COMPANYMERGE_SIMILARITY_THRESHOLD/ServiceConfig, passed directly to merge_baseline_graph.
_LOG_FILENAME = "company_merge_capstone.jsonl"

_SEED_DUTY_TEXT = "Report the incident to the competent authority without undue delay"
_SEED_CAPABILITY_NAME = "Capstone Seeded Incident Notification Capability"
_SEED_REQUIREMENT_TEXT = (
    "Capstone seeding: notify the competent authority of a qualifying incident without undue delay"
)
_SEED_ROLE_NAME = "Capstone Seeded Incident Notifier"
_SEED_SOURCE_REF = "capstone-seed"

_NODE_LABELS = ("RegulatoryInstrument", "Role", "Requirement", "Obligation", "Capability")
_EDGE_TYPES = ("DEFINES", "EXPRESSES", "HAS", "SATISFIED_BY", "REQUIRES")

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
    """S2's fix (PLAN_REVIEWED.md §12), adapted for #42: additively `MERGE`
    one extra Role/Requirement/Obligation/Capability chain into
    `baseline_graph` (a `{short}_baseline` graph -- never `policy_system`/
    `policy_system_capstone_test`), fully wired with its own
    regulation-scoped `DEFINES`/`EXPRESSES`/`HAS`/`SATISFIED_BY`/`REQUIRES`
    edges. Mirrors `ps_service.domain_mapper.graph_writer`'s exact write
    shapes -- additive-only, the same MERGE semantics every other write in
    this system already relies on for safety, targeting only a
    per-regulation baseline graph already treated as a disposable test
    fixture (#15's own precedent).

    The seeded `Capability`'s name is the SAME literal string for every
    caller (`_SEED_CAPABILITY_NAME`) -- since `capability_id(name)` is a
    pure function of name alone, this guarantees an identical canonical
    Capability id across whichever regulations this is called for,
    independent of real CRA/GDPR/NIS2 content (AC-002's live proof, not left
    to chance). The `Obligation` id is Role-scoped (#42), so the two seeded
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


def _snapshot_counts(single_tenant_graph: GraphHandle) -> dict[str, int]:
    """Per-label node counts + per-relationship-type edge counts -- AC-005's
    "no growth" proof operates on this whole snapshot, not just a single
    total.
    """
    counts = {
        label: _count(single_tenant_graph, f"MATCH (n:{label}) RETURN count(n)")
        for label in _NODE_LABELS
    }
    counts.update(
        {
            rel: _count(single_tenant_graph, f"MATCH ()-[r:{rel}]->() RETURN count(r)")
            for rel in _EDGE_TYPES
        }
    )
    return counts


def _snapshot_embeddings(single_tenant_graph: GraphHandle) -> dict[str, tuple[float, ...] | None]:
    """Id -> embedding (or None) for every Obligation/Capability node --
    AC-005's "zero further embedding-backfill writes" proof compares this
    whole map before/after the second pass, not just the seeded pair.
    """
    embeddings: dict[str, tuple[float, ...] | None] = {}
    for label in ("Obligation", "Capability"):
        for node_id, embedding in _query_rows(
            single_tenant_graph, f"MATCH (n:{label}) RETURN n.id, n.embedding"
        ):
            embeddings[cast("str", node_id)] = (
                tuple(cast("list[float]", embedding)) if embedding is not None else None
            )
    return embeddings


def _assert_ac001_every_node_type_present(
    single_tenant_graph: GraphHandle, regulatory_instrument_id: str
) -> None:
    assert (
        _count(
            single_tenant_graph,
            "MATCH (n:RegulatoryInstrument {id: $id}) RETURN count(n)",
            {"id": regulatory_instrument_id},
        )
        == 1
    ), f"{regulatory_instrument_id}: Regulation node missing from {_CAPSTONE_GRAPH_NAME}"

    role_count = _count(
        single_tenant_graph,
        "MATCH (:RegulatoryInstrument {id: $id})-[:DEFINES]->(:Role) RETURN count(*)",
        {"id": regulatory_instrument_id},
    )
    requirement_count = _count(
        single_tenant_graph,
        "MATCH (:RegulatoryInstrument {id: $id})-[:EXPRESSES]->(:Requirement) RETURN count(*)",
        {"id": regulatory_instrument_id},
    )
    obligation_count = _count(
        single_tenant_graph,
        "MATCH (:RegulatoryInstrument {id: $id})-[:DEFINES]->(:Role)-[:HAS]->(:Obligation) "
        "RETURN count(*)",
        {"id": regulatory_instrument_id},
    )
    capability_count = _count(
        single_tenant_graph,
        "MATCH (:RegulatoryInstrument {id: $id})-[:DEFINES]->(:Role)-[:HAS]->(:Obligation)"
        "-[:REQUIRES]->(:Capability) "
        "RETURN count(*)",
        {"id": regulatory_instrument_id},
    )
    assert role_count > 0, f"{regulatory_instrument_id}: no Role reachable via DEFINES"
    assert requirement_count > 0, (
        f"{regulatory_instrument_id}: no Requirement reachable via EXPRESSES"
    )
    assert obligation_count > 0, f"{regulatory_instrument_id}: no Obligation reachable via Role HAS"
    assert capability_count > 0, (
        f"{regulatory_instrument_id}: no Capability reachable via Obligation REQUIRES"
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
    """#42: the guaranteed exact-key duplicate is a shared `Capability`.
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
    """AC-003/AC-004 non-forcing caveat (PLAN_REVIEWED.md §12): whether real
    CRA/GDPR/NIS2 content contains a genuine cross-regulation semantic match
    (AC-003) or near-miss (AC-004) is not something this test controls or
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


def _assert_ac006_traversal_reachable(
    single_tenant_graph: GraphHandle, regulatory_instrument_id: str
) -> None:
    has_chain_count = _count(
        single_tenant_graph,
        "MATCH (:RegulatoryInstrument {id: $id})-[:DEFINES]->(:Role)-[:HAS]->(:Obligation)"
        "-[:REQUIRES]->(:Capability) "
        "RETURN count(*)",
        {"id": regulatory_instrument_id},
    )
    satisfied_chain_count = _count(
        single_tenant_graph,
        "MATCH (:RegulatoryInstrument {id: $id})-[:EXPRESSES]->(:Requirement)"
        "-[:SATISFIED_BY]->(:Obligation) "
        "RETURN count(*)",
        {"id": regulatory_instrument_id},
    )
    assert has_chain_count > 0, (
        f"{regulatory_instrument_id}: no live "
        f"Regulation->DEFINES->Role->HAS->Obligation->REQUIRES->"
        f"Capability traversal in {_CAPSTONE_GRAPH_NAME}"
    )
    assert satisfied_chain_count > 0, (
        f"{regulatory_instrument_id}: no live "
        f"Regulation->EXPRESSES->Requirement->SATISFIED_BY->Obligation "
        f"traversal in {_CAPSTONE_GRAPH_NAME}"
    )


def _assert_run_id_logged(
    log_entries: list[dict[str, object]], *, action: str, run_id: str, entity_id: str
) -> None:
    matches = [
        entry
        for entry in log_entries
        if entry.get("action") == action and entry.get("run_id") == run_id
    ]
    assert matches, f"no log entry found for action={action!r} run_id={run_id!r}"
    assert any(
        entry.get("entity_id") == entity_id and entry.get("outcome") == "succeeded"
        for entry in matches
    ), f"no succeeded entry with entity_id={entity_id!r} for action={action!r} run_id={run_id!r}"


def _assert_dedup_decisions_correlated(log_entries: list[dict[str, object]], run_id: str) -> None:
    matches = [
        entry
        for entry in log_entries
        if entry.get("action") == "dedupe_canonical_nodes" and entry.get("run_id") == run_id
    ]
    assert matches, (
        f"no dedupe_canonical_nodes log entries correlated to run_id={run_id!r} (AC-007)"
    )


def _assert_ac008_no_role_or_requirement_dedup(log_entries: list[dict[str, object]]) -> None:
    dedup_entries = [
        entry for entry in log_entries if entry.get("action") == "dedupe_canonical_nodes"
    ]
    assert dedup_entries, (
        "expected at least one dedupe_canonical_nodes log entry across the whole run"
    )
    for entry in dedup_entries:
        entity_id = cast("str", entry.get("entity_id"))
        assert entity_id.startswith("cap_"), (
            f"dedupe_canonical_nodes fired for entity_id={entity_id!r}, which is not a "
            "Capability (cap_*) id -- since #42, Role/Requirement/Obligation dedup is all "
            "out of scope (AC-008); only Capability is deduped"
        )


@pytest.mark.falkordb_live
@pytest.mark.llm_live
@pytest.mark.skipif(
    not _LLM_INTERFACE_EMBED_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_EMBED_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_live_three_regulation_company_merge_capstone_across_cra_gdpr_nis2(
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _LLM_INTERFACE_EMBED_MODEL is not None  # narrows type; skipif already guards this
    embed_model = _LLM_INTERFACE_EMBED_MODEL

    # load_config() still resolves falkordb_host/port correctly here -- only
    # PS_LLMINTERFACE_MODEL/PS_LLMINTERFACE_EMBED_MODEL are stripped by the autouse fixture,
    # not PS_FALKORDB_HOST/PORT.
    config = load_config()
    db = connect_from_config(config)

    # --- Safety check (BEFORE): the real policy_system graph must be provably untouched ---
    set(db.list_graphs())
    real_single_tenant_graph = select_graph(db, _REAL_SINGLE_TENANT_GRAPH_NAME)
    real_node_count_before = _count(real_single_tenant_graph, "MATCH (n) RETURN count(n)")

    # --- Point every write this test makes at the disposable capstone graph, never at
    # "policy_system" directly -- single_tenant_graph_name()'s own existing env-var override,
    # not a new mechanism. ---
    monkeypatch.setenv("PS_FALKORDB_GRAPH", _CAPSTONE_GRAPH_NAME)
    assert single_tenant_graph_name() == _CAPSTONE_GRAPH_NAME
    single_tenant_graph = select_graph(db, single_tenant_graph_name())

    baseline_graphs = {
        fixture.short_name: select_graph(db, baseline_graph_name(fixture.short_name))
        for fixture in _REGULATIONS
    }

    # --- S2's seeding fix: a guaranteed exact-key duplicate across GDPR/NIS2 ---
    seeded_by_regulatory_instrument: dict[str, _SeededIds] = {
        fixture.short_name: _seed_duplicate_obligation(
            baseline_graphs[fixture.short_name], fixture.regulatory_instrument_id
        )
        for fixture in _REGULATIONS
        if fixture.seed_duplicate
    }

    emitter, log_path = make_emitter(filename=_LOG_FILENAME)

    run_ids: list[str] = []

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
                    similarity_threshold=_SIMILARITY_THRESHOLD,
                    emitter=emitter,
                )

    # --- Pass 1: the actual 3-regulation merge ---
    _run_pass(1)

    counts_after_pass_1 = _snapshot_counts(single_tenant_graph)
    embeddings_after_pass_1 = _snapshot_embeddings(single_tenant_graph)

    # --- AC-005: re-run the WHOLE capstone a second time against the SAME
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

    # --- AC-001 / AC-006, per regulation ---
    for fixture in _REGULATIONS:
        _assert_ac001_every_node_type_present(single_tenant_graph, fixture.regulatory_instrument_id)
        _assert_ac006_traversal_reachable(single_tenant_graph, fixture.regulatory_instrument_id)

    for seeded in seeded_by_regulatory_instrument.values():
        _assert_seeded_nodes_present(single_tenant_graph, seeded)

    # --- AC-002: the seeded exact-key duplicate converges onto one canonical node ---
    _assert_ac002_exact_duplicate_converges(single_tenant_graph, seeded_by_regulatory_instrument)

    # --- AC-003 / AC-004: mechanism-presence only, non-forcing (see helper docstring) ---
    _report_ac003_ac004_mechanism_presence(single_tenant_graph, log_entries)

    # --- AC-007: distinct run_id per regulation (per pass), correlated dedup decisions ---
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

    # --- AC-008: no dedupe_canonical_nodes decision ever fires for a Role/Requirement id ---
    _assert_ac008_no_role_or_requirement_dedup(log_entries)

    # --- Safety check (AFTER): the real policy_system graph must be provably untouched ---
    real_node_count_after = _count(real_single_tenant_graph, "MATCH (n) RETURN count(n)")
    assert real_node_count_after == real_node_count_before, (
        f"the real {_REAL_SINGLE_TENANT_GRAPH_NAME!r} graph's node count changed "
        f"({real_node_count_before} -> {real_node_count_after}) -- this must NEVER happen"
    )

    # Informational, not asserted on: real observed shape for IMPL_20.md.
