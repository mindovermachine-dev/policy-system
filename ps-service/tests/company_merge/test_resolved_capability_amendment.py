"""An amended version lands its Capability where the first version's semantic resolution put it.

The first version resolves its Capability `X` onto `A` by similarity and leaves a
`ResolvedCapability {id: X, resolves_to: A}` marker. An unrelated regulation then brings `B`,
closer to `X` than `A` is. The amended version regenerates `X` (ids are content hashes) and
follows the marker to `A` before dedup, so `X` is never scored, embedded, minted or reviewed again
and the amended Obligations `REQUIRE` `A`, never `B`. These tests drive the real
`merge_baseline_graph` over the gateway rig and read the graph and the logs it leaves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from company_merge._gateway_fakes import RigSubmitter, family_a_baseline_graph
from company_merge._resolution_fixtures import (
    AMENDED_VERSION,
    EMBED_MODEL,
    FIRST_VERSION,
    MERGE,
    NEWER_ID,
    RESOLVED_ID,
    RESOLVED_NAME,
    TARGET_ID,
    ByNameEmbedding,
    obligation_of,
    seed_target,
    unrelated_baseline,
    version_baseline,
)
from company_merge._seed_merge_graphs import BaselineGraphReader
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.graph_gateway.models import UpsertNode

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines
    from company_merge._gateway_fakes import FamilyABaseline, MergeRig
    from ps_service.graph_gateway.models import GroupOutcome, MutationGroup
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.logging import LogEmitter

_MARKER = "ResolvedCapability"


class _RecordingSubmitter(RigSubmitter):
    """The rig's gateway, keeping every group submitted to it (applied or not)."""

    def __init__(self, rig: MergeRig) -> None:
        super().__init__(rig)
        self.submitted: list[MutationGroup] = []

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        self.submitted.append(group)
        return super().submit_group(group)


def _ingest(
    merge_rig: MergeRig,
    baseline: FamilyABaseline,
    caller: ByNameEmbedding,
    emitter: LogEmitter,
    submitter: GroupSubmitter | None = None,
) -> None:
    keywords = merge_rig.merge_keywords()
    if submitter is not None:
        keywords["submitter"] = submitter
    merge_baseline_graph(
        baseline.instrument_id,
        baseline_graph=BaselineGraphReader(family_a_baseline_graph(baseline)),
        single_tenant_graph=merge_rig.legacy,
        embed_model=EMBED_MODEL,
        merge_threshold=MERGE,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=caller,
        emitter=emitter,
        **keywords,
    )


def _ingest_first_version_then_the_newer_capability(
    merge_rig: MergeRig, caller: ByNameEmbedding, make_emitter: MakeEmitter
) -> None:
    seed_target(merge_rig.policy_system)
    _ingest(merge_rig, version_baseline(FIRST_VERSION), caller, make_emitter()[0])
    _ingest(merge_rig, unrelated_baseline(), caller, make_emitter()[0])


def _requires_targets_of(merge_rig: MergeRig, obligation: str) -> set[str]:
    return {
        key[4]
        for key in merge_rig.policy_system.edges
        if key[0] == "REQUIRES" and key[2] == obligation
    }


def _edges_touching(merge_rig: MergeRig, node_id: str) -> dict[tuple[str, ...], dict[str, object]]:
    return {
        key: dict(props)
        for key, props in merge_rig.policy_system.edges.items()
        if node_id in (key[2], key[4])
    }


def test_an_amended_version_lands_its_obligations_on_the_capability_chosen_first(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    caller = ByNameEmbedding()
    _ingest_first_version_then_the_newer_capability(merge_rig, caller, make_emitter)
    amended = version_baseline(AMENDED_VERSION)

    _ingest(merge_rig, amended, caller, make_emitter()[0])

    assert ("Capability", NEWER_ID) in merge_rig.policy_system.nodes
    assert _requires_targets_of(merge_rig, obligation_of(amended)) == {TARGET_ID}


def test_an_amended_version_creates_no_requires_edge_to_a_newer_closer_capability(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    caller = ByNameEmbedding()
    _ingest_first_version_then_the_newer_capability(merge_rig, caller, make_emitter)
    amended = version_baseline(AMENDED_VERSION)

    _ingest(merge_rig, amended, caller, make_emitter()[0])

    assert NEWER_ID not in _requires_targets_of(merge_rig, obligation_of(amended))


def test_an_amended_version_never_mints_or_embeds_the_resolved_id(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    caller = ByNameEmbedding()
    _ingest_first_version_then_the_newer_capability(merge_rig, caller, make_emitter)
    calls_before = len(caller.calls)

    _ingest(merge_rig, version_baseline(AMENDED_VERSION), caller, make_emitter()[0])

    assert ("Capability", RESOLVED_ID) not in merge_rig.policy_system.nodes
    assert RESOLVED_NAME not in caller.calls[calls_before:]
    assert not [
        props
        for (label, _), props in merge_rig.policy_system.nodes.items()
        if label == "PendingReview" and props.get("incoming_id") == RESOLVED_ID
    ]


def test_an_amended_version_leaves_the_prior_versions_edges_unchanged(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    caller = ByNameEmbedding()
    _ingest_first_version_then_the_newer_capability(merge_rig, caller, make_emitter)
    first_obligation = obligation_of(version_baseline(FIRST_VERSION))
    before = _edges_touching(merge_rig, first_obligation)

    _ingest(merge_rig, version_baseline(AMENDED_VERSION), caller, make_emitter()[0])

    assert _edges_touching(merge_rig, first_obligation) == before
    assert _requires_targets_of(merge_rig, first_obligation) == {TARGET_ID}


def test_rerunning_the_same_merge_keeps_exactly_one_marker(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    caller = ByNameEmbedding()
    seed_target(merge_rig.policy_system)
    _ingest(merge_rig, version_baseline(FIRST_VERSION), caller, make_emitter()[0])
    groups_after_first_run = len(merge_rig.groups)
    second_run = _RecordingSubmitter(merge_rig)

    _ingest(merge_rig, version_baseline(FIRST_VERSION), caller, make_emitter()[0], second_run)

    markers = {
        node_id: props
        for (label, node_id), props in merge_rig.policy_system.nodes.items()
        if label == _MARKER
    }
    assert markers == {RESOLVED_ID: {"resolves_to": TARGET_ID}}
    assert second_run.submitted
    assert not [
        primitive
        for group in second_run.submitted
        for primitive in group.primitives
        if isinstance(primitive, UpsertNode) and primitive.label == _MARKER
    ]
    logged_by_second_run = merge_rig.groups[groups_after_first_run:]
    assert not [
        entry for group in logged_by_second_run for entry in group.entries if entry.name == _MARKER
    ]


def test_a_redirected_capability_is_logged_as_redirected_with_its_terminal(
    merge_rig: MergeRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    caller = ByNameEmbedding()
    _ingest_first_version_then_the_newer_capability(merge_rig, caller, make_emitter)
    emitter, log_path = make_emitter(filename="amended.jsonl")

    _ingest(merge_rig, version_baseline(AMENDED_VERSION), caller, emitter)
    emitter.flush()

    decisions = [
        entry
        for entry in read_lines(log_path)
        if entry.get("action") == "dedupe_canonical_nodes" and entry.get("entity_id") == RESOLVED_ID
    ]
    [entry] = decisions
    assert entry["component"] == "company_merge"
    assert entry["outcome"] == "redirected"
    assert entry["kind"] == "Capability"
    assert entry["terminal_id"] == TARGET_ID
    assert entry["marker"] == _MARKER
    assert entry["regulatory_instrument_id"] == AMENDED_VERSION
    assert RESOLVED_NAME not in str(entry)
