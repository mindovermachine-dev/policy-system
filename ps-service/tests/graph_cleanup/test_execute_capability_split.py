"""`split-capability` end to end: preview, passkey signature, executor, one gateway group.

The MCP tool creates the approval, the signing router verifies a real WebAuthn assertion and runs
the registered executor, which re-plans from the policy graph and the baselines, records the
`applied` audit row first and then writes one gateway group. `check-cleanup-approval` and
`list-audit-events` report the result. Only the graph openers (the approved boundary), the
gateway rig's in-memory log and graphs, the stores and the role grants are fakes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import FakeAccessRoleStore
from fastapi.testclient import TestClient
from graph_gateway._fakes import close_all_rigs
from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)
from passkey_signing._fakes import FakeSigningCredentialStore
from passkey_signing._harness import (
    ACTOR_ISSUER,
    ACTOR_SUBJECT,
    ORIGIN,
    RP_ID,
    Authenticator,
    enroll,
    sign_challenge,
)

from graph_cleanup._fakes import FakeApprovalStore
from graph_cleanup._gateway_fakes import SPLIT_A, SPLIT_POLICY, SPLIT_X, CleanupRig
from graph_cleanup._mcp_calls import call_tool, factory
from ps_service.api.dependencies import provide_pending_approval_store
from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.audit.models import AuditEventRow
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup import dependencies, graph_reader
from ps_service.graph_cleanup.executors import TOOL_SPLIT_CAPABILITY, verify_capability_split_effect
from ps_service.graph_cleanup.service import failed_audit_row, interrupted_audit_row
from ps_service.graph_gateway.errors import GraphLogPersistenceError
from ps_service.logging import configure
from ps_service.main import create_app
from ps_service.passkey_signing.router import provide_signing_credential_store

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from ps_service.graph_gateway.system_audit import SystemAuditRecorder
    from ps_service.passkey_signing.models import PendingApprovalRow

_OWNER = ("owner", "https://issuer.example.com/")
_OFFICER = (ACTOR_SUBJECT, ACTOR_ISSUER)
_TOOL = "split-capability"
_ACTION = "capability.split"


class _World:
    """A governed split fixture behind the real app, signing router and MCP tools."""

    def __init__(self, *, legacy: bool) -> None:
        self.rig = CleanupRig()
        self.rig.seed_capability_split_world(governed=True, legacy=legacy)
        self.graph = self.rig.read
        self.audit = self.rig.audit
        self.pending = FakeApprovalStore()
        self.credentials = FakeSigningCredentialStore()
        self.roles = FakeAccessRoleStore(expected_owner=_OWNER)
        self.roles.bootstrap_first_owner(_OWNER)
        self.roles.grant(actor=_OWNER, target=_OFFICER, access_role=AccessRole.COMPLIANCE_OFFICER)
        self.authenticator = Authenticator()
        enroll(self.credentials, self.authenticator)
        app = create_app(
            ServiceConfig(
                host="127.0.0.1",
                port=8000,
                graceful_shutdown_seconds=10,
                logging_dir=None,
                is_local_test_bypass_active=True,
                authentik_api_token="t",
                authentik_base_url="https://authentik.example.com",
            )
        )
        app.dependency_overrides[provide_pending_approval_store] = lambda: self.pending
        app.dependency_overrides[provide_signing_credential_store] = lambda: self.credentials
        self.client = TestClient(app)

    def fail_graph_write(self, error: Exception) -> None:
        """Make the gateway fail the group's append (the write the executor maps to `failed`)."""
        self.rig.store.fail_appends(error=GraphLogPersistenceError(str(error)))

    def approve(self) -> tuple[PendingApprovalRow, str]:
        created = json.loads(
            call_tool(_TOOL, {"x_id": SPLIT_X, "acknowledge_governance_change": True}, _OFFICER)
        )
        row = self.pending.get_by_id(created["pending_approval_id"])
        assert row is not None
        return row, created["approval_url"].rsplit("#", 1)[1]

    def sign(self, row: PendingApprovalRow, code: str):
        assertion = self.authenticator.build_assertion(
            rp_id=RP_ID, origin=ORIGIN, challenge=sign_challenge(row), sign_count=1
        )
        return self.client.post(
            f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
        )


def _opener(value: object) -> Callable[[], Callable[[object], object]]:
    def _build() -> Callable[[object], object]:
        def _open(_config: object) -> object:
            return value

        return _open

    return _build


def _install(monkeypatch: pytest.MonkeyPatch, world: _World) -> None:
    _override(
        monkeypatch,
        pending_approval_store_factory=factory(world.pending),
        role_store_factory=factory(world.roles),
        audit_store_factory=factory(world.audit),
        graph_factory=factory(world.graph),
    )
    monkeypatch.setattr(
        dependencies, "build_default_graph_cleanup_graph_opener", _opener(world.graph)
    )
    monkeypatch.setattr(
        dependencies,
        "build_default_graph_cleanup_baseline_opener",
        _opener(world.rig.baseline_graphs()),
    )
    monkeypatch.setattr(dependencies, "PsycopgAuditStore", factory(world.audit))
    monkeypatch.setattr(dependencies, "PsycopgAccessRoleStore", factory(world.roles))
    install_graph_write_gateway(
        world.rig.gateway, system_audit=lambda _config: cast("SystemAuditRecorder", object())
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    configure()
    built = _World(legacy=False)
    _install(monkeypatch, built)
    yield built
    install_graph_write_gateway(None)
    close_all_rigs()


@pytest.fixture
def legacy_world(monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    configure()
    built = _World(legacy=True)
    _install(monkeypatch, built)
    yield built
    install_graph_write_gateway(None)
    close_all_rigs()


def _sources(rig: CleanupRig, rel_type: str, target_id: str) -> list[str]:
    return sorted(
        key[2]
        for key in rig.graph.edges
        if key[0] == rel_type and key[3] == "Capability" and key[4] == target_id
    )


def _governors(rig: CleanupRig) -> list[tuple[str, str]]:
    return sorted((key[2], key[4]) for key in rig.graph.edges if key[0] == "GOVERNED_BY")


def test_signed_split_mints_x_moves_edges_deletes_marker_audit_first(world: _World) -> None:
    a_before = dict(world.rig.graph.nodes[("Capability", SPLIT_A)])
    row, code = world.approve()
    assert world.rig.logged_groups == []

    response = world.sign(row, code)

    assert response.status_code == 200
    assert response.json() == {"status": "signed", "x_id": SPLIT_X, "a_id": SPLIT_A, "split": True}
    # The applied row precedes the one group, which carries the row's id as its audit link.
    assert world.rig.order() == ["audit:applied", "log_append"]
    [group] = world.rig.logged_groups
    [recorded] = world.audit.rows
    [event_id] = world.audit.event_ids
    assert world.rig.store.links[group.group_id] == event_id
    # X is minted from the source baseline's node; the marker is gone; A is untouched.
    minted = dict(world.rig.graph.nodes[("Capability", SPLIT_X)])
    created_at = minted.pop("created_at")
    assert isinstance(created_at, str)
    assert datetime.fromisoformat(created_at).tzinfo is not None
    embedding = minted.pop("embedding")
    assert list(cast("list[float]", embedding)) == [1.0, 0.0]
    minted.pop("id", None)
    assert minted == {"name": "Vulnerability patching", "confidence": 0.9}
    assert ("ResolvedCapability", SPLIT_X) not in world.rig.graph.nodes
    assert dict(world.rig.graph.nodes[("Capability", SPLIT_A)]) == a_before
    # Edges: held on X move or are added; shared stay on both; the rest stay on A.
    assert _sources(world.rig, "REQUIRES", SPLIT_X) == ["obl_added", "obl_moves", "obl_shared"]
    assert _sources(world.rig, "REQUIRES", SPLIT_A) == ["obl_shared", "obl_stays"]
    assert _sources(world.rig, "COVERS", SPLIT_X) == ["pa_moves"]
    assert _sources(world.rig, "COVERS", SPLIT_A) == []
    assert _sources(world.rig, "MITIGATED_BY", SPLIT_X) == []
    assert _sources(world.rig, "MITIGATED_BY", SPLIT_A) == ["rp_stays"]
    # A keeps its Policy; X starts ungoverned.
    assert _governors(world.rig) == [(SPLIT_A, SPLIT_POLICY)]
    # The applied row names the actor, X, A, the moved edges and the approval.
    assert (recorded.action, recorded.resource_type, recorded.resource_id) == (
        _ACTION,
        "capability",
        SPLIT_X,
    )
    assert (recorded.actor_subject, recorded.actor_issuer) == _OFFICER
    assert recorded.outcome == "applied"
    details = recorded.details
    assert (details["x_id"], details["a_id"], details["approval_id"]) == (SPLIT_X, SPLIT_A, row.id)
    assert details["acknowledged"] is True
    assert (details["policy_id"], details["policy_status"]) == (SPLIT_POLICY, "approved")
    assert details["source_baseline"] == "nis2_baseline"
    assert details.get("reason_code") is None
    moved = sorted(
        (e["rel_type"], e["source_id"], e["target_id"])
        for e in cast("list[dict[str, str]]", details["moved_edges"])
    )
    assert moved == [("COVERS", "pa_moves", SPLIT_A), ("REQUIRES", "obl_moves", SPLIT_A)]
    assert details["before"] is not None
    assert details["after"] is not None


def test_the_split_outcome_is_checkable_and_its_audit_row_is_listed_by_action(
    world: _World,
) -> None:
    row, code = world.approve()
    assert world.sign(row, code).status_code == 200
    [recorded] = world.audit.rows
    [event_id] = world.audit.event_ids

    status = json.loads(
        call_tool("check-cleanup-approval", {"pending_approval_id": row.id}, _OFFICER)
    )

    assert status["tool_name"] == _TOOL
    assert status["status"] == "signed"
    assert status["outcome"] == {"x_id": SPLIT_X, "a_id": SPLIT_A, "split": True}
    world.audit.history = [
        AuditEventRow(
            id=event_id,
            occurred_at=datetime.now(UTC),
            actor_subject=recorded.actor_subject,
            actor_issuer=recorded.actor_issuer,
            action=recorded.action,
            resource_type=recorded.resource_type,
            resource_id=recorded.resource_id,
            outcome="applied",
            details=recorded.details,
        )
    ]
    listed = json.loads(call_tool("list-audit-events", {"action": _ACTION}, _OWNER))
    [event] = listed["events"]
    assert (event["id"], event["action"], event["resource_id"], event["outcome"]) == (
        event_id,
        _ACTION,
        SPLIT_X,
        "applied",
    )
    assert event["details"]["approval_id"] == row.id


def test_an_identity_less_edge_on_a_is_stamped_then_moved(legacy_world: _World) -> None:
    row, code = legacy_world.approve()

    response = legacy_world.sign(row, code)

    assert response.status_code == 200
    assert _sources(legacy_world.rig, "REQUIRES", SPLIT_X) == [
        "obl_added",
        "obl_moves",
        "obl_shared",
    ]
    assert _sources(legacy_world.rig, "REQUIRES", SPLIT_A) == ["obl_shared", "obl_stays"]
    assert _sources(legacy_world.rig, "COVERS", SPLIT_A) == []
    assert all(key[5] for key in legacy_world.rig.graph.edges)
    assert legacy_world.rig.order() == ["audit:applied", "log_append", "log_append"]


def test_the_effect_verifier_sees_the_split_only_once_it_is_applied(world: _World) -> None:
    row, code = world.approve()
    assert verify_capability_split_effect(row, world.graph) is False

    assert world.sign(row, code).status_code == 200

    assert verify_capability_split_effect(row, world.graph) is True


def test_importing_the_app_registers_the_split_executor_and_verifier() -> None:
    from ps_service.passkey_signing.executors import (
        resolve_approval_executor,
        resolve_effect_verifier,
    )

    assert resolve_approval_executor(TOOL_SPLIT_CAPABILITY) is not None
    assert resolve_effect_verifier(TOOL_SPLIT_CAPABILITY) is not None


def test_the_marker_presence_read_names_its_label_and_parameter() -> None:
    query = graph_reader._RESOLVED_CAPABILITY_PRESENT_QUERY  # pyright: ignore[reportPrivateUsage]

    assert query == "MATCH (m:ResolvedCapability {id: $capability_id}) RETURN count(m)"


def test_a_failed_split_row_names_the_action_x_and_the_reason(world: _World) -> None:
    row, _code = world.approve()

    action, resource_type, resource_id, details = failed_audit_row(row, "graph_write_failed")
    orphan = interrupted_audit_row(row)

    assert (action, resource_type, resource_id) == (_ACTION, "capability", SPLIT_X)
    assert (details["x_id"], details["a_id"], details["approval_id"]) == (SPLIT_X, SPLIT_A, row.id)
    assert details["reason_code"] == "graph_write_failed"
    assert details["acknowledged"] is True
    assert details["policy_id"] == SPLIT_POLICY
    assert orphan[:3] == (_ACTION, "capability", SPLIT_X)
    assert orphan[3]["reason_code"] == "interrupted_no_effect"
