"""Slice 8b (issue #210): the merge test doubles no longer interpret graph writes.

Company Merge submits every `policy_system` write as a gateway group, so the stand-in the merge
READS through must refuse a write statement instead of silently applying it (a write that reached
it would be a production write path that skipped the gateway). The old-Cypher interpreter that
still has a job -- Restore's interim snapshot writer (#214) and the frozen-reference agreement
test -- lives in `_legacy_reference.SnapshotWriteStore`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from company_merge._legacy_reference import SnapshotWriteStore
from company_merge._seed_merge_graphs import SingleTenantStore

if TYPE_CHECKING:
    from company_merge._gateway_fakes import MergeRig

_NODE_WRITE = "MERGE (n:Obligation {id: $id}) ON CREATE SET n += $properties"
_EDGE_WRITE = (
    "MATCH (s:Role {id: $source_id}), (t:Obligation {id: $target_id}) MERGE (s)-[:HAS]->(t)"
)
_TESTS = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("statement", [_NODE_WRITE, _EDGE_WRITE])
def test_the_single_tenant_read_double_refuses_a_write_statement(statement: str) -> None:
    store = SingleTenantStore()

    with pytest.raises(AssertionError, match="write"):
        store.query(statement, {"id": "x", "properties": {}, "source_id": "a", "target_id": "b"})

    assert store.nodes == {}


def test_the_rig_read_handle_sees_what_the_gateway_wrote_and_refuses_writes(
    merge_rig: MergeRig,
) -> None:
    merge_rig.policy_system.seed_node(("Obligation",), "obl_1", {"text": "t"})

    rows = merge_rig.legacy.query(
        "MATCH (n:Obligation) WHERE n.id IN $ids RETURN n.id", {"ids": ["obl_1", "obl_2"]}
    ).result_set

    assert rows == [["obl_1"]]
    with pytest.raises(AssertionError, match="write"):
        merge_rig.legacy.query(_NODE_WRITE, {"id": "obl_2", "properties": {}})
    assert ("Obligation", "obl_2") not in merge_rig.policy_system.nodes


def test_the_snapshot_write_store_still_interprets_the_old_node_write() -> None:
    store = SnapshotWriteStore()

    store.query(_NODE_WRITE, {"id": "obl_1", "properties": {"text": "t"}})

    assert store.nodes_of("Obligation") == {"obl_1": {"text": "t"}}


def test_the_mirror_shim_is_gone_from_every_test_package() -> None:
    needle = "Mirror" + "LegacyHandle"
    holders = [
        str(path.relative_to(_TESTS))
        for path in _TESTS.rglob("*.py")
        if needle in path.read_text(encoding="utf-8")
    ]

    assert holders == []
