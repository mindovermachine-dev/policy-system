"""The Graph Cleanup side of the gateway seam.

`submit_cleanup_group` is the one place a cleanup writer's primitives reach the gateway and the one
place gateway failures become Graph Cleanup's own errors: a stale `ExpectedPosition` is the stale
error, every other gateway failure is the sanitized persistence error, and a group that is
committed but not applied yet is SUCCESS (the documented committed-not-applied exception; the edit
is in the log and the gateway applies it on recovery).
"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_cleanup._gateway_fakes import POLICY_SYSTEM, CleanupRig
from ps_service.api.errors import GraphWriteUnavailableError
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.dependencies import (
    GraphCleanupDependencies,
    build_default_graph_cleanup_graph_opener,
)
from ps_service.graph_cleanup.errors import (
    GraphCleanupPersistenceError,
    GraphCleanupStaleStateError,
)
from ps_service.graph_cleanup.graph_submission import (
    CleanupSubmitter,
    GraphWriteTarget,
    cleanup_submitter,
    edge_identity,
    ensure_stamped,
    graph_write_target,
    read_log_position,
    refuse_unstorable_values,
    require_caught_up,
    submit_cleanup_group,
)
from ps_service.graph_gateway.edge_identity_backfill import EdgeIdentityBackfillOutcome
from ps_service.graph_gateway.errors import (
    GraphApplyError,
    GraphLogPersistenceError,
    GraphUnavailableError,
    GraphWriteRejectedError,
    MissingTargetError,
    StaleGraphStateError,
    UnexpectedGraphReplyError,
    UnlistedNameError,
)
from ps_service.graph_gateway.models import (
    DeleteEdge,
    ExpectedPosition,
    GroupOutcome,
    MutationGroup,
    NodeRef,
    UpsertNode,
)
from ps_service.persistence.errors import StatePostgresConnectionError
from ps_service.policy_lifecycle.graph_submission import edge_identity as lifecycle_edge_identity

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.graph_gateway.graph_reader import PersistedElements

_HOST = "10.1.2.3"
_SANITIZED = "the policy graph database could not complete the write"
_STALE = "the preview is stale; nothing was written"
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=False,
)


def _group() -> tuple[UpsertNode, ...]:
    return (UpsertNode(label="Capability", id="cap_x", properties={"name": "x"}),)


def _target(rig: CleanupRig) -> GraphWriteTarget:
    return GraphWriteTarget(rig.gateway, POLICY_SYSTEM)


def _submit(target: GraphWriteTarget, **kwargs: object) -> None:
    submit_cleanup_group(
        target,
        _group(),
        audit_event_id=None,
        stale_message=_STALE,
        **kwargs,  # pyright: ignore[reportArgumentType] -- test passes through optional keywords
    )


class _RaisingSubmitter:
    """A `CleanupSubmitter` whose `submit_group` raises `error` (or returns `outcome`)."""

    def __init__(self, error: Exception | None = None, outcome: GroupOutcome | None = None) -> None:
        self._error = error
        self._outcome = outcome
        self.submitted = 0

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        del group
        self.submitted += 1
        if self._error is not None:
            raise self._error
        assert self._outcome is not None
        return self._outcome

    def is_caught_up(self, graph: str) -> bool:
        del graph
        return True

    def stamp_legacy_edge_identity(
        self, graph: str, audit_event_id: str | None
    ) -> EdgeIdentityBackfillOutcome:
        del graph, audit_event_id
        return EdgeIdentityBackfillOutcome(status="unchanged", stamped=0, skipped=0)

    def read_persisted_elements(self, graph: str) -> PersistedElements:
        raise AssertionError("not used")

    def read_log_position(self, graph: str) -> int:
        del graph
        return 0


def _outcome(status: str) -> GroupOutcome:
    logged = status != "unchanged"
    return GroupOutcome.model_validate(
        {
            "graph": POLICY_SYSTEM,
            "first_position": 1 if logged else None,
            "last_position": 1 if logged else None,
            "status": status,
        }
    )


# -- the shared edge identity ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("edge_type", "source", "target"),
    [("GOVERNED_BY", "cap_a", "pol_1"), ("REQUIRES", "obl_x", "cap_y"), ("MERGED_INTO", "a", "b")],
)
def test_edge_identity_is_the_shared_convention(edge_type: str, source: str, target: str) -> None:
    assert edge_identity(edge_type, source, target) == f"{edge_type}:{source}->{target}"
    assert edge_identity(edge_type, source, target) == lifecycle_edge_identity(
        edge_type, source, target
    )


# -- the stale / persistence split (catch order: F5) ----------------------------------------------


def test_a_stale_position_is_the_stale_error_with_the_flows_message() -> None:
    target = GraphWriteTarget(_RaisingSubmitter(StaleGraphStateError("policy_system", 3, 4)), "g")

    with pytest.raises(GraphCleanupStaleStateError) as raised:
        _submit(target)

    assert str(raised.value) == _STALE
    assert isinstance(raised.value.__cause__, StaleGraphStateError)


@pytest.mark.parametrize(
    "error",
    [
        GraphWriteRejectedError(f"rejected by {_HOST}"),
        MissingTargetError(f"missing at {_HOST}"),
        UnlistedNameError(f"unlisted at {_HOST}"),
        GraphApplyError("policy_system", 3),
        GraphUnavailableError(),
        GraphLogPersistenceError(f"log failed at {_HOST}"),
        UnexpectedGraphReplyError(f"odd reply from {_HOST}"),
        StatePostgresConnectionError(f"postgres at {_HOST}"),
        GraphWriteUnavailableError(f"no gateway at {_HOST}"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_every_other_gateway_failure_is_the_sanitized_persistence_error(error: Exception) -> None:
    target = GraphWriteTarget(_RaisingSubmitter(error), "g")

    with pytest.raises(GraphCleanupPersistenceError) as raised:
        _submit(target)

    assert str(raised.value) == _SANITIZED
    assert raised.value.__cause__ is error


def test_an_error_that_is_not_a_gateway_failure_is_a_bug_and_propagates() -> None:
    target = GraphWriteTarget(_RaisingSubmitter(RuntimeError("bug")), "g")

    with pytest.raises(RuntimeError):
        _submit(target)


@pytest.mark.parametrize("status", ["applied", "unchanged", "committed_apply_pending"])
def test_a_group_that_took_effect_or_is_committed_returns_normally(status: str) -> None:
    submitter = _RaisingSubmitter(outcome=_outcome(status))

    _submit(GraphWriteTarget(submitter, "g"))

    assert submitter.submitted == 1


def test_a_real_stale_position_is_refused_and_nothing_of_ours_is_logged(
    cleanup_rig: CleanupRig,
) -> None:
    target = _target(cleanup_rig)
    expected = read_log_position(target)
    cleanup_rig.append_unrelated_group()
    logged = len(cleanup_rig.logged_groups)

    with pytest.raises(GraphCleanupStaleStateError):
        _submit(target, preconditions=(ExpectedPosition(position=expected),))

    assert len(cleanup_rig.logged_groups) == logged


def test_committed_but_not_applied_is_success_and_leaks_nothing(cleanup_rig: CleanupRig) -> None:
    cleanup_rig.graph.fail_on_write(redis.exceptions.ConnectionError(f"connecting to {_HOST}:6379"))

    _submit(_target(cleanup_rig))

    assert len(cleanup_rig.logged_groups) == 1
    assert not cleanup_rig.gateway.is_caught_up(POLICY_SYSTEM)


# -- the real gateway's failures ------------------------------------------------------------------


def _refused(rig: CleanupRig) -> None:
    submit_cleanup_group(
        _target(rig),
        (UpsertNode(label="Nope", id="x"),),
        audit_event_id=None,
        stale_message=_STALE,
    )


def _log_write_fails(rig: CleanupRig) -> None:
    rig.store.fail_appends(error=GraphLogPersistenceError("the graph log append failed"))
    _submit(_target(rig))


def _postgres_down(rig: CleanupRig) -> None:
    rig.store.fail_appends()
    _submit(_target(rig))


def _graph_unreachable(rig: CleanupRig) -> None:
    rig.graph.fail_on_read(redis.exceptions.ConnectionError(f"Error connecting to {_HOST}:6379"))
    _submit(_target(rig))


def _behind_its_log(rig: CleanupRig) -> None:
    rig.graph.fail_on_write(redis.exceptions.ConnectionError("down"))
    _submit(_target(rig))
    require_caught_up(_target(rig))


def _position_unreadable(rig: CleanupRig) -> None:
    rig.store.fail_reads()
    read_log_position(_target(rig))


def _stamp_unreadable(rig: CleanupRig) -> None:
    rig.graph.fail_on_read(redis.exceptions.ConnectionError(f"Error connecting to {_HOST}:6379"))
    ensure_stamped(_target(rig), None)


_FAILURES: dict[str, Callable[[CleanupRig], None]] = {
    "gateway-refused": _refused,
    "log-write-failed": _log_write_fails,
    "log-unavailable": _postgres_down,
    "graph-unreachable": _graph_unreachable,
    "graph-behind-its-log": _behind_its_log,
    "position-unreadable": _position_unreadable,
    "stamp-unreadable": _stamp_unreadable,
}


@pytest.mark.parametrize("failure", list(_FAILURES))
def test_every_real_gateway_failure_is_the_sanitized_persistence_error(
    cleanup_rig: CleanupRig, failure: str
) -> None:
    with pytest.raises(GraphCleanupPersistenceError) as raised:
        _FAILURES[failure](cleanup_rig)

    assert str(raised.value) == _SANITIZED
    assert _HOST not in str(raised.value)


def test_a_value_the_graph_cannot_store_is_refused_before_anything_is_written() -> None:
    with pytest.raises(GraphCleanupPersistenceError) as raised, refuse_unstorable_values():
        UpsertNode(label="Obligation", id="o", properties={"nested": [[1]], "secret": "s3cr3t"})

    assert str(raised.value) == _SANITIZED
    assert "s3cr3t" not in str(raised.value)
    assert raised.value.__cause__ is not None


# -- the stamp: once per target, after the guard, never for an empty group -------------------------


def test_an_empty_group_submits_and_stamps_nothing(cleanup_rig: CleanupRig) -> None:
    cleanup_rig.seed_governs("cap_a", "pol_1", legacy=True)

    submit_cleanup_group(_target(cleanup_rig), (), audit_event_id=None, stale_message=_STALE)

    assert cleanup_rig.logged_groups == []


def test_legacy_edges_are_stamped_once_per_target(cleanup_rig: CleanupRig) -> None:
    cleanup_rig.seed_capability("cap_a")
    cleanup_rig.seed_policy("pol_1", title="p", status="draft")
    cleanup_rig.seed_governs("cap_a", "pol_1", legacy=True)
    target = _target(cleanup_rig)

    first = ensure_stamped(target, None)
    second = ensure_stamped(target, None)

    assert (first, second) == (1, 0)
    assert len(cleanup_rig.logged_groups) == 1


def test_a_submit_stamps_legacy_edges_ahead_of_the_group_under_its_audit_link(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_capability("cap_a")
    cleanup_rig.seed_policy("pol_1", title="p", status="draft")
    cleanup_rig.seed_governs("cap_a", "pol_1", legacy=True)
    audit_id = str(uuid.uuid4())
    cleanup_rig.store.audit_rows.add(audit_id)
    primitives = (
        DeleteEdge(
            type="GOVERNED_BY",
            identity=edge_identity("GOVERNED_BY", "cap_a", "pol_1"),
            source=NodeRef(label="Capability", id="cap_a"),
            target=NodeRef(label="Policy", id="pol_1"),
        ),
    )

    submit_cleanup_group(
        _target(cleanup_rig), primitives, audit_event_id=audit_id, stale_message=_STALE
    )

    links = [cleanup_rig.store.links[g.group_id] for g in cleanup_rig.logged_groups]
    assert links == [audit_id, audit_id]  # the stamp group, then ours
    assert not [k for k in cleanup_rig.graph.edges if k[0] == "GOVERNED_BY"]


# -- wiring ---------------------------------------------------------------------------------------


def test_a_gateway_that_cannot_read_the_log_position_is_a_wiring_error() -> None:
    class _NoPosition:
        def submit_group(self, group: MutationGroup) -> GroupOutcome:
            raise AssertionError

        def is_caught_up(self, graph: str) -> bool:
            raise AssertionError

        def stamp_legacy_edge_identity(self, graph: str, audit_event_id: str | None) -> object:
            raise AssertionError

        def read_persisted_elements(self, graph: str) -> object:
            raise AssertionError

    with pytest.raises(TypeError):
        cleanup_submitter(_NoPosition())  # pyright: ignore[reportArgumentType] -- the point of the test


def test_the_rig_gateway_is_a_cleanup_submitter(cleanup_rig: CleanupRig) -> None:
    assert isinstance(cleanup_submitter(cleanup_rig.gateway), CleanupSubmitter)


def test_no_installed_gateway_is_the_persistence_error() -> None:
    def _unavailable(_config: ServiceConfig) -> CleanupSubmitter:
        raise GraphWriteUnavailableError(f"no gateway at {_HOST}")

    dependencies = GraphCleanupDependencies(
        open_single_tenant_graph=lambda _config: object(),  # pyright: ignore[reportArgumentType]
        graph_write_gateway=_unavailable,
    )

    with pytest.raises(GraphCleanupPersistenceError) as raised:
        graph_write_target(dependencies, _CONFIG)

    assert str(raised.value) == _SANITIZED


@pytest.mark.parametrize("graph_env", [None, "custom_graph"])
def test_the_group_goes_to_the_graph_the_reads_open(
    monkeypatch: pytest.MonkeyPatch, cleanup_rig: CleanupRig, graph_env: str | None
) -> None:
    if graph_env is None:
        monkeypatch.delenv("PS_FALKORDB_GRAPH", raising=False)
    else:
        monkeypatch.setenv("PS_FALKORDB_GRAPH", graph_env)
    opened: list[str] = []

    def _connect(_config: ServiceConfig) -> object:
        return object()

    def _select(_client: object, name: str) -> object:
        opened.append(name)
        return object()

    monkeypatch.setattr("ps_service.company_merge.falkordb_client.connect_from_config", _connect)
    # detroit-exception: no FalkorDB server here; the graph name passed IS the spec
    monkeypatch.setattr("ps_service.company_merge.falkordb_client.select_graph", _select)
    build_default_graph_cleanup_graph_opener()(_CONFIG)
    dependencies = cleanup_rig.dependencies(roles=None)  # pyright: ignore[reportArgumentType] -- roles are not read here

    target = graph_write_target(dependencies, _CONFIG)

    assert opened == [target.graph_name]
    assert target.graph_name == os.environ.get("PS_FALKORDB_GRAPH", "policy_system")
