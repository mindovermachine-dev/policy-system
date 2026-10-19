"""A Capability merge then its unmerge, end to end over the gateway rig.

The real merge executor writes a group through the gateway; the audit row it recorded is then the
history the real unmerge executor plans from, so the snapshot the unmerge reads is exactly the one
the merge produced, not a hand-built twin. The graph after the unmerge is compared with the graph
before the merge: the absorbed Capability is back with its original properties (`created_at`
included) and edges, and its `MergedCapability` marker is gone.

A merge recorded before markers existed (its `after` snapshot keeps the absorbed node as a
`merged` tombstone with a `MERGED_INTO` edge) is unmerged the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest

from graph_cleanup._fakes import ABSORBED, ABSORBED_CREATED_AT, SURVIVOR
from graph_cleanup._merge_runs import run_capability_merge
from graph_cleanup._unmerge_runs import run_capability_unmerge, unmerge_row
from ps_service.audit.models import AuditEventRow
from ps_service.company_merge.dedup import read_existing_canonical_index
from ps_service.company_merge.reviewable_kinds import CAPABILITY
from ps_service.graph_cleanup.audit_actions import CapabilityMergeDetails
from ps_service.graph_cleanup.models import EdgeRecord, GraphSnapshot, NodeRecord
from ps_service.graph_gateway.digest import canonical_digest

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.company_merge.falkordb_client import GraphHandle


def _unmerge(rig: CleanupRig) -> dict[str, object]:
    """Unmerge `ABSORBED`; the approval names the id `run_capability_merge` signed with."""
    return run_capability_unmerge(
        rig, row=unmerge_row(rig, ABSORBED, merge_approval_id="approval-1")
    )


def _premerge_world(rig: CleanupRig) -> None:
    """Both Capabilities active with edges on both; the absorbed one has a `created_at`."""
    rig.seed_capability_merge_world()
    rig.graph.nodes[("Capability", ABSORBED)]["created_at"] = ABSORBED_CREATED_AT
    rig.graph.nodes[("Capability", SURVIVOR)]["created_at"] = "2026-08-01T08:00:00+00:00"


def _as_recorded_before_markers(details: CapabilityMergeDetails) -> CapabilityMergeDetails:
    """The same merge as an earlier version recorded it: the absorbed node a `merged` tombstone."""
    assert details.after is not None
    nodes = tuple(
        NodeRecord(
            label="Capability", id=ABSORBED, properties={"name": "Absorbed", "status": "merged"}
        )
        if node.id == ABSORBED
        else node
        for node in details.after.nodes
    )
    redirect = EdgeRecord(
        rel_type="MERGED_INTO",
        source_label="Capability",
        source_id=ABSORBED,
        target_label="Capability",
        target_id=SURVIVOR,
    )
    after = GraphSnapshot(nodes=nodes, edges=(*details.after.edges, redirect))
    return details.model_copy(update={"after": after})


def _recorded_merge_as_history(rig: CleanupRig, *, old_shape: bool = False) -> None:
    """Make the merge's own `applied` audit row the history the unmerge plans from."""
    [recorded] = rig.audit.rows
    details = CapabilityMergeDetails.model_validate(recorded.details)
    if old_shape:
        details = _as_recorded_before_markers(details)
    rig.audit.history = [
        AuditEventRow(
            id="evt-merge",
            occurred_at=datetime(2026, 10, 5, tzinfo=UTC),
            actor_subject=recorded.actor_subject,
            actor_issuer=recorded.actor_issuer,
            action="capability.merge",
            resource_type="capability",
            resource_id=ABSORBED,
            outcome="applied",
            details=details.model_dump(mode="json"),
        )
    ]


@pytest.mark.parametrize("old_shape", [False, True], ids=["after-markers", "before-markers"])
def test_merge_then_unmerge_restores_properties_edges_and_created_at(
    cleanup_rig: CleanupRig, *, old_shape: bool
) -> None:
    _premerge_world(cleanup_rig)
    original = canonical_digest(cleanup_rig.graph)
    assert run_capability_merge(cleanup_rig)["merged"] is True
    assert ("Capability", ABSORBED) not in cleanup_rig.graph.nodes
    assert cleanup_rig.graph.nodes[("MergedCapability", ABSORBED)] == {"merged_into": SURVIVOR}
    _recorded_merge_as_history(cleanup_rig, old_shape=old_shape)

    outcome = _unmerge(cleanup_rig)

    assert outcome["unmerged"] is True
    assert cleanup_rig.graph.nodes[("Capability", ABSORBED)] == {
        "name": "Absorbed",
        "type": "technical",
        "confidence": 0.8,
        "created_at": ABSORBED_CREATED_AT,
        "status": "active",
    }
    assert ("MergedCapability", ABSORBED) not in cleanup_rig.graph.nodes
    assert canonical_digest(cleanup_rig.graph) == original


def test_a_survivor_edge_gained_since_the_merge_survives_the_unmerge(
    cleanup_rig: CleanupRig,
) -> None:
    _premerge_world(cleanup_rig)
    assert run_capability_merge(cleanup_rig)["merged"] is True
    _recorded_merge_as_history(cleanup_rig)
    cleanup_rig.seed_endpoint("Obligation", "obl_new")
    cleanup_rig.seed_edge(
        ("REQUIRES", "Obligation", "obl_new", "Capability", SURVIVOR), legacy=False
    )

    assert _unmerge(cleanup_rig)["unmerged"] is True

    on_survivor = {(k[0], k[2]) for k in cleanup_rig.graph.edges if k[4] == SURVIVOR}
    assert ("REQUIRES", "obl_new") in on_survivor
    assert ("REQUIRES", "obl_1") not in on_survivor


class _CanonicalIndexRead:
    """Answers `read_existing_canonical_index`'s query for Capabilities from the rig graph."""

    def __init__(self, rig: CleanupRig) -> None:
        self._rig = rig

    def query(self, q: str, params: dict[str, object] | None = None) -> _Rows:
        del params
        assert q == "MATCH (n:Capability) RETURN n.id, n.name, n.embedding", q
        return _Rows(
            [
                [node_id, props["name"], props.get("embedding")]
                for (label, node_id), props in self._rig.graph.nodes.items()
                if label == "Capability"
            ]
        )


class _Rows:
    def __init__(self, result_set: list[object]) -> None:
        self.result_set = result_set


def test_recreated_capability_without_embedding_is_backfilled_by_the_next_dedup(
    cleanup_rig: CleanupRig,
) -> None:
    """A snapshot never holds an embedding, so the recreated node reads as never cached.

    The dedup index reports `embedding=None` for it, which `dedupe_canonical_nodes` embeds and
    backfills (`test_dedup_*`), exactly like any Capability that was never embedded.
    """
    _premerge_world(cleanup_rig)
    cleanup_rig.graph.nodes[("Capability", ABSORBED)]["embedding"] = [1.0, 0.0]
    assert run_capability_merge(cleanup_rig)["merged"] is True
    _recorded_merge_as_history(cleanup_rig)

    assert _unmerge(cleanup_rig)["unmerged"] is True

    index = {
        n.id: n.embedding
        for n in read_existing_canonical_index(
            cast("GraphHandle", _CanonicalIndexRead(cleanup_rig)), CAPABILITY
        )
    }
    assert index[ABSORBED] is None
