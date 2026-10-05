"""Authentication + `ComplianceOfficer` gate on every graph-cleanup MCP tool (issue #190).

AC-BI-001: no real authenticated session (the local-test bypass included) ->
rejected, the graph opener and every store are never touched.
AC-BI-002: a caller without an explicit `ComplianceOfficer` grant (including
SystemAdmin / SystemOwner -- no hierarchy override) -> rejected, graph unchanged.

`_TOOLS` is extended by each later slice that registers a cleanup tool.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import pytest
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- importable test package, same cross-package convention as `test_restore_instrument_authz_gate.py`
    FakeAccessRoleStore,
    RaisingAccessRoleStore,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from ps_service.authz.models import AccessRole
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

_TOOLS: list[tuple[str, dict[str, object]]] = [
    ("find-capability-merge-candidates", {}),
    ("find-duplicate-obligations", {}),
    ("merge-capabilities", {"survivor_id": "cap_a", "absorbed_id": "cap_b"}),
    (
        "merge-capabilities",
        {"survivor_id": "cap_a", "absorbed_id": "cap_b", "acknowledge_governance_change": True},
    ),
    ("merge-obligations", {"survivor_id": "obl_a", "absorbed_id": "obl_b"}),
    ("release-capability-governance", {"capability_id": "cap_a"}),
    ("unmerge", {"merged_id": "cap_b"}),
    ("check-cleanup-approval", {"pending_approval_id": "approval-1"}),
]

_OWNER = "existing-system-owner"
_ISSUER = "https://issuer.example.com/"
_ACCESS_DENIED = "error: You do not have the required access role for this action."
_UNAUTHENTICATED = "error: graph cleanup requires a real authenticated caller"


@contextlib.contextmanager
def _verified_actor(sub: str) -> Generator[None]:
    token = AccessToken(token="t", client_id="c", scopes=[], subject=sub, claims={"iss": _ISSUER})
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


class _ForbiddenGraphOpener:
    """Fails the test if the gate ever lets a call reach the graph."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _config: object) -> object:
        self.calls += 1
        message = "graph opened behind a closed gate"
        raise AssertionError(message)


def _install(monkeypatch: pytest.MonkeyPatch, store: object) -> _ForbiddenGraphOpener:
    opener = _ForbiddenGraphOpener()
    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
        _opener_factory(opener),
    )
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _store_factory(store))
    return opener


def _seeded_store() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=(_OWNER, _ISSUER))
    store.bootstrap_first_owner((_OWNER, _ISSUER))
    return store


def _store_factory(store: object) -> Callable[..., object]:
    def _factory(*_args: object, **_kwargs: object) -> object:
        return store

    return _factory


def _opener_factory(opener: Callable[[object], object]) -> Callable[[], Callable[[object], object]]:
    def _factory() -> Callable[[object], object]:
        return opener

    return _factory


def _call(name: str, args: dict[str, object]) -> str:
    result = asyncio.run(mcp_server.server.call_tool(name, args))
    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


@pytest.mark.parametrize(("name", "args"), _TOOLS)
@pytest.mark.parametrize("bypass", [False, True])
def test_no_real_session_is_rejected_even_under_the_local_test_bypass(
    monkeypatch: pytest.MonkeyPatch, name: str, args: dict[str, object], *, bypass: bool
) -> None:
    if bypass:
        monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    opener = _install(monkeypatch, RaisingAccessRoleStore())

    assert _call(name, args) == _UNAUTHENTICATED
    assert opener.calls == 0


@pytest.mark.parametrize(("name", "args"), _TOOLS)
@pytest.mark.parametrize("role", [None, AccessRole.SYSTEM_ADMIN, AccessRole.SYSTEM_OWNER])
def test_caller_without_an_explicit_compliance_officer_grant_is_denied(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: dict[str, object],
    role: AccessRole | None,
) -> None:
    configure()
    store = _seeded_store()
    sub = "caller"
    if role is AccessRole.SYSTEM_OWNER:
        sub = _OWNER
    elif role is AccessRole.SYSTEM_ADMIN:
        store.grant(
            actor=(_OWNER, _ISSUER), target=(sub, _ISSUER), access_role=AccessRole.SYSTEM_ADMIN
        )
    opener = _install(monkeypatch, store)

    with _verified_actor(sub):
        assert _call(name, args) == _ACCESS_DENIED
    assert opener.calls == 0


@pytest.mark.parametrize(("name", "args"), _TOOLS)
def test_store_outage_fails_closed(
    monkeypatch: pytest.MonkeyPatch, name: str, args: dict[str, object]
) -> None:
    configure()
    opener = _install(monkeypatch, RaisingAccessRoleStore())

    with _verified_actor("caller"):
        text = _call(name, args)

    assert text == "error: The authorization store is temporarily unavailable."
    assert opener.calls == 0
