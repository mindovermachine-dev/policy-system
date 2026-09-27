"""Tests for the `SystemAdmin`-or-above authz gate on the catalog-source MCP
tools (issue #133, Slice 4): AC-BI-008 (a non-`SystemAdmin` caller is denied
on each of `set-catalog-source`/`reset-catalog-source`/`get-catalog-source`)
and AC-BI-009 (a `SystemAdmin` caller's calls succeed exactly as each tool
already documents).

PLAN.md §3.3: under the local-test bypass, the gate is skipped entirely --
that behavior is unchanged and already covered by every one of the ~12
bypass-active tests in `test_catalog_source_skills.py`, deliberately left
untouched by this slice. Every test here instead binds a real, non-bypass
`AccessToken` (`_verified_actor`, below), so `config.is_local_test_bypass_active`
is `False` and the new `authz_service.require_role(...)` check in each
tool's body actually runs.

`_verified_actor`/`_fake_store_factory` are local copies of
`test_near_miss_tools.py:168-201`'s own helpers, already re-established a
second time by `test_access_role_tools.py` (PLAN.md §3.3 names this exact
pattern as the one to reuse, "no new test infrastructure needs
inventing"). Graph fakes (`_FakeSingletonGraph`/`_install_graph`, plus
`_DEFAULT_URL`/`_OVERRIDE_URL`) are imported directly from
`test_catalog_source_skills.py` rather than re-declared, so this file can
never silently drift from that file's own fixture shapes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

import pytest
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package (has `__init__.py`); this cross-package import mirrors `test_access_role_tools.py`'s own convention
    FakeAccessRoleStore,
    RaisingAccessRoleStore,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from mcp_interface.test_catalog_source_skills import (
    _DEFAULT_URL,  # pyright: ignore[reportPrivateUsage]  -- reuse this issue's own "zero changes to that file" fixtures verbatim rather than re-declaring them, mirrors `test_near_miss_tools.py`'s own cross-module private-import convention
    _OVERRIDE_URL,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _FakeSingletonGraph,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _install_graph,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.authz.models import AccessRole
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

_SYSTEM_OWNER_SUBJECT = "existing-system-owner"
_NON_ADMIN_SUBJECT = "authenticated-user-only-caller"
_NEW_SYSTEM_ADMIN_SUBJECT = "newly-granted-system-admin"
_CALLER_ISSUER = "https://issuer.example.com/"

_ACCESS_DENIED_MESSAGE = "error: You do not have the required access role for this action."


@contextlib.contextmanager
def _verified_actor(*, sub: str, iss: str = _CALLER_ISSUER) -> Generator[None]:
    """Bind a real, verified `AccessToken` onto the MCP SDK's own auth contextvar.

    Local copy of `test_near_miss_tools.py`'s own `_verified_actor` (issue
    #131) -- `_resolve_authz_actor`/`_resolve_principal` both read
    `get_access_token()` off this exact contextvar, normally populated by
    `AuthContextMiddleware` from a real HTTP request. Deliberately never
    sets `PS_SERVICE_LOCAL_TEST_BYPASS`, so `config.is_local_test_bypass_active`
    stays `False` throughout this file -- the gate under test must run.
    """
    access_token = AccessToken(
        token="test-token", client_id="test-client", scopes=[], subject=sub, claims={"iss": iss}
    )
    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(token)


def _fake_store_factory(store: object) -> Callable[[object], object]:
    """An `AccessRoleStore`-shaped factory returning the same fake store every call.

    Monkeypatched onto `mcp_server.PsycopgAccessRoleStore` -- each gated
    tool body calls it as `PsycopgAccessRoleStore(config)`, so this must
    accept (and ignore) one positional argument.
    """
    return lambda _config: store


def _seeded_store() -> FakeAccessRoleStore:
    """A store with one already-bootstrapped `SystemOwner` under a *different*
    subject, so a later real caller in these tests never wins the
    once-ever bootstrap race themselves (AC-BI-001) and instead genuinely
    defaults to `AuthenticatedUser` alone (AC-BI-002) -- the precondition
    AC-BI-008's denial actually needs.
    """
    store = FakeAccessRoleStore()
    store.bootstrap_first_owner((_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER))
    return store


def _call(tool: str, args: dict[str, object] | None = None) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool(tool, args or {}))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


_TOOLS_AND_ARGS: list[tuple[str, dict[str, object]]] = [
    ("set-catalog-source", {"url": _OVERRIDE_URL}),
    ("reset-catalog-source", {}),
    ("get-catalog-source", {}),
]


@pytest.mark.parametrize(("tool", "args"), _TOOLS_AND_ARGS)
def test_authenticated_user_only_caller_is_denied(
    tool: str, args: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-008: a real, non-bypass caller holding only `AuthenticatedUser`
    (never elevated) gets the fixed access-denied message on each of the
    three catalog-source tools, and nothing is persisted.
    """
    configure()
    store = _seeded_store()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))
    graph = _FakeSingletonGraph()
    _install_graph(monkeypatch, graph)

    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        result = _call(tool, args)

    assert result.is_error is False
    assert _text(result) == _ACCESS_DENIED_MESSAGE
    assert graph.url is None  # the gate rejected before any FalkorDB mutation


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        (
            "set-catalog-source",
            {"url": _OVERRIDE_URL},
            {"url": _OVERRIDE_URL, "source": "override"},
        ),
        ("reset-catalog-source", {}, {"url": _DEFAULT_URL, "source": "default"}),
        ("get-catalog-source", {}, {"url": _DEFAULT_URL, "source": "default"}),
    ],
)
def test_system_admin_caller_succeeds_exactly_as_documented(
    tool: str, args: dict[str, object], expected: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-009: the same shape of caller, after being granted `SystemAdmin`
    via the existing `grant-access-role` flow (here seeded directly on the
    fake store, equivalent to that flow's own effect), succeeds on each
    tool exactly as it already documents -- no change to any tool's own
    success-path behavior.
    """
    configure()
    store = _seeded_store()
    store.grant(
        actor=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
        target=(_NEW_SYSTEM_ADMIN_SUBJECT, _CALLER_ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))
    graph = _FakeSingletonGraph()
    _install_graph(monkeypatch, graph)

    with _verified_actor(sub=_NEW_SYSTEM_ADMIN_SUBJECT):
        result = _call(tool, args)

    assert result.is_error is False
    assert json.loads(_text(result)) == expected


@pytest.mark.parametrize(("tool", "args"), _TOOLS_AND_ARGS)
def test_store_outage_fails_closed_instead_of_defaulting_or_succeeding(
    tool: str, args: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-011 (Slice 5, exhaustive): a simulated store outage on each of the three
    catalog-source tools surfaces the distinct `authorization_store_unavailable` result,
    never a silent default/success -- these three call sites' own fail-closed catches
    (`except (AccessDeniedError, AuthorizationStoreUnavailableError)`) were introduced in
    Slice 4 but never previously exercised by an MCP round trip against a raising store.
    """
    configure()
    monkeypatch.setattr(
        mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(RaisingAccessRoleStore())
    )
    graph = _FakeSingletonGraph()
    _install_graph(monkeypatch, graph)

    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        result = _call(tool, args)

    assert result.is_error is False
    assert _text(result) == "error: The authorization store is temporarily unavailable."
    assert graph.url is None  # the gate rejected before any FalkorDB mutation


def test_sole_bootstrapped_system_owner_also_satisfies_the_system_admin_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`require_role`'s own `SystemAdmin`-or-above hierarchy fix (PLAN.md §0.8)
    reaches this gate too: the very first-ever caller, auto-bootstrapped to
    `SystemOwner` alone (never separately granted `SystemAdmin`), still
    passes `set-catalog-source`'s gate -- otherwise the sole `SystemOwner`
    could never manage the catalog source at all.
    """
    configure()
    store = FakeAccessRoleStore()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))
    graph = _FakeSingletonGraph()
    _install_graph(monkeypatch, graph)

    with _verified_actor(sub=_SYSTEM_OWNER_SUBJECT):
        result = _call("set-catalog-source", {"url": _OVERRIDE_URL})

    assert result.is_error is False
    assert json.loads(_text(result)) == {"url": _OVERRIDE_URL, "source": "override"}
