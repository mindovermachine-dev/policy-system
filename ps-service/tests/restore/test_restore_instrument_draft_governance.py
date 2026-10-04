"""Tests for `restore_instrument`'s draft-governance import (issue #183).

Restoring an internal curated instrument imports its Policy/Standard/Control
tree as `draft`, owned by the restoring caller. This replaces the restore
path's earlier Policy *convergence* (issue #54, S6, AC-BI-021): a draft must
never converge onto an existing canonical Policy, because its draft
Standards/Controls would then hang under a possibly-approved parent. The live
ingestion-to-merge path (`merge_baseline_graph`) keeps its convergence -- that
is proven in `tests/company_merge`, not here.

`_run_baseline_merge` is driven directly against scripted fakes mirroring
`tests/company_merge/test_merge_baseline_graph.py`'s conventions (duplicated
rather than imported cross-directory, matching this suite's per-component fake
convention -- see `tests/restore/conftest.py`'s own docstring), plus a
`_FakeDb` satisfying `_run_baseline_merge`'s `db.select_graph(name)` call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.logging.emitter import LogEmitter

from dataclasses import dataclass

import ps_service.restore.restore_instrument as restore_instrument_module
from ps_service.domain_mapper.identity import policy_id
from ps_service.export.models import SerializedGraph, SerializedNode
from ps_service.restore.errors import RestoreOwnerRequiredError
from restore._fixtures import build_restore_artifact

_THRESHOLD = 0.85
_OWNER = ("alice@example.com", "https://idp.example/")


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


@dataclass
class _FakeNode:
    properties: dict[str, object]


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeBaselineGraph:
    """Answers every `read_baseline_graph` query; only governance content is scripted.

    Policy/Standard/Control are answered for the restore path's unfiltered
    `MATCH (n:<Label>) RETURN n` read with full-property node objects.
    """

    def __init__(
        self,
        *,
        regulatory_instrument_properties: dict[str, object],
        capability_rows: list[object],
        policy_nodes: list[dict[str, object]],
        standard_nodes: list[dict[str, object]],
        control_nodes: list[dict[str, object]],
        governed_by_rows: list[object],
        supported_by_rows: list[object],
        implemented_by_rows: list[object],
    ) -> None:
        self._regulatory_instrument_properties = regulatory_instrument_properties
        self._capability_rows: list[object] = [
            [*cast("list[object]", row), None] if len(cast("list[object]", row)) == 4 else row
            for row in capability_rows
        ]
        self._nodes = {
            "Policy": policy_nodes,
            "Standard": standard_nodes,
            "Control": control_nodes,
        }
        self._governed_by_rows = governed_by_rows
        self._supported_by_rows = supported_by_rows
        self._implemented_by_rows = implemented_by_rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        del params
        for label, nodes in self._nodes.items():
            if q == f"MATCH (n:{label}) RETURN n":
                return _FakeQueryResult([[_FakeNode(dict(node))] for node in nodes])
        if "[e:DEFINES]" in q or "[e:EXPRESSES]" in q:
            return _FakeQueryResult([])
        if "[:HAS]" in q or "[:SATISFIED_BY]" in q or "[:REQUIRES]" in q:
            return _FakeQueryResult([])
        if "[:GOVERNED_BY]" in q:
            return _FakeQueryResult(self._governed_by_rows)
        if "[:SUPPORTED_BY]" in q:
            return _FakeQueryResult(self._supported_by_rows)
        if "[:IMPLEMENTED_BY]" in q:
            return _FakeQueryResult(self._implemented_by_rows)
        if "(n:PracticeArea) RETURN" in q or "(n:RiskPath) RETURN" in q:
            return _FakeQueryResult([])
        if "[:COVERS]" in q or "[:OWNS]" in q or "[:MITIGATED_BY]" in q or "[:VERIFIED_BY]" in q:
            return _FakeQueryResult([])
        if "n.role_id" in q:
            return _FakeQueryResult([])  # Requirement
        if "n.description" in q:
            return _FakeQueryResult(self._capability_rows)
        if "n.name, n.confidence" in q:
            return _FakeQueryResult([])  # Role
        if "(n:Obligation) RETURN" in q:
            return _FakeQueryResult([])
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _FakeQueryResult([[_FakeNode(self._regulatory_instrument_properties)]])
        raise AssertionError(f"unexpected query issued: {q!r}")


class _FakeSingleTenantGraph:
    """Answers the Capability/Policy index reads from pre-seeded rows; records every call."""

    def __init__(
        self,
        *,
        capability_rows: list[object] | None = None,
        policy_rows: list[object] | None = None,
    ) -> None:
        self._capability_rows = capability_rows or []
        self._policy_rows = policy_rows or []
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return _FakeQueryResult([list(cast("list[object]", r)) for r in self._capability_rows])
        if "(n:Policy) RETURN n.id, n.title, n.embedding" in q:
            return _FakeQueryResult([list(cast("list[object]", r)) for r in self._policy_rows])
        return _FakeQueryResult([[0]])  # any write

    def calls_matching(self, substring: str) -> list[_RecordedCall]:
        return [call for call in self.calls if substring in call.query]

    def writes(self) -> list[_RecordedCall]:
        read_markers = ("RETURN n.id, n.name, n.embedding", "RETURN n.id, n.title, n.embedding")
        return [c for c in self.calls if not any(m in c.query for m in read_markers)]


class _FakeDb:
    """A plain name -> `GraphHandle` lookup for `_run_baseline_merge`'s `select_graph` calls."""

    def __init__(self, graphs: dict[str, GraphHandle]) -> None:
        self._graphs = graphs

    def select_graph(self, name: str) -> GraphHandle:
        return self._graphs[name]


def _governance_baseline(
    *, instrument_id: str, capability_id_value: str, policy_id_value: str, policy_title: str
) -> _FakeBaselineGraph:
    """One Capability, one Policy, one Standard, one Control, fully wired (internal-sourced)."""
    standard_id_value = f"std_{policy_id_value}_v1"
    control_id_value = f"ctrl_{standard_id_value}_manual"
    return _FakeBaselineGraph(
        regulatory_instrument_properties={"id": instrument_id, "title": "Engineering Practices"},
        capability_rows=[[capability_id_value, "Engineering Review Capability", 0.8, None]],
        policy_nodes=[{"id": policy_id_value, "title": policy_title, "status": "draft"}],
        standard_nodes=[{"id": standard_id_value, "title": "Code Review Standard"}],
        control_nodes=[{"id": control_id_value, "title": "Peer Review", "type": "manual"}],
        governed_by_rows=[[capability_id_value, policy_id_value]],
        supported_by_rows=[[policy_id_value, standard_id_value]],
        implemented_by_rows=[[standard_id_value, control_id_value]],
    )


def _run_merge(
    baseline: _FakeBaselineGraph,
    snapshot: _FakeSingleTenantGraph,
    *,
    instrument_id: str,
    emitter: LogEmitter,
    policy_embeddings: dict[str, tuple[float, ...]] | None = None,
    owner: tuple[str, str] | None,
) -> dict[str, int]:
    baseline_staged_name = "engprac_baseline__restoring__token"
    snapshot_name = "policy_system__restoring__token"
    fake_db = cast("FalkorDB", _FakeDb({baseline_staged_name: baseline, snapshot_name: snapshot}))
    return restore_instrument_module._run_baseline_merge(  # pyright: ignore[reportPrivateUsage]
        fake_db,
        baseline_staged_name,
        instrument_id,
        {},
        _THRESHOLD,
        snapshot_name,
        emitter,
        restore_instrument_module.GovernanceImport(
            owner=owner, policy_embeddings=policy_embeddings or {}
        ),
    )


def test_restore_mints_draft_policy_owned_by_the_caller_instead_of_converging(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-001/003: an incoming Policy that would semantically match an existing
    canonical Policy is still minted as its own `draft` node, owned by the caller.
    """
    emitter, _log_path = make_emitter()
    instrument_id = "ENGPRAC-3.0"
    existing_policy_id = policy_id("Engineering Practices Policy")
    incoming_title = "Engineering Practice Policy"  # reworded -> distinct id
    incoming_policy_id = policy_id(incoming_title)
    assert incoming_policy_id != existing_policy_id

    baseline = _governance_baseline(
        instrument_id=instrument_id,
        capability_id_value="cap_engineering_review_abc",
        policy_id_value=incoming_policy_id,
        policy_title=incoming_title,
    )
    # An existing Policy carrying the SAME vector as the incoming one -- under the old
    # convergence path this would have resolved the incoming Policy onto it.
    snapshot = _FakeSingleTenantGraph(
        policy_rows=[[existing_policy_id, "Engineering Practices Policy", [1.0, 0.0]]]
    )

    _run_merge(
        baseline,
        snapshot,
        instrument_id=instrument_id,
        emitter=emitter,
        policy_embeddings={incoming_policy_id: (1.0, 0.0)},
        owner=_OWNER,
    )

    policy_mints = snapshot.calls_matching("MERGE (n:Policy {id: $id}) ON CREATE SET")
    assert len(policy_mints) == 1
    assert policy_mints[0].params is not None
    assert policy_mints[0].params["id"] == incoming_policy_id
    properties = cast("dict[str, object]", policy_mints[0].params["properties"])
    assert properties["status"] == "draft"
    assert properties["owner_subject"] == _OWNER[0]
    assert properties["owner_issuer"] == _OWNER[1]

    governed_by = snapshot.calls_matching("[:GOVERNED_BY]")
    assert len(governed_by) == 1
    assert governed_by[0].params is not None
    assert governed_by[0].params["target_id"] == incoming_policy_id, (
        "the governance edge must target the draft Policy, not the existing one it resembles"
    )
    assert not snapshot.calls_matching("(n:Policy) RETURN n.id, n.title, n.embedding"), (
        "restore must not read the Policy index at all -- there is no Policy dedup"
    )


def test_restore_writes_standard_and_control_as_draft_and_wires_their_edges(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-003/006: the whole tree lands as draft with its SUPPORTED_BY/IMPLEMENTED_BY edges."""
    emitter, _log_path = make_emitter()
    instrument_id = "ENGPRAC-3.0"
    pol = policy_id("Access Policy")
    baseline = _governance_baseline(
        instrument_id=instrument_id,
        capability_id_value="cap_access",
        policy_id_value=pol,
        policy_title="Access Policy",
    )
    snapshot = _FakeSingleTenantGraph()

    _run_merge(baseline, snapshot, instrument_id=instrument_id, emitter=emitter, owner=_OWNER)

    for label in ("Standard", "Control"):
        mints = snapshot.calls_matching(f"MERGE (n:{label} {{id: $id}}) ON CREATE SET")
        assert len(mints) == 1
        assert mints[0].params is not None
        assert cast("dict[str, object]", mints[0].params["properties"])["status"] == "draft"
    assert len(snapshot.calls_matching("[:SUPPORTED_BY]")) == 1
    assert len(snapshot.calls_matching("[:IMPLEMENTED_BY]")) == 1


def test_restore_returns_governance_counts_for_the_audit_entry(make_emitter: MakeEmitter) -> None:
    """AC-BI-014: the returned counts (folded into the audit log) include the draft counts."""
    emitter, _log_path = make_emitter()
    instrument_id = "ENGPRAC-3.0"
    baseline = _governance_baseline(
        instrument_id=instrument_id,
        capability_id_value="cap_access",
        policy_id_value=policy_id("Access Policy"),
        policy_title="Access Policy",
    )

    counts = _run_merge(
        baseline,
        _FakeSingleTenantGraph(),
        instrument_id=instrument_id,
        emitter=emitter,
        owner=_OWNER,
    )

    assert counts["governance_policies"] == 1
    assert counts["governance_standards"] == 1
    assert counts["governance_controls"] == 1
    assert counts["governance_status_overridden"] == 0


def test_restore_with_governance_content_and_no_owner_is_refused_before_any_write(
    make_emitter: MakeEmitter,
) -> None:
    """Ownerless drafts are never created: the merge refuses before touching the snapshot."""
    emitter, _log_path = make_emitter()
    instrument_id = "ENGPRAC-3.0"
    baseline = _governance_baseline(
        instrument_id=instrument_id,
        capability_id_value="cap_access",
        policy_id_value=policy_id("Access Policy"),
        policy_title="Access Policy",
    )
    snapshot = _FakeSingleTenantGraph()

    with pytest.raises(RestoreOwnerRequiredError):
        _run_merge(baseline, snapshot, instrument_id=instrument_id, emitter=emitter, owner=None)

    assert snapshot.calls == []


def test_external_baseline_restore_needs_no_owner_and_writes_no_governance(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-007: an external-sourced restore (no Policy nodes) is a no-op for the whole pass."""
    emitter, _log_path = make_emitter()
    instrument_id = "RT60-1.0"
    baseline = _FakeBaselineGraph(
        regulatory_instrument_properties={"id": instrument_id, "title": "RT60"},
        capability_rows=[["cap_no_governance", "Some Capability", 0.8, None]],
        policy_nodes=[],
        standard_nodes=[],
        control_nodes=[],
        governed_by_rows=[],
        supported_by_rows=[],
        implemented_by_rows=[],
    )
    snapshot = _FakeSingleTenantGraph()

    counts = _run_merge(
        baseline, snapshot, instrument_id=instrument_id, emitter=emitter, owner=None
    )

    assert not any(
        "Policy" in c.query or "Standard" in c.query or "Control" in c.query
        for c in snapshot.writes()
    )
    assert counts["governance_policies"] == 0


def test_restore_instrument_refuses_governance_content_without_an_owner_before_the_graph(
    make_emitter: MakeEmitter,
) -> None:
    """The owner check runs before staging: a rejected restore never touches FalkorDB."""
    baseline = SerializedGraph(
        nodes=(
            SerializedNode(label="RegulatoryInstrument", properties={"id": "ENGPRAC-1.0"}),
            SerializedNode(label="Policy", properties={"id": "pol_a", "title": "P"}),
        ),
        edges=(),
    )
    artifact = build_restore_artifact(
        instrument_id="ENGPRAC-1.0",
        short_name="ENGPRAC",
        native_graph=SerializedGraph(nodes=(), edges=()),
        baseline_graph=baseline,
    )

    emitter, _log_path = make_emitter()

    class _UntouchableDb:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"the graph must not be touched, but .{name} was accessed")

    with pytest.raises(RestoreOwnerRequiredError):
        restore_instrument_module.restore_instrument(
            artifact,
            db=cast("FalkorDB", _UntouchableDb()),
            single_tenant_graph_name="policy_system",
            similarity_threshold=_THRESHOLD,
            actor="tester",
            emitter=emitter,
            owner=None,
        )
