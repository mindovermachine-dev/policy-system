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
from typing import TYPE_CHECKING, cast

import pytest
import redis.exceptions
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package (has `__init__.py`); mirrors `test_access_role_tools.py`'s own cross-package import convention
    FakeAccessRoleStore,
)

from ps_service.api.errors import AccessDeniedError
from ps_service.authz.models import AccessRole
from ps_service.domain_mapper.identity import control_id, standard_id
from ps_service.policy_lifecycle.errors import (
    PolicyControlNotFoundError,
    PolicyDraftAccessDeniedError,
    PolicyIncompleteForProposalError,
    PolicyInvalidStatusTransitionError,
    PolicyLifecycleGraphUnavailableError,
    PolicyNotFoundError,
    PolicySelfApprovalBlockedError,
    PolicyStandardNotFoundError,
    PolicySupersedePriorNotApprovedError,
    PolicyTitleAlreadyExistsError,
)
from ps_service.policy_lifecycle.service import (
    ControlDraftInput,
    StandardDraftInput,
    add_control_to_draft,
    add_standard_to_draft,
    approve_policy,
    create_policy_draft,
    get_policy,
    propose_policy,
    reject_policy,
    revert_policy_to_draft,
    update_control_draft,
    update_policy_draft,
    update_standard_draft,
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


# --- `update_policy_draft` (issue #136, Slice 1) ----------------------------


class _UpdatePolicyDraftFakeGraph(_ReadFakeGraph):
    """Extends `_ReadFakeGraph` with `update_policy_fields`'s own writes.

    Every other query (the tree read, `backfill_governance_status`'s three
    `SET` statements) is handled by `_ReadFakeGraph.query` unchanged; only
    `update_policy_fields`'s own two distinguishing shapes (`$set_properties`
    map-merge, `= null` single-field clear) are intercepted here.
    """

    def __init__(
        self, policy: _PolicyFixture | None, *, raise_on_write: Exception | None = None
    ) -> None:
        super().__init__(policy)
        self.write_queries: list[str] = []
        self.write_params: list[dict[str, object] | None] = []
        self.raise_on_write = raise_on_write

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "$set_properties" not in q and "= null" not in q:
            return super().query(q, params)
        self.write_queries.append(q)
        self.write_params.append(params)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def test_update_policy_draft_applies_only_supplied_fields() -> None:
    """AC-BI-008: only the supplied fields are written, nothing else."""
    graph = _UpdatePolicyDraftFakeGraph(_draft_policy())
    store = FakeAccessRoleStore()

    result = update_policy_draft(
        actor=_OWNER,
        policy_id="pol_x",
        fields={"description": "updated description"},
        graph=graph,
        access_role_store=store,
    )

    assert result.policy_id == "pol_x"
    assert result.updated_fields == ("description",)
    assert len(graph.write_queries) == 1
    assert graph.write_params[0] == {
        "policy_id": "pol_x",
        "set_properties": {"description": "updated description"},
    }


def test_update_policy_draft_clears_a_field_via_explicit_none() -> None:
    graph = _UpdatePolicyDraftFakeGraph(_draft_policy())
    store = FakeAccessRoleStore()

    result = update_policy_draft(
        actor=_OWNER,
        policy_id="pol_x",
        fields={"scope_out": None},
        graph=graph,
        access_role_store=store,
    )

    assert result.updated_fields == ("scope_out",)
    assert len(graph.write_queries) == 1
    assert "SET p.scope_out = null" in graph.write_queries[0]
    assert graph.write_params[0] == {"policy_id": "pol_x"}


def test_update_policy_draft_rejects_non_owner_non_elevated() -> None:
    """AC-BI-002."""
    graph = _UpdatePolicyDraftFakeGraph(_draft_policy())
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        update_policy_draft(
            actor=_OTHER,
            policy_id="pol_x",
            fields={"description": "hijacked"},
            graph=graph,
            access_role_store=store,
        )
    assert graph.write_queries == []


def test_update_policy_draft_allows_system_owner_override() -> None:
    """AC-BI-002's carve-out: a non-owner `SystemOwner` still succeeds."""
    graph = _UpdatePolicyDraftFakeGraph(_draft_policy())
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OTHER, access_role=AccessRole.SYSTEM_OWNER)

    result = update_policy_draft(
        actor=_OTHER,
        policy_id="pol_x",
        fields={"description": "edited by system owner"},
        graph=graph,
        access_role_store=store,
    )

    assert result.policy_id == "pol_x"
    assert len(graph.write_queries) == 1


def test_update_policy_draft_rejects_non_draft_status() -> None:
    """AC-BI-004."""
    policy = _PolicyFixture(id="pol_x", title="X", status="proposed", version="1", owner=_OWNER)
    graph = _UpdatePolicyDraftFakeGraph(policy)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyInvalidStatusTransitionError) as exc_info:
        update_policy_draft(
            actor=_OWNER,
            policy_id="pol_x",
            fields={"description": "too late"},
            graph=graph,
            access_role_store=store,
        )

    assert exc_info.value.action == "edit"
    assert exc_info.value.current_status == "proposed"
    assert exc_info.value.required_status == "draft"
    assert graph.write_queries == []


def test_update_policy_draft_raises_not_found() -> None:
    """AC-BI-012."""
    graph = _UpdatePolicyDraftFakeGraph(None)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyNotFoundError):
        update_policy_draft(
            actor=_OWNER,
            policy_id="pol_missing",
            fields={"description": "x"},
            graph=graph,
            access_role_store=store,
        )


def test_update_policy_draft_graph_failure_raises_translated_error() -> None:
    """AC-BI-013's 'graph unavailable' failure mode."""
    graph = _UpdatePolicyDraftFakeGraph(
        _draft_policy(), raise_on_write=redis.exceptions.ConnectionError("boom")
    )
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        update_policy_draft(
            actor=_OWNER,
            policy_id="pol_x",
            fields={"description": "x"},
            graph=graph,
            access_role_store=store,
        )


# --- `add_standard_to_draft` (issue #136, Slice 2) --------------------------


class _AddStandardToDraftFakeGraph(_ReadFakeGraph):
    """Extends `_ReadFakeGraph` with `add_standard_to_policy`'s own write.

    Every other query (the tree read, `backfill_governance_status`'s three
    `SET` statements) is handled by `_ReadFakeGraph.query` unchanged; only
    `add_standard_to_policy`'s own distinguishing `MERGE ... Standard` shape
    is intercepted here.
    """

    def __init__(
        self, policy: _PolicyFixture | None, *, raise_on_write: Exception | None = None
    ) -> None:
        super().__init__(policy)
        self.write_queries: list[str] = []
        self.write_params: list[dict[str, object] | None] = []
        self.raise_on_write = raise_on_write

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "SUPPORTED_BY]->(s:Standard {id: $standard_id})" not in q:
            return super().query(q, params)
        self.write_queries.append(q)
        self.write_params.append(params)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def test_add_standard_to_draft_creates_standard_with_deterministic_id() -> None:
    """AC-BI-009: the returned `standard_id` matches the id formula deterministically."""
    graph = _AddStandardToDraftFakeGraph(_draft_policy())
    store = FakeAccessRoleStore()

    result = add_standard_to_draft(
        actor=_OWNER,
        policy_id="pol_x",
        title="Encryption Standard",
        fields={},
        graph=graph,
        access_role_store=store,
    )

    assert result.policy_id == "pol_x"
    assert result.title == "Encryption Standard"
    assert result.status == "draft"
    assert result.standard_id == standard_id("pol_x", "Encryption Standard")
    assert len(graph.write_queries) == 1
    assert graph.write_params[0] == {
        "policy_id": "pol_x",
        "standard_id": result.standard_id,
        "properties": {
            "implementation_status": "draft",
            "title": "Encryption Standard",
            "status": "draft",
        },
    }


def test_add_standard_to_draft_implementation_status_independent_of_governance_status() -> None:
    """AC-BI-007: governance `status` is always `draft`, `implementation_status`
    defaults to `draft` too unless the caller supplied a different one.
    """
    graph = _AddStandardToDraftFakeGraph(_draft_policy())
    store = FakeAccessRoleStore()

    result = add_standard_to_draft(
        actor=_OWNER,
        policy_id="pol_x",
        title="Encryption Standard",
        fields={"implementation_status": "implemented"},
        graph=graph,
        access_role_store=store,
    )

    assert result.status == "draft"
    params = cast("dict[str, object]", graph.write_params[0])
    props = cast("dict[str, object]", params["properties"])
    assert props["status"] == "draft"
    assert props["implementation_status"] == "implemented"


def test_add_standard_to_draft_rejects_non_owner_non_elevated() -> None:
    """AC-BI-003."""
    graph = _AddStandardToDraftFakeGraph(_draft_policy())
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        add_standard_to_draft(
            actor=_OTHER,
            policy_id="pol_x",
            title="Encryption Standard",
            fields={},
            graph=graph,
            access_role_store=store,
        )
    assert graph.write_queries == []


def test_add_standard_to_draft_allows_system_owner_override() -> None:
    """AC-BI-003's carve-out: a non-owner `SystemOwner` still succeeds."""
    graph = _AddStandardToDraftFakeGraph(_draft_policy())
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OTHER, access_role=AccessRole.SYSTEM_OWNER)

    result = add_standard_to_draft(
        actor=_OTHER,
        policy_id="pol_x",
        title="Encryption Standard",
        fields={},
        graph=graph,
        access_role_store=store,
    )

    assert result.policy_id == "pol_x"
    assert len(graph.write_queries) == 1


def test_add_standard_to_draft_rejects_non_draft_parent_status() -> None:
    """AC-BI-004."""
    policy = _PolicyFixture(id="pol_x", title="X", status="proposed", version="1", owner=_OWNER)
    graph = _AddStandardToDraftFakeGraph(policy)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyInvalidStatusTransitionError) as exc_info:
        add_standard_to_draft(
            actor=_OWNER,
            policy_id="pol_x",
            title="Encryption Standard",
            fields={},
            graph=graph,
            access_role_store=store,
        )

    assert exc_info.value.action == "edit"
    assert exc_info.value.current_status == "proposed"
    assert exc_info.value.required_status == "draft"
    assert graph.write_queries == []


def test_add_standard_to_draft_raises_not_found_for_missing_parent_policy() -> None:
    """AC-BI-012."""
    graph = _AddStandardToDraftFakeGraph(None)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyNotFoundError):
        add_standard_to_draft(
            actor=_OWNER,
            policy_id="pol_missing",
            title="Encryption Standard",
            fields={},
            graph=graph,
            access_role_store=store,
        )


def test_add_standard_to_draft_graph_failure_raises_translated_error() -> None:
    """AC-BI-013's 'graph unavailable' failure mode."""
    graph = _AddStandardToDraftFakeGraph(
        _draft_policy(), raise_on_write=redis.exceptions.ConnectionError("boom")
    )
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        add_standard_to_draft(
            actor=_OWNER,
            policy_id="pol_x",
            title="Encryption Standard",
            fields={},
            graph=graph,
            access_role_store=store,
        )


# --- `update_standard_draft` (issue #136, Slice 3) ---------------------------


@dataclass
class _StandardParentNode:
    """Mutable fixture: one Standard's parent-Policy owner/status plus the
    Standard's own (possibly `None`, pre-backfill) status.

    A single mutable instance so the same fake graph answers
    `find_standard_with_parent`'s read both before and after
    `backfill_governance_status`'s own `s.status IS NULL` write actually
    mutates `standard_status` (CHANGES.md finding #2) -- proving the backfill
    genuinely changes what the second read sees, not just that both calls
    happen.
    """

    policy_id: str
    policy_owner: tuple[str, str]
    policy_status: str
    standard_status: str | None
    standard_title: str = "Encryption Standard"


class _StandardWithParentFakeGraph:
    """A `GraphHandle` double for `update_standard_draft`'s backfill-then-reread + write."""

    def __init__(
        self, node: _StandardParentNode | None, *, raise_on_write: Exception | None = None
    ) -> None:
        self._node = node
        self.write_queries: list[str] = []
        self.write_params: list[dict[str, object] | None] = []
        self.raise_on_write = raise_on_write

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        node = self._node
        if "RETURN p.id, p.owner_subject, p.owner_issuer, p.status, s.status, s.title" in q:
            if node is None:
                return _FakeQueryResult(result_set=[])
            return _FakeQueryResult(
                result_set=[
                    [
                        node.policy_id,
                        node.policy_owner[0],
                        node.policy_owner[1],
                        node.policy_status,
                        node.standard_status,
                        node.standard_title,
                    ]
                ]
            )
        if "s.status IS NULL" in q and "SET s.status = p.status" in q:
            if node is not None and node.standard_status is None:
                node.standard_status = node.policy_status
            return _FakeQueryResult()
        if "$set_properties" not in q and "= null" not in q:
            return _FakeQueryResult()
        self.write_queries.append(q)
        self.write_params.append(params)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def _standard_node(
    *, owner: tuple[str, str] = _OWNER, status: str = "draft", policy_id: str = "pol_x"
) -> _StandardParentNode:
    return _StandardParentNode(
        policy_id=policy_id, policy_owner=owner, policy_status="draft", standard_status=status
    )


def test_update_standard_draft_applies_only_supplied_fields() -> None:
    """AC-BI-008."""
    graph = _StandardWithParentFakeGraph(_standard_node())
    store = FakeAccessRoleStore()

    result = update_standard_draft(
        actor=_OWNER,
        standard_id="std-1",
        fields={"description": "updated description"},
        graph=graph,
        access_role_store=store,
    )

    assert result.standard_id == "std-1"
    assert result.policy_id == "pol_x"
    assert result.status == "draft"
    assert len(graph.write_queries) == 1
    assert graph.write_params[0] == {
        "standard_id": "std-1",
        "set_properties": {"description": "updated description"},
    }


def test_update_standard_draft_rejects_non_owner_non_elevated() -> None:
    """AC-BI-003."""
    graph = _StandardWithParentFakeGraph(_standard_node())
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        update_standard_draft(
            actor=_OTHER,
            standard_id="std-1",
            fields={"description": "hijacked"},
            graph=graph,
            access_role_store=store,
        )
    assert graph.write_queries == []


def test_update_standard_draft_ownership_is_transitive_from_parent_policy() -> None:
    """AC-BI-003: proves the gate genuinely reads the parent-Policy traversal
    row, not some nonexistent Standard-level ownership field -- this fake's
    own `_StandardParentNode` carries no "owner" concept beyond
    `policy_owner`, so a non-owner of the PARENT POLICY is rejected even
    though the Standard being mutated has no ownership field of its own.
    """
    graph = _StandardWithParentFakeGraph(_standard_node(owner=_OWNER))
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        update_standard_draft(
            actor=_OTHER,
            standard_id="std-1",
            fields={"description": "hijacked"},
            graph=graph,
            access_role_store=store,
        )


def test_update_standard_draft_allows_system_owner_override() -> None:
    """AC-BI-003's carve-out: a non-owner `SystemOwner` still succeeds."""
    graph = _StandardWithParentFakeGraph(_standard_node())
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OTHER, access_role=AccessRole.SYSTEM_OWNER)

    result = update_standard_draft(
        actor=_OTHER,
        standard_id="std-1",
        fields={"description": "edited by system owner"},
        graph=graph,
        access_role_store=store,
    )

    assert result.standard_id == "std-1"
    assert len(graph.write_queries) == 1


def test_update_standard_draft_rejects_non_draft_status() -> None:
    """AC-BI-004: the STANDARD's own status gates this, not the parent Policy's."""
    node = _standard_node(status="proposed")
    graph = _StandardWithParentFakeGraph(node)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyInvalidStatusTransitionError) as exc_info:
        update_standard_draft(
            actor=_OWNER,
            standard_id="std-1",
            fields={"description": "too late"},
            graph=graph,
            access_role_store=store,
        )

    assert exc_info.value.action == "edit"
    assert exc_info.value.current_status == "proposed"
    assert exc_info.value.required_status == "draft"
    assert graph.write_queries == []


def test_update_standard_draft_raises_not_found() -> None:
    """AC-BI-012."""
    graph = _StandardWithParentFakeGraph(None)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyStandardNotFoundError):
        update_standard_draft(
            actor=_OWNER,
            standard_id="std-missing",
            fields={"description": "x"},
            graph=graph,
            access_role_store=store,
        )


def test_update_standard_draft_graph_failure_raises_translated_error() -> None:
    """AC-BI-013's 'graph unavailable' failure mode."""
    graph = _StandardWithParentFakeGraph(
        _standard_node(), raise_on_write=redis.exceptions.ConnectionError("boom")
    )
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        update_standard_draft(
            actor=_OWNER,
            standard_id="std-1",
            fields={"description": "x"},
            graph=graph,
            access_role_store=store,
        )


def test_update_standard_draft_backfills_legacy_null_status_before_gating() -> None:
    """CHANGES.md finding #2 (High): a legacy Standard minted before any
    backfilling call (e.g. via the internal-seed adapter) can still have a
    `NULL` own `status` even though its parent Policy is genuinely `draft`.
    Without the backfill-then-reread fix (`_read_standard_with_parent_backfilled`),
    this call would be spuriously rejected with
    `PolicyInvalidStatusTransitionError` (current_status=None) even though
    the Standard is legitimately draft-eligible once backfilled.
    """
    node = _StandardParentNode(
        policy_id="pol_x", policy_owner=_OWNER, policy_status="draft", standard_status=None
    )
    graph = _StandardWithParentFakeGraph(node)
    store = FakeAccessRoleStore()

    result = update_standard_draft(
        actor=_OWNER,
        standard_id="std-1",
        fields={"description": "now editable"},
        graph=graph,
        access_role_store=store,
    )

    assert result.standard_id == "std-1"
    assert node.standard_status == "draft"
    assert len(graph.write_queries) == 1


# --- `add_control_to_draft` (issue #136, Slice 4) ----------------------------


class _AddControlToDraftFakeGraph:
    """A `GraphHandle` double for `add_control_to_draft`'s backfill-then-reread + write.

    Mirrors `_StandardWithParentFakeGraph` (Slice 3) exactly for the read/
    backfill half -- PLAN.md §1.4's own correction: `add-control-to-draft`
    shares Slice 3's **one-hop** `find_standard_with_parent` ownership shape
    (the caller supplies a `standard_id`, just like `update-standard-draft`
    does), never a two-hop traversal (that's `update-control-draft` alone,
    Slice 5, reached via a `control_id`). Adds `add_control_to_standard`'s
    own distinguishing `MERGE ... Control` write branch.
    """

    def __init__(
        self, node: _StandardParentNode | None, *, raise_on_write: Exception | None = None
    ) -> None:
        self._node = node
        self.write_queries: list[str] = []
        self.write_params: list[dict[str, object] | None] = []
        self.raise_on_write = raise_on_write

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        node = self._node
        if "RETURN p.id, p.owner_subject, p.owner_issuer, p.status, s.status, s.title" in q:
            if node is None:
                return _FakeQueryResult(result_set=[])
            return _FakeQueryResult(
                result_set=[
                    [
                        node.policy_id,
                        node.policy_owner[0],
                        node.policy_owner[1],
                        node.policy_status,
                        node.standard_status,
                        node.standard_title,
                    ]
                ]
            )
        if "s.status IS NULL" in q and "SET s.status = p.status" in q:
            if node is not None and node.standard_status is None:
                node.standard_status = node.policy_status
            return _FakeQueryResult()
        if "IMPLEMENTED_BY]->(c:Control {id: $control_id})" not in q:
            return _FakeQueryResult()
        self.write_queries.append(q)
        self.write_params.append(params)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def test_add_control_to_draft_creates_control_with_deterministic_id() -> None:
    """AC-BI-009-shaped: the returned `control_id` matches the id formula deterministically."""
    graph = _AddControlToDraftFakeGraph(_standard_node())
    store = FakeAccessRoleStore()

    result = add_control_to_draft(
        actor=_OWNER,
        standard_id="std-1",
        title="Key Rotation Check",
        control_type="automated",
        fields={},
        graph=graph,
        access_role_store=store,
    )

    assert result.standard_id == "std-1"
    assert result.policy_id == "pol_x"
    assert result.status == "draft"
    assert result.control_id == control_id("std-1", "Key Rotation Check")
    assert len(graph.write_queries) == 1
    assert graph.write_params[0] == {
        "standard_id": "std-1",
        "control_id": result.control_id,
        "properties": {
            "implementation_status": "planned",
            "title": "Key Rotation Check",
            "type": "automated",
            "status": "draft",
        },
    }


def test_add_control_to_draft_defaults_implementation_status_planned_not_draft() -> None:
    """AC-BI-007: Control's own `implementation_status` default is `"planned"`,
    the one deliberate divergence from `add_standard_to_draft`'s own `"draft"`
    default -- easy to copy-paste wrong.
    """
    graph = _AddControlToDraftFakeGraph(_standard_node())
    store = FakeAccessRoleStore()

    result = add_control_to_draft(
        actor=_OWNER,
        standard_id="std-1",
        title="Key Rotation Check",
        control_type="manual",
        fields={},
        graph=graph,
        access_role_store=store,
    )

    assert result.status == "draft"
    params = cast("dict[str, object]", graph.write_params[0])
    props = cast("dict[str, object]", params["properties"])
    assert props["status"] == "draft"
    assert props["implementation_status"] == "planned"


def test_add_control_to_draft_rejects_non_owner_non_elevated() -> None:
    """AC-BI-003."""
    graph = _AddControlToDraftFakeGraph(_standard_node())
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        add_control_to_draft(
            actor=_OTHER,
            standard_id="std-1",
            title="Key Rotation Check",
            control_type="manual",
            fields={},
            graph=graph,
            access_role_store=store,
        )
    assert graph.write_queries == []


def test_add_control_to_draft_ownership_is_transitive_from_parent_policy() -> None:
    """AC-BI-003: proves the gate genuinely reads the parent-Policy traversal
    row, not some nonexistent Standard-level ownership field -- mirrors
    `test_update_standard_draft_ownership_is_transitive_from_parent_policy`.
    """
    graph = _AddControlToDraftFakeGraph(_standard_node(owner=_OWNER))
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        add_control_to_draft(
            actor=_OTHER,
            standard_id="std-1",
            title="Key Rotation Check",
            control_type="manual",
            fields={},
            graph=graph,
            access_role_store=store,
        )


def test_add_control_to_draft_allows_system_owner_override() -> None:
    """AC-BI-003's carve-out: a non-owner `SystemOwner` still succeeds."""
    graph = _AddControlToDraftFakeGraph(_standard_node())
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OTHER, access_role=AccessRole.SYSTEM_OWNER)

    result = add_control_to_draft(
        actor=_OTHER,
        standard_id="std-1",
        title="Key Rotation Check",
        control_type="manual",
        fields={},
        graph=graph,
        access_role_store=store,
    )

    assert result.standard_id == "std-1"
    assert len(graph.write_queries) == 1


def test_add_control_to_draft_rejects_non_draft_parent_status() -> None:
    """AC-BI-004: the STANDARD's own status gates this, not the parent Policy's."""
    node = _standard_node(status="proposed")
    graph = _AddControlToDraftFakeGraph(node)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyInvalidStatusTransitionError) as exc_info:
        add_control_to_draft(
            actor=_OWNER,
            standard_id="std-1",
            title="Key Rotation Check",
            control_type="manual",
            fields={},
            graph=graph,
            access_role_store=store,
        )

    assert exc_info.value.action == "edit"
    assert exc_info.value.current_status == "proposed"
    assert exc_info.value.required_status == "draft"
    assert graph.write_queries == []


def test_add_control_to_draft_raises_not_found_for_missing_parent_standard() -> None:
    """AC-BI-012."""
    graph = _AddControlToDraftFakeGraph(None)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyStandardNotFoundError):
        add_control_to_draft(
            actor=_OWNER,
            standard_id="std-missing",
            title="Key Rotation Check",
            control_type="manual",
            fields={},
            graph=graph,
            access_role_store=store,
        )


def test_add_control_to_draft_graph_failure_raises_translated_error() -> None:
    """AC-BI-013's 'graph unavailable' failure mode."""
    graph = _AddControlToDraftFakeGraph(
        _standard_node(), raise_on_write=redis.exceptions.ConnectionError("boom")
    )
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        add_control_to_draft(
            actor=_OWNER,
            standard_id="std-1",
            title="Key Rotation Check",
            control_type="manual",
            fields={},
            graph=graph,
            access_role_store=store,
        )


def test_add_control_to_draft_backfills_legacy_null_status_before_gating() -> None:
    """CHANGES.md finding #2 (High): a legacy Standard minted before any
    backfilling call can still have a `NULL` own `status` even though its
    parent Policy is genuinely `draft`. Without the backfill-then-reread fix
    (`_read_standard_with_parent_backfilled`), this call would be spuriously
    rejected with `PolicyInvalidStatusTransitionError` (current_status=None)
    even though the Standard is legitimately draft-eligible once backfilled.
    """
    node = _StandardParentNode(
        policy_id="pol_x", policy_owner=_OWNER, policy_status="draft", standard_status=None
    )
    graph = _AddControlToDraftFakeGraph(node)
    store = FakeAccessRoleStore()

    result = add_control_to_draft(
        actor=_OWNER,
        standard_id="std-1",
        title="Key Rotation Check",
        control_type="manual",
        fields={},
        graph=graph,
        access_role_store=store,
    )

    assert result.standard_id == "std-1"
    assert node.standard_status == "draft"
    assert len(graph.write_queries) == 1


# --- `update_control_draft` (issue #136, Slice 5) -----------------------------


@dataclass
class _ControlParentNode:
    """Mutable fixture: one Control's root-Policy owner/status plus the
    Control's own (possibly `None`, pre-backfill) status -- the two-hop
    analogue of `_StandardParentNode` (Slice 3). Deliberately carries no
    ownership concept anywhere except `policy_owner`, since neither Control
    nor its parent Standard has an ownership field of its own (TASK.md's
    Implementation-decisions paragraph).
    """

    policy_id: str
    policy_owner: tuple[str, str]
    policy_status: str
    control_status: str | None
    standard_id: str = "std-1"
    control_title: str = "Key Rotation Check"


class _ControlWithParentFakeGraph:
    """A `GraphHandle` double for `update_control_draft`'s backfill-then-reread + write.

    The read branch below asserts the query text contains BOTH
    `SUPPORTED_BY` and `IMPLEMENTED_BY` before ever answering it -- proving
    `update_control_draft` genuinely issues the two-hop `Policy
    -[:SUPPORTED_BY]-> Standard -[:IMPLEMENTED_BY]-> Control` traversal
    (`find_control_with_parent`), never a one-hop stand-in that would
    silently succeed against a Standard-level "owner" that doesn't exist in
    this schema (mirrors `_StandardWithParentFakeGraph`'s own shape, one hop
    deeper).
    """

    def __init__(
        self, node: _ControlParentNode | None, *, raise_on_write: Exception | None = None
    ) -> None:
        self._node = node
        self.write_queries: list[str] = []
        self.write_params: list[dict[str, object] | None] = []
        self.raise_on_write = raise_on_write

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        node = self._node
        if "RETURN s.id, p.id, p.owner_subject, p.owner_issuer, p.status, c.status, c.title" in q:
            assert "SUPPORTED_BY" in q, "must genuinely traverse Policy -> Standard"
            assert "IMPLEMENTED_BY" in q, "must genuinely traverse Standard -> Control"
            if node is None:
                return _FakeQueryResult(result_set=[])
            return _FakeQueryResult(
                result_set=[
                    [
                        node.standard_id,
                        node.policy_id,
                        node.policy_owner[0],
                        node.policy_owner[1],
                        node.policy_status,
                        node.control_status,
                        node.control_title,
                    ]
                ]
            )
        if "c.status IS NULL" in q and "SET c.status = p.status" in q:
            if node is not None and node.control_status is None:
                node.control_status = node.policy_status
            return _FakeQueryResult()
        if "$set_properties" not in q and "= null" not in q:
            return _FakeQueryResult()
        self.write_queries.append(q)
        self.write_params.append(params)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def _control_node(
    *, owner: tuple[str, str] = _OWNER, status: str = "draft", policy_id: str = "pol_x"
) -> _ControlParentNode:
    return _ControlParentNode(
        policy_id=policy_id, policy_owner=owner, policy_status="draft", control_status=status
    )


def test_update_control_draft_applies_only_supplied_fields() -> None:
    """AC-BI-008."""
    graph = _ControlWithParentFakeGraph(_control_node())
    store = FakeAccessRoleStore()

    result = update_control_draft(
        actor=_OWNER,
        control_id="ctrl-1",
        fields={"description": "updated description"},
        graph=graph,
        access_role_store=store,
    )

    assert result.control_id == "ctrl-1"
    assert result.standard_id == "std-1"
    assert result.policy_id == "pol_x"
    assert result.status == "draft"
    assert len(graph.write_queries) == 1
    assert graph.write_params[0] == {
        "control_id": "ctrl-1",
        "set_properties": {"description": "updated description"},
    }


def test_update_control_draft_rejects_non_owner_non_elevated() -> None:
    """AC-BI-003."""
    graph = _ControlWithParentFakeGraph(_control_node())
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyDraftAccessDeniedError):
        update_control_draft(
            actor=_OTHER,
            control_id="ctrl-1",
            fields={"description": "hijacked"},
            graph=graph,
            access_role_store=store,
        )
    assert graph.write_queries == []


def test_update_control_draft_ownership_traverses_two_hops_not_one() -> None:
    """Proves `update_control_draft` genuinely walks `Policy -[:SUPPORTED_BY]->
    Standard -[:IMPLEMENTED_BY]-> Control`, not a one-hop stand-in:
    `_ControlWithParentFakeGraph`'s own read branch asserts both
    `SUPPORTED_BY` and `IMPLEMENTED_BY` appear in the query text before
    answering it at all -- a call that only did a one-hop traversal would
    either fail that assertion outright or never match this branch and come
    back not-found. Also confirms ownership is resolvable ONLY from the ROOT
    Policy two hops up: `_ControlParentNode` carries no ownership concept on
    the Control or its parent Standard, only `policy_owner` -- a non-owner is
    still correctly rejected even though nothing "Standard-level" exists to
    check.
    """
    graph = _ControlWithParentFakeGraph(_control_node(owner=_OWNER))
    store = FakeAccessRoleStore()

    result = update_control_draft(
        actor=_OWNER,
        control_id="ctrl-1",
        fields={"description": "edited"},
        graph=graph,
        access_role_store=store,
    )

    assert result.control_id == "ctrl-1"
    assert result.policy_id == "pol_x"
    assert result.standard_id == "std-1"

    with pytest.raises(PolicyDraftAccessDeniedError):
        update_control_draft(
            actor=_OTHER,
            control_id="ctrl-1",
            fields={"description": "hijacked"},
            graph=_ControlWithParentFakeGraph(_control_node(owner=_OWNER)),
            access_role_store=store,
        )


def test_update_control_draft_allows_system_owner_override() -> None:
    """AC-BI-003's carve-out: a non-owner `SystemOwner` still succeeds."""
    graph = _ControlWithParentFakeGraph(_control_node())
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_OTHER, access_role=AccessRole.SYSTEM_OWNER)

    result = update_control_draft(
        actor=_OTHER,
        control_id="ctrl-1",
        fields={"description": "edited by system owner"},
        graph=graph,
        access_role_store=store,
    )

    assert result.control_id == "ctrl-1"
    assert len(graph.write_queries) == 1


def test_update_control_draft_rejects_non_draft_status() -> None:
    """AC-BI-004: the CONTROL's own status gates this, not the root Policy's."""
    node = _control_node(status="proposed")
    graph = _ControlWithParentFakeGraph(node)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyInvalidStatusTransitionError) as exc_info:
        update_control_draft(
            actor=_OWNER,
            control_id="ctrl-1",
            fields={"description": "too late"},
            graph=graph,
            access_role_store=store,
        )

    assert exc_info.value.action == "edit"
    assert exc_info.value.current_status == "proposed"
    assert exc_info.value.required_status == "draft"
    assert graph.write_queries == []


def test_update_control_draft_raises_not_found() -> None:
    """AC-BI-012."""
    graph = _ControlWithParentFakeGraph(None)
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyControlNotFoundError):
        update_control_draft(
            actor=_OWNER,
            control_id="ctrl-missing",
            fields={"description": "x"},
            graph=graph,
            access_role_store=store,
        )


def test_update_control_draft_graph_failure_raises_translated_error() -> None:
    """AC-BI-013's 'graph unavailable' failure mode."""
    graph = _ControlWithParentFakeGraph(
        _control_node(), raise_on_write=redis.exceptions.ConnectionError("boom")
    )
    store = FakeAccessRoleStore()

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        update_control_draft(
            actor=_OWNER,
            control_id="ctrl-1",
            fields={"description": "x"},
            graph=graph,
            access_role_store=store,
        )


def test_update_control_draft_backfills_legacy_null_status_before_gating() -> None:
    """CHANGES.md finding #2 (High), two-hop case: a legacy Control minted
    before any backfilling call can still have a `NULL` own `status` even
    though its root Policy is genuinely `draft`. Without the backfill-then-
    reread fix (`_read_control_with_parent_backfilled`), this call would be
    spuriously rejected with `PolicyInvalidStatusTransitionError`
    (current_status=None) even though the Control is legitimately
    draft-eligible once backfilled.
    """
    node = _ControlParentNode(
        policy_id="pol_x", policy_owner=_OWNER, policy_status="draft", control_status=None
    )
    graph = _ControlWithParentFakeGraph(node)
    store = FakeAccessRoleStore()

    result = update_control_draft(
        actor=_OWNER,
        control_id="ctrl-1",
        fields={"description": "now editable"},
        graph=graph,
        access_role_store=store,
    )

    assert result.control_id == "ctrl-1"
    assert node.control_status == "draft"
    assert len(graph.write_queries) == 1


# --- `create_policy_draft`'s `supersedes_policy_id` fork (issue #136, Slice 6) --

_PRIOR_POLICY_ID = "pol_data_protection_policy_prior01"
_PRIOR_STANDARD_ID = "std_encryption_standard_prior01"
_PRIOR_CONTROL_ID = "ctrl_key_rotation_prior01"


def _prior_fixture(*, status: str, version: str = "3") -> _PolicyFixture:
    return _PolicyFixture(
        id=_PRIOR_POLICY_ID, title=_TITLE, status=status, version=version, owner=_ACTOR
    )


def _forked_rows() -> list[object]:
    """One Standard (with one Control), full property maps, as `read_policy_tree_for_fork`
    would return them -- both still carrying the SOURCE's own `"approved"`
    status, proving AC-BI-007 (forked children come out `"draft"`, never
    copied from the source).
    """
    return [
        [
            _PRIOR_STANDARD_ID,
            {
                "id": _PRIOR_STANDARD_ID,
                "title": "Encryption Standard",
                "status": "approved",
                "procedure": "rotate keys quarterly",
            },
            _PRIOR_CONTROL_ID,
            {
                "id": _PRIOR_CONTROL_ID,
                "title": "Key Rotation",
                "status": "approved",
                "type": "automated",
                "evidence_ref": "https://example.com/evidence",
            },
        ]
    ]


@dataclass
class _RecordedWrite:
    query: str
    params: dict[str, object]


class _ForkFakeGraph:
    """A `GraphHandle` double for the supersede-fork's own tests.

    Answers the prior Policy's narrow tree read (`_read_transition_target`'s
    own `read_policy_tree` query), its full-content fork read
    (`read_policy_tree_for_fork`), and `find_existing_policy`'s title-
    collision check (`existing=None` by default -- no collision);
    `backfill_governance_status`'s three `SET ... IS NULL`/`coalesce`
    statements are treated as no-ops (fixtures are always pre-backfilled);
    every other query is recorded verbatim in `self.writes`, letting a test
    assert exactly which node ids a write ever targeted -- AC-BI-006's own
    "the prior tree is never mutated" proof.
    """

    def __init__(
        self,
        *,
        prior: _PolicyFixture | None,
        forked_rows: list[object],
        existing: tuple[str, str] | None = None,
    ) -> None:
        self._prior = prior
        self._forked_rows = forked_rows
        self._existing = existing
        self.writes: list[_RecordedWrite] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        p = params or {}
        if q.strip() == "MATCH (p:Policy {id: $policy_id}) RETURN p.id, p.title":
            rows: list[object] = [[self._existing[0], self._existing[1]]] if self._existing else []
            return _FakeQueryResult(result_set=rows)
        if "RETURN s.id, properties(s), c.id, properties(c)" in q:
            return _FakeQueryResult(result_set=self._forked_rows)
        if "s.id, s.title, s.status, c.id" in q:
            if self._prior is None:
                return _FakeQueryResult(result_set=[])
            return _FakeQueryResult(result_set=_policy_tree_rows(self._prior))
        if "coalesce" in q or "IS NULL" in q:
            return _FakeQueryResult()
        self.writes.append(_RecordedWrite(query=q, params=dict(p)))
        return _FakeQueryResult()


def test_supersede_fork_mints_new_policy_with_successor_version() -> None:
    """AC-BI-005's version arithmetic, string-typed: prior `"5"` -> new `"6"`
    (a non-`"1"`-adjacent value, so this cannot pass by coincidence with the
    ordinary-draft default).
    """
    recorder: list[str] = []
    graph = _ForkFakeGraph(prior=_prior_fixture(status="approved", version="5"), forked_rows=[])
    audit_store = _FakeAuditStore(recorder)

    result = create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        supersedes_policy_id=_PRIOR_POLICY_ID,
        graph=graph,
        audit_store=audit_store,
    )

    assert result.version == "6"
    assert result.superseded_policy_id == _PRIOR_POLICY_ID
    policy_write = next(w for w in graph.writes if "MERGE (p:Policy" in w.query)
    assert cast("dict[str, object]", policy_write.params["properties"])["version"] == "6"


def test_supersede_fork_writes_superseded_by_edge_prior_to_new() -> None:
    recorder: list[str] = []
    graph = _ForkFakeGraph(prior=_prior_fixture(status="approved"), forked_rows=_forked_rows())
    audit_store = _FakeAuditStore(recorder)

    result = create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        supersedes_policy_id=_PRIOR_POLICY_ID,
        graph=graph,
        audit_store=audit_store,
    )

    edge_writes = [w for w in graph.writes if "SUPERSEDED_BY" in w.query]
    assert len(edge_writes) == 1
    assert edge_writes[0].params == {"prior_id": _PRIOR_POLICY_ID, "new_id": result.policy_id}


def test_supersede_fork_forks_full_standard_control_content_not_just_titles() -> None:
    recorder: list[str] = []
    graph = _ForkFakeGraph(prior=_prior_fixture(status="approved"), forked_rows=_forked_rows())
    audit_store = _FakeAuditStore(recorder)

    create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        supersedes_policy_id=_PRIOR_POLICY_ID,
        graph=graph,
        audit_store=audit_store,
    )

    standard_write = next(w for w in graph.writes if "SUPPORTED_BY" in w.query)
    standard_properties = cast("dict[str, object]", standard_write.params["properties"])
    assert standard_properties["procedure"] == "rotate keys quarterly"
    control_write = next(w for w in graph.writes if "IMPLEMENTED_BY" in w.query)
    control_properties = cast("dict[str, object]", control_write.params["properties"])
    assert control_properties["evidence_ref"] == "https://example.com/evidence"


def test_supersede_fork_forked_children_are_draft_not_copied_from_approved_source() -> None:
    """AC-BI-007: governance status is re-derived as `"draft"`, never copied
    from the source Standard/Control's own `"approved"` status.
    """
    recorder: list[str] = []
    graph = _ForkFakeGraph(prior=_prior_fixture(status="approved"), forked_rows=_forked_rows())
    audit_store = _FakeAuditStore(recorder)

    create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        supersedes_policy_id=_PRIOR_POLICY_ID,
        graph=graph,
        audit_store=audit_store,
    )

    standard_write = next(w for w in graph.writes if "SUPPORTED_BY" in w.query)
    assert cast("dict[str, object]", standard_write.params["properties"])["status"] == "draft"
    control_write = next(w for w in graph.writes if "IMPLEMENTED_BY" in w.query)
    assert cast("dict[str, object]", control_write.params["properties"])["status"] == "draft"


def test_supersede_fork_never_writes_to_prior_tree_nodes() -> None:
    """AC-BI-006: the superseded Policy's own content, and its original
    Standard/Control children, are never mutated by the fork -- no write's
    own target-node id parameter ever names a prior-tree id.
    """
    recorder: list[str] = []
    graph = _ForkFakeGraph(prior=_prior_fixture(status="approved"), forked_rows=_forked_rows())
    audit_store = _FakeAuditStore(recorder)

    create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        supersedes_policy_id=_PRIOR_POLICY_ID,
        graph=graph,
        audit_store=audit_store,
    )

    prior_ids = {_PRIOR_POLICY_ID, _PRIOR_STANDARD_ID, _PRIOR_CONTROL_ID}
    for write in graph.writes:
        for key in ("policy_id", "standard_id", "control_id"):
            if key in write.params:
                assert write.params[key] not in prior_ids
    # The one place a prior id legitimately appears in a write at all: as the
    # `SUPERSEDED_BY` edge's own SOURCE endpoint -- never as a node whose own
    # properties are being `SET`.
    edge_write = next(w for w in graph.writes if "SUPERSEDED_BY" in w.query)
    assert edge_write.params["prior_id"] == _PRIOR_POLICY_ID


def test_supersede_missing_prior_raises_not_found() -> None:
    """AC-BI-011: a nonexistent `supersedes_policy_id` is rejected before any
    write or audit event.
    """
    recorder: list[str] = []
    graph = _ForkFakeGraph(prior=None, forked_rows=[])
    audit_store = _FakeAuditStore(recorder)

    with pytest.raises(PolicyNotFoundError):
        create_policy_draft(
            actor=_ACTOR,
            title=_TITLE,
            supersedes_policy_id="pol_missing",
            graph=graph,
            audit_store=audit_store,
        )

    assert graph.writes == []
    assert audit_store.calls == []


def test_supersede_non_approved_prior_raises_named_error() -> None:
    """AC-BI-011: an existing-but-not-`approved` prior is rejected before any
    write or audit event, no fork attempted.
    """
    recorder: list[str] = []
    graph = _ForkFakeGraph(prior=_prior_fixture(status="draft"), forked_rows=[])
    audit_store = _FakeAuditStore(recorder)

    with pytest.raises(PolicySupersedePriorNotApprovedError) as exc_info:
        create_policy_draft(
            actor=_ACTOR,
            title=_TITLE,
            supersedes_policy_id=_PRIOR_POLICY_ID,
            graph=graph,
            audit_store=audit_store,
        )

    assert exc_info.value.policy_id == _PRIOR_POLICY_ID
    assert exc_info.value.actual_status == "draft"
    assert graph.writes == []
    assert audit_store.calls == []


def test_supersede_records_audit_event_with_supersedes_policy_id() -> None:
    """AC-BI-014: the fork's `applied` `policy.create_draft` audit event's
    own `details.supersedes_policy_id` is asserted directly -- not just that
    no exception was raised.
    """
    recorder: list[str] = []
    graph = _ForkFakeGraph(prior=_prior_fixture(status="approved"), forked_rows=_forked_rows())
    audit_store = _FakeAuditStore(recorder)

    result = create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        supersedes_policy_id=_PRIOR_POLICY_ID,
        graph=graph,
        audit_store=audit_store,
    )

    applied_calls = [call for call in audit_store.calls if call.outcome == "applied"]
    assert len(applied_calls) == 1
    assert applied_calls[0].action == "policy.create_draft"
    assert applied_calls[0].resource_id == result.policy_id
    assert applied_calls[0].details["supersedes_policy_id"] == _PRIOR_POLICY_ID


def test_ordinary_create_policy_draft_still_has_null_supersedes_policy_id_in_audit_details() -> (
    None
):
    """Regression guard: the ordinary (non-fork) path's audit details still
    carry `supersedes_policy_id`, present-but-`None`.
    """
    recorder: list[str] = []
    graph = _FakeGraph(recorder)
    audit_store = _FakeAuditStore(recorder)

    create_policy_draft(actor=_ACTOR, title=_TITLE, graph=graph, audit_store=audit_store)

    assert audit_store.calls[0].details["supersedes_policy_id"] is None
