"""A replay that failed leaves its graph gated across a restart (#207 S18, AC-RD-011 persistence).

The failure is written into the graph's progress record (`state="failed"`, `kind` = the error
class, `position` = the position the failure names), so a restarted service finds it by probing the
graph, applies nothing, deletes nothing and reports the graph gated. The remedy is the operator's:
delete the graph key (and fix the cause); the next startup then rebuilds it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import (
    GraphReplayError,
    GraphReplayGatedError,
    GraphReplayStoppedError,
)
from ps_service.graph_gateway.gateway import GatewaySettings
from ps_service.graph_gateway.models import (
    DigestCheckpoint,
    MutationGroup,
    StartupReplayReport,
    UpsertNode,
)
from ps_service.graph_gateway.replay_state import read_replay_state

if TYPE_CHECKING:
    from collections.abc import Callable

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_ENTRIES = 6
_BAD = "sha256:" + "00" * 32


def _build(*, checkpoint_at: int | None = None) -> tuple[GatewayRig, str]:
    rig = GatewayRig(settings=GatewaySettings(replay_page_size=2))
    for number in range(1, _ENTRIES + 1):
        rig.gateway.submit_group(
            MutationGroup(
                graph=_GRAPH,
                audit_event_id=_AUDIT_EVENT_ID,
                primitives=(UpsertNode(label="Capability", id=f"cap-{number}", properties={}),),
                checkpoint_requested=checkpoint_at == number,
            )
        )
    digest = canonical_digest(rig.graphs.open(_GRAPH))
    rig.graphs.open(_GRAPH).flush()
    return rig, digest


def _writes(rig: GatewayRig) -> int:
    return sum(1 for q in rig.graphs.open(_GRAPH).queries if q.kind in {"write", "index"})


def _digest_scans(rig: GatewayRig) -> int:
    return sum(1 for q in rig.graphs.open(_GRAPH).queries if q.template.startswith("digest_"))


def _corrupt_checkpoint(rig: GatewayRig, position: int) -> None:
    rig.store.checkpoints[(_GRAPH, position)] = DigestCheckpoint(
        graph=_GRAPH, position=position, canonical_digest=_BAD
    )


def _tamper_checkpoint(rig: GatewayRig) -> None:
    _corrupt_checkpoint(rig, 4)


def _tamper_gap(rig: GatewayRig) -> None:
    rig.store.tamper_gap(_GRAPH, 5)


def _tamper_entry(rig: GatewayRig) -> None:
    rig.store.tamper_corrupt(_GRAPH, 5)


def test_a_graph_that_failed_verification_is_still_gated_after_a_restart() -> None:
    rig, _ = _build(checkpoint_at=4)
    _corrupt_checkpoint(rig, 4)
    first = rig.restart().startup_replay()
    assert first == StartupReplayReport(gated=(_GRAPH,))
    nodes_after_failure = dict(rig.graphs.open(_GRAPH).nodes)
    writes, scans = _writes(rig), _digest_scans(rig)

    restarted = rig.restart()
    second = restarted.startup_replay()

    assert second == StartupReplayReport(gated=(_GRAPH,))
    assert restarted.gated_graphs() == (_GRAPH,)
    assert _writes(rig) == writes  # nothing re-applied
    assert _digest_scans(rig) == scans  # and nothing re-verified: the record already says failed
    assert rig.graphs.open(_GRAPH).nodes == nodes_after_failure  # nothing flushed or deleted
    with pytest.raises(GraphReplayGatedError) as caught:
        restarted.submit_group(
            MutationGroup(
                graph=_GRAPH,
                audit_event_id=_AUDIT_EVENT_ID,
                primitives=(UpsertNode(label="Capability", id="late", properties={}),),
            )
        )
    assert caught.value.position == 4


@pytest.mark.parametrize(
    ("tamper", "kind", "position"),
    [
        pytest.param(_tamper_checkpoint, "GraphDigestMismatchError", 4),
        pytest.param(_tamper_gap, "GraphLogGapError", 5),
        pytest.param(_tamper_entry, "GraphLogCorruptEntryError", 5),
    ],
    ids=["digest-mismatch", "gap", "corrupt-entry"],
)
def test_every_kind_of_replay_failure_is_recorded_in_the_graph_as_failed(
    tamper: Callable[[GatewayRig], None], kind: str, position: int
) -> None:
    rig, _ = _build(checkpoint_at=4)
    tamper(rig)
    with pytest.raises(GraphReplayError):
        rig.restart().replay_graph(_GRAPH)

    state = read_replay_state(rig.graphs.open(_GRAPH))

    assert state is not None
    assert (state.state, state.kind, state.position) == ("failed", kind, position)


def test_a_stopped_or_infrastructure_interrupted_replay_is_not_recorded_as_failed() -> None:
    rig, _ = _build()
    rig.gateway.stop_replay()
    with pytest.raises(GraphReplayStoppedError):
        rig.gateway.replay_graph(_GRAPH)

    state = read_replay_state(rig.graphs.open(_GRAPH))

    assert state is not None
    assert state.state == "in_progress"


def test_deleting_the_failed_graph_key_lets_the_next_startup_rebuild_it() -> None:
    rig, expected = _build(checkpoint_at=4)
    _corrupt_checkpoint(rig, 4)
    rig.restart().startup_replay()
    # The operator fixes the cause (here: the checkpoint row) and deletes the graph key.
    rig.store.checkpoints.pop((_GRAPH, 4))
    rig.graphs.open(_GRAPH).flush()

    report = rig.restart().startup_replay()

    assert report == StartupReplayReport(replayed=(_GRAPH,))
    assert canonical_digest(rig.graphs.open(_GRAPH)) == expected
    assert read_replay_state(rig.graphs.open(_GRAPH)) is None
