"""A replay that is interrupted resumes from its progress record (#207 S12a, AC-RD-005).

The progress record is a sentinel node inside the graph being rebuilt (`replay_state`). It is
excluded from the digest, removed when the replay completes, and found again after a restart even
though the applied marker was never rewound. Every test enters at `GraphWriteGateway.replay_graph`.
"""

from __future__ import annotations

import pytest
import redis.exceptions

from graph_gateway._fakes import GatewayRig, InMemoryGraph
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import (
    GraphDigestMismatchError,
    GraphReplayGatedError,
    GraphReplayUnverifiableError,
)
from ps_service.graph_gateway.gateway import GatewaySettings
from ps_service.graph_gateway.models import DigestCheckpoint, MutationGroup, UpsertNode
from ps_service.graph_gateway.replay_state import ReplayState, read_replay_state, write_replay_state

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_ENTRIES = 12
_DOWN = redis.exceptions.ConnectionError("falkordb is down")
_SETTINGS = GatewaySettings(batch_size=1, replay_page_size=4)
"""One write query per entry and four entries per page: the writes of a page are countable."""


def _build(*, checkpoint_at: int | None = None) -> tuple[GatewayRig, str]:
    """Log `_ENTRIES` node upserts (one group each), return the rig and the full-graph digest."""
    rig = GatewayRig(settings=_SETTINGS)
    for number in range(1, _ENTRIES + 1):
        rig.gateway.submit_group(
            MutationGroup(
                graph=_GRAPH,
                audit_event_id=_AUDIT_EVENT_ID,
                primitives=(
                    UpsertNode(label="Capability", id=f"cap-{number}", properties={"n": number}),
                ),
                checkpoint_requested=checkpoint_at == number,
            )
        )
    digest = canonical_digest(rig.graphs.open(_GRAPH))
    rig.graphs.open(_GRAPH).flush()
    return rig, digest


@pytest.mark.parametrize(
    "writes_before_the_fault",
    [
        pytest.param(0, id="before-the-first-write"),
        pytest.param(2, id="mid-first-page"),
        pytest.param(4, id="exactly-at-a-page-boundary"),
        pytest.param(6, id="mid-second-page"),
    ],
)
def test_interrupted_replay_resumes_from_its_progress_record_and_reaches_the_same_digest(
    writes_before_the_fault: int,
) -> None:
    rig, expected = _build()
    graph = rig.graphs.open(_GRAPH)
    graph.fail_after_n_writes(writes_before_the_fault, _DOWN)
    with pytest.raises(redis.exceptions.ConnectionError):
        rig.gateway.replay_graph(_GRAPH)
    graph.heal()

    report = rig.restart().replay_graph(_GRAPH)

    assert report.resumed_from > 0
    assert report.head == _ENTRIES
    assert canonical_digest(graph) == expected


def test_a_replay_that_was_not_interrupted_reports_no_resume() -> None:
    rig, _ = _build()

    report = rig.restart().replay_graph(_GRAPH)

    assert report.resumed_from == 0


def test_resume_does_not_redo_the_pages_the_progress_record_covers() -> None:
    rig, _ = _build()
    graph = rig.graphs.open(_GRAPH)
    graph.fail_after_n_writes(4, _DOWN)  # page one (entries 1..4) is applied, page two fails
    with pytest.raises(redis.exceptions.ConnectionError):
        rig.gateway.replay_graph(_GRAPH)
    graph.heal()
    writes_before = sum(1 for query in graph.queries if query.kind == "write")

    report = rig.restart().replay_graph(_GRAPH)

    resumed_writes = sum(1 for query in graph.queries if query.kind == "write") - writes_before
    assert report.resumed_from == 5
    assert resumed_writes == _ENTRIES - 4


def test_replay_progress_sentinel_is_not_part_of_the_digest() -> None:
    rig, _ = _build()
    graph = rig.graphs.open(_GRAPH)
    graph.fail_after_n_writes(4, _DOWN)
    with pytest.raises(redis.exceptions.ConnectionError):
        rig.gateway.replay_graph(_GRAPH)
    graph.heal()
    with_sentinel = canonical_digest(graph)
    assert read_replay_state(graph) is not None

    graph.replay_state = None

    assert canonical_digest(graph) == with_sentinel


def test_replay_progress_sentinel_is_removed_when_replay_completes() -> None:
    rig, _ = _build()

    rig.restart().replay_graph(_GRAPH)

    assert read_replay_state(rig.graphs.open(_GRAPH)) is None


def test_a_partial_graph_with_a_sentinel_is_detected_though_the_marker_says_applied() -> None:
    rig, expected = _build()
    graph = rig.graphs.open(_GRAPH)
    graph.fail_after_n_writes(4, _DOWN)
    with pytest.raises(redis.exceptions.ConnectionError):
        rig.gateway.replay_graph(_GRAPH)
    graph.heal()
    assert rig.store.read_applied_position(_GRAPH) == _ENTRIES  # the marker never went back

    report = rig.restart().replay_graph(_GRAPH)

    assert report.resumed_from == 5
    assert canonical_digest(graph) == expected


def test_a_failure_leaves_the_progress_record_at_the_last_completed_page() -> None:
    rig, _ = _build()
    graph = rig.graphs.open(_GRAPH)
    graph.fail_after_n_writes(6, _DOWN)
    with pytest.raises(redis.exceptions.ConnectionError):
        rig.gateway.replay_graph(_GRAPH)

    state = read_replay_state(graph)

    assert state is not None
    assert (state.position, state.state) == (4, "in_progress")


# Verification across a resume (CHANGES A2).


@pytest.mark.parametrize(
    ("writes_before_the_fault", "recorded_position"),
    [
        pytest.param(5, 5, id="first-tail-page"),
        pytest.param(9, 9, id="second-tail-page"),
    ],
)
def test_interrupted_after_the_checkpoint_the_resume_keeps_the_verification(
    writes_before_the_fault: int, recorded_position: int
) -> None:
    rig, expected = _build(checkpoint_at=5)
    graph = rig.graphs.open(_GRAPH)
    graph.fail_after_n_writes(writes_before_the_fault, _DOWN)
    with pytest.raises(redis.exceptions.ConnectionError):
        rig.gateway.replay_graph(_GRAPH)
    graph.heal()
    state = read_replay_state(graph)
    assert state is not None
    assert (state.position, state.verified) == (recorded_position, 5)
    scans_before = _digest_scans(graph)

    report = rig.restart().replay_graph(_GRAPH)

    assert report.verified_position == 5
    assert report.resumed_from > 5
    assert _digest_scans(graph) == scans_before  # no second digest after the checkpoint
    assert canonical_digest(graph) == expected


def test_interrupted_exactly_at_the_checkpoint_before_the_digest_the_resume_digests_once() -> None:
    rig, expected = _build(checkpoint_at=5)
    graph = rig.graphs.open(_GRAPH)
    graph.fail_on_read(_DOWN, times=1)  # the digest scan at the checkpoint
    with pytest.raises(redis.exceptions.ConnectionError):
        rig.gateway.replay_graph(_GRAPH)
    state = read_replay_state(graph)
    assert state is not None
    assert (state.position, state.verified) == (5, -1)
    graph.heal()
    scans_before = _digest_scans(graph)

    report = rig.restart().replay_graph(_GRAPH)

    assert report.verified_position == 5
    assert _digest_scans(graph) - scans_before == 1
    assert canonical_digest(graph) == expected


def test_a_corrupted_checkpoint_digest_on_resume_raises_the_same_mismatch() -> None:
    rig, _ = _build(checkpoint_at=5)
    graph = rig.graphs.open(_GRAPH)
    graph.fail_on_read(_DOWN, times=1)
    with pytest.raises(redis.exceptions.ConnectionError):
        rig.gateway.replay_graph(_GRAPH)
    graph.heal()
    rig.store.checkpoints[(_GRAPH, 5)] = DigestCheckpoint(
        graph=_GRAPH, position=5, canonical_digest="sha256:" + "00" * 32
    )

    with pytest.raises(GraphDigestMismatchError) as raised:
        rig.restart().replay_graph(_GRAPH)

    assert (raised.value.graph, raised.value.position) == (_GRAPH, 5)


def test_a_record_past_an_unverified_checkpoint_fails_closed_and_deletes_nothing() -> None:
    rig, _ = _build(checkpoint_at=5)
    graph = rig.graphs.open(_GRAPH)
    for number in range(1, 8):  # a graph holding entries 1..7, built by hand
        graph.nodes[("Capability", f"cap-{number}")] = {"n": number}
    write_replay_state(graph, ReplayState(position=7, state="in_progress", kind="", verified=-1))
    held = dict(graph.nodes)

    gateway = rig.restart()
    with pytest.raises(GraphReplayUnverifiableError) as raised:
        gateway.replay_graph(_GRAPH)

    assert (raised.value.graph, raised.value.position) == (_GRAPH, 5)
    assert graph.nodes == held  # nothing was deleted or rebuilt
    state = read_replay_state(graph)
    assert state is not None
    assert state.state == "failed"
    with pytest.raises(GraphReplayGatedError):
        gateway.submit_group(
            MutationGroup(
                graph=_GRAPH,
                audit_event_id=_AUDIT_EVENT_ID,
                primitives=(UpsertNode(label="Capability", id="late", properties={}),),
            )
        )


def test_a_graph_whose_progress_record_says_failed_stays_gated_without_applying_anything() -> None:
    rig, _ = _build()
    graph = rig.graphs.open(_GRAPH)
    write_replay_state(graph, ReplayState(position=4, state="failed", kind="x", verified=-1))
    writes_before = sum(1 for query in graph.queries if query.kind == "write")

    with pytest.raises(GraphReplayGatedError) as raised:
        rig.restart().replay_graph(_GRAPH)

    assert "failed at log position 4" in str(raised.value)
    assert sum(1 for query in graph.queries if query.kind == "write") == writes_before


def _digest_scans(graph: InMemoryGraph) -> int:
    return sum(1 for query in graph.queries if query.template == "digest_node_scan")
