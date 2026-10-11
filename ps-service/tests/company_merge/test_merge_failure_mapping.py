"""Gateway failures reach Company Merge callers as `CompanyMergePersistenceError` (issue #210, 2d).

AC-BI-008: a refused group, a group committed but not applied, a graph that is behind its log and
a failed stamp all surface as `CompanyMergePersistenceError("FalkorDB write failed: ...")`, with
no gateway internals in the message, and FalkorDB connectivity health is recorded as the gateway
records it (transient exhaustion marks FALKORDB unhealthy; a permanent refusal does not). Read
failures keep the existing `FalkorDB query failed` text and mark FALKORDB unhealthy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from company_merge._gateway_fakes import POLICY_SYSTEM, MergeRig
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.graph_submission import GraphWriteTarget, read_rows, submit_group
from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.graph_gateway.errors import GraphLogUnavailableError
from ps_service.graph_gateway.models import NodeRef, UpsertEdge, UpsertNode

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphQueryResult
    from ps_service.graph_gateway.edge_identity_backfill import EdgeIdentityBackfillOutcome
    from ps_service.graph_gateway.models import GroupOutcome, MutationGroup

_PREFIX = "FalkorDB write failed: "
_HOST = "Error 111 connecting to 10.0.0.5:6379"


def _target(rig: MergeRig) -> GraphWriteTarget:
    return GraphWriteTarget(rig.gateway, POLICY_SYSTEM, rig.audit_event_id)


def _role(role_id: str = "role_a") -> UpsertNode:
    return UpsertNode(label="Role", id=role_id, properties={"name": "A"})


def test_a_group_the_gateway_refuses_is_a_persistence_error_and_nothing_is_logged(
    merge_rig: MergeRig,
) -> None:
    dangling = UpsertEdge(
        type="DEFINES",
        source=NodeRef(label="RegulatoryInstrument", id="missing"),
        target=NodeRef(label="Role", id="role_a"),
        identity="DEFINES:missing->role_a",
        properties={},
    )

    with pytest.raises(CompanyMergePersistenceError) as exc_info:
        submit_group(_target(merge_rig), (_role(), dangling))

    message = str(exc_info.value)
    assert message.startswith(_PREFIX)
    assert merge_rig.groups == []
    assert is_healthy(FALKORDB) is True  # a refusal is not a connectivity problem


def test_a_committed_but_unapplied_group_is_a_persistence_error_without_the_host(
    merge_rig: MergeRig,
) -> None:
    merge_rig.policy_system.fail_on_write(redis.exceptions.ConnectionError(_HOST))

    with pytest.raises(CompanyMergePersistenceError) as exc_info:
        submit_group(_target(merge_rig), (_role(),))

    message = str(exc_info.value)
    assert message.startswith(_PREFIX)
    assert "10.0.0.5" not in message
    assert "6379" not in message
    assert len(merge_rig.groups) == 1  # committed to the log, not yet applied
    assert is_healthy(FALKORDB) is False  # the gateway recorded the transient exhaustion


def test_a_graph_still_behind_its_log_after_the_group_is_a_persistence_error(
    merge_rig: MergeRig,
) -> None:
    submitter = _CountingSubmitter(merge_rig, caught_up=False)

    with pytest.raises(CompanyMergePersistenceError) as exc_info:
        submit_group(
            GraphWriteTarget(submitter, POLICY_SYSTEM, merge_rig.audit_event_id), (_role(),)
        )

    assert str(exc_info.value).startswith(_PREFIX)
    assert "committed to the graph log" in str(exc_info.value)


def test_a_permanent_apply_refusal_is_a_persistence_error_and_is_not_a_health_problem(
    merge_rig: MergeRig,
) -> None:
    merge_rig.policy_system.fail_on_write(redis.exceptions.ResponseError("Type mismatch"))

    with pytest.raises(CompanyMergePersistenceError) as exc_info:
        submit_group(_target(merge_rig), (_role(),))

    assert str(exc_info.value).startswith(_PREFIX)
    assert "Type mismatch" not in str(exc_info.value)
    assert is_healthy(FALKORDB) is True


def test_an_unreachable_log_is_a_persistence_error_chained_to_the_gateway_error(
    merge_rig: MergeRig,
) -> None:
    merge_rig.store.fail_appends(error=GraphLogUnavailableError())

    with pytest.raises(CompanyMergePersistenceError) as exc_info:
        submit_group(_target(merge_rig), (_role(),))

    assert str(exc_info.value).startswith(_PREFIX)
    assert isinstance(exc_info.value.__cause__, GraphLogUnavailableError)


class _CountingSubmitter:
    """Delegates to the rig's gateway; counts stamps, can refuse them or report a lagging graph."""

    def __init__(
        self, rig: MergeRig, *, refuse_stamp: bool = False, caught_up: bool = True
    ) -> None:
        self._rig = rig
        self._caught_up = caught_up
        self._refuse_stamp = refuse_stamp
        self.stamps = 0

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        return self._rig.gateway.submit_group(group)

    def is_caught_up(self, graph: str) -> bool:
        return self._caught_up and self._rig.gateway.is_caught_up(graph)

    def stamp_legacy_edge_identity(
        self, graph: str, audit_event_id: str
    ) -> EdgeIdentityBackfillOutcome:
        self.stamps += 1
        if self._refuse_stamp:
            raise GraphLogUnavailableError
        return self._rig.gateway.stamp_legacy_edge_identity(graph, audit_event_id)


def test_the_graph_is_stamped_once_per_target_however_many_groups_follow(
    merge_rig: MergeRig,
) -> None:
    submitter = _CountingSubmitter(merge_rig)
    target = GraphWriteTarget(submitter, POLICY_SYSTEM, merge_rig.audit_event_id)

    submit_group(target, (_role("role_a"),))
    submit_group(target, (_role("role_b"),))
    submit_group(target, ())

    assert submitter.stamps == 1
    assert len(merge_rig.groups) == 2


def test_nothing_to_write_neither_stamps_nor_submits(merge_rig: MergeRig) -> None:
    submitter = _CountingSubmitter(merge_rig)

    submit_group(GraphWriteTarget(submitter, POLICY_SYSTEM, merge_rig.audit_event_id), ())

    assert submitter.stamps == 0
    assert merge_rig.groups == []


def test_a_failed_stamp_is_a_persistence_error_and_the_group_is_not_submitted(
    merge_rig: MergeRig,
) -> None:
    submitter = _CountingSubmitter(merge_rig, refuse_stamp=True)
    target = GraphWriteTarget(submitter, POLICY_SYSTEM, merge_rig.audit_event_id)

    with pytest.raises(CompanyMergePersistenceError, match=r"^FalkorDB write failed: "):
        submit_group(target, (_role(),))

    assert merge_rig.groups == []
    assert target.stamped is False  # the next attempt stamps again


class _DownGraph:
    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        del q, params
        raise redis.exceptions.ConnectionError(_HOST)


def test_a_failed_read_marks_falkordb_unhealthy_and_is_a_query_failed_error() -> None:
    with pytest.raises(CompanyMergePersistenceError, match=r"^FalkorDB query failed: "):
        read_rows(_DownGraph(), "MATCH (n) RETURN n")

    assert is_healthy(FALKORDB) is False
