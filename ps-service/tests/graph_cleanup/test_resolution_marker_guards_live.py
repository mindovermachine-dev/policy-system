"""Live FalkorDB proof that the cleanup guards read only `MergedCapability` markers.

A Capability id can carry a `ResolvedCapability` marker that Company Merge records for a semantic
resolution. That marker is not a merge: it names no deleted node and has no snapshot to restore
from, so the merge and unmerge guards must not read it as one. The fast doubles key nodes by
label and id, which proves only the label in the query text; these tests run the real reads of
`graph_cleanup/graph_reader.py` on a real graph that holds both kinds side by side.

Run with:
`PS_TEST_FALKORDB_HOST=127.0.0.1 PS_TEST_FALKORDB_PORT=16379
uv run pytest ps-service/tests/graph_cleanup/test_resolution_marker_guards_live.py
-m falkordb_live -q`
Uses a uniquely named throwaway graph, deleted after each test.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from graph_gateway.live_endpoints import FALKORDB_TEST_TIMEOUTS, falkordb_endpoint

from ps_service.graph_cleanup.graph_reader import (
    read_capability_merged,
    read_capability_present_without_marker,
    read_capability_unmerge_state,
)
from ps_service.graph_cleanup.models import CapabilityUnmergeInputs
from ps_service.graph_gateway.connection import connect, select_graph

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.graph_gateway.connection import WritableGraph as GraphHandle

pytestmark = pytest.mark.falkordb_live

_RESOLVED = "cap_resolved"
_SURVIVOR = "cap_survivor"
_ABSORBED = "cap_absorbed"


@pytest.fixture
def graph() -> Iterator[GraphHandle]:
    host, port = falkordb_endpoint()
    db = connect(host=host, port=port, **FALKORDB_TEST_TIMEOUTS)
    name = f"graph_cleanup_resolution_marker_live_{uuid.uuid4().hex[:8]}"
    handle = select_graph(db, name)
    handle.query(
        "CREATE (:Capability {id: $resolved, name: 'Resolved', status: 'active'}), "
        "(:ResolvedCapability {id: $resolved, resolves_to: $survivor}), "
        "(:Capability {id: $survivor, name: 'Survivor', status: 'active'}), "
        "(:ResolvedCapability {id: $survivor, resolves_to: $resolved}), "
        "(:MergedCapability {id: $absorbed, merged_into: $survivor})",
        {"resolved": _RESOLVED, "survivor": _SURVIVOR, "absorbed": _ABSORBED},
    )
    try:
        yield handle
    finally:
        if name in db.list_graphs():
            db.select_graph(name).delete()


def _unmerge_inputs(*, survivor_id: str, absorbed_id: str) -> CapabilityUnmergeInputs:
    return CapabilityUnmergeInputs(
        merge_approval_id="approval-1",
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        absorbed_properties={"name": "Absorbed"},
        policy_case=1,
        restore_requires=(),
        restore_covers=(),
        restore_mitigated=(),
        remove_requires=(),
        remove_covers=(),
        remove_mitigated=(),
        restore_policy_id=None,
        remove_policy_edge=False,
        snapshot_survivor_edges=(),
    )


def test_live_merge_and_unmerge_guards_ignore_a_same_id_resolution_marker(
    graph: GraphHandle,
) -> None:
    assert read_capability_present_without_marker(graph, capability_id=_RESOLVED) is True
    assert read_capability_merged(graph, survivor_id=_SURVIVOR, absorbed_id=_RESOLVED) is False

    state = read_capability_unmerge_state(
        graph, inputs=_unmerge_inputs(survivor_id=_SURVIVOR, absorbed_id=_RESOLVED)
    )

    assert state.marker_targets == {}
    assert state.absorbed_status == "active"
    assert state.survivor is not None
    assert state.survivor.id == _SURVIVOR


def test_live_unmerge_state_still_reads_a_merge_marker_beside_a_resolution_marker(
    graph: GraphHandle,
) -> None:
    state = read_capability_unmerge_state(
        graph, inputs=_unmerge_inputs(survivor_id=_SURVIVOR, absorbed_id=_ABSORBED)
    )

    assert state.marker_targets == {_ABSORBED: (_SURVIVOR,)}
    assert read_capability_merged(graph, survivor_id=_SURVIVOR, absorbed_id=_ABSORBED) is True
