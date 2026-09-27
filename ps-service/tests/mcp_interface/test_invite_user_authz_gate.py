"""Tests for the `SystemAdmin`-or-above authz gate on the `invite-user` MCP
tool (issue #140): AC-BI-001 (a non-`SystemAdmin` caller is denied, `error:`-
prefixed, without contacting Authentik), AC-BI-002 (the local-test bypass
skips the role check entirely), and a store-outage fail-closed case mirroring
`test_catalog_source_authz_gate.py`'s own coverage of the same
`require_role`/`PsycopgAccessRoleStore` gate shape.

`_verified_actor`/`_fake_store_factory`/`_seeded_store` and the subject/issuer
constants are reused via cross-module import from
`test_catalog_source_authz_gate.py` (that file's own established convention
for its `_DEFAULT_URL`/`_OVERRIDE_URL`/etc.), rather than re-declared here.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from authz._fakes import (
    RaisingAccessRoleStore,  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package, mirrors test_catalog_source_authz_gate.py's own convention
)
from mcp.types import CallToolResult, TextContent

from mcp_interface.test_catalog_source_authz_gate import (
    _ACCESS_DENIED_MESSAGE,  # pyright: ignore[reportPrivateUsage]  -- reuse this issue's own gate-test fixtures verbatim rather than re-declaring them
    _NON_ADMIN_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _fake_store_factory,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _seeded_store,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.invitations.client import InvitationResult
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    import pytest

    from ps_service.config import ServiceConfig

_EMAIL = "target@example.com"


def _call(args: dict[str, object] | None = None) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("invite-user", args or {"email": _EMAIL}))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _never_called_create_invitation(
    config: ServiceConfig, email: str, **_kwargs: object
) -> InvitationResult:
    del config, email
    message = "create_invitation must not be called when the gate rejects the caller"
    raise AssertionError(message)


def _canned_create_invitation(
    config: ServiceConfig, email: str, **_kwargs: object
) -> InvitationResult:
    del config, email
    return InvitationResult(
        itoken="tok-1",
        invite_url="https://authentik.example.com/if/flow/ps-invite-enrollment/?itoken=tok-1",
    )


def test_authenticated_user_only_caller_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-001: a real, non-bypass caller holding only `AuthenticatedUser`
    (never elevated) gets the fixed access-denied message, and Authentik is
    never contacted.
    """
    configure()
    store = _seeded_store()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))
    monkeypatch.setattr(mcp_server, "create_invitation", _never_called_create_invitation)

    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        result = _call()

    assert result.is_error is False
    assert _text(result) == _ACCESS_DENIED_MESSAGE


def test_local_test_bypass_skips_role_check_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-002: under the local-test bypass, no bearer token is bound at
    all and the role check is skipped entirely -- the tool proceeds straight
    to (a fake) Authentik and succeeds.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    monkeypatch.setattr(mcp_server, "create_invitation", _canned_create_invitation)

    result = _call()

    assert result.is_error is False


def test_store_outage_fails_closed_instead_of_defaulting_or_succeeding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors `test_catalog_source_authz_gate.py`'s own store-outage
    coverage: a simulated `AccessRoleStore` outage surfaces the distinct
    `authorization_store_unavailable` result, never a silent default/success,
    and Authentik is never contacted.
    """
    configure()
    monkeypatch.setattr(
        mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(RaisingAccessRoleStore())
    )
    monkeypatch.setattr(mcp_server, "create_invitation", _never_called_create_invitation)

    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        result = _call()

    assert result.is_error is False
    assert _text(result) == "error: The authorization store is temporarily unavailable."
