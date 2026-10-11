"""Both `resolve_review` decisions map a gateway failure like every stage writer (issue #210, 11b).

AC-BI-008, for `keep-separate` and `merge`: a group the gateway rejects, a group committed to the
log but not applied (`committed_apply_pending`) and a log that cannot be reached all surface as
`CompanyMergePersistenceError("FalkorDB write failed: ...")`, with no gateway internals in the
message; FalkorDB connectivity health is recorded as the gateway records it (transient exhaustion
marks FALKORDB unhealthy; a rejection or an unreachable log does not). The stage writers are in
`test_merge_writer_failure_matrix.py` (11a); the API classification is in
`tests/api/test_routes_near_misses.py`.

The submitter seam is the gateway rig (in-memory log and FalkorDB fakes, the true boundaries); a
rejection is injected at the `GroupSubmitter` boundary with a hand-written wrapper.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest
import redis.exceptions

from company_merge._gateway_fakes import POLICY_SYSTEM
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.pending_review import resolve_review
from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.graph_gateway.errors import (
    GraphLogUnavailableError,
    MissingTargetError,
    StaleGraphStateError,
    UnlistedNameError,
)

if TYPE_CHECKING:
    from company_merge._gateway_fakes import MergeRig
    from ps_service.graph_gateway.edge_identity_backfill import EdgeIdentityBackfillOutcome
    from ps_service.graph_gateway.models import GroupOutcome, MutationGroup

_PREFIX = "FalkorDB write failed: "
_HOST = "Error 111 connecting to 10.0.0.5:6379"
_LEAKS = (
    "10.0.0.5",
    "6379",
    "committed_apply_pending",
    "GraphApply",
    "GraphLog",
    "StaleGraphState",
    "MissingTarget",
    "UnlistedName",
    "sequence",
    "sha256",
    "Traceback",
)
_REVIEW_ID = "review_aaa"
_DECISIONS = pytest.mark.parametrize("decision", ["keep-separate", "merge"])

type _Decision = Literal["keep-separate", "merge"]


def _seed_review(rig: MergeRig) -> None:
    graph = rig.policy_system
    graph.seed_node(("Capability",), "cap_incoming", {"name": "incoming"})
    graph.seed_node(("Capability",), "cap_existing", {"name": "existing"})
    graph.seed_node(("Obligation",), "obl_a", {"text": "text"})
    graph.seed_node(
        ("PendingReview",),
        _REVIEW_ID,
        {
            "kind": "Capability",
            "status": "pending",
            "incoming_id": "cap_incoming",
            "incoming_text": "in",
            "nearest_existing_id": "cap_existing",
            "nearest_existing_text": "ex",
            "similarity": 0.91,
            "created_at": "2024-06-01T00:00:00+00:00",
        },
    )
    graph.seed_legacy_edge(("REQUIRES", "Obligation", "obl_a", "Capability", "cap_incoming"), {})


class _RejectingSubmitter:
    """The rig's gateway, except every submitted group is refused with `error`."""

    def __init__(self, rig: MergeRig, error: Exception) -> None:
        self._rig = rig
        self._error = error

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        del group
        raise self._error

    def is_caught_up(self, graph: str) -> bool:
        return self._rig.gateway.is_caught_up(graph)

    def stamp_legacy_edge_identity(
        self, graph: str, audit_event_id: str
    ) -> EdgeIdentityBackfillOutcome:
        return self._rig.gateway.stamp_legacy_edge_identity(graph, audit_event_id)


def _assert_clean(error: CompanyMergePersistenceError) -> None:
    message = str(error)
    assert message.startswith(_PREFIX)
    for leak in _LEAKS:
        assert leak not in message, f"{leak!r} leaked into {message!r}"


def _resolve(rig: MergeRig, target: GraphWriteTarget, decision: _Decision) -> None:
    resolve_review(rig.legacy, _REVIEW_ID, decision, target)


def _review_and_candidates_intact(rig: MergeRig) -> None:
    nodes = rig.policy_system.nodes
    assert ("PendingReview", _REVIEW_ID) in nodes
    assert ("Capability", "cap_incoming") in nodes
    assert ("Capability", "cap_existing") in nodes
    assert rig.legacy.edge_count("REQUIRES") == 1


@pytest.mark.parametrize(
    "refusal",
    [
        UnlistedNameError("primitive 0 names a label outside the allow-list"),
        MissingTargetError("primitive 1 names an endpoint that does not exist"),
        StaleGraphStateError("policy_system", 41, 43),
    ],
    ids=["unlisted-name", "missing-target", "stale-state"],
)
@_DECISIONS
def test_a_rejected_resolution_group_is_a_persistence_error_and_changes_nothing(
    decision: _Decision, refusal: Exception, merge_rig: MergeRig
) -> None:
    _seed_review(merge_rig)
    target = GraphWriteTarget(
        _RejectingSubmitter(merge_rig, refusal), POLICY_SYSTEM, merge_rig.audit_event_id
    )

    with pytest.raises(CompanyMergePersistenceError) as exc_info:
        _resolve(merge_rig, target, decision)

    _assert_clean(exc_info.value)
    assert exc_info.value.__cause__ is refusal
    # Only the one-time legacy edge-identity stamp reached the log, never the resolution.
    assert {entry.name for entry in merge_rig.logged()} <= {"REQUIRES"}
    _review_and_candidates_intact(merge_rig)
    assert is_healthy(FALKORDB) is True  # a refusal is not a connectivity problem


@_DECISIONS
def test_a_resolution_committed_but_not_applied_is_a_persistence_error_and_marks_falkordb_unhealthy(
    decision: _Decision, merge_rig: MergeRig
) -> None:
    """FalkorDB is down when the gateway applies: the group is in the log (applied on recovery)
    but the resolution must not report success.
    """
    _seed_review(merge_rig)
    merge_rig.policy_system.fail_on_write(redis.exceptions.ConnectionError(_HOST))
    target = GraphWriteTarget(merge_rig.gateway, POLICY_SYSTEM, merge_rig.audit_event_id)

    with pytest.raises(CompanyMergePersistenceError) as exc_info:
        _resolve(merge_rig, target, decision)

    _assert_clean(exc_info.value)
    assert len(merge_rig.groups) == 1
    assert is_healthy(FALKORDB) is False


@_DECISIONS
def test_an_unreachable_log_fails_a_resolution_with_the_persistence_error_and_logs_nothing(
    decision: _Decision, merge_rig: MergeRig
) -> None:
    _seed_review(merge_rig)
    merge_rig.store.fail_appends(error=GraphLogUnavailableError())
    target = GraphWriteTarget(merge_rig.gateway, POLICY_SYSTEM, merge_rig.audit_event_id)

    with pytest.raises(CompanyMergePersistenceError) as exc_info:
        _resolve(merge_rig, target, decision)

    _assert_clean(exc_info.value)
    assert isinstance(exc_info.value.__cause__, GraphLogUnavailableError)
    assert merge_rig.store.groups == []
    _review_and_candidates_intact(merge_rig)
