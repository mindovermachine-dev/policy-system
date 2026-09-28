"""Tests for the `ComplianceOfficer` authz gate on the `restore_instrument` MCP
tool (issue #145, Slice 8): AC-BI-005 (a caller who is only `AuthenticatedUser`,
or who holds `SystemAdmin`/`SystemOwner` without an explicit `ComplianceOfficer`
grant, is denied -- no implicit hierarchy override) and AC-BI-007 (a
`ComplianceOfficer`-holding caller's call succeeds exactly as the tool already
documents).

Mirrors `test_catalog_source_authz_gate.py`/`test_ingest_regulation_authz_gate.py`
exactly: `_verified_actor`/`_fake_store_factory`/`FakeAccessRoleStore`/
`RaisingAccessRoleStore` are local copies of those files' own helpers.

Every test here binds a real, non-bypass `AccessToken` (`_verified_actor`), so
`config.is_local_test_bypass_active` is `False` and the new
`authz_service.require_role(...)` check in `restore_instrument`'s body
actually runs -- distinct from every test in `test_restore_instrument_tool.py`,
which drives the tool under the local-test bypass (or, for its own real-token
test, now seeds a `ComplianceOfficer` grant via `_grant_compliance_officer`)
and needs no change here.

`_INSTRUMENT_ID`/`_fake_dependencies`/`_valid_transport`/`_FakeCatalogRestoreStage`/
`_set_similarity_threshold` are imported directly from
`test_restore_instrument_tool.py` rather than re-declared, so this file can
never silently drift from that file's own restore-artifact fixtures.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package (has `__init__.py`); this cross-package import mirrors `test_access_role_tools.py`'s own convention
    FakeAccessRoleStore,
    RaisingAccessRoleStore,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from mcp_interface.test_restore_instrument_tool import (
    _INSTRUMENT_ID,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _fake_dependencies,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _FakeCatalogRestoreStage,  # pyright: ignore[reportPrivateUsage]  -- reuse this issue's own "zero changes to that file" fixtures verbatim rather than re-declaring them, mirrors `test_ingest_regulation_authz_gate.py`'s own cross-module private-import convention
    _set_similarity_threshold,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _valid_transport,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.authz.models import AccessRole
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Generator

    import pytest

_SYSTEM_OWNER_SUBJECT = "existing-system-owner"
_NON_ADMIN_SUBJECT = "authenticated-user-only-caller"
_SYSTEM_ADMIN_SUBJECT = "system-admin-without-grant"
_COMPLIANCE_OFFICER_SUBJECT = "granted-compliance-officer"
_CALLER_ISSUER = "https://issuer.example.com/"

_ACCESS_DENIED_MESSAGE = "error: You do not have the required access role for this action."


@contextlib.contextmanager
def _verified_actor(*, sub: str, iss: str = _CALLER_ISSUER) -> Generator[None]:
    """Bind a real, verified `AccessToken` onto the MCP SDK's own auth contextvar.

    Local copy of `test_catalog_source_authz_gate.py`'s own `_verified_actor`
    -- deliberately never sets `PS_SERVICE_LOCAL_TEST_BYPASS`, so
    `config.is_local_test_bypass_active` stays `False` throughout this file --
    the gate under test must run.
    """
    access_token = AccessToken(
        token="test-token", client_id="test-client", scopes=[], subject=sub, claims={"iss": iss}
    )
    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(token)


def _fake_store_factory(store: object) -> object:
    """An `AccessRoleStore`-shaped factory returning the same fake store every call.

    Monkeypatched onto `mcp_server.PsycopgAccessRoleStore` -- the gated tool
    body calls it as `PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))`,
    so this must accept (and ignore) both the positional `config` and the
    `audit_store` keyword.
    """

    def _factory(_config: object, **_kwargs: object) -> object:
        return store

    return _factory


def _seeded_store() -> FakeAccessRoleStore:
    """A store with one already-bootstrapped `SystemOwner` under a *different*
    subject, so a later real caller in these tests never wins the
    once-ever bootstrap race themselves and instead genuinely defaults to
    `AuthenticatedUser` alone -- the precondition the denial tests actually
    need.
    """
    store = FakeAccessRoleStore(expected_owner=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER))
    store.bootstrap_first_owner((_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER))
    return store


def _call_restore_instrument() -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool("restore_instrument", {"instrument_id": _INSTRUMENT_ID})
    )
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _install_fake_restore_dependencies(monkeypatch: pytest.MonkeyPatch) -> _FakeCatalogRestoreStage:
    _set_similarity_threshold(monkeypatch)
    stage = _FakeCatalogRestoreStage()
    fake_dependencies = _fake_dependencies(_valid_transport(), stage)
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: fake_dependencies
    )
    return stage


def test_authenticated_user_only_caller_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-005: a real, non-bypass caller holding only `AuthenticatedUser`
    (never elevated) gets the fixed access-denied message, and no restore
    stage ever runs.
    """
    configure()
    store = _seeded_store()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))
    stage = _install_fake_restore_dependencies(monkeypatch)

    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        result = _call_restore_instrument()

    assert result.is_error is False
    assert _text(result) == _ACCESS_DENIED_MESSAGE
    assert stage.calls == []


def test_system_admin_without_explicit_grant_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-005: `require_role`'s own non-hierarchical contract for
    `ComplianceOfficer` -- a caller holding `SystemAdmin` (but never
    separately granted `ComplianceOfficer`) is still denied, proving no
    implicit admin override reaches this gate.
    """
    configure()
    store = _seeded_store()
    store.grant(
        actor=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
        target=(_SYSTEM_ADMIN_SUBJECT, _CALLER_ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))
    stage = _install_fake_restore_dependencies(monkeypatch)

    with _verified_actor(sub=_SYSTEM_ADMIN_SUBJECT):
        result = _call_restore_instrument()

    assert result.is_error is False
    assert _text(result) == _ACCESS_DENIED_MESSAGE
    assert stage.calls == []


def test_system_owner_without_explicit_grant_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-005: the same non-hierarchical proof, for the sole bootstrapped
    `SystemOwner` themselves -- unlike a `SystemAdmin`-minimum gate (where
    `SystemOwner` is deliberately also satisfying), a `ComplianceOfficer`
    minimum is exact-match only, so even `SystemOwner` needs its own
    explicit grant.
    """
    configure()
    store = _seeded_store()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))
    stage = _install_fake_restore_dependencies(monkeypatch)

    with _verified_actor(sub=_SYSTEM_OWNER_SUBJECT):
        result = _call_restore_instrument()

    assert result.is_error is False
    assert _text(result) == _ACCESS_DENIED_MESSAGE
    assert stage.calls == []


def test_caller_holding_compliance_officer_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-007: a caller granted `ComplianceOfficer` succeeds exactly as
    `test_restore_instrument_tool.py`'s own happy path already documents --
    no change to the tool's own success-path response shape.
    """
    configure()
    store = _seeded_store()
    store.grant(
        actor=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
        target=(_COMPLIANCE_OFFICER_SUBJECT, _CALLER_ISSUER),
        access_role=AccessRole.COMPLIANCE_OFFICER,
    )
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))
    _install_fake_restore_dependencies(monkeypatch)

    with _verified_actor(sub=_COMPLIANCE_OFFICER_SUBJECT):
        result = _call_restore_instrument()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["instrument_id"] == _INSTRUMENT_ID
    assert [s["stage"] for s in body["stages"]] == [
        "verified",
        "staged",
        "merged_and_finalized",
    ]


def test_store_outage_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-011: a simulated store outage surfaces the distinct
    `authorization_store_unavailable` result, never a silent default/success,
    and no restore stage ever runs.
    """
    configure()
    monkeypatch.setattr(
        mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(RaisingAccessRoleStore())
    )
    stage = _install_fake_restore_dependencies(monkeypatch)

    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        result = _call_restore_instrument()

    assert result.is_error is False
    assert _text(result) == "error: The authorization store is temporarily unavailable."
    assert stage.calls == []
