"""Replay reads the log in pages, in order, and refuses a log it cannot trust (#207 S10).

A page is cut at the checkpoint position so the digest is taken exactly there; a gap in the
sequence or an entry that does not decode stops the replay at that sequence with a typed error.
Gaps and corrupt entries cannot exist in the real database (its triggers and the content hash
forbid them), so these are proven with a tampered in-memory log (defence in depth, PLAN D22).
"""

from __future__ import annotations

import pytest

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import (
    GraphLogCorruptEntryError,
    GraphLogGapError,
    GraphReplayError,
)
from ps_service.graph_gateway.gateway import GatewaySettings
from ps_service.graph_gateway.models import MutationGroup, UpsertNode

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"


def _node(index: int) -> UpsertNode:
    return UpsertNode(label="Capability", id=f"cap-{index:02d}", properties={"name": f"n{index}"})


def _rig(entries: int, *, checkpoint_at_end: bool = True, page_size: int = 5000) -> GatewayRig:
    rig = GatewayRig(settings=GatewaySettings(replay_page_size=page_size))
    rig.gateway.submit_group(
        MutationGroup(
            graph=_GRAPH,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=tuple(_node(i) for i in range(1, entries + 1)),
            checkpoint_requested=checkpoint_at_end,
        )
    )
    rig.graphs.open(_GRAPH).flush()
    return rig


def test_replay_reads_the_log_in_pages_and_reaches_the_same_digest() -> None:
    whole = _rig(50)
    whole.restart().replay_graph(_GRAPH)
    paged = _rig(50, page_size=7)

    report = paged.restart().replay_graph(_GRAPH)

    assert canonical_digest(paged.graphs.open(_GRAPH)) == canonical_digest(
        whole.graphs.open(_GRAPH)
    )
    assert report.pages == 8
    assert max(size for size in paged.store.page_sizes) <= 7
    assert report.verified_position == 50


def test_a_page_is_cut_at_the_checkpoint_position() -> None:
    rig = GatewayRig(settings=GatewaySettings(replay_page_size=7))
    for count, checkpoint in ((10, True), (5, False)):
        rig.gateway.submit_group(
            MutationGroup(
                graph=_GRAPH,
                audit_event_id=_AUDIT_EVENT_ID,
                primitives=tuple(_node(100 * count + i) for i in range(count)),
                checkpoint_requested=checkpoint,
            )
        )
    rig.graphs.open(_GRAPH).flush()
    rig.store.page_reads.clear()

    report = rig.restart().replay_graph(_GRAPH)

    assert report.verified_position == 10
    assert rig.store.page_reads == [(1, 7), (8, 10), (11, 15)]  # no page crosses position 10


def test_a_gap_in_the_log_raises_a_typed_error_naming_graph_and_sequence() -> None:
    rig = _rig(10, page_size=4)
    rig.store.tamper_gap(_GRAPH, 6)

    with pytest.raises(GraphLogGapError) as raised:
        rig.restart().replay_graph(_GRAPH)

    assert isinstance(raised.value, GraphReplayError)
    assert (raised.value.graph, raised.value.position) == (_GRAPH, 6)
    assert _GRAPH in str(raised.value)
    assert "6" in str(raised.value)


def test_a_corrupt_entry_raises_a_typed_error_naming_graph_and_sequence() -> None:
    rig = _rig(10, page_size=4)
    rig.store.tamper_corrupt(_GRAPH, 6)

    with pytest.raises(GraphLogCorruptEntryError) as raised:
        rig.restart().replay_graph(_GRAPH)

    assert isinstance(raised.value, GraphReplayError)
    assert (raised.value.graph, raised.value.position) == (_GRAPH, 6)
    assert "bogus" not in str(raised.value)


@pytest.mark.parametrize("tamper", ["gap", "corrupt"])
def test_replay_stops_at_the_first_bad_entry_and_applies_nothing_past_it(tamper: str) -> None:
    rig = _rig(10, page_size=100)
    getattr(rig.store, f"tamper_{tamper}")(_GRAPH, 6)
    rig.store.markers[_GRAPH] = 0

    with pytest.raises(GraphReplayError):
        rig.restart().replay_graph(_GRAPH)

    applied = {node_id for _, node_id in rig.graphs.open(_GRAPH).nodes}
    assert applied == {f"cap-{i:02d}" for i in range(1, 6)}
    assert rig.store.markers[_GRAPH] == 0  # a failed replay does not move the marker


def test_a_log_shorter_than_its_head_is_a_gap_and_never_loops() -> None:
    rig = _rig(10, page_size=4)
    rig.store.report_head_extra = 2  # last_position says 12, the log holds 10

    with pytest.raises(GraphLogGapError) as raised:
        rig.restart().replay_graph(_GRAPH)

    assert raised.value.position == 11


def test_a_page_size_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="replay_page_size"):
        GatewaySettings(replay_page_size=0)
