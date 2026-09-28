"""Tests for `ps_service.policy_lifecycle.service.create_policy_draft` (issue #134, S11).

`_FakeGraph`/`_FakeAuditStore` are local, call-recording fakes (not the
richer `_FakeGraph` in `test_graph_writer.py`, which models actual FalkorDB
node/property state for `backfill_governance_status`'s idempotency
contract) -- this slice's own test contract (PLAN.md S11) needs only:
whether a Policy with the target id already exists, what queries get
issued, and in what order relative to the audit writes. `_recorder` (a
shared `list[str]`) is what the ordering test asserts against: both fakes
append a tag to it on every call, so "audit precedes graph write" is a
plain index comparison rather than timestamp/mocking machinery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package (has `__init__.py`); mirrors `test_access_role_tools.py`'s own cross-package import convention
    FakeAccessRoleStore,
)

from ps_service.api.errors import AccessDeniedError
from ps_service.authz.models import AccessRole
from ps_service.policy_lifecycle.errors import (
    PolicyDraftAccessDeniedError,
    PolicyIncompleteForProposalError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
    PolicyNotFoundError,
    PolicySelfApprovalBlockedError,
    PolicyTitleAlreadyExistsError,
)
from ps_service.policy_lifecycle.service import (
    ControlDraftInput,
    StandardDraftInput,
    approve_policy,
    create_policy_draft,
    get_policy,
    propose_policy,
    reject_policy,
    revert_policy_to_draft,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Literal

    from ps_service.audit.models import AuditQueryFilters, AuditQueryPage

_ACTOR = ("alice", "https://issuer.example")
_TITLE = "Data Protection Policy"


@dataclass
class _RecordedAuditCall:
    action: str
    resource_id: str
    outcome: str
    details: Mapping[str, object]


class _FakeAuditStore:
    """Records every `record_standalone` call, tagging `recorder` for ordering checks."""

    def __init__(self, recorder: list[str]) -> None:
        self._recorder = recorder
        self.calls: list[_RecordedAuditCall] = []

    def record(
        self,
        cur: object,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> None:
        """Not exercised here -- `create_policy_draft` only ever calls `record_standalone`."""
        del cur, actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details
        raise NotImplementedError

    def record_standalone(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> None:
        del resource_type
        self._recorder.append("audit")
        self.calls.append(
            _RecordedAuditCall(
                action=action, resource_id=resource_id, outcome=outcome, details=details
            )
        )
        assert actor_subject == _ACTOR[0]
        assert actor_issuer == _ACTOR[1]

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        """Not exercised here -- present only for `AuditStore` `Protocol` conformance."""
        del filters, cursor, page_size
        raise NotImplementedError


@dataclass
class _FakeQueryResult:
    result_set: list[object] = field(default_factory=list)


class _FakeGraph:
    """A `GraphHandle` double: returns a canned existence-check row, records write calls."""

    def __init__(self, recorder: list[str], *, existing: tuple[str, str] | None = None) -> None:
        self._recorder = recorder
        self._existing = existing
        self.write_queries: list[str] = []
        self.raise_on_write: Exception | None = None

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        del params
        if "RETURN p.id, p.title" in q:
            rows: list[object] = [[self._existing[0], self._existing[1]]] if self._existing else []
            return _FakeQueryResult(result_set=rows)
        self._recorder.append("graph_write")
        self.write_queries.append(q)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def test_success_creates_policy_with_expected_shape_and_draft_children() -> None:
    recorder: list[str] = []
    graph = _FakeGraph(recorder)
    audit_store = _FakeAuditStore(recorder)

    result = create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        standards=(
            StandardDraftInput(
                title="Encryption Standard",
                controls=(ControlDraftInput(title="Key Rotation Check", control_type="automated"),),
            ),
        ),
        graph=graph,
        audit_store=audit_store,
    )

    assert result.title == _TITLE
    assert result.status == "draft"
    assert result.version == "1"
    assert result.owner_subject == _ACTOR[0]
    assert result.owner_issuer == _ACTOR[1]
    assert result.policy_id.startswith("pol_data_protection_policy_")
    assert len(result.standard_ids) == 1
    assert len(result.control_ids) == 1
    # Policy write + Standard write + Control write, all through `graph.query`.
    assert len(graph.write_queries) == 3
    assert any("MERGE (p:Policy" in q for q in graph.write_queries)
    assert any("MERGE (p)-[:SUPPORTED_BY]->(s:Standard" in q for q in graph.write_queries)
    assert any("MERGE (s)-[:IMPLEMENTED_BY]->(c:Control" in q for q in graph.write_queries)


def test_duplicate_title_raises_named_error_records_rejected_audit_and_skips_graph_write() -> None:
    recorder: list[str] = []
    graph = _FakeGraph(recorder, existing=("pol_data_protection_policy_aaaaaa", _TITLE))
    audit_store = _FakeAuditStore(recorder)

    with pytest.raises(PolicyTitleAlreadyExistsError) as exc_info:
        create_policy_draft(actor=_ACTOR, title=_TITLE, graph=graph, audit_store=audit_store)

    assert exc_info.value.existing_policy_id == "pol_data_protection_policy_aaaaaa"
    assert exc_info.value.title == _TITLE
    assert "pol_data_protection_policy_aaaaaa" in str(exc_info.value)
    assert graph.write_queries == []
    assert len(audit_store.calls) == 1
    rejected_call = audit_store.calls[0]
    assert rejected_call.action == "policy.create_draft"
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "title_already_exists"


def test_audit_write_precedes_graph_write() -> None:
    recorder: list[str] = []
    graph = _FakeGraph(recorder)
    audit_store = _FakeAuditStore(recorder)

    create_policy_draft(actor=_ACTOR, title=_TITLE, graph=graph, audit_store=audit_store)

    assert recorder.index("audit") < recorder.index("graph_write")


def test_graph_failure_after_audit_success_records_failed_event_and_raises() -> None:
    recorder: list[str] = []
    graph = _FakeGraph(recorder)
    graph.raise_on_write = redis.exceptions.ConnectionError("boom")
    audit_store = _FakeAuditStore(recorder)

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        create_policy_draft(actor=_ACTOR, title=_TITLE, graph=graph, audit_store=audit_store)

    assert [call.outcome for call in audit_store.calls] == ["applied", "failed"]
    assert {call.resource_id for call in audit_store.calls} == {audit_store.calls[0].resource_id}


_OWNER = ("alice", "https://issuer.example")
_OTHER = ("bob", "https://issuer.example")
_GRANTER = ("system-admin-tool", "https://issuer.example")


@dataclass
class _ControlFixture:
    id: str
    title: str
    status: str
    control_type: str


@dataclass
class _StandardFixture:
    id: str
    title: str
    status: str
    controls: tuple[_ControlFixture, ...] = field(default_factory=tuple)


@dataclass
class _PolicyFixture:
    id: str
    title: str
    status: str
    version: str
    owner: tuple[str, str]
    standards: tuple[_StandardFixture, ...] = field(default_factory=tuple)


def _policy_tree_rows(policy: _PolicyFixture) -> list[object]:
    """Build `read_policy_tree`'s own canned row shape for one `_PolicyFixture` (S13).

    A module-level function (not a private method) specifically so
    `_ApproveFakeGraph` below (S17/S25 -- which must read a SECOND, distinct
    Policy tree for the auto-deprecated prior) can reuse it directly without
    reaching into `_ReadFakeGraph`'s own internals.
    """
    head = [
        policy.id,
        policy.title,
        policy.status,
        policy.version,
        policy.owner[0],
        policy.owner[1],
    ]
    if not policy.standards:
        return [[*head, None, None, None, None, None, None, None]]
    rows: list[object] = []
    for standard in policy.standards:
        standard_head = [standard.id, standard.title, standard.status]
        if not standard.controls:
            rows.append([*head, *standard_head, None, None, None, None])
            continue
        for control in standard.controls:
            rows.append(
                [
                    *head,
                    *standard_head,
                    control.id,
                    control.title,
                    control.status,
                    control.control_type,
                ]
            )
    return rows


class _ReadFakeGraph:
    """A `GraphHandle` double for `get_policy`'s own tests (S13).

    Answers `graph_writer.read_policy_tree`'s single `RETURN` query with
    canned rows built from `_policy`; every other query (i.e. each of
    `backfill_governance_status`'s three `SET` statements) is treated as a
    no-op, since these tests always seed already-backfilled fixtures.
    """

    def __init__(self, policy: _PolicyFixture | None) -> None:
        self._policy = policy

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        del params
        if "s.id, s.title, s.status, c.id" not in q:
            return _FakeQueryResult()
        if self._policy is None:
            return _FakeQueryResult(result_set=[])
        return _FakeQueryResult(result_set=_policy_tree_rows(self._policy))


def _draft_policy(*, owner: tuple[str, str] = _OWNER, policy_id: str = "pol_x") -> _PolicyFixture:
    return _PolicyFixture(id=policy_id, title="X", status="draft", version="1", owner=owner)


def test_owner_reads_own_draft_successfully_with_full_tree() -> None:
    policy = _PolicyFixture(
        id="pol_x",
        title="X",
        status="draft",
        version="1",
        owner=_OWNER,
        standards=(
            _StandardFixture(
                id="std_1",
                title="Encryption Standard",
                status="draft",
                controls=(
                    _ControlFixture(
                        id="ctrl_1", title="Key Rotation", status="draft", control_type="automated"
                    ),
                ),
            ),
        ),
    )
    graph = _ReadFakeGraph(policy)
    store = FakeAccessRoleStore()

    result = get_policy(actor=_OWNER, policy_id="pol_x", graph=graph, access_role_store=store)

    assert result.policy_id == "pol_x"
    assert result.status == "draft"
    assert result.owner_subject == _OWNER[0]
    assert result.owner_issuer == _OWNER[1]
    assert len(result.standards) == 1
    standard = result.standards[0]
    assert standard.standard_id == "std_1"
    assert len(standard.controls) == 1
    assert standard.controls[0].control_id == "ctrl_1"
    assert standard.controls[0].control_type == "automated"


def test_system_owner_reads_anyones_draft() -> None:
    graph = _ReadFakeGraph(_draft_policy())
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OTHER, access_role=AccessRole.SYSTEM_OWNER)

    result = get_policy(actor=_OTHER, policy_id="pol_x", graph=graph, access_role_store=store)

    assert result.policy_id == "pol_x"


def test_system_admin_reads_anyones_draft() -> None:
    graph = _ReadFakeGraph(_draft_policy())
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OTHER, access_role=AccessRole.SYSTEM_ADMIN)

    result = get_policy(actor=_OTHER, policy_id="pol_x", graph=graph, access_role_store=store)

    assert result.policy_id == "pol_x"


def test_non_owner_policy_manager_is_rejected() -> None:
    graph = _ReadFakeGraph(_draft_policy())
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OTHER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicyDraftAccessDeniedError):
        get_policy(actor=_OTHER, policy_id="pol_x", graph=graph, access_role_store=store)


def test_non_owner_authenticated_user_is_rejected() -> None:
    graph = _ReadFakeGraph(_draft_policy())
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        get_policy(actor=_OTHER, policy_id="pol_x", graph=graph, access_role_store=store)


@pytest.mark.parametrize("status", ["proposed", "approved", "deprecated"])
def test_non_draft_policy_is_readable_by_any_authenticated_caller(status: str) -> None:
    graph = _ReadFakeGraph(
        _PolicyFixture(id="pol_y", title="Y", status=status, version="1", owner=_OWNER)
    )
    store = FakeAccessRoleStore()

    result = get_policy(actor=_OTHER, policy_id="pol_y", graph=graph, access_role_store=store)

    assert result.status == status


def test_nonexistent_policy_raises_not_found() -> None:
    graph = _ReadFakeGraph(None)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyNotFoundError):
        get_policy(actor=_OWNER, policy_id="pol_missing", graph=graph, access_role_store=store)


# --- `propose_policy` (issue #134, S15) -------------------------------------


@dataclass
class _RecordedTransitionAuditCall:
    actor_subject: str
    actor_issuer: str
    action: str
    resource_id: str
    outcome: str
    details: Mapping[str, object]


class _TransitionFakeAuditStore:
    """Records every `record_standalone` call, tagging `recorder` for ordering checks.

    Unlike this file's own `_FakeAuditStore` (which asserts every call's
    actor is the fixed `_ACTOR`), `propose_policy`'s own rejected-audit calls
    are recorded under the *calling* actor -- which, for the non-owner test
    case, is deliberately not the Policy's owner -- so no such assertion is
    made here.
    """

    def __init__(self, recorder: list[str]) -> None:
        self._recorder = recorder
        self.calls: list[_RecordedTransitionAuditCall] = []

    def record(self, *args: object, **kwargs: object) -> None:
        """Not exercised here -- `propose_policy` only ever calls `record_standalone`."""
        del args, kwargs
        raise NotImplementedError

    def record_standalone(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> None:
        del resource_type
        self._recorder.append("audit")
        self.calls.append(
            _RecordedTransitionAuditCall(
                actor_subject=actor_subject,
                actor_issuer=actor_issuer,
                action=action,
                resource_id=resource_id,
                outcome=outcome,
                details=details,
            )
        )

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        """Not exercised here -- present only for `AuditStore` `Protocol` conformance."""
        del filters, cursor, page_size
        raise NotImplementedError


class _ProposeFakeGraph(_ReadFakeGraph):
    """Extends `_ReadFakeGraph` with `cascade_status`'s single cascading write (S15).

    Every other query (the tree read, each of `backfill_governance_status`'s
    three `SET` statements) is handled by `_ReadFakeGraph.query` unchanged;
    only the cascade's own distinguishing `SET p.status = $target_status`
    substring is intercepted here, tagging `recorder` (shared with the audit
    fake, for the audit-before-graph-write ordering test) and recording each
    write's query text/params.
    """

    def __init__(
        self,
        recorder: list[str],
        policy: _PolicyFixture | None,
        *,
        raise_on_write: Exception | None = None,
    ) -> None:
        super().__init__(policy)
        self._recorder = recorder
        self.write_queries: list[str] = []
        self.write_params: list[dict[str, object] | None] = []
        self.raise_on_write = raise_on_write

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "SET p.status = $target_status" not in q:
            return super().query(q, params)
        self._recorder.append("graph_write")
        self.write_queries.append(q)
        self.write_params.append(params)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def _proposable_draft(
    *,
    owner: tuple[str, str] = _OWNER,
    policy_id: str = "pol_x",
    standards: tuple[_StandardFixture, ...] = (
        _StandardFixture(id="std_1", title="Encryption Standard", status="draft"),
    ),
) -> _PolicyFixture:
    return _PolicyFixture(
        id=policy_id, title="X", status="draft", version="1", owner=owner, standards=standards
    )


def test_owner_proposes_complete_draft_succeeds_and_cascades_whole_tree() -> None:
    recorder: list[str] = []
    policy = _proposable_draft(
        standards=(
            _StandardFixture(
                id="std_1",
                title="Encryption Standard",
                status="draft",
                controls=(
                    _ControlFixture(
                        id="ctrl_1", title="Key Rotation", status="draft", control_type="automated"
                    ),
                ),
            ),
        )
    )
    graph = _ProposeFakeGraph(recorder, policy)
    audit_store = _TransitionFakeAuditStore(recorder)

    result = propose_policy(actor=_OWNER, policy_id="pol_x", graph=graph, audit_store=audit_store)

    assert result.policy_id == "pol_x"
    assert result.status == "proposed"
    assert result.standard_ids == ("std_1",)
    assert result.control_ids == ("ctrl_1",)
    assert len(graph.write_queries) == 1
    assert graph.write_params[0] == {"policy_id": "pol_x", "target_status": "proposed"}
    assert len(audit_store.calls) == 1
    applied_call = audit_store.calls[0]
    assert applied_call.action == "policy.propose"
    assert applied_call.outcome == "applied"
    assert applied_call.details["affected_node_ids"] == ("pol_x", "std_1", "ctrl_1")
    assert applied_call.details["from_status"] == "draft"
    assert applied_call.details["to_status"] == "proposed"
    assert "reason_code" not in applied_call.details


def test_non_owner_attempt_is_rejected_with_rejected_audit() -> None:
    recorder: list[str] = []
    graph = _ProposeFakeGraph(recorder, _proposable_draft())
    audit_store = _TransitionFakeAuditStore(recorder)

    with pytest.raises(PolicyDraftAccessDeniedError):
        propose_policy(actor=_OTHER, policy_id="pol_x", graph=graph, audit_store=audit_store)

    assert graph.write_queries == []
    assert len(audit_store.calls) == 1
    rejected_call = audit_store.calls[0]
    assert rejected_call.actor_subject == _OTHER[0]
    assert rejected_call.action == "policy.propose"
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "access_denied"
    assert rejected_call.details["affected_node_ids"] == ("pol_x",)
    assert rejected_call.details["from_status"] == "draft"
    assert rejected_call.details["to_status"] == "draft"


def test_zero_standard_draft_is_rejected_as_incomplete_with_rejected_audit() -> None:
    recorder: list[str] = []
    graph = _ProposeFakeGraph(recorder, _proposable_draft(standards=()))
    audit_store = _TransitionFakeAuditStore(recorder)

    with pytest.raises(PolicyIncompleteForProposalError):
        propose_policy(actor=_OWNER, policy_id="pol_x", graph=graph, audit_store=audit_store)

    assert graph.write_queries == []
    assert len(audit_store.calls) == 1
    rejected_call = audit_store.calls[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "incomplete_for_proposal"


def test_standard_with_zero_controls_still_succeeds() -> None:
    recorder: list[str] = []
    policy = _proposable_draft(
        standards=(_StandardFixture(id="std_1", title="Encryption Standard", status="draft"),)
    )
    graph = _ProposeFakeGraph(recorder, policy)
    audit_store = _TransitionFakeAuditStore(recorder)

    result = propose_policy(actor=_OWNER, policy_id="pol_x", graph=graph, audit_store=audit_store)

    assert result.status == "proposed"
    assert result.standard_ids == ("std_1",)
    assert result.control_ids == ()
    assert len(graph.write_queries) == 1


def test_wrong_current_status_is_rejected_via_require_status() -> None:
    recorder: list[str] = []
    policy = _proposable_draft()
    policy.status = "proposed"
    graph = _ProposeFakeGraph(recorder, policy)
    audit_store = _TransitionFakeAuditStore(recorder)

    with pytest.raises(PolicyInvalidStatusTransitionError):
        propose_policy(actor=_OWNER, policy_id="pol_x", graph=graph, audit_store=audit_store)

    assert graph.write_queries == []
    assert len(audit_store.calls) == 1
    rejected_call = audit_store.calls[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "invalid_status"
    assert rejected_call.details["from_status"] == "proposed"
    assert rejected_call.details["to_status"] == "proposed"


def test_propose_audit_write_precedes_graph_write() -> None:
    recorder: list[str] = []
    graph = _ProposeFakeGraph(recorder, _proposable_draft())
    audit_store = _TransitionFakeAuditStore(recorder)

    propose_policy(actor=_OWNER, policy_id="pol_x", graph=graph, audit_store=audit_store)

    assert recorder.index("audit") < recorder.index("graph_write")


def test_propose_graph_failure_after_audit_success_records_failed_event_and_raises() -> None:
    recorder: list[str] = []
    graph = _ProposeFakeGraph(
        recorder, _proposable_draft(), raise_on_write=redis.exceptions.ConnectionError("boom")
    )
    audit_store = _TransitionFakeAuditStore(recorder)

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        propose_policy(actor=_OWNER, policy_id="pol_x", graph=graph, audit_store=audit_store)

    assert [call.outcome for call in audit_store.calls] == ["applied", "failed"]
    assert {call.resource_id for call in audit_store.calls} == {"pol_x"}


def test_propose_nonexistent_policy_raises_not_found() -> None:
    recorder: list[str] = []
    graph = _ProposeFakeGraph(recorder, None)
    audit_store = _TransitionFakeAuditStore(recorder)

    with pytest.raises(PolicyNotFoundError):
        propose_policy(actor=_OWNER, policy_id="pol_missing", graph=graph, audit_store=audit_store)


# --- `approve_policy` (issue #134, S17/S25) ---------------------------------

_MANAGER = ("carol", "https://issuer.example")
_OWNER_OTHER_ISSUER = (_OWNER[0], "https://other-issuer.example")


class _ApproveFakeGraph:
    """A `GraphHandle` double for `approve_policy`'s own tests (S17/S25).

    Unlike `_ProposeFakeGraph` above (which only ever reads/cascades ONE
    Policy per call), `approve_policy` may read/cascade a SECOND, distinct
    Policy tree -- the `SUPERSEDED_BY`-linked prior, once auto-deprecation
    (D-10) kicks in -- so this fake keys `_ReadFakeGraph`'s own row-building
    off a `policy_id -> _PolicyFixture` map, plus one optional seeded
    `SUPERSEDED_BY` edge (`successor_id -> prior_id`), a test-only stand-in
    for the edge no production tool in issue #134 ever creates itself
    (D-10 -- that's #136's own fork tool).
    """

    def __init__(
        self,
        recorder: list[str],
        policies: dict[str, _PolicyFixture],
        *,
        superseded_by: dict[str, str] | None = None,
        raise_on_write_for: str | None = None,
        raise_on_write: Exception | None = None,
    ) -> None:
        self._recorder = recorder
        self._policies = policies
        self._superseded_by = superseded_by or {}
        self._raise_on_write_for = raise_on_write_for
        self._raise_on_write = raise_on_write
        self.write_queries: list[str] = []
        self.write_params: list[dict[str, object] | None] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        assert params is not None
        if "SET p.status = $target_status" in q:
            self._recorder.append("graph_write")
            self.write_queries.append(q)
            self.write_params.append(params)
            written_policy_id = params["policy_id"]
            if self._raise_on_write is not None and written_policy_id == self._raise_on_write_for:
                raise self._raise_on_write
            return _FakeQueryResult()
        if "SUPERSEDED_BY" in q:
            successor_id = params["successor_policy_id"]
            assert isinstance(successor_id, str)
            prior_id = self._superseded_by.get(successor_id)
            if prior_id is None:
                return _FakeQueryResult(result_set=[])
            prior = self._policies.get(prior_id)
            if prior is None or prior.status != "approved":
                return _FakeQueryResult(result_set=[])
            return _FakeQueryResult(result_set=[[prior_id]])
        if "s.id, s.title, s.status, c.id" in q:
            policy_id = params["policy_id"]
            assert isinstance(policy_id, str)
            policy = self._policies.get(policy_id)
            if policy is None:
                return _FakeQueryResult(result_set=[])
            return _FakeQueryResult(result_set=_policy_tree_rows(policy))
        # `backfill_governance_status`'s three `SET ... IS NULL` statements: no-op.
        return _FakeQueryResult()


def _approvable_proposed(
    *,
    owner: tuple[str, str] = _OWNER,
    policy_id: str = "pol_x",
    status: str = "proposed",
    standards: tuple[_StandardFixture, ...] = (
        _StandardFixture(id="std_1", title="Encryption Standard", status="proposed"),
    ),
) -> _PolicyFixture:
    return _PolicyFixture(
        id=policy_id, title="X", status=status, version="1", owner=owner, standards=standards
    )


def test_policy_manager_non_owner_approves_successfully() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed()
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    result = approve_policy(
        actor=_MANAGER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        access_role_store=store,
    )

    assert result.policy_id == "pol_x"
    assert result.status == "approved"
    assert result.standard_ids == ("std_1",)
    assert result.auto_deprecated_policy_id is None
    assert len(graph.write_queries) == 1
    assert graph.write_params[0] == {"policy_id": "pol_x", "target_status": "approved"}
    assert len(audit_store.calls) == 1
    applied_call = audit_store.calls[0]
    assert applied_call.action == "policy.approve"
    assert applied_call.outcome == "applied"
    assert applied_call.details["from_status"] == "proposed"
    assert applied_call.details["to_status"] == "approved"


def test_owner_who_also_holds_policy_manager_self_approval_is_blocked() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed()
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OWNER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicySelfApprovalBlockedError):
        approve_policy(
            actor=_OWNER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            access_role_store=store,
        )

    assert graph.write_queries == []
    assert len(audit_store.calls) == 1
    rejected_call = audit_store.calls[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "self_approval_blocked"


def test_same_subject_different_issuer_approver_succeeds() -> None:
    """AC-BI-017's critical regression case: same `sub`, different `iss`, is a different person."""
    recorder: list[str] = []
    policy = _approvable_proposed(owner=_OWNER)
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OWNER_OTHER_ISSUER, access_role=AccessRole.POLICY_MANAGER)

    result = approve_policy(
        actor=_OWNER_OTHER_ISSUER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        access_role_store=store,
    )

    assert result.status == "approved"
    assert len(graph.write_queries) == 1
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].outcome == "applied"


def test_non_policy_manager_is_rejected_with_access_denied() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed()
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()

    with pytest.raises(AccessDeniedError):
        approve_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            access_role_store=store,
        )

    assert graph.write_queries == []
    assert audit_store.calls == []


def test_approve_wrong_current_status_is_rejected_via_require_status() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed(status="draft")
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicyInvalidStatusTransitionError):
        approve_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            access_role_store=store,
        )

    assert graph.write_queries == []
    assert len(audit_store.calls) == 1
    rejected_call = audit_store.calls[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "invalid_status"


def test_approve_audit_write_precedes_graph_write() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed()
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    approve_policy(
        actor=_MANAGER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        access_role_store=store,
    )

    assert recorder.index("audit") < recorder.index("graph_write")


def test_approve_graph_failure_after_audit_success_records_failed_event_and_raises() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed()
    graph = _ApproveFakeGraph(
        recorder,
        {"pol_x": policy},
        raise_on_write_for="pol_x",
        raise_on_write=redis.exceptions.ConnectionError("boom"),
    )
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        approve_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            access_role_store=store,
        )

    assert [call.outcome for call in audit_store.calls] == ["applied", "failed"]
    assert {call.resource_id for call in audit_store.calls} == {"pol_x"}


def test_approve_nonexistent_policy_raises_not_found() -> None:
    recorder: list[str] = []
    graph = _ApproveFakeGraph(recorder, {})
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicyNotFoundError):
        approve_policy(
            actor=_MANAGER,
            policy_id="pol_missing",
            graph=graph,
            audit_store=audit_store,
            access_role_store=store,
        )


def test_approving_a_superseded_by_approved_prior_auto_deprecates_it_in_the_same_call() -> None:
    recorder: list[str] = []
    successor = _approvable_proposed(policy_id="pol_new")
    prior = _approvable_proposed(policy_id="pol_old", status="approved")
    graph = _ApproveFakeGraph(
        recorder,
        {"pol_new": successor, "pol_old": prior},
        superseded_by={"pol_new": "pol_old"},
    )
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    result = approve_policy(
        actor=_MANAGER,
        policy_id="pol_new",
        graph=graph,
        audit_store=audit_store,
        access_role_store=store,
    )

    assert result.status == "approved"
    assert result.auto_deprecated_policy_id == "pol_old"
    assert len(graph.write_queries) == 2
    assert graph.write_params[0] == {"policy_id": "pol_new", "target_status": "approved"}
    assert graph.write_params[1] == {"policy_id": "pol_old", "target_status": "deprecated"}
    assert len(audit_store.calls) == 2
    successor_call, prior_call = audit_store.calls
    assert successor_call.action == "policy.approve"
    assert successor_call.resource_id == "pol_new"
    assert successor_call.outcome == "applied"
    assert prior_call.action == "policy.auto_deprecate"
    assert prior_call.resource_id == "pol_old"
    assert prior_call.outcome == "applied"
    assert prior_call.details["from_status"] == "approved"
    assert prior_call.details["to_status"] == "deprecated"


@pytest.mark.parametrize("prior_status", ["draft", "proposed"])
def test_superseded_by_prior_not_yet_approved_is_left_untouched(prior_status: str) -> None:
    recorder: list[str] = []
    successor = _approvable_proposed(policy_id="pol_new")
    prior = _approvable_proposed(policy_id="pol_old", status=prior_status)
    graph = _ApproveFakeGraph(
        recorder,
        {"pol_new": successor, "pol_old": prior},
        superseded_by={"pol_new": "pol_old"},
    )
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    result = approve_policy(
        actor=_MANAGER,
        policy_id="pol_new",
        graph=graph,
        audit_store=audit_store,
        access_role_store=store,
    )

    assert result.status == "approved"
    assert result.auto_deprecated_policy_id is None
    assert len(graph.write_queries) == 1
    assert graph.write_params[0] == {"policy_id": "pol_new", "target_status": "approved"}
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].action == "policy.approve"


# --- `reject_policy` (issue #134, S19) --------------------------------------
#
# Mirrors `approve_policy`'s own test list exactly (same AC-BI-006/008/017
# test shapes), with the target status reversed to `"draft"` and the action
# `"policy.reject"` -- proving the gate stack (RBAC, `block_self_approval`,
# `require_status`) is genuinely shared with `approve_policy`, not
# reimplemented differently. No auto-deprecation test: `reject_policy` never
# calls `find_approved_prior` (D-10 is approve-only).


def test_policy_manager_non_owner_rejects_successfully_and_cascades_to_draft() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed()
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    result = reject_policy(
        actor=_MANAGER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        access_role_store=store,
    )

    assert result.policy_id == "pol_x"
    assert result.status == "draft"
    assert result.standard_ids == ("std_1",)
    assert len(graph.write_queries) == 1
    assert graph.write_params[0] == {"policy_id": "pol_x", "target_status": "draft"}
    assert len(audit_store.calls) == 1
    applied_call = audit_store.calls[0]
    assert applied_call.action == "policy.reject"
    assert applied_call.outcome == "applied"
    assert applied_call.details["from_status"] == "proposed"
    assert applied_call.details["to_status"] == "draft"


def test_owner_who_also_holds_policy_manager_self_rejection_is_blocked() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed()
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OWNER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicySelfApprovalBlockedError):
        reject_policy(
            actor=_OWNER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            access_role_store=store,
        )

    assert graph.write_queries == []
    assert len(audit_store.calls) == 1
    rejected_call = audit_store.calls[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "self_approval_blocked"


def test_same_subject_different_issuer_rejecter_succeeds() -> None:
    """AC-BI-017's critical regression case: same `sub`, different `iss`, is a different person."""
    recorder: list[str] = []
    policy = _approvable_proposed(owner=_OWNER)
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OWNER_OTHER_ISSUER, access_role=AccessRole.POLICY_MANAGER)

    result = reject_policy(
        actor=_OWNER_OTHER_ISSUER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        access_role_store=store,
    )

    assert result.status == "draft"
    assert len(graph.write_queries) == 1
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].outcome == "applied"


def test_non_policy_manager_rejecter_is_rejected_with_access_denied() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed()
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()

    with pytest.raises(AccessDeniedError):
        reject_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            access_role_store=store,
        )

    assert graph.write_queries == []
    assert audit_store.calls == []


def test_reject_wrong_current_status_is_rejected_via_require_status() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed(status="draft")
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicyInvalidStatusTransitionError):
        reject_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            access_role_store=store,
        )

    assert graph.write_queries == []
    assert len(audit_store.calls) == 1
    rejected_call = audit_store.calls[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "invalid_status"


def test_reject_audit_write_precedes_graph_write() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed()
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    reject_policy(
        actor=_MANAGER,
        policy_id="pol_x",
        graph=graph,
        audit_store=audit_store,
        access_role_store=store,
    )

    assert recorder.index("audit") < recorder.index("graph_write")


def test_reject_graph_failure_after_audit_success_records_failed_event_and_raises() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed()
    graph = _ApproveFakeGraph(
        recorder,
        {"pol_x": policy},
        raise_on_write_for="pol_x",
        raise_on_write=redis.exceptions.ConnectionError("boom"),
    )
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        reject_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=graph,
            audit_store=audit_store,
            access_role_store=store,
        )

    assert [call.outcome for call in audit_store.calls] == ["applied", "failed"]
    assert {call.resource_id for call in audit_store.calls} == {"pol_x"}


def test_reject_nonexistent_policy_raises_not_found() -> None:
    recorder: list[str] = []
    graph = _ApproveFakeGraph(recorder, {})
    audit_store = _TransitionFakeAuditStore(recorder)
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)

    with pytest.raises(PolicyNotFoundError):
        reject_policy(
            actor=_MANAGER,
            policy_id="pol_missing",
            graph=graph,
            audit_store=audit_store,
            access_role_store=store,
        )


# --- `revert_policy_to_draft` (issue #134, S21) ------------------------------
#
# Structurally distinct from `approve_policy`/`reject_policy`: owner-only, no
# `PolicyManager` RBAC gate at all. The non-owner test below deliberately
# grants the non-owner caller `PolicyManager` to prove this is genuinely
# owner-only, not role-gated (AC-BI-002/007).


def test_owner_reverts_own_proposed_policy_succeeds_and_cascades_to_draft() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed()
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)

    result = revert_policy_to_draft(
        actor=_OWNER, policy_id="pol_x", graph=graph, audit_store=audit_store
    )

    assert result.policy_id == "pol_x"
    assert result.status == "draft"
    assert result.standard_ids == ("std_1",)
    assert len(graph.write_queries) == 1
    assert graph.write_params[0] == {"policy_id": "pol_x", "target_status": "draft"}
    assert len(audit_store.calls) == 1
    applied_call = audit_store.calls[0]
    assert applied_call.action == "policy.revert"
    assert applied_call.outcome == "applied"
    assert applied_call.details["from_status"] == "proposed"
    assert applied_call.details["to_status"] == "draft"


def test_revert_non_owner_policy_manager_is_rejected_proving_owner_only_not_role_gated() -> None:
    """A `PolicyManager` who is not the owner is still rejected -- this action has no RBAC gate."""
    recorder: list[str] = []
    policy = _approvable_proposed()
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)

    with pytest.raises(PolicyDraftAccessDeniedError):
        revert_policy_to_draft(
            actor=_MANAGER, policy_id="pol_x", graph=graph, audit_store=audit_store
        )

    assert graph.write_queries == []
    assert len(audit_store.calls) == 1
    rejected_call = audit_store.calls[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "access_denied"


def test_revert_wrong_current_status_is_rejected_via_require_status() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed(status="draft")
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)

    with pytest.raises(PolicyInvalidStatusTransitionError):
        revert_policy_to_draft(
            actor=_OWNER, policy_id="pol_x", graph=graph, audit_store=audit_store
        )

    assert graph.write_queries == []
    assert len(audit_store.calls) == 1
    rejected_call = audit_store.calls[0]
    assert rejected_call.outcome == "rejected"
    assert rejected_call.details["reason_code"] == "invalid_status"


def test_revert_audit_write_precedes_graph_write() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed()
    graph = _ApproveFakeGraph(recorder, {"pol_x": policy})
    audit_store = _TransitionFakeAuditStore(recorder)

    revert_policy_to_draft(actor=_OWNER, policy_id="pol_x", graph=graph, audit_store=audit_store)

    assert recorder.index("audit") < recorder.index("graph_write")


def test_revert_graph_failure_after_audit_success_records_failed_event_and_raises() -> None:
    recorder: list[str] = []
    policy = _approvable_proposed()
    graph = _ApproveFakeGraph(
        recorder,
        {"pol_x": policy},
        raise_on_write_for="pol_x",
        raise_on_write=redis.exceptions.ConnectionError("boom"),
    )
    audit_store = _TransitionFakeAuditStore(recorder)

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        revert_policy_to_draft(
            actor=_OWNER, policy_id="pol_x", graph=graph, audit_store=audit_store
        )

    assert [call.outcome for call in audit_store.calls] == ["applied", "failed"]
    assert {call.resource_id for call in audit_store.calls} == {"pol_x"}


def test_revert_nonexistent_policy_raises_not_found() -> None:
    recorder: list[str] = []
    graph = _ApproveFakeGraph(recorder, {})
    audit_store = _TransitionFakeAuditStore(recorder)

    with pytest.raises(PolicyNotFoundError):
        revert_policy_to_draft(
            actor=_OWNER, policy_id="pol_missing", graph=graph, audit_store=audit_store
        )
