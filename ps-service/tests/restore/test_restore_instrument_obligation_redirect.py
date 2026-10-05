"""Offline restore follows `MergedObligation` markers (issue #190, H1 / CHANGES.md A7).

Re-restoring an artifact that regenerates an Obligation a Compliance Officer cleanup merge
absorbed must not mint it again: the offline merge drops it, re-targets its `SATISFIED_BY` /
`REQUIRES` edges onto the survivor, and re-MERGEs `HAS` onto the survivor (idempotent).
Fakes are hand-written and satisfy `GraphHandle` structurally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

import ps_service.restore.restore_instrument as restore_instrument_module
from ps_service.company_merge.errors import CompanyMergeValidationError
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.logging import configure

if TYPE_CHECKING:
    from falkordb import FalkorDB

    from ps_service.company_merge.falkordb_client import GraphHandle

_THRESHOLD = 0.85
_INSTRUMENT_ID = "REG-R-1.0"
_ROLE = "role_r"
_REQUIREMENT = "REG-R-1.0_req_art_1.1"
_ABSORBED = obligation_id(_ROLE, "Do the thing.")
_SURVIVOR = obligation_id(_ROLE, "Do the thing")
_CAP = capability_id("Thing Doing")


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


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
    """One Role, one Requirement, the regenerated (absorbed) Obligation requiring one Capability."""

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        del params
        if "[:HAS]" in q:
            return _FakeQueryResult([[_ROLE, _ABSORBED]])
        if "[:SATISFIED_BY]" in q:
            return _FakeQueryResult([[_REQUIREMENT, _ABSORBED]])
        if "[:REQUIRES]" in q:
            return _FakeQueryResult([[_ABSORBED, _CAP]])
        if any(f"(n:{label})" in q for label in ("PracticeArea", "RiskPath")):
            return _FakeQueryResult([])
        if "n.description" in q:
            return _FakeQueryResult([[_CAP, "Thing Doing", 0.8, None, None]])
        if "(n:Obligation) RETURN" in q:
            return _FakeQueryResult([[_ABSORBED, "Do the thing.", 0.9]])
        if "n.role_id" in q:
            return _FakeQueryResult([[_REQUIREMENT, "Must do.", "requirement", 0.9, _ROLE]])
        if "n.name, n.confidence" in q:
            return _FakeQueryResult([[_ROLE, "Operator", 0.9]])
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _FakeQueryResult(
                [[_FakeRegulatoryInstrumentNode({"id": _INSTRUMENT_ID, "title": "R"})]]
            )
        return _FakeQueryResult([])


class _FakeSnapshotGraph:
    """The restore snapshot copy of the single-tenant graph, with a `MergedObligation` marker."""

    def __init__(self, *, survivor_exists: bool = True) -> None:
        self._survivor_exists = survivor_exists
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        if "MergedObligation" in q:
            return _FakeQueryResult([[_ABSORBED, _SURVIVOR]])
        if "MATCH (o:Obligation) WHERE o.id IN $ids" in q:
            return _FakeQueryResult([[_SURVIVOR]] if self._survivor_exists else [])
        if "MERGED_INTO" in q or "(n:Capability) RETURN" in q:
            return _FakeQueryResult([])
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


def _restore(snapshot: _FakeSnapshotGraph) -> None:
    fake_db = cast(
        "FalkorDB",
        _FakeDb(
            {
                "baseline__restoring__t": _FakeBaselineGraph(),
                "policy_system__restoring__t": snapshot,
            }
        ),
    )
    restore_instrument_module._run_baseline_merge(  # pyright: ignore[reportPrivateUsage]
        fake_db,
        "baseline__restoring__t",
        _INSTRUMENT_ID,
        {},
        _THRESHOLD,
        "policy_system__restoring__t",
        None,
    )


def test_restore_never_mints_the_absorbed_obligation() -> None:
    snapshot = _FakeSnapshotGraph()

    _restore(snapshot)

    mints = snapshot.matching("MERGE (n:Obligation {id: $id}) ON CREATE SET")
    assert mints == []
    written = [c.params for c in snapshot.calls if c.params is not None and "MERGE (" in c.query]
    assert not any(_ABSORBED in map(str, p.values()) for p in written)


def test_restore_lands_satisfied_by_and_requires_on_the_survivor() -> None:
    snapshot = _FakeSnapshotGraph()

    _restore(snapshot)

    satisfied = [c.params for c in snapshot.matching("[:SATISFIED_BY]")]
    requires = [c.params for c in snapshot.matching("[:REQUIRES]")]
    assert satisfied == [{"source_id": _REQUIREMENT, "target_id": _SURVIVOR}]
    assert requires == [{"source_id": _SURVIVOR, "target_id": _CAP}]


def test_restore_re_merges_the_has_edge_onto_the_survivor_without_a_duplicate() -> None:
    snapshot = _FakeSnapshotGraph()

    _restore(snapshot)

    has_writes = snapshot.matching("[:HAS]")
    assert [c.params for c in has_writes] == [{"source_id": _ROLE, "target_id": _SURVIVOR}]
    assert all("MERGE (s)-[:HAS]->(t)" in c.query for c in has_writes)


def test_restore_fails_closed_when_the_survivor_no_longer_exists() -> None:
    snapshot = _FakeSnapshotGraph(survivor_exists=False)

    with pytest.raises(CompanyMergeValidationError):
        _restore(snapshot)

    assert [c for c in snapshot.calls if "MERGE (" in c.query] == []
