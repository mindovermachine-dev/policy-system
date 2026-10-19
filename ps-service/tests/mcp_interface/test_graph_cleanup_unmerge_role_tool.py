"""`unmerge` (preview + approval) through the MCP tool, Role merges.

The graph and the audit history are what a REAL `role.merge` left on the Graph Cleanup gateway
rig. Covers the preview (the Role and Obligations to restore, the count of Obligations the
survivor gained, approval bound to the merge and the state, no graph write), conflicts and
dependent merges explained (no approval) and sanitised errors. Gate behaviour is in
`test_graph_cleanup_authz_gate.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from authz._fakes import FakeAccessRoleStore
from graph_cleanup._fakes import (
    ABSORBED_SOURCE_REF,
    INSTRUMENT_ID,
    OBL_MOVED_1,
    OBL_MOVED_2,
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
    FakeApprovalStore,
    approval_row_count,
)
from graph_cleanup._gateway_fakes import CleanupReadGraph, CleanupRig
from graph_cleanup._unmerge_runs import role_unmerge_world
from graph_gateway._fakes import close_all_rigs
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from ps_service.audit.errors import AuditPersistenceError, AuditPostgresUnavailableError
from ps_service.audit.models import AuditEventRow
from ps_service.authz.models import AccessRole
from ps_service.graph_cleanup.audit_actions import ObligationMergeDetails
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server
from ps_service.passkey_signing.errors import PendingApprovalPersistenceError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

_OWNER = "owner"
_OFFICER = "officer"
_ADMIN = "admin"
_PLAIN = "plain"
_ISSUER = "https://issuer.example.com/"
_ARGS: dict[str, object] = {"merged_id": ROLE_ABSORBED}


@contextlib.contextmanager
def _actor(sub: str) -> Generator[None]:
    token = AccessToken(token="t", client_id="c", scopes=[], subject=sub, claims={"iss": _ISSUER})
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def _factory(value: object) -> Callable[..., object]:
    def _make(*_args: object, **_kwargs: object) -> object:
        return value

    return _make


def _officers() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=(_OWNER, _ISSUER))
    store.bootstrap_first_owner((_OWNER, _ISSUER))
    store.grant(
        actor=(_OWNER, _ISSUER),
        target=(_OFFICER, _ISSUER),
        access_role=AccessRole.COMPLIANCE_OFFICER,
    )
    store.grant(
        actor=(_OWNER, _ISSUER), target=(_ADMIN, _ISSUER), access_role=AccessRole.SYSTEM_ADMIN
    )
    return store


class _Env:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rig = CleanupRig()
        role_unmerge_world(self.rig)
        self.pending = FakeApprovalStore()
        monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _factory(_officers()))
        monkeypatch.setattr(mcp_server, "PsycopgPendingApprovalStore", _factory(self.pending))
        monkeypatch.setattr(
            "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
            _factory(self._open_graph),
        )
        monkeypatch.setattr(
            "ps_service.graph_cleanup.dependencies.PsycopgAuditStore", _factory(self.rig.audit)
        )

    def _open_graph(self, _config: object) -> CleanupReadGraph:
        return self.rig.read

    def graph_state(self) -> tuple[object, object]:
        """The rig graph's nodes and edges, to compare before and after a call."""
        graph = self.rig.graph
        return dict(graph.nodes), dict(graph.edges)

    def call(self, name: str, args: dict[str, object], *, sub: str = _OFFICER) -> str:
        with _actor(sub):
            result = asyncio.run(mcp_server.server.call_tool(name, args))
        assert isinstance(result, CallToolResult)
        block = result.content[0]
        assert isinstance(block, TextContent)
        return block.text


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Env]:
    configure()
    built = _Env(monkeypatch)
    yield built
    close_all_rigs()


def test_a_merged_role_returns_the_preview_and_a_pending_approval_and_writes_nothing(
    env: _Env,
) -> None:
    before = env.graph_state()
    groups = len(env.rig.logged_groups)

    body = json.loads(env.call("unmerge", _ARGS))

    assert set(body) == {"preview", "pending_approval_id", "approval_url", "expires_at"}
    preview = body["preview"]
    assert preview["kind"] == "role"
    assert (preview["merged_id"], preview["survivor_id"]) == (ROLE_ABSORBED, ROLE_SURVIVOR)
    assert preview["merged_name"] == "market surveillance authorities"
    assert preview["survivor_name"] == "Market Surveillance Authority"
    assert preview["instrument_id"] == INSTRUMENT_ID
    assert preview["merge_approval_id"] == "approval-1"
    assert preview["obligations_to_restore"] == [OBL_MOVED_1, OBL_MOVED_2]
    assert preview["defines_source_ref"] == ABSORBED_SOURCE_REF
    assert preview["survivor_obligations_added_since_merge"] == 0
    assert "stay on the survivor" in preview["note"]
    row = env.pending.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert (row.actor_subject, row.actor_issuer) == (_OFFICER, _ISSUER)
    assert row.tool_name == "unmerge"
    assert row.normalized_args == {
        "merged_id": ROLE_ABSORBED,
        "merge_approval_id": "approval-1",
        "kind": "role",
        "state_digest": preview["state_digest"],
    }
    assert env.graph_state() == before
    assert len(env.rig.logged_groups) == groups
    assert approval_row_count(env.pending) == 1


def test_the_preview_counts_the_obligations_the_survivor_gained_since_the_merge(
    env: _Env,
) -> None:
    env.rig.seed_endpoint("Obligation", "obl_since", text="obl_since")
    env.rig.seed_has(ROLE_SURVIVOR, "obl_since")

    preview = json.loads(env.call("unmerge", _ARGS))["preview"]

    assert preview["survivor_obligations_added_since_merge"] == 1
    assert preview["obligations_to_restore"] == [OBL_MOVED_1, OBL_MOVED_2]


def test_a_conflict_with_the_live_graph_is_explained_and_creates_no_approval(env: _Env) -> None:
    del env.rig.graph.nodes[("MergedRole", ROLE_ABSORBED)]

    text = env.call("unmerge", _ARGS)

    assert text.startswith("error: ")
    assert "no MergedRole marker" in text
    assert approval_row_count(env.pending) == 0


def test_an_unknown_id_names_merge_roles_among_the_tools_whose_merges_can_be_unmerged(
    env: _Env,
) -> None:
    text = env.call("unmerge", {"merged_id": "role_never_merged"})

    assert text.startswith("error: no merge of 'role_never_merged' was found")
    assert "merge-roles" in text
    assert approval_row_count(env.pending) == 0


def test_an_unaudited_merge_of_a_moved_obligation_is_refused_by_the_live_checks(
    env: _Env,
) -> None:
    graph = env.rig.graph
    del graph.nodes[("Obligation", OBL_MOVED_1)]
    for key in [k for k in graph.edges if OBL_MOVED_1 in (k[2], k[4])]:
        del graph.edges[key]
    graph.nodes[("MergedObligation", OBL_MOVED_1)] = {"merged_into": "obl_other"}

    text = env.call("unmerge", _ARGS)

    assert text.startswith("error: ")
    assert OBL_MOVED_1 in text
    assert approval_row_count(env.pending) == 0


# -- only an explicit ComplianceOfficer may unmerge a Role merge ----------------------

_ACCESS_DENIED = "error: You do not have the required access role for this action."
_UNAUTHENTICATED = "error: graph cleanup requires a real authenticated caller"


@pytest.mark.parametrize(
    "sub", [_OWNER, _ADMIN, _PLAIN], ids=["system_owner", "system_admin", "no_grant"]
)
def test_a_caller_without_an_explicit_compliance_officer_grant_gets_an_error_and_no_approval(
    env: _Env, sub: str
) -> None:
    before = env.graph_state()

    text = env.call("unmerge", _ARGS, sub=sub)

    assert text == _ACCESS_DENIED
    assert approval_row_count(env.pending) == 0
    assert env.graph_state() == before


@pytest.mark.parametrize("bypass", [False, True], ids=["no_session", "local_test_bypass"])
def test_no_real_session_is_refused_even_under_the_local_test_bypass(
    env: _Env, monkeypatch: pytest.MonkeyPatch, *, bypass: bool
) -> None:
    if bypass:
        monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    before = env.graph_state()

    result = asyncio.run(mcp_server.server.call_tool("unmerge", _ARGS))

    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert block.text == _UNAUTHENTICATED
    assert approval_row_count(env.pending) == 0
    assert env.graph_state() == before


# -- a later merge that depends on the Role merge blocks the unmerge ------------------


def _later_obligation_merge(env: _Env, *, survivor: str, absorbed: str, minutes: int) -> str:
    """Put a later `obligation.merge` row in the history; return its audit event id."""
    event_id = f"evt-obl-merge-{minutes}"
    details = ObligationMergeDetails(
        survivor_id=survivor,
        absorbed_id=absorbed,
        role_id=ROLE_SURVIVOR,
        approval_id=f"approval-obl-{minutes}",
    ).model_dump(mode="json")
    env.rig.audit.history = [
        AuditEventRow(
            id=event_id,
            occurred_at=datetime(2026, 10, 5, 1, minutes, tzinfo=UTC),
            actor_subject=_OFFICER,
            actor_issuer=_ISSUER,
            action="obligation.merge",
            resource_type="obligation",
            resource_id=absorbed,
            outcome="applied",
            details=details,
        ),
        *env.rig.audit.history,
    ]
    return event_id


def test_a_later_merge_of_a_moved_obligation_blocks_the_unmerge_and_names_its_audit_event(
    env: _Env,
) -> None:
    event_id = _later_obligation_merge(env, survivor="obl_other", absorbed=OBL_MOVED_1, minutes=5)

    text = env.call("unmerge", _ARGS)

    assert text.startswith("error: ")
    assert event_id in text
    assert "approval-obl-5" in text
    assert approval_row_count(env.pending) == 0


# -- failures return a fixed message that leaks nothing ---------------------------------

_LEAKS = ("10.0.0.1", "6379", "refused", "relation", "Traceback", "hunter2", "/etc/")
_GRAPH_UNAVAILABLE = "error: the policy graph database is not reachable"
_AUDIT_UNREADABLE = "error: the audit trail could not be read right now; try again shortly"
_UNEXPECTED = "error: an unexpected error occurred"


class _Unreachable:
    """A graph opener that fails the way a dead database does."""

    def __call__(self, _config: object) -> object:
        message = "connection refused to 10.0.0.1:6379 relation x password=hunter2"
        raise ConnectionError(message)


class _RedisDown:
    """A read handle whose every query hits a dead FalkorDB."""

    def query(self, *_args: object, **_kwargs: object) -> object:
        message = "Connection refused 10.0.0.1:6379 relation x"
        raise redis.exceptions.ConnectionError(message)


class _Garbage:
    """A read handle that answers with rows of the wrong shape (an unexpected failure)."""

    def query(self, *_args: object, **_kwargs: object) -> object:
        return type("Result", (), {"result_set": [["only one column"]]})()


class _DownApprovalStore(FakeApprovalStore):
    """An approval store that cannot reach Postgres."""

    def create_pending_approval(self, **_kwargs: object) -> object:  # type: ignore[override]
        message = "connection to 10.0.0.1:6379 refused: relation pending_approvals /etc/secret"
        raise PendingApprovalPersistenceError(message)


def _open_dead_redis(_config: object) -> object:
    return _RedisDown()


def _open_garbage(_config: object) -> object:
    return _Garbage()


@pytest.mark.parametrize(
    ("opener", "expected"),
    [
        (_Unreachable(), _GRAPH_UNAVAILABLE),
        (_open_dead_redis, _GRAPH_UNAVAILABLE),
        (_open_garbage, _UNEXPECTED),
    ],
    ids=["graph_unreachable", "graph_dies_on_read", "unexpected_failure"],
)
def test_a_graph_or_unexpected_failure_returns_a_fixed_message_and_creates_no_approval(
    env: _Env, monkeypatch: pytest.MonkeyPatch, opener: Callable[..., object], expected: str
) -> None:
    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
        _factory(opener),
    )
    before = env.graph_state()

    text = env.call("unmerge", _ARGS)

    assert text == expected
    assert not any(leak in text for leak in _LEAKS)
    assert approval_row_count(env.pending) == 0
    assert env.graph_state() == before


@pytest.mark.parametrize(
    "error",
    [
        AuditPostgresUnavailableError("db host 10.0.0.1 down"),
        AuditPersistenceError("relation audit_events"),
    ],
    ids=["audit_store_unreachable", "audit_read_failed"],
)
def test_an_unreadable_audit_trail_returns_a_fixed_message_and_creates_no_approval(
    env: _Env, error: Exception
) -> None:
    env.rig.audit.query_error = error
    before = env.graph_state()

    text = env.call("unmerge", _ARGS)

    assert text == _AUDIT_UNREADABLE
    assert not any(leak in text for leak in _LEAKS)
    assert approval_row_count(env.pending) == 0
    assert env.graph_state() == before


def test_an_unreachable_approval_store_returns_a_fixed_message_and_leaves_no_trace(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    down = _DownApprovalStore()
    monkeypatch.setattr(mcp_server, "PsycopgPendingApprovalStore", _factory(down))
    before = env.graph_state()

    text = env.call("unmerge", _ARGS)

    assert text == _UNEXPECTED
    assert approval_row_count(down) == 0
    assert env.graph_state() == before
