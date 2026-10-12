"""Tests for `ps_service.policy_lifecycle.graph_writer`'s read functions (`find_*`, `read_*`).

Every write is a Graph Write Gateway group since issue #211 and is tested over the gateway rig
(`test_backfill_gateway.py`, `test_draft_edit_gateway.py`, `test_create_gateway.py`,
`test_claim_create_gateway.py`, `test_cascade_gateway.py`, `test_fork_repoint_gateway.py`). The
fakes here are shallow call recorders matched by a distinguishing substring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ps_service.policy_lifecycle.graph_writer import (
    ControlWithParent,
    ForkGovernance,
    StandardWithParent,
    find_approved_prior,
    find_control_with_parent,
    find_standard_with_parent,
    read_capability_governors,
    read_fork_governance,
    read_policy_tree_for_fork,
)


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


# --- `find_approved_prior` (issue #134, S24) --------------------------------
#
# No production code anywhere in issue #134 ever CREATES a `SUPERSEDED_BY`
# edge (that is #136's own fork tool, `supersedes_policy_id`, explicitly out
# of this issue's scope) -- so these tests seed the edge directly via a
# test-only Cypher `MERGE` against this file's own fake `GraphHandle`, the
# same way a real fixture would have to until #136 ships its own writer.
# A future #136 implementer: this is why no `create_superseded_by_edge`-shaped
# helper exists in `graph_writer.py` yet.


class _SupersededByFakeGraph:
    """A `GraphHandle` double: seeds one `SUPERSEDED_BY` edge, answers `find_approved_prior`'s read.

    `seed_superseded_by` mimics the test-only `MERGE` described above --
    real Cypher is never actually run here, only its effect (one prior id,
    with one status) is modeled, matching this file's own established
    "shallow, call-shape fake" convention (`test_service.py`'s
    `_FakeGraph`/`_ReadFakeGraph`) rather than the richer node-graph model
    `backfill_governance_status`'s own tests above use.
    """

    def __init__(self) -> None:
        self._edges: dict[str, tuple[str, str]] = {}

    def seed_superseded_by(self, *, prior_id: str, prior_status: str, successor_id: str) -> None:
        self._edges[successor_id] = (prior_id, prior_status)

    def query(self, q: str, params: dict[str, object] | None = None) -> _SupersededByQueryResult:
        assert "SUPERSEDED_BY" in q
        assert params is not None
        successor_id = params["successor_policy_id"]
        assert isinstance(successor_id, str)
        edge = self._edges.get(successor_id)
        if edge is None:
            return _SupersededByQueryResult(result_set=[])
        prior_id, prior_status = edge
        if prior_status != "approved":
            return _SupersededByQueryResult(result_set=[])
        return _SupersededByQueryResult(result_set=[[prior_id]])


@dataclass
class _SupersededByQueryResult:
    """A `GraphQueryResult` double with a settable `result_set`.

    Unlike the shared, always-empty `_FakeQueryResult` above.
    """

    result_set: list[object]


def test_approved_prior_linked_via_superseded_by_is_found() -> None:
    graph = _SupersededByFakeGraph()
    graph.seed_superseded_by(prior_id="pol-old", prior_status="approved", successor_id="pol-new")

    assert find_approved_prior(graph, "pol-new") == "pol-old"


def test_draft_prior_linked_via_superseded_by_is_not_returned() -> None:
    graph = _SupersededByFakeGraph()
    graph.seed_superseded_by(prior_id="pol-old", prior_status="draft", successor_id="pol-new")

    assert find_approved_prior(graph, "pol-new") is None


def test_proposed_prior_linked_via_superseded_by_is_not_returned() -> None:
    graph = _SupersededByFakeGraph()
    graph.seed_superseded_by(prior_id="pol-old", prior_status="proposed", successor_id="pol-new")

    assert find_approved_prior(graph, "pol-new") is None


def test_no_superseded_by_edge_returns_none() -> None:
    graph = _SupersededByFakeGraph()

    assert find_approved_prior(graph, "pol-new") is None


# --- `find_standard_with_parent` (issue #136, Slice 3) ---


@dataclass
class _RowQueryResult:
    """A `GraphQueryResult` double with a settable `result_set`.

    Mirrors `_SupersededByQueryResult` above.
    """

    result_set: list[object]


class _FindStandardWithParentFakeGraph:
    """A `GraphHandle` double answering `find_standard_with_parent`'s one-hop query."""

    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def query(self, q: str, params: dict[str, object] | None = None) -> _RowQueryResult:
        del params
        assert "RETURN p.id, p.owner_subject, p.owner_issuer, p.status, s.status, s.title" in q
        return _RowQueryResult(result_set=[list(self._row)] if self._row is not None else [])


def test_find_standard_with_parent_returns_policy_and_standard_fields() -> None:
    graph = _FindStandardWithParentFakeGraph(
        ("pol-1", "alice", "https://issuer.example", "draft", "draft", "Encryption Standard")
    )

    row = find_standard_with_parent(graph, "std-1")

    assert row == StandardWithParent(
        policy_id="pol-1",
        policy_owner_subject="alice",
        policy_owner_issuer="https://issuer.example",
        policy_status="draft",
        standard_status="draft",
        standard_title="Encryption Standard",
    )


def test_find_standard_with_parent_returns_none_when_no_match() -> None:
    graph = _FindStandardWithParentFakeGraph(None)

    assert find_standard_with_parent(graph, "std-missing") is None


# --- `find_control_with_parent` (issue #136, Slice 5) ---


class _FindControlWithParentFakeGraph:
    """A `GraphHandle` double answering `find_control_with_parent`'s two-hop query."""

    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def query(self, q: str, params: dict[str, object] | None = None) -> _RowQueryResult:
        del params
        # Both hops must be present in the query text -- proves this is
        # genuinely `Policy -[:SUPPORTED_BY]-> Standard -[:IMPLEMENTED_BY]->
        # Control`, not a one-hop stand-in.
        assert "SUPPORTED_BY" in q
        assert "IMPLEMENTED_BY" in q
        assert (
            "RETURN s.id, p.id, p.owner_subject, p.owner_issuer, p.status, c.status, c.title" in q
        )
        return _RowQueryResult(result_set=[list(self._row)] if self._row is not None else [])


def test_find_control_with_parent_returns_standard_policy_and_control_fields() -> None:
    graph = _FindControlWithParentFakeGraph(
        (
            "std-1",
            "pol-1",
            "alice",
            "https://issuer.example",
            "draft",
            "draft",
            "Key Rotation Check",
        )
    )

    row = find_control_with_parent(graph, "ctrl-1")

    assert row == ControlWithParent(
        standard_id="std-1",
        policy_id="pol-1",
        policy_owner_subject="alice",
        policy_owner_issuer="https://issuer.example",
        policy_status="draft",
        control_status="draft",
        control_title="Key Rotation Check",
    )


def test_find_control_with_parent_returns_none_when_no_match() -> None:
    graph = _FindControlWithParentFakeGraph(None)

    assert find_control_with_parent(graph, "ctrl-missing") is None


# --- `read_policy_tree_for_fork` / `create_policy_draft`'s `version`/
# `supersedes_policy_id` params (issue #136, Slice 6) -----------------------


class _ReadPolicyTreeForForkFakeGraph:
    """A `GraphHandle` double answering `read_policy_tree_for_fork`'s single query."""

    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _RowQueryResult:
        del params
        assert "RETURN s.id, properties(s), c.id, properties(c)" in q
        return _RowQueryResult(result_set=self._rows)


def test_read_policy_tree_for_fork_returns_full_properties_not_narrow_record() -> None:
    """G7: unlike `read_policy_tree`'s own `StandardRecord`/`ControlRecord`
    (id/title/status only), this carries every content field.
    """
    graph = _ReadPolicyTreeForForkFakeGraph(
        [
            [
                "std-1",
                {
                    "id": "std-1",
                    "title": "Encryption Standard",
                    "status": "approved",
                    "procedure": "rotate keys quarterly",
                },
                "ctrl-1",
                {
                    "id": "ctrl-1",
                    "title": "Key Rotation",
                    "status": "approved",
                    "type": "automated",
                    "evidence_ref": "https://example.com/evidence",
                },
            ]
        ]
    )

    records = read_policy_tree_for_fork(graph, "pol-1")

    assert len(records) == 1
    standard = records[0]
    assert standard.title == "Encryption Standard"
    assert standard.properties["procedure"] == "rotate keys quarterly"
    assert len(standard.controls) == 1
    control = standard.controls[0]
    assert control.title == "Key Rotation"
    assert control.properties["evidence_ref"] == "https://example.com/evidence"


def test_read_policy_tree_for_fork_handles_standard_with_zero_controls() -> None:
    graph = _ReadPolicyTreeForForkFakeGraph(
        [["std-1", {"id": "std-1", "title": "Logging Standard", "status": "approved"}, None, None]]
    )

    records = read_policy_tree_for_fork(graph, "pol-1")

    assert len(records) == 1
    assert records[0].controls == ()


def test_read_policy_tree_for_fork_returns_empty_tuple_for_zero_standards() -> None:
    graph = _ReadPolicyTreeForForkFakeGraph([])

    assert read_policy_tree_for_fork(graph, "pol-1") == ()


# --- `read_capability_governors` (issue #185; the claim itself is gateway-tested in
# `test_claim_create_gateway.py`) --------


class _RowsFakeGraph:
    """Records every `query()` call; returns canned rows for each call in order."""

    def __init__(self, *row_sets: list[object]) -> None:
        self.calls: list[_RecordedCall] = []
        self._row_sets = list(row_sets)

    def query(self, q: str, params: dict[str, object] | None = None) -> _RowsResult:
        self.calls.append(_RecordedCall(q, params))
        rows = self._row_sets.pop(0) if self._row_sets else []
        return _RowsResult(rows)


@dataclass
class _RowsResult:
    result_set: list[object]


def test_read_capability_governors_maps_existing_ids_and_omits_missing_ones() -> None:
    graph = _RowsFakeGraph([["cap_a", None], ["cap_b", "pol-9"]])

    governors = read_capability_governors(graph, ("cap_a", "cap_b", "cap_missing"))

    assert governors == {"cap_a": None, "cap_b": "pol-9"}
    assert len(graph.calls) == 1
    assert graph.calls[0].params == {"capability_ids": ["cap_a", "cap_b", "cap_missing"]}


def test_read_capability_governors_issues_a_read_without_write_keywords() -> None:
    graph = _RowsFakeGraph([])

    read_capability_governors(graph, ("cap_a",))

    query = graph.calls[0].query
    assert "GOVERNED_BY" in query
    assert not any(kw in query for kw in ("MERGE", "CREATE", "SET ", "DELETE"))


# --- fork governance read + atomic approve re-point (issue #185) ------------


def test_read_fork_governance_is_none_without_an_inbound_superseded_by_edge() -> None:
    graph = _RowsFakeGraph([])

    assert read_fork_governance(graph, "pol-new") is None
    assert graph.calls[0].params == {"policy_id": "pol-new"}


def test_read_fork_governance_returns_prior_and_governed_capability_ids() -> None:
    graph = _RowsFakeGraph([["pol-old", ["cap_a", "cap_b"]]])

    governance = read_fork_governance(graph, "pol-new")

    assert governance == ForkGovernance(prior_id="pol-old", capability_ids=("cap_a", "cap_b"))


def test_read_fork_governance_allows_a_prior_that_governs_nothing() -> None:
    graph = _RowsFakeGraph([["pol-old", []]])

    governance = read_fork_governance(graph, "pol-new")

    assert governance == ForkGovernance(prior_id="pol-old", capability_ids=())


def test_read_fork_governance_is_a_pure_read() -> None:
    graph = _RowsFakeGraph([])

    read_fork_governance(graph, "pol-new")

    query = graph.calls[0].query
    assert "SUPERSEDED_BY" in query
    assert not re.search(r"\b(DELETE|MERGE|CREATE|SET|REMOVE)\b", query)
