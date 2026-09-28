"""Tests for `ps_service.policy_lifecycle.graph_writer.backfill_governance_status`
(GH issue #134, S8): the idempotent, self-healing backfill for Standard/Control's
`status` property and Policy's `version` property (D-7).

The fake `GraphHandle` below is richer than the shallow call-recording fakes
used elsewhere in this codebase (e.g. `tests/company_merge/
test_graph_writer_embedding_backfill.py`'s own `_FakeGraph`) because S8's own
test contract (PLAN.md) is a genuine idempotency/no-overwrite guarantee, not
just "issues the expected call shape" -- proving it requires tracking actual
node property state across two calls. It interprets exactly the three fixed
Cypher statements `backfill_governance_status` is known to issue (matched by
a distinguishing substring in each), against an in-memory Policy/Standard/
Control graph built from the nodes/edges the test passes in -- not a general
Cypher engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from ps_service.policy_lifecycle.graph_writer import (
    ControlDraft,
    ControlWithParent,
    StandardDraft,
    StandardWithParent,
    add_control_to_standard,
    add_standard_to_policy,
    backfill_governance_status,
    create_policy_draft,
    find_approved_prior,
    find_control_with_parent,
    find_standard_with_parent,
    read_policy_tree_for_fork,
    update_control_fields,
    update_policy_fields,
    update_standard_fields,
)


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeQueryResult:
    @property
    def result_set(self) -> list[object]:
        return []


@dataclass
class _FakeNode:
    id: str
    properties: dict[str, object] = field(default_factory=dict)


class _FakeGraph:
    """In-memory `Policy -[:SUPPORTED_BY]-> Standard -[:IMPLEMENTED_BY]-> Control` tree.

    `property_write_count` counts every property actually changed across all
    calls -- the mechanism the idempotency test (below) uses to prove a
    second call changes nothing.
    """

    def __init__(
        self,
        *,
        policy: _FakeNode,
        standards: tuple[_FakeNode, ...] = (),
        controls_by_standard_id: dict[str, tuple[_FakeNode, ...]] | None = None,
    ) -> None:
        self.policy = policy
        self.standards = standards
        self.controls_by_standard_id: dict[str, tuple[_FakeNode, ...]] = (
            controls_by_standard_id or {}
        )
        self.calls: list[_RecordedCall] = []
        self.property_write_count = 0

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        assert params is not None
        assert params["policy_id"] == self.policy.id
        if "p.version = coalesce" in q:
            self._backfill_policy_version()
        elif "c.status IS NULL" in q:
            self._backfill_control_status()
        elif "s.status IS NULL" in q:
            self._backfill_standard_status()
        return _FakeQueryResult()

    def _backfill_policy_version(self) -> None:
        if self.policy.properties.get("version") is None:
            self.policy.properties["version"] = "1"
            self.property_write_count += 1

    def _backfill_standard_status(self) -> None:
        policy_status = self.policy.properties["status"]
        for standard in self.standards:
            if standard.properties.get("status") is None:
                standard.properties["status"] = policy_status
                self.property_write_count += 1

    def _backfill_control_status(self) -> None:
        policy_status = self.policy.properties["status"]
        standard_ids = {standard.id for standard in self.standards}
        for standard_id, controls in self.controls_by_standard_id.items():
            if standard_id not in standard_ids:
                continue
            for control in controls:
                if control.properties.get("status") is None:
                    control.properties["status"] = policy_status
                    self.property_write_count += 1


def test_standard_and_control_with_null_status_get_the_policys_status() -> None:
    policy = _FakeNode("pol-1", {"status": "approved"})
    standard = _FakeNode("std-1")
    control = _FakeNode("ctrl-1")
    graph = _FakeGraph(
        policy=policy, standards=(standard,), controls_by_standard_id={"std-1": (control,)}
    )

    backfill_governance_status(graph, "pol-1")

    assert standard.properties["status"] == "approved"
    assert control.properties["status"] == "approved"


def test_second_call_in_a_row_changes_zero_properties() -> None:
    """AC-BI-020's 're-running changes nothing': a second call against the
    same, already-backfilled graph issues queries but writes zero properties.
    """
    policy = _FakeNode("pol-1", {"status": "approved"})
    standard = _FakeNode("std-1")
    control = _FakeNode("ctrl-1")
    graph = _FakeGraph(
        policy=policy, standards=(standard,), controls_by_standard_id={"std-1": (control,)}
    )

    backfill_governance_status(graph, "pol-1")
    write_count_after_first_call = graph.property_write_count
    backfill_governance_status(graph, "pol-1")

    assert write_count_after_first_call > 0
    assert graph.property_write_count == write_count_after_first_call
    assert len(graph.calls) == 6


def test_policy_with_null_version_gets_version_one() -> None:
    policy = _FakeNode("pol-1", {"status": "draft"})
    graph = _FakeGraph(policy=policy)

    backfill_governance_status(graph, "pol-1")

    assert policy.properties["version"] == "1"


def test_existing_non_null_status_is_never_overwritten() -> None:
    """Proves the guard is truly `IS NULL`-based, not 'always sync to parent':
    a Standard/Control already carrying a `status` keeps it, even though it
    differs from the Policy's own current `status`.
    """
    policy = _FakeNode("pol-1", {"status": "approved"})
    standard = _FakeNode("std-1", {"status": "deprecated"})
    control = _FakeNode("ctrl-1", {"status": "proposed"})
    graph = _FakeGraph(
        policy=policy, standards=(standard,), controls_by_standard_id={"std-1": (control,)}
    )

    backfill_governance_status(graph, "pol-1")

    assert standard.properties["status"] == "deprecated"
    assert control.properties["status"] == "proposed"


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


# --- `update_policy_fields` (issue #136, Slice 1) ---------------------------


class _RecordingFakeGraph:
    """A `GraphHandle` double that only records every `query()` call verbatim."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult()


def test_update_policy_fields_issues_partial_set_merge() -> None:
    graph = _RecordingFakeGraph()

    update_policy_fields(graph, policy_id="pol-1", properties={"description": "new text"})

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert "SET p += $set_properties" in call.query
    assert call.params == {
        "policy_id": "pol-1",
        "set_properties": {"description": "new text"},
    }


def test_update_policy_fields_clears_a_field_to_null_via_explicit_set() -> None:
    """CHANGES.md finding #6: an explicit `None` value clears the field via
    its own `SET p.<field> = null` statement, never via `+=` map-merge.
    """
    graph = _RecordingFakeGraph()

    update_policy_fields(
        graph, policy_id="pol-1", properties={"description": "new text", "scope_out": None}
    )

    assert len(graph.calls) == 2
    merge_call, null_call = graph.calls
    assert "SET p += $set_properties" in merge_call.query
    assert merge_call.params == {
        "policy_id": "pol-1",
        "set_properties": {"description": "new text"},
    }
    assert "SET p.scope_out = null" in null_call.query
    assert null_call.params == {"policy_id": "pol-1"}


def test_update_policy_fields_with_only_null_values_issues_no_set_merge_call() -> None:
    graph = _RecordingFakeGraph()

    update_policy_fields(graph, policy_id="pol-1", properties={"scope_out": None})

    assert len(graph.calls) == 1
    assert "SET p.scope_out = null" in graph.calls[0].query


# --- `add_standard_to_policy` (issue #136, Slice 2) -------------------------


def test_add_standard_to_policy_issues_one_merge_with_draft_and_default_impl_status() -> None:
    graph = _RecordingFakeGraph()

    add_standard_to_policy(
        graph,
        policy_id="pol-1",
        standard_id="std-1",
        title="Encryption Standard",
        extra_properties={},
    )

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert "MERGE (p)-[:SUPPORTED_BY]->(s:Standard {id: $standard_id})" in call.query
    assert "SET s += $properties" in call.query
    assert call.params == {
        "policy_id": "pol-1",
        "standard_id": "std-1",
        "properties": {
            "implementation_status": "draft",
            "title": "Encryption Standard",
            "status": "draft",
        },
    }


def test_add_standard_to_policy_uses_caller_supplied_implementation_status_when_given() -> None:
    graph = _RecordingFakeGraph()

    add_standard_to_policy(
        graph,
        policy_id="pol-1",
        standard_id="std-1",
        title="Encryption Standard",
        extra_properties={"implementation_status": "implemented", "description": "text"},
    )

    assert len(graph.calls) == 1
    params = cast("dict[str, object]", graph.calls[0].params)
    props = cast("dict[str, object]", params["properties"])
    assert props["implementation_status"] == "implemented"
    assert props["description"] == "text"
    assert props["status"] == "draft"


def test_add_standard_to_policy_drops_explicit_none_values_rather_than_writing_them() -> None:
    """No prior value exists to clear on a newly-minted node (CHANGES.md finding #6's
    carve-out for add/create functions) -- an explicit `None` in `extra_properties`
    simply means "don't set this field", not a `SET s.<key> = null` statement.
    """
    graph = _RecordingFakeGraph()

    add_standard_to_policy(
        graph,
        policy_id="pol-1",
        standard_id="std-1",
        title="Encryption Standard",
        extra_properties={"description": None},
    )

    assert len(graph.calls) == 1
    params = cast("dict[str, object]", graph.calls[0].params)
    props = cast("dict[str, object]", params["properties"])
    assert "description" not in props
    assert " = null" not in graph.calls[0].query


# --- `find_standard_with_parent` / `update_standard_fields` (issue #136, Slice 3) --


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


def test_update_standard_fields_issues_partial_set_merge() -> None:
    graph = _RecordingFakeGraph()

    update_standard_fields(graph, standard_id="std-1", properties={"description": "new text"})

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert "SET s += $set_properties" in call.query
    assert call.params == {
        "standard_id": "std-1",
        "set_properties": {"description": "new text"},
    }


def test_update_standard_fields_clears_a_field_to_null_via_explicit_set() -> None:
    """CHANGES.md finding #6: an explicit `None` value clears the field via
    its own `SET s.<field> = null` statement, never via `+=` map-merge.
    """
    graph = _RecordingFakeGraph()

    update_standard_fields(
        graph, standard_id="std-1", properties={"description": "new text", "procedure": None}
    )

    assert len(graph.calls) == 2
    merge_call, null_call = graph.calls
    assert "SET s += $set_properties" in merge_call.query
    assert merge_call.params == {
        "standard_id": "std-1",
        "set_properties": {"description": "new text"},
    }
    assert "SET s.procedure = null" in null_call.query
    assert null_call.params == {"standard_id": "std-1"}


def test_update_standard_fields_with_only_null_values_issues_no_set_merge_call() -> None:
    graph = _RecordingFakeGraph()

    update_standard_fields(graph, standard_id="std-1", properties={"procedure": None})

    assert len(graph.calls) == 1
    assert "SET s.procedure = null" in graph.calls[0].query


# --- `add_control_to_standard` (issue #136, Slice 4) -------------------------


def test_add_control_to_standard_issues_one_merge_with_draft_and_default_impl_status() -> None:
    graph = _RecordingFakeGraph()

    add_control_to_standard(
        graph,
        standard_id="std-1",
        control_id="ctrl-1",
        title="Key Rotation Check",
        control_type="manual",
        extra_properties={},
    )

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert "MERGE (s)-[:IMPLEMENTED_BY]->(c:Control {id: $control_id})" in call.query
    assert "SET c += $properties" in call.query
    assert call.params == {
        "standard_id": "std-1",
        "control_id": "ctrl-1",
        "properties": {
            "implementation_status": "planned",
            "title": "Key Rotation Check",
            "type": "manual",
            "status": "draft",
        },
    }


def test_add_control_to_standard_uses_caller_supplied_implementation_status_when_given() -> None:
    graph = _RecordingFakeGraph()

    add_control_to_standard(
        graph,
        standard_id="std-1",
        control_id="ctrl-1",
        title="Key Rotation Check",
        control_type="automated",
        extra_properties={"implementation_status": "implemented", "description": "text"},
    )

    assert len(graph.calls) == 1
    params = cast("dict[str, object]", graph.calls[0].params)
    props = cast("dict[str, object]", params["properties"])
    assert props["implementation_status"] == "implemented"
    assert props["description"] == "text"
    assert props["status"] == "draft"
    assert props["type"] == "automated"


def test_add_control_to_standard_drops_explicit_none_values_rather_than_writing_them() -> None:
    """No prior value exists to clear on a newly-minted node (CHANGES.md finding #6's
    carve-out for add/create functions) -- an explicit `None` in `extra_properties`
    simply means "don't set this field", not a `SET c.<key> = null` statement.
    """
    graph = _RecordingFakeGraph()

    add_control_to_standard(
        graph,
        standard_id="std-1",
        control_id="ctrl-1",
        title="Key Rotation Check",
        control_type="manual",
        extra_properties={"description": None},
    )

    assert len(graph.calls) == 1
    params = cast("dict[str, object]", graph.calls[0].params)
    props = cast("dict[str, object]", params["properties"])
    assert "description" not in props
    assert " = null" not in graph.calls[0].query


# --- `find_control_with_parent` / `update_control_fields` (issue #136, Slice 5) --


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


def test_update_control_fields_issues_partial_set_merge() -> None:
    graph = _RecordingFakeGraph()

    update_control_fields(graph, control_id="ctrl-1", properties={"description": "new text"})

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert "SET c += $set_properties" in call.query
    assert call.params == {
        "control_id": "ctrl-1",
        "set_properties": {"description": "new text"},
    }


def test_update_control_fields_clears_a_field_to_null_via_explicit_set() -> None:
    """CHANGES.md finding #6: an explicit `None` value clears the field via
    its own `SET c.<field> = null` statement, never via `+=` map-merge.
    """
    graph = _RecordingFakeGraph()

    update_control_fields(
        graph, control_id="ctrl-1", properties={"description": "new text", "evidence_ref": None}
    )

    assert len(graph.calls) == 2
    merge_call, null_call = graph.calls
    assert "SET c += $set_properties" in merge_call.query
    assert merge_call.params == {
        "control_id": "ctrl-1",
        "set_properties": {"description": "new text"},
    }
    assert "SET c.evidence_ref = null" in null_call.query
    assert null_call.params == {"control_id": "ctrl-1"}


def test_update_control_fields_with_only_null_values_issues_no_set_merge_call() -> None:
    graph = _RecordingFakeGraph()

    update_control_fields(graph, control_id="ctrl-1", properties={"evidence_ref": None})

    assert len(graph.calls) == 1
    assert "SET c.evidence_ref = null" in graph.calls[0].query


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


def test_create_policy_draft_writes_given_version_not_hardcoded_one() -> None:
    """CHANGES.md finding #1 (High): `version` is a real kwarg, threaded into
    the Policy write -- not the pre-#136 hardcoded literal `"1"`.
    """
    graph = _RecordingFakeGraph()

    create_policy_draft(
        graph,
        policy_id="pol-2",
        title="Data Protection Policy",
        owner_subject="alice",
        owner_issuer="https://issuer.example",
        version="4",
    )

    policy_write = graph.calls[0]
    assert "MERGE (p:Policy {id: $policy_id}) SET p += $properties" in policy_write.query
    params = cast("dict[str, object]", policy_write.params)
    properties = cast("dict[str, object]", params["properties"])
    assert properties["version"] == "4"


def test_create_policy_draft_defaults_version_to_one_when_omitted() -> None:
    """Every existing non-fork call site keeps working byte-for-byte."""
    graph = _RecordingFakeGraph()

    create_policy_draft(
        graph,
        policy_id="pol-2",
        title="Data Protection Policy",
        owner_subject="alice",
        owner_issuer="https://issuer.example",
    )

    params = cast("dict[str, object]", graph.calls[0].params)
    properties = cast("dict[str, object]", params["properties"])
    assert properties["version"] == "1"


def test_create_policy_draft_with_supersedes_policy_id_writes_superseded_by_edge() -> None:
    graph = _RecordingFakeGraph()

    create_policy_draft(
        graph,
        policy_id="pol-2",
        title="Data Protection Policy",
        owner_subject="alice",
        owner_issuer="https://issuer.example",
        supersedes_policy_id="pol-1",
        version="2",
    )

    edge_calls = [call for call in graph.calls if "SUPERSEDED_BY" in call.query]
    assert len(edge_calls) == 1
    assert edge_calls[0].params == {"prior_id": "pol-1", "new_id": "pol-2"}


def test_create_policy_draft_without_supersedes_policy_id_writes_no_superseded_by_edge() -> None:
    graph = _RecordingFakeGraph()

    create_policy_draft(
        graph,
        policy_id="pol-2",
        title="Data Protection Policy",
        owner_subject="alice",
        owner_issuer="https://issuer.example",
    )

    assert not any("SUPERSEDED_BY" in call.query for call in graph.calls)


def test_create_policy_draft_forked_standard_and_control_extra_properties_survive_into_write() -> (
    None
):
    """A forked Standard/Control's `extra_properties` (real content, not just
    title) is spread into the write, with `title`/`type`/`status` always
    set AFTER the spread so they can never be overridden by a stray copied
    value.
    """
    graph = _RecordingFakeGraph()

    create_policy_draft(
        graph,
        policy_id="pol-2",
        title="Data Protection Policy",
        owner_subject="alice",
        owner_issuer="https://issuer.example",
        standards=(
            StandardDraft(
                id="std-2",
                title="Encryption Standard",
                controls=(
                    ControlDraft(
                        id="ctrl-2",
                        title="Key Rotation",
                        control_type="automated",
                        extra_properties={
                            "evidence_ref": "https://example.com/evidence",
                            # stray copied value -- must lose to the forced "draft" below
                            "status": "approved",
                        },
                    ),
                ),
                extra_properties={
                    "procedure": "rotate keys quarterly",
                    # stray copied value -- must lose to the forced "draft" below
                    "status": "approved",
                },
            ),
        ),
        supersedes_policy_id="pol-1",
        version="2",
    )

    standard_call = next(call for call in graph.calls if "SUPPORTED_BY" in call.query)
    standard_params = cast("dict[str, object]", standard_call.params)
    standard_properties = cast("dict[str, object]", standard_params["properties"])
    assert standard_properties["procedure"] == "rotate keys quarterly"
    assert standard_properties["title"] == "Encryption Standard"
    assert standard_properties["status"] == "draft"

    control_call = next(call for call in graph.calls if "IMPLEMENTED_BY" in call.query)
    control_params = cast("dict[str, object]", control_call.params)
    control_properties = cast("dict[str, object]", control_params["properties"])
    assert control_properties["evidence_ref"] == "https://example.com/evidence"
    assert control_properties["title"] == "Key Rotation"
    assert control_properties["type"] == "automated"
    assert control_properties["status"] == "draft"
