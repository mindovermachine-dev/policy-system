"""`merge_baseline_graph` mints canonical Capability nodes through the Graph Write Gateway (#210).

Slice 5a: a `match_kind="new"` Capability is one create-only group on `policy_system`, with
`created_at` fixed from the injected `ValueSource` and the embedding carried in the primitive.
Equivalence (AC-BI-002) against the frozen legacy reference is met modulo edge identity, with the
reference and the gateway path given the same generated values; live digest and live interpreter
agreement are not verifiable here (no FalkorDB in this sandbox).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from company_merge._gateway_fakes import (
    FamilyABaseline,
    MergeRig,
    family_a_baseline_graph,
    stepped_values,
)
from company_merge._legacy_reference import (
    FROZEN_STATEMENT_HASHES,
    LegacyRecordingGraph,
    write_legacy_canonical_nodes,
    write_legacy_embedding_backfill,
    write_legacy_instrument_roles_requirements,
    write_legacy_obligations,
    write_legacy_pending_reviews,
)
from company_merge._seed_merge_graphs import (
    EMBED_MODEL,
    SIMILARITY_THRESHOLD,
    BaselineGraphReader,
    OneHotEmbeddingCaller,
    SingleTenantStore,
)
from ps_service.company_merge import dedup, graph_reader
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.models import BaselineNode, CanonicalResolution
from ps_service.graph_gateway.digest import canonical_digest

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from ps_service.logging import LogEmitter

_CAP_A = "capability_encrypt_data"
_CAP_B = "capability_rotate_keys"
_CAP_C = "capability_patch_hosts"


def _baseline(instrument_id: str, **capabilities: tuple[str, float]) -> FamilyABaseline:
    return FamilyABaseline(
        instrument_id=instrument_id,
        instrument_properties={"title": instrument_id},
        capabilities={f"capability_{key}": value for key, value in capabilities.items()},
    )


_FIRST = _baseline("CRA-1.0", encrypt_data=("Encrypt data", 0.8), rotate_keys=("Rotate keys", 0.7))
_SECOND = _baseline(
    "NIS2-1.0", encrypt_data=("Encrypt data", 0.8), patch_hosts=("Patch hosts", 0.6)
)
_SCENARIOS: dict[str, tuple[FamilyABaseline, ...]] = {
    "S-capability-fresh": (_FIRST,),
    "S-capability-rerun": (_FIRST, _FIRST),
    "S-capability-second-regulation": (_FIRST, _SECOND),
}


@pytest.fixture
def emitter(make_emitter: MakeEmitter) -> LogEmitter:
    return make_emitter()[0]


def _reference(steps: tuple[FamilyABaseline, ...], emitter: LogEmitter) -> LegacyRecordingGraph:
    """The frozen writer's graph: dedup reads what the reference itself has built so far."""
    reference = LegacyRecordingGraph()
    reads = SingleTenantStore(nodes=reference.nodes)
    values, embeddings = stepped_values(), OneHotEmbeddingCaller()
    for step in steps:
        read = graph_reader.read_baseline_graph(
            BaselineGraphReader(family_a_baseline_graph(step)), step.instrument_id
        )
        outcome = dedup.dedupe_canonical_nodes(
            read.capability_nodes,
            kind="Capability",
            single_tenant_graph=reads,
            model=EMBED_MODEL,
            threshold=SIMILARITY_THRESHOLD,
            call_embedding=embeddings,
            emitter=emitter,
        )
        write_legacy_instrument_roles_requirements(
            reference,
            step.instrument_id,
            read.regulatory_instrument_properties,
            read.role_nodes,
            read.requirement_nodes,
            read.provenance_edges,
        )
        write_legacy_obligations(reference, read.obligation_nodes)
        write_legacy_canonical_nodes(
            reference, read.capability_nodes, outcome.resolutions, kind="Capability", values=values
        )
        write_legacy_embedding_backfill(reference, outcome.embedding_backfills, kind="Capability")
        write_legacy_pending_reviews(
            reference, outcome.near_misses, kind="Capability", values=values
        )
    return reference


def _production(steps: tuple[FamilyABaseline, ...], rig: MergeRig, emitter: LogEmitter) -> None:
    values, embeddings = stepped_values(), OneHotEmbeddingCaller()
    for step in steps:
        merge_baseline_graph(
            step.instrument_id,
            baseline_graph=BaselineGraphReader(family_a_baseline_graph(step)),
            single_tenant_graph=rig.legacy,
            embed_model=EMBED_MODEL,
            similarity_threshold=SIMILARITY_THRESHOLD,
            call_embedding=embeddings,
            emitter=emitter,
            values=values,
            **rig.merge_keywords(),
        )


@pytest.mark.parametrize("scenario", list(_SCENARIOS))
def test_the_frozen_reference_and_the_gateway_path_build_the_same_graph_for_capabilities(
    scenario: str, merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    """AC-BI-002, family C (Capability), same generated values; modulo edge identity."""
    steps = _SCENARIOS[scenario]

    reference = _reference(steps, emitter)
    _production(steps, merge_rig, emitter)

    assert canonical_digest(merge_rig.policy_system) == canonical_digest(
        reference.as_gateway_graph()
    )


def test_the_scenarios_exercise_what_they_claim_to(emitter: LogEmitter) -> None:
    """Guard of the guard: embeddings are minted, ids recur, and every mint carries created_at."""
    fresh = _reference(_SCENARIOS["S-capability-fresh"], emitter)
    second = _reference(_SCENARIOS["S-capability-second-regulation"], emitter)

    # the first mint met an empty index (no embedding), the second was compared against it
    # the compared graph includes PendingReview nodes (the second mint is a near-miss of the first)
    assert any(label == "PendingReview" for label, _ in fresh.nodes)
    assert "embedding" not in fresh.nodes[("Capability", _CAP_A)]
    assert "embedding" in fresh.nodes[("Capability", _CAP_B)]
    # a recurring id is matched, not minted twice; the new one is stamped later than the first two
    assert sorted(key[1] for key in second.nodes if key[0] == "Capability") == sorted(
        [_CAP_A, _CAP_B, _CAP_C]
    )
    stamps = {
        key[1]: str(node["created_at"])
        for key, node in second.nodes.items()
        if key[0] == "Capability"
    }
    assert stamps[_CAP_C] > stamps[_CAP_B] > stamps[_CAP_A]


def test_the_merge_mints_new_capabilities_as_one_group_with_the_injected_created_at(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    _production(_SCENARIOS["S-capability-fresh"], merge_rig, emitter)

    entries = [e for g in merge_rig.groups for e in g.entries if e.name == "Capability"]
    assert [e.identity for e in entries] == [_CAP_A, _CAP_B]
    assert [e.content["properties"]["created_at"] for e in entries] == [  # pyright: ignore[reportIndexIssue, reportArgumentType]
        "2026-03-01T12:00:00+00:00",
        "2026-03-01T12:00:01+00:00",
    ]
    assert entries[0].embedding is None
    assert entries[1].embedding is not None
    assert (
        len({id(g) for g in merge_rig.groups if any(e.name == "Capability" for e in g.entries)})
        == 1
    )


def test_a_rerun_logs_no_capability_entry(merge_rig: MergeRig, emitter: LogEmitter) -> None:
    _production(_SCENARIOS["S-capability-fresh"], merge_rig, emitter)

    def capability_entries() -> list[str]:
        return [e.identity for g in merge_rig.groups for e in g.entries if e.name == "Capability"]

    before = len(capability_entries())

    _production(_SCENARIOS["S-capability-fresh"], merge_rig, emitter)

    assert len(capability_entries()) == before


def test_the_frozen_capability_statement_matches_its_pinned_hash() -> None:
    class _Capture:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def query(self, q: str, params: dict[str, object] | None = None) -> object:
            del params
            self.statements.append(q)

    capture = _Capture()
    node = BaselineNode("c", {})

    write_legacy_canonical_nodes(
        capture,
        (node,),
        (CanonicalResolution("c", "c", "new", None),),
        kind="Capability",
        values=stepped_values(),
    )

    assert [hashlib.sha256(s.encode()).hexdigest() for s in capture.statements] == [
        FROZEN_STATEMENT_HASHES["capability"]
    ]


def test_the_frozen_backfill_statement_matches_its_pinned_hash() -> None:
    class _Capture:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def query(self, q: str, params: dict[str, object] | None = None) -> object:
            del params
            self.statements.append(q)

    capture = _Capture()

    write_legacy_embedding_backfill(capture, {"c": (0.5,)}, kind="Capability")

    assert [hashlib.sha256(s.encode()).hexdigest() for s in capture.statements] == [
        FROZEN_STATEMENT_HASHES["embedding_backfill"]
    ]


def test_the_second_regulation_backfills_the_first_mint_and_logs_it_once(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    """AC-BI-006 end to end: the unembedded first mint gets its vector, a re-run logs nothing."""
    _production(_SCENARIOS["S-capability-second-regulation"], merge_rig, emitter)
    node = merge_rig.policy_system.nodes[("Capability", _CAP_A)]
    assert node["embedding"] is not None
    before = len(merge_rig.logged())

    _production((_SECOND,), merge_rig, emitter)

    assert len(merge_rig.logged()) == before
