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

from ps_service.policy_lifecycle.graph_writer import backfill_governance_status, find_approved_prior


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
