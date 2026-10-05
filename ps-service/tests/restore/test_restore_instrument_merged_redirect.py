"""Offline restore follows `MERGED_INTO` tombstone redirects (issue #190, AC-BI-008/009).

A restored baseline artifact regenerates a Capability whose id is a `merged`
tombstone in the target deployment. The offline Company Merge pass resolves it
through `MERGED_INTO` to the survivor: the incoming `REQUIRES` edge attaches to
the survivor and no Capability node is minted. Fakes are hand-written and
satisfy `GraphHandle` structurally, mirroring the sibling restore tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import ps_service.restore.restore_instrument as restore_instrument_module
from ps_service.domain_mapper.identity import capability_id, obligation_id

if TYPE_CHECKING:
    from falkordb import FalkorDB

    from ps_service.company_merge.falkordb_client import GraphHandle

_THRESHOLD = 0.85
_INSTRUMENT_ID = "REG-R-1.0"


class _FakeQueryResult:
    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeRegulatoryInstrumentNode:
    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeBaselineGraph:
    """One Obligation requiring one Capability; every other `read_baseline_graph` read is empty."""

    def __init__(self, *, capability_id_value: str, obligation_id_value: str) -> None:
        self._capability_id = capability_id_value
        self._obligation_id = obligation_id_value

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "[:REQUIRES]" in q:
            return _FakeQueryResult([[self._obligation_id, self._capability_id]])
        if "(n:PracticeArea)" in q or "(n:RiskPath)" in q:
            return _FakeQueryResult([])
        if "n.description" in q:
            return _FakeQueryResult([[self._capability_id, "Absorbed", 0.8, None, None]])
        if "(n:Obligation) RETURN" in q:
            return _FakeQueryResult([[self._obligation_id, "Do the thing.", 0.9]])
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _FakeQueryResult(
                [[_FakeRegulatoryInstrumentNode({"id": _INSTRUMENT_ID, "title": "R"})]]
            )
        return _FakeQueryResult([])


class _FakeSnapshotGraph:
    """The restore snapshot copy of the single-tenant graph: two Capabilities, one tombstoned."""

    def __init__(self, *, absorbed_id: str, survivor_id: str) -> None:
        self._absorbed_id = absorbed_id
        self._survivor_id = survivor_id
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        if "MergedObligation" in q:
            return _FakeQueryResult([])
        if "MERGED_INTO" in q:
            return _FakeQueryResult([[self._absorbed_id, self._survivor_id]])
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return _FakeQueryResult(
                [
                    [self._absorbed_id, "Absorbed", [1.0, 0.0]],
                    [self._survivor_id, "Survivor", [0.0, 1.0]],
                ]
            )
        if q == "UNWIND $ids AS id MATCH (n {id: id}) RETURN id":
            return _FakeQueryResult([])
        return _FakeQueryResult([[0]])

    def matching(self, substring: str) -> list[_RecordedCall]:
        return [c for c in self.calls if substring in c.query]


class _FakeDb:
    def __init__(self, graphs: dict[str, GraphHandle]) -> None:
        self._graphs = graphs

    def select_graph(self, name: str) -> GraphHandle:
        return self._graphs[name]


def test_restore_attaches_requires_to_survivor_and_mints_no_capability() -> None:
    absorbed_id = capability_id("Absorbed")
    survivor_id = capability_id("Survivor")
    obl_id = obligation_id("role_r", "Do the thing.")
    baseline = _FakeBaselineGraph(capability_id_value=absorbed_id, obligation_id_value=obl_id)
    snapshot = _FakeSnapshotGraph(absorbed_id=absorbed_id, survivor_id=survivor_id)
    fake_db = cast(
        "FalkorDB",
        _FakeDb({"baseline__restoring__t": baseline, "policy_system__restoring__t": snapshot}),
    )

    restore_instrument_module._run_baseline_merge(  # pyright: ignore[reportPrivateUsage]
        fake_db,
        "baseline__restoring__t",
        _INSTRUMENT_ID,
        {absorbed_id: (1.0, 0.0)},
        _THRESHOLD,
        "policy_system__restoring__t",
        None,
    )

    assert snapshot.matching("MERGE (n:Capability {id: $id}) ON CREATE SET") == []
    requires_writes = snapshot.matching("[:REQUIRES]")
    assert [c.params for c in requires_writes] == [{"source_id": obl_id, "target_id": survivor_id}]


def test_restore_never_scores_a_tombstone_as_a_semantic_candidate() -> None:
    """AC-BI-009 offline half: a tombstone's cached embedding is not in the candidate pool."""
    tomb_id = capability_id("Absorbed")
    survivor_id = capability_id("Survivor")
    new_id = capability_id("Brand New")
    obl_id = obligation_id("role_r", "Do the thing.")
    baseline = _FakeBaselineGraph(capability_id_value=new_id, obligation_id_value=obl_id)
    snapshot = _FakeSnapshotGraph(absorbed_id=tomb_id, survivor_id=survivor_id)
    fake_db = cast(
        "FalkorDB",
        _FakeDb({"baseline__restoring__t": baseline, "policy_system__restoring__t": snapshot}),
    )

    # Identical to the tombstone's vector: would semantic-merge onto it if it were a candidate.
    restore_instrument_module._run_baseline_merge(  # pyright: ignore[reportPrivateUsage]
        fake_db,
        "baseline__restoring__t",
        _INSTRUMENT_ID,
        {new_id: (1.0, 0.0)},
        _THRESHOLD,
        "policy_system__restoring__t",
        None,
    )

    requires_writes = snapshot.matching("[:REQUIRES]")
    assert [c.params for c in requires_writes] == [{"source_id": obl_id, "target_id": new_id}]
    assert len(snapshot.matching("MERGE (n:Capability {id: $id}) ON CREATE SET")) == 1
