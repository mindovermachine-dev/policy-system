"""Tests for `ps_service.company_merge.graph_writer.persist_canonical_nodes`
(PLAN_REVIEWED.md §10 Increment 11, mint half): the create-only canonical
Capability node writer (Capability is the only canonically-deduped kind since
#42) -- the load-bearing invariant that makes "existing canonical node's
properties are never overwritten" hold.

Since issue #210 the mint is a Graph Write Gateway group: `created_at` is drawn from the injected
`ValueSource` while the group is built, and the embedding travels in the primitive's `embedding`
field (the graph stores it as the node's `embedding`), not in its properties. A canonical id the
graph already holds is decided BEFORE submission from one batched read and left out of the group.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from company_merge._gateway_fakes import POLICY_SYSTEM, stepped_values
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.generated_values import ValueSource
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.graph_writer import (
    build_canonical_node_primitives,
    persist_canonical_nodes,
)
from ps_service.company_merge.models import BaselineNode, CanonicalResolution

if TYPE_CHECKING:
    from company_merge._gateway_fakes import MergeRig
    from ps_service.graph_gateway.models import GraphLogEntry

_FIRST_INSTANT = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def _canonical_node(
    node_id: str = "capability_abc123", text: str = "Do the thing."
) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"text": text, "confidence": 0.9})


def _new(node: BaselineNode, embedding: tuple[float, ...] | None = None) -> CanonicalResolution:
    return CanonicalResolution(
        incoming_id=node.id, canonical_id=node.id, match_kind="new", embedding=embedding
    )


def _persist(
    rig: MergeRig,
    nodes: tuple[BaselineNode, ...],
    resolutions: tuple[CanonicalResolution, ...],
    *,
    kind: str = "Capability",
) -> None:
    assert kind in ("Capability", "Policy")
    persist_canonical_nodes(
        GraphWriteTarget(rig.gateway, POLICY_SYSTEM, rig.audit_event_id),
        rig.legacy,
        nodes,
        resolutions,
        kind="Capability" if kind == "Capability" else "Policy",
        values=stepped_values(),
    )


def _entries(rig: MergeRig) -> list[GraphLogEntry]:
    return [entry for group in rig.groups for entry in group.entries]


def _properties(entry: GraphLogEntry) -> dict[str, object]:
    properties = entry.content["properties"]
    assert isinstance(properties, dict)
    return {str(key): value for key, value in properties.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def test_new_resolution_mints_a_create_only_node_with_embedding_and_created_at(
    merge_rig: MergeRig,
) -> None:
    node = _canonical_node()
    embedding = (0.1, 0.2, 0.3)

    _persist(merge_rig, (node,), (_new(node, embedding),))

    [entry] = _entries(merge_rig)
    assert (entry.name, entry.identity) == ("Capability", node.id)
    assert _properties(entry) == {
        "text": "Do the thing.",
        "confidence": 0.9,
        "created_at": _FIRST_INSTANT.isoformat(),
    }
    assert entry.embedding == embedding  # fixed in the primitive, not in its properties
    assert merge_rig.policy_system.nodes[("Capability", node.id)]["embedding"] == [0.1, 0.2, 0.3]


def test_new_resolution_with_no_embedding_gets_no_embedding(merge_rig: MergeRig) -> None:
    """A mint that never triggered any comparison (embedding=None) carries no embedding at all --
    not `None`, not an empty list; its embedding is filled in later (set-if-null backfill).
    """
    node = _canonical_node()

    _persist(merge_rig, (node,), (_new(node),))

    [entry] = _entries(merge_rig)
    assert entry.embedding is None
    assert "embedding" not in merge_rig.policy_system.nodes[("Capability", node.id)]
    assert _properties(entry) == {
        "text": "Do the thing.",
        "confidence": 0.9,
        "created_at": _FIRST_INSTANT.isoformat(),
    }


def test_exact_match_resolution_submits_nothing(merge_rig: MergeRig) -> None:
    node = _canonical_node()
    resolution = CanonicalResolution(
        incoming_id=node.id, canonical_id=node.id, match_kind="exact", embedding=None
    )

    _persist(merge_rig, (node,), (resolution,))

    assert merge_rig.groups == []


def test_semantic_match_resolution_submits_nothing(merge_rig: MergeRig) -> None:
    node = _canonical_node(node_id="capability_incoming", text="Some duty.")
    resolution = CanonicalResolution(
        incoming_id=node.id,
        canonical_id="capability_existing",
        match_kind="semantic",
        embedding=None,
    )

    _persist(merge_rig, (node,), (resolution,))

    assert merge_rig.groups == []


def test_mixed_resolutions_only_write_for_new(merge_rig: MergeRig) -> None:
    exact_node = _canonical_node(node_id="capability_exact", text="Existing duty.")
    new_node = _canonical_node(node_id="capability_new", text="New duty.")
    semantic_node = _canonical_node(node_id="capability_semantic", text="Matched duty.")
    resolutions = (
        CanonicalResolution(
            incoming_id=exact_node.id,
            canonical_id=exact_node.id,
            match_kind="exact",
            embedding=None,
        ),
        _new(new_node, (0.5, 0.6)),
        CanonicalResolution(
            incoming_id=semantic_node.id,
            canonical_id="capability_existing_other",
            match_kind="semantic",
            embedding=None,
        ),
    )

    _persist(merge_rig, (exact_node, new_node, semantic_node), resolutions)

    [entry] = _entries(merge_rig)
    assert entry.identity == new_node.id
    assert _properties(entry) == {
        "text": "New duty.",
        "confidence": 0.9,
        "created_at": _FIRST_INSTANT.isoformat(),
    }
    assert entry.embedding == (0.5, 0.6)


def test_a_mint_stamps_created_at_as_iso_8601_text(merge_rig: MergeRig) -> None:
    """Issue #35, Slice 1 (PLAN.md §2.2): a `match_kind="new"` mint always carries `created_at`,
    an ISO-8601 UTC timestamp that round-trips through `datetime.fromisoformat` (needed by the
    older-wins winner rule of the near-miss merge).
    """
    node = _canonical_node()

    _persist(merge_rig, (node,), (_new(node),))

    [entry] = _entries(merge_rig)
    created_at = _properties(entry)["created_at"]
    assert isinstance(created_at, str)
    assert datetime.fromisoformat(created_at) == _FIRST_INSTANT


def test_capability_kind_writes_capability_label(merge_rig: MergeRig) -> None:
    node = BaselineNode(
        id="capability_abc123", properties={"name": "Encrypt data", "confidence": 0.8}
    )

    _persist(merge_rig, (node,), (_new(node),))

    assert [entry.name for entry in _entries(merge_rig)] == ["Capability"]


def test_an_existing_canonical_id_keeps_its_properties_and_is_not_resubmitted(
    merge_rig: MergeRig,
) -> None:
    node = _canonical_node()
    merge_rig.policy_system.seed_node(("Capability",), node.id, {"text": "original"})

    _persist(merge_rig, (node,), (_new(node, (0.1, 0.2)),))

    assert merge_rig.groups == []
    assert merge_rig.policy_system.nodes[("Capability", node.id)] == {"text": "original"}


def test_one_created_at_is_drawn_per_new_node_in_resolution_order(merge_rig: MergeRig) -> None:
    first = _canonical_node(node_id="capability_one", text="One.")
    second = _canonical_node(node_id="capability_two", text="Two.")

    _persist(merge_rig, (first, second), (_new(first), _new(second)))

    stamps = [_properties(entry)["created_at"] for entry in _entries(merge_rig)]
    assert stamps == [
        _FIRST_INSTANT.isoformat(),
        datetime(2026, 3, 1, 12, 0, 1, tzinfo=UTC).isoformat(),
    ]


def test_the_builder_draws_created_at_before_anything_is_submitted() -> None:
    """The group is a value: once built, the clock no longer matters."""
    node = _canonical_node()
    draws: list[int] = []

    def _now() -> datetime:
        draws.append(1)
        return _FIRST_INSTANT

    counting = ValueSource(now=_now, new_id=stepped_values().new_id)

    [primitive] = build_canonical_node_primitives(
        set(), (node,), (_new(node),), kind="Capability", values=counting
    )

    assert draws == [1]
    assert primitive.properties["created_at"] == _FIRST_INSTANT.isoformat()


def test_an_unstorable_property_raises_the_persistence_error_before_any_submit(
    merge_rig: MergeRig,
) -> None:
    node = BaselineNode(id="capability_bad", properties={"name": "x", "confidence": float("nan")})

    with pytest.raises(CompanyMergePersistenceError, match=r"Capability 'capability_bad'.*cannot"):
        _persist(merge_rig, (node,), (_new(node),))

    assert merge_rig.groups == []
