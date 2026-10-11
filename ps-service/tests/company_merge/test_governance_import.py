"""Tests for `ps_service.company_merge.governance_import` (issue #227, AC-BI-005).

The draft-governance pass is shared by restore and live internal ingest: a
Policy/Standard/Control tree is imported as `draft`, owned by the importing
caller, written only with `MERGE ... ON CREATE SET`. These tests drive the real
pass against a stateful fake of the FalkorDB boundary that honours
`ON CREATE SET`, so the "never overwrites an existing node" guarantee is proven
against behaviour, not Cypher text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

import pytest

from ps_service.company_merge.errors import GovernanceOwnerRequiredError
from ps_service.company_merge.governance_import import (
    GovernanceImport,
    governance_count_fields,
    import_draft_governance,
)
from ps_service.company_merge.models import BaselineGraph, BaselineNode, GovernanceDraftCounts

_OWNER = ("alice@example.com", "https://idp.example/")
_UPSERT = re.compile(r"^MERGE \(n:(?P<label>\w+) \{id: \$id\}\) ON CREATE SET n \+= \$properties$")


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeQueryResult:
    @property
    def result_set(self) -> list[object]:
        return []


class _StatefulFakeGraph:
    """Records every call and keeps nodes keyed by `(label, id)`, honouring `ON CREATE SET`."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []
        self.nodes: dict[tuple[str, str], dict[str, object]] = {}

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        match = _UPSERT.match(q)
        assert match is not None, f"every governance write must be ON CREATE SET, got {q!r}"
        assert params is not None
        key = (match["label"], cast("str", params["id"]))
        if key not in self.nodes:
            self.nodes[key] = dict(cast("dict[str, object]", params["properties"]))
        return _FakeQueryResult()


def _baseline(
    *,
    policies: tuple[BaselineNode, ...] = (),
    standards: tuple[BaselineNode, ...] = (),
    controls: tuple[BaselineNode, ...] = (),
) -> BaselineGraph:
    return BaselineGraph(
        regulatory_instrument_id="ENGPRAC-3.0",
        regulatory_instrument_properties={"id": "ENGPRAC-3.0"},
        role_nodes=(),
        requirement_nodes=(),
        obligation_nodes=(),
        capability_nodes=(),
        provenance_edges=(),
        bare_edges=(),
        policy_nodes=policies,
        standard_nodes=standards,
        control_nodes=controls,
    )


def _policy(status: str = "draft") -> BaselineNode:
    return BaselineNode(id="pol_a", properties={"title": "Access Policy", "status": status})


def _standard() -> BaselineNode:
    return BaselineNode(id="std_a", properties={"title": "Access Standard", "status": "approved"})


def _control() -> BaselineNode:
    return BaselineNode(id="ctrl_a", properties={"title": "Access Control", "type": "manual"})


def test_baseline_without_governance_writes_nothing_and_returns_none() -> None:
    graph = _StatefulFakeGraph()
    mapping: dict[str, str] = {}

    counts = import_draft_governance(
        _baseline(),
        single_tenant_graph=graph,
        governance=GovernanceImport(),
        canonical_id_by_incoming_id=mapping,
    )

    assert counts is None
    assert graph.calls == []
    assert mapping == {}


def test_governance_content_without_an_owner_is_refused_before_any_write() -> None:
    graph = _StatefulFakeGraph()

    with pytest.raises(GovernanceOwnerRequiredError):
        import_draft_governance(
            _baseline(policies=(_policy(),)),
            single_tenant_graph=graph,
            governance=GovernanceImport(owner=None),
            canonical_id_by_incoming_id={},
        )

    assert graph.calls == []


def test_tree_is_written_as_draft_owned_by_the_caller_with_on_create_set_only() -> None:
    graph = _StatefulFakeGraph()
    mapping: dict[str, str] = {}

    counts = import_draft_governance(
        _baseline(policies=(_policy(),), standards=(_standard(),), controls=(_control(),)),
        single_tenant_graph=graph,
        governance=GovernanceImport(owner=_OWNER),
        canonical_id_by_incoming_id=mapping,
    )

    assert counts == GovernanceDraftCounts(policies=1, standards=1, controls=1, status_overridden=1)
    assert graph.nodes[("Policy", "pol_a")]["status"] == "draft"
    assert graph.nodes[("Policy", "pol_a")]["owner_subject"] == _OWNER[0]
    assert graph.nodes[("Policy", "pol_a")]["owner_issuer"] == _OWNER[1]
    assert graph.nodes[("Standard", "std_a")]["status"] == "draft"
    assert graph.nodes[("Control", "ctrl_a")]["status"] == "draft"
    assert mapping == {"pol_a": "pol_a"}


def test_an_existing_approved_node_is_never_overwritten_by_a_reimport() -> None:
    graph = _StatefulFakeGraph()
    graph.nodes[("Policy", "pol_a")] = {"status": "approved", "owner_subject": "someone-else"}

    import_draft_governance(
        _baseline(policies=(_policy(),)),
        single_tenant_graph=graph,
        governance=GovernanceImport(owner=_OWNER),
        canonical_id_by_incoming_id={},
    )

    assert graph.nodes[("Policy", "pol_a")] == {
        "status": "approved",
        "owner_subject": "someone-else",
    }


def test_count_fields_are_all_zero_without_governance_content() -> None:
    assert governance_count_fields(None) == {
        "governance_policies": 0,
        "governance_standards": 0,
        "governance_controls": 0,
        "governance_status_overridden": 0,
    }


def test_count_fields_carry_the_written_counts() -> None:
    counts = GovernanceDraftCounts(policies=11, standards=21, controls=39, status_overridden=2)

    assert governance_count_fields(counts) == {
        "governance_policies": 11,
        "governance_standards": 21,
        "governance_controls": 39,
        "governance_status_overridden": 2,
    }
