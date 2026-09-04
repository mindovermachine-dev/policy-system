"""Live FalkorDB proof for `ps_service.restore.staging.stage_graph` (CHANGES2.md §3.6).

Supersedes `test_staging_stage_dump_live.py` (PLAN.md Slice 2.5, tested
`stage_dump`, which no longer exists -- and which was permanently `xfail`
for content fidelity due to the `DUMP`/`RESTORE` server defect this
redesign removes entirely).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.export.serialize import serialize_graph
from ps_service.restore.errors import ArtifactContentRejectedError
from ps_service.restore.staging import stage_graph

if TYPE_CHECKING:
    from falkordb import FalkorDB

_ALLOWED_LABELS = frozenset({"Test"})
_ALLOWED_RELATIONSHIP_TYPES = frozenset({"LINKS_TO"})


def _small_graph() -> SerializedGraph:
    return SerializedGraph(
        nodes=(
            SerializedNode(label="Test", properties={"id": "x", "name": "y"}),
            SerializedNode(label="Test", properties={"id": "z"}),
        ),
        edges=(
            SerializedEdge(
                relationship_type="LINKS_TO",
                source_label="Test",
                source_id="x",
                target_label="Test",
                target_id="z",
                properties={"n": 1},
            ),
        ),
    )


@pytest.mark.falkordb_live
def test_stage_graph_populates_a_staged_key_never_touching_key_name_itself(
    live_falkordb: FalkorDB,
) -> None:
    key_name = f"__ac66_slice25_key_{uuid.uuid4().hex}__"
    assert live_falkordb.connection.exists(key_name) == 0

    staged_name = stage_graph(
        live_falkordb,
        _small_graph(),
        key_name,
        allowed_labels=_ALLOWED_LABELS,
        allowed_relationship_types=_ALLOWED_RELATIONSHIP_TYPES,
    )

    try:
        assert staged_name != key_name
        assert staged_name.startswith(f"{key_name}__restoring__")
        assert live_falkordb.connection.exists(key_name) == 0  # never touched

        restored = serialize_graph(graph_query_handle(live_falkordb, staged_name))
        expected = _small_graph()

        # SerializedNode/SerializedEdge carry a `dict` field, so they are not
        # hashable -- compare via sorted lists (dict `==` is order-independent).
        assert sorted(restored.nodes, key=lambda n: n.properties["id"]) == sorted(
            expected.nodes, key=lambda n: n.properties["id"]
        )
        assert sorted(restored.edges, key=lambda e: (e.source_id, e.target_id)) == sorted(
            expected.edges, key=lambda e: (e.source_id, e.target_id)
        )
    finally:
        live_falkordb.connection.delete(staged_name)
        assert live_falkordb.connection.exists(staged_name) == 0


@pytest.mark.falkordb_live
def test_stage_graph_called_twice_produces_two_different_staged_names(
    live_falkordb: FalkorDB,
) -> None:
    key_name = f"__ac66_slice25_key2_{uuid.uuid4().hex}__"

    staged_a = stage_graph(
        live_falkordb,
        _small_graph(),
        key_name,
        allowed_labels=_ALLOWED_LABELS,
        allowed_relationship_types=_ALLOWED_RELATIONSHIP_TYPES,
    )
    staged_b = stage_graph(
        live_falkordb,
        _small_graph(),
        key_name,
        allowed_labels=_ALLOWED_LABELS,
        allowed_relationship_types=_ALLOWED_RELATIONSHIP_TYPES,
    )

    try:
        assert staged_a != staged_b
    finally:
        live_falkordb.connection.delete(staged_a, staged_b)
        assert live_falkordb.connection.exists(staged_a) == 0
        assert live_falkordb.connection.exists(staged_b) == 0


@pytest.mark.falkordb_live
def test_stage_graph_rejects_a_disallowed_label_before_creating_any_staged_key(
    live_falkordb: FalkorDB,
) -> None:
    key_name = f"__ac66_slice25_key3_{uuid.uuid4().hex}__"
    rejected_graph = SerializedGraph(
        nodes=(SerializedNode(label="EvilLabel", properties={"id": "x"}),),
        edges=(),
    )
    prefix = f"{key_name}__restoring__*"
    before = set(live_falkordb.connection.keys(prefix))  # pyright: ignore[reportUnknownMemberType] -- redis-py: `.keys()`'s own stub signature carries an Unknown `**kwargs`

    with pytest.raises(ArtifactContentRejectedError):
        stage_graph(
            live_falkordb,
            rejected_graph,
            key_name,
            allowed_labels=_ALLOWED_LABELS,
            allowed_relationship_types=_ALLOWED_RELATIONSHIP_TYPES,
        )

    after = set(live_falkordb.connection.keys(prefix))  # pyright: ignore[reportUnknownMemberType] -- redis-py: `.keys()`'s own stub signature carries an Unknown `**kwargs`
    assert after == before  # no new staged key appeared
    assert live_falkordb.connection.exists(key_name) == 0


@pytest.mark.falkordb_live
def test_stage_graph_creates_the_staged_key_even_for_a_genuinely_empty_graph(
    live_falkordb: FalkorDB,
) -> None:
    """Batch 5 regression: a genuinely empty `SerializedGraph` (D1's "dump
    whatever is there" -- zero nodes/edges is legitimate content, e.g. an
    instrument's native or baseline leg genuinely has none) makes
    `populate_graph` issue zero write calls, which would otherwise leave
    `staged_name` never actually created (FalkorDB creates a graph key
    lazily, only on its first command). `restore_instrument`'s later
    finalize step (`staging.stage_and_finalize_policy_system_leg`) RENAMEs
    the staged name into place -- a `RENAME` off a key that was never
    created raises `redis.exceptions.ResponseError('no such key')`.
    `stage_graph` must vivify the staged key directly in this case,
    mirroring `snapshot_single_tenant`'s own MA4 empty-key vivify fix.
    """
    key_name = f"__ac66_slice55_empty_key_{uuid.uuid4().hex}__"
    empty_graph = SerializedGraph(nodes=(), edges=())

    staged_name = stage_graph(
        live_falkordb,
        empty_graph,
        key_name,
        allowed_labels=_ALLOWED_LABELS,
        allowed_relationship_types=_ALLOWED_RELATIONSHIP_TYPES,
    )

    try:
        assert live_falkordb.connection.exists(staged_name) == 1
        restored = serialize_graph(graph_query_handle(live_falkordb, staged_name))
        assert restored == empty_graph
    finally:
        live_falkordb.connection.delete(staged_name)
