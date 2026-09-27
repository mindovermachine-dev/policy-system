"""Tests for the registered access-role MCP tools (issue #133, Slices 1-3).

PLAN.md §4: Slice 1 shipped bootstrap + `list-access-roles`, end to end.
Slice 2 added `grant-access-role`/`revoke-access-role` -- `SystemAdmin`/
`PolicyManager` grant+revoke, plus grant of `SystemOwner` (CHANGES.md's
MAJOR resolution, Appendix A). This slice (Slice 3) widens
`revoke-access-role`'s own `Literal[...]` schema to include `"SystemOwner"`
and wires up `rules.enforce_system_owner_floor` (AC-BI-006) -- the real,
hermetic 5-step MCP scenario from CHANGES.md Appendix A
(`test_appendix_a_five_step_multi_owner_scenario_proves_ac_bi_006_and_007`
below) is the PRIMARY proof of AC-BI-006/AC-BI-007's reachability, not a
fake-store-seeded shortcut.

Every grant/revoke test sets `PS_AUTH_ISSUER` (PLAN.md §0.12: the target's
issuer is filled in from `config.auth_issuer` at the service layer, not
passed by the caller) to the same value `_verified_actor`'s own `iss` claim
uses -- this codebase has no multi-issuer concept, so the two are always
the same value in any real deployment too.

`_verified_actor()`/`_fake_store_factory()` are local copies of
`tests/mcp_interface/test_near_miss_tools.py:168-201`'s own helpers (issue
#131) -- this is the pattern's second occurrence, not yet DRY's
third-occurrence extraction threshold (L2 DRY), and `test_near_miss_tools.py`
already documents that these bind a real `AccessToken` directly onto the MCP
SDK's own `auth_context_var` contextvar, the same one `get_access_token()`
(`_resolve_principal`/`_resolve_signing_actor`/`_resolve_authz_actor`) reads.

`FakeAccessRoleStore`/`RaisingAccessRoleStore` (this issue's own doubles,
`tests/authz/_fakes.py`) are monkeypatched onto
`mcp_server.PsycopgAccessRoleStore`, mirroring `test_near_miss_tools.py`'s
own `_fake_store_factory`-onto-`PsycopgPendingApprovalStore` pattern exactly.

Real Postgres is not reachable in this sandbox (confirmed via
`nc -z localhost 5432`) -- every test here is hermetic, against
`FakeAccessRoleStore`/`RaisingAccessRoleStore`, per PLAN.md's own
"hermetic tests with a fake/in-memory store... are sufficient for this
slice" instruction.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

import pytest
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package (has `__init__.py`); this cross-package import mirrors `test_near_miss_tools.py`'s own `from api.test_routes_near_misses import ...` convention
    FakeAccessRoleStore,
    RaisingAccessRoleStore,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from ps_service.authz.models import AccessRole
from ps_service.logging import configure, reset_for_tests, resolve_default_log_path
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

_FIRST_CALLER_SUBJECT = "first-caller"
_SECOND_CALLER_SUBJECT = "second-caller"
_THIRD_CALLER_SUBJECT = "third-caller"
_ACTOR_ISSUER = "https://issuer.example.com/"


@contextlib.contextmanager
def _verified_actor(*, sub: str, iss: str = _ACTOR_ISSUER) -> Generator[None]:
    """Bind a real, verified `AccessToken` onto the MCP SDK's own auth contextvar.

    Local copy of `test_near_miss_tools.py`'s own `_verified_actor` (issue
    #131) -- `_resolve_authz_actor`/`_resolve_principal` both read
    `get_access_token()` off this exact contextvar, normally populated by
    `AuthContextMiddleware` from a real HTTP request.
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

    Monkeypatched onto `mcp_server.PsycopgAccessRoleStore` -- the tool body
    calls it as `PsycopgAccessRoleStore(config)`, so this must accept (and
    ignore) one positional argument.
    """
    return lambda _config: store


def _call_list_access_roles() -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("list-access-roles", {}))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_first_ever_caller_is_bootstrapped_and_can_list_both_of_their_own_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-001: against a freshly-migrated, empty store, the first-ever
    verified caller is auto-bootstrapped to `AuthenticatedUser` +
    `SystemOwner`, and `list-access-roles` (called by that same caller)
    succeeds, reporting both roles for that one subject.
    """
    configure()
    store = FakeAccessRoleStore()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        result = _call_list_access_roles()

    assert result.is_error is False
    body = json.loads(_text(result))
    subjects_and_roles = {
        (entry["principal_subject"], entry["access_role"]) for entry in body["assignments"]
    }
    assert subjects_and_roles == {
        (_FIRST_CALLER_SUBJECT, "AuthenticatedUser"),
        (_FIRST_CALLER_SUBJECT, "SystemOwner"),
    }
    assert all(entry["principal_issuer"] == _ACTOR_ISSUER for entry in body["assignments"])
    # Exactly one active SystemOwner (the bootstrapped caller) -- AC-BI-007's
    # floor-warning condition is already true from the very first call.
    assert body["system_owner_floor_warning"] is True


def test_second_caller_defaults_to_authenticated_user_and_is_denied_the_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-002 + §0.9's list-gate: once the store is non-empty (a prior
    caller already bootstrapped), a second, distinct principal defaults to
    `AuthenticatedUser` alone and is rejected by `list-access-roles`'s own
    `SystemAdmin`-or-above gate -- one round trip proves both facts.
    """
    configure()
    store = FakeAccessRoleStore()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        first_result = _call_list_access_roles()
    assert first_result.is_error is False

    with _verified_actor(sub=_SECOND_CALLER_SUBJECT):
        second_result = _call_list_access_roles()

    assert second_result.is_error is False
    text = _text(second_result)
    assert text == "error: You do not have the required access role for this action."
    # The second caller's own default role never got written as a persisted
    # row -- only the bootstrap winner's two rows exist.
    assert {row.principal_subject for row in store.list_all_assignments()} == {
        _FIRST_CALLER_SUBJECT
    }


def test_authz_store_outage_fails_closed_instead_of_defaulting_or_bootstrapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-011 (bootstrap-path half): a simulated
    `AccessRolePostgresConnectionError` from the store makes
    `list-access-roles` return the distinct `authorization_store_unavailable`
    error, never a silent default/bootstrap/success.
    """
    configure()
    monkeypatch.setattr(
        mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(RaisingAccessRoleStore())
    )

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        result = _call_list_access_roles()

    assert result.is_error is False
    assert _text(result) == "error: The authorization store is temporarily unavailable."


def test_list_access_roles_without_a_real_authenticated_caller_is_refused() -> None:
    """PLAN.md §3.3: under the local-test bypass (no `AccessToken` ever
    bound), access-role management is refused outright -- never a synthetic
    bypass identity reaching the store.
    """
    configure()
    result = _call_list_access_roles()

    assert result.is_error is False
    assert _text(result) == "error: access-role management requires a real authenticated caller"


# --- grant-access-role / revoke-access-role (Slice 2, PLAN.md §4) -----------


def _call_grant_access_role(principal_subject: str, access_role: str) -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool(
            "grant-access-role",
            {"principal_subject": principal_subject, "access_role": access_role},
        )
    )
    assert isinstance(result, CallToolResult)
    return result


def _call_revoke_access_role(principal_subject: str, access_role: str) -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool(
            "revoke-access-role",
            {"principal_subject": principal_subject, "access_role": access_role},
        )
    )
    assert isinstance(result, CallToolResult)
    return result


def test_bootstrapped_owner_grants_system_admin_to_a_second_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-003/AC-BI-015: a real MCP round trip -- grant, roster row, one audit event."""
    configure()
    monkeypatch.setenv("PS_AUTH_ISSUER", _ACTOR_ISSUER)
    store = FakeAccessRoleStore()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        bootstrap_result = _call_list_access_roles()
    assert bootstrap_result.is_error is False

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        grant_result = _call_grant_access_role(_SECOND_CALLER_SUBJECT, "SystemAdmin")

    assert grant_result.is_error is False
    body = json.loads(_text(grant_result))
    assert body == {
        "principal_subject": _SECOND_CALLER_SUBJECT,
        "access_role": "SystemAdmin",
        "granted_by_subject": _FIRST_CALLER_SUBJECT,
        "system_owner_floor_warning": True,
    }
    target = (_SECOND_CALLER_SUBJECT, _ACTOR_ISSUER)
    assert AccessRole.SYSTEM_ADMIN in store.active_roles_for(target)
    grant_events = [event for event in store._events if event.event_type == "grant"]  # pyright: ignore[reportPrivateUsage]  -- test-only fake, direct-field audit-trail assertion mirrors `test_service.py`'s own convention
    assert len(grant_events) == 1
    assert grant_events[0].actor_subject == _FIRST_CALLER_SUBJECT
    assert grant_events[0].target_subject == _SECOND_CALLER_SUBJECT
    assert grant_events[0].access_role is AccessRole.SYSTEM_ADMIN


def test_new_system_admin_grants_policy_manager_to_a_third_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-004: the newly-granted SystemAdmin (not just SystemOwner) may grant PolicyManager."""
    configure()
    monkeypatch.setenv("PS_AUTH_ISSUER", _ACTOR_ISSUER)
    store = FakeAccessRoleStore()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        _call_list_access_roles()  # bootstraps FIRST_CALLER as SystemOwner
        _call_grant_access_role(_SECOND_CALLER_SUBJECT, "SystemAdmin")

    with _verified_actor(sub=_SECOND_CALLER_SUBJECT):
        result = _call_grant_access_role(_THIRD_CALLER_SUBJECT, "PolicyManager")

    assert result.is_error is False
    assert AccessRole.POLICY_MANAGER in store.active_roles_for(
        (_THIRD_CALLER_SUBJECT, _ACTOR_ISSUER)
    )


def test_system_owner_grants_a_peer_system_owner_and_both_show_as_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHANGES.md MAJOR resolution, Appendix A step 2: the new SystemOwner-peer-grant flow.

    A non-owner `SystemAdmin` cannot grant `SystemOwner` -- RBAC-rejected --
    proving the widened `Literal` is not also a widened RBAC surface for
    anyone but an existing `SystemOwner`.
    """
    configure()
    monkeypatch.setenv("PS_AUTH_ISSUER", _ACTOR_ISSUER)
    store = FakeAccessRoleStore()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        _call_list_access_roles()  # bootstraps FIRST_CALLER as the sole SystemOwner
        grant_result = _call_grant_access_role(_SECOND_CALLER_SUBJECT, "SystemOwner")

    assert grant_result.is_error is False
    body = json.loads(_text(grant_result))
    assert body["system_owner_floor_warning"] is False  # two owners now
    assert store.count_active_system_owners() == 2
    for subject in (_FIRST_CALLER_SUBJECT, _SECOND_CALLER_SUBJECT):
        assert AccessRole.SYSTEM_OWNER in store.active_roles_for((subject, _ACTOR_ISSUER))

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        _call_grant_access_role(_THIRD_CALLER_SUBJECT, "SystemAdmin")  # THIRD_CALLER: SystemAdmin

    with _verified_actor(sub=_THIRD_CALLER_SUBJECT):
        rejected_result = _call_grant_access_role("a-fourth-caller", "SystemOwner")

    assert rejected_result.is_error is False
    assert _text(rejected_result) == (
        "error: You do not have the required access role for this action."
    )
    fourth_target = ("a-fourth-caller", _ACTOR_ISSUER)
    assert AccessRole.SYSTEM_OWNER not in store.active_roles_for(fourth_target)


def test_system_owner_cannot_grant_system_admin_to_themselves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005: the bootstrapped SystemOwner may not grant SystemAdmin to their own subject."""
    configure()
    monkeypatch.setenv("PS_AUTH_ISSUER", _ACTOR_ISSUER)
    store = FakeAccessRoleStore()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        _call_list_access_roles()  # bootstraps
        result = _call_grant_access_role(_FIRST_CALLER_SUBJECT, "SystemAdmin")

    assert result.is_error is False
    assert _text(result) == "error: You cannot grant or revoke your own access roles."
    assert AccessRole.SYSTEM_ADMIN not in store.active_roles_for(
        (_FIRST_CALLER_SUBJECT, _ACTOR_ISSUER)
    )


def test_grant_access_role_naming_a_role_outside_the_closed_set_is_rejected_by_the_mcp_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-013 (outer layer): the MCP tool's own `Literal[...]` schema rejects `"SuperAdmin"`
    before the tool body -- and therefore `grant_role`/the store -- ever runs.
    """
    configure()
    monkeypatch.setenv("PS_AUTH_ISSUER", _ACTOR_ISSUER)
    store = FakeAccessRoleStore()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    with (
        _verified_actor(sub=_FIRST_CALLER_SUBJECT),
        pytest.raises(ToolError, match="literal_error"),
    ):
        _call_grant_access_role(_SECOND_CALLER_SUBJECT, "SuperAdmin")

    # the tool body never ran -- nothing was ever bootstrapped either
    assert store.list_all_assignments() == ()


def test_grant_access_role_without_a_real_authenticated_caller_is_refused() -> None:
    """PLAN.md §3.3: under the local-test bypass, granting is refused outright, same as listing."""
    configure()
    result = _call_grant_access_role(_SECOND_CALLER_SUBJECT, "SystemAdmin")

    assert result.is_error is False
    assert _text(result) == "error: access-role management requires a real authenticated caller"


def test_grant_access_role_store_outage_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-011 (Slice 5, exhaustive): a simulated store outage on `grant-access-role`
    surfaces the distinct `authorization_store_unavailable` result, never a silent
    default/success -- this call site's own fail-closed catch was never previously
    exercised by an MCP round trip (only `list-access-roles`' was, Slice 1).
    """
    configure()
    monkeypatch.setenv("PS_AUTH_ISSUER", _ACTOR_ISSUER)
    monkeypatch.setattr(
        mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(RaisingAccessRoleStore())
    )

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        result = _call_grant_access_role(_SECOND_CALLER_SUBJECT, "SystemAdmin")

    assert result.is_error is False
    assert _text(result) == "error: The authorization store is temporarily unavailable."


def test_revoke_of_system_admin_and_policy_manager_round_trips_with_its_own_audit_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-015: revoke of SystemAdmin (non-owner) round-trips, its own 'revoke' audit event."""
    configure()
    monkeypatch.setenv("PS_AUTH_ISSUER", _ACTOR_ISSUER)
    store = FakeAccessRoleStore()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        _call_list_access_roles()  # bootstraps
        _call_grant_access_role(_SECOND_CALLER_SUBJECT, "SystemAdmin")
        revoke_result = _call_revoke_access_role(_SECOND_CALLER_SUBJECT, "SystemAdmin")

    assert revoke_result.is_error is False
    body = json.loads(_text(revoke_result))
    assert body == {
        "principal_subject": _SECOND_CALLER_SUBJECT,
        "access_role": "SystemAdmin",
        "revoked_by_subject": _FIRST_CALLER_SUBJECT,
        "system_owner_floor_warning": True,
    }
    assert AccessRole.SYSTEM_ADMIN not in store.active_roles_for(
        (_SECOND_CALLER_SUBJECT, _ACTOR_ISSUER)
    )
    revoke_events = [event for event in store._events if event.event_type == "revoke"]  # pyright: ignore[reportPrivateUsage]  -- test-only fake, direct-field audit-trail assertion mirrors `test_service.py`'s own convention
    assert len(revoke_events) == 1
    assert revoke_events[0].actor_subject == _FIRST_CALLER_SUBJECT
    assert revoke_events[0].target_subject == _SECOND_CALLER_SUBJECT
    assert revoke_events[0].access_role is AccessRole.SYSTEM_ADMIN


def test_self_revoke_of_system_admin_is_blocked_the_same_way_as_self_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005 extended to revoke: the RBAC-eligible SystemOwner may not target themselves.

    Revoke RBAC for `SystemAdmin` requires the actor hold `SystemOwner`
    (mirroring grant's own requirement) -- so the bootstrapped SystemOwner
    themselves is the actor that actually reaches `block_self_target`,
    exactly as the analogous self-grant scenario does.
    """
    configure()
    monkeypatch.setenv("PS_AUTH_ISSUER", _ACTOR_ISSUER)
    store = FakeAccessRoleStore()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        _call_list_access_roles()  # bootstraps FIRST_CALLER as SystemOwner
        result = _call_revoke_access_role(_FIRST_CALLER_SUBJECT, "SystemAdmin")

    assert result.is_error is False
    assert _text(result) == "error: You cannot grant or revoke your own access roles."


def test_revoke_access_role_naming_a_role_outside_the_closed_set_is_rejected_by_the_mcp_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-013 (outer layer), revoke side: `"SuperOwner"` is rejected by the schema itself.

    `revoke-access-role`'s own `Literal` is `["SystemOwner", "SystemAdmin",
    "PolicyManager"]` (Slice 3 widens it to include `"SystemOwner"` --
    `test_revoke_of_system_owner_...` tests below exercise that role for
    real) -- a name outside even that widened set is still rejected before
    the tool body ever runs.
    """
    configure()
    monkeypatch.setenv("PS_AUTH_ISSUER", _ACTOR_ISSUER)

    with (
        _verified_actor(sub=_FIRST_CALLER_SUBJECT),
        pytest.raises(ToolError, match="literal_error"),
    ):
        _call_revoke_access_role(_SECOND_CALLER_SUBJECT, "SuperOwner")


def test_revoke_access_role_without_a_real_authenticated_caller_is_refused() -> None:
    """PLAN.md §3.3: under the local-test bypass, revoking is refused outright, same as granting."""
    configure()
    result = _call_revoke_access_role(_SECOND_CALLER_SUBJECT, "SystemAdmin")

    assert result.is_error is False
    assert _text(result) == "error: access-role management requires a real authenticated caller"


def test_revoke_access_role_store_outage_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-011 (Slice 5, exhaustive): a simulated store outage on `revoke-access-role`
    surfaces the distinct `authorization_store_unavailable` result, never a silent
    default/success -- this call site's own fail-closed catch was never previously
    exercised by an MCP round trip (only `list-access-roles`' was, Slice 1).
    """
    configure()
    monkeypatch.setenv("PS_AUTH_ISSUER", _ACTOR_ISSUER)
    monkeypatch.setattr(
        mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(RaisingAccessRoleStore())
    )

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        result = _call_revoke_access_role(_SECOND_CALLER_SUBJECT, "SystemAdmin")

    assert result.is_error is False
    assert _text(result) == "error: The authorization store is temporarily unavailable."


# --- SystemOwner revoke, floor protection, soft warning (Slice 3, PLAN.md §4) ------


def test_appendix_a_five_step_multi_owner_scenario_proves_ac_bi_006_and_007(
    monkeypatch: pytest.MonkeyPatch, read_lines: Callable[[Path], list[dict[str, object]]]
) -> None:
    """CHANGES.md Appendix A's real, MCP-reachable 5-step multi-owner scenario.

    The PRIMARY proof of AC-BI-006 (the floor rejection fires) and AC-BI-007
    (the warning is a genuine true -> false -> true transition, not an
    unconditional fact) -- every step is a real MCP tool call against the
    hermetic fake store, not a fake-store-seeded shortcut:

    1. A (first-ever caller) bootstraps SystemOwner. Count=1 -> warning true.
    2. A grants SystemOwner to B (Slice 2's own peer-grant flow). Count=2 ->
       warning false.
    3. A grants SystemAdmin to C.
    4. C (SystemAdmin, not SystemOwner) revokes SystemOwner from A -- the
       widened revoke RBAC (CHANGES.md) lets this succeed: self-target is
       fine (C != A), and the floor check passes (2 -> 1, not 0). Count=1
       -> warning true again.
    5. C attempts to revoke SystemOwner from B, the one remaining owner --
       the floor check rejects it (1 -> 0), `SystemOwnerFloorViolationError`.

    Also confirms AC-BI-007's "logged" half: at least the two count==1
    moments (steps 1 and 4) each emit a `component="authz"`,
    `outcome="warning"` log line.
    """
    configure()
    monkeypatch.setenv("PS_AUTH_ISSUER", _ACTOR_ISSUER)
    store = FakeAccessRoleStore()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    # Step 1: A is the first-ever caller -- bootstraps as the sole SystemOwner.
    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        step1 = _call_list_access_roles()
    assert step1.is_error is False
    assert json.loads(_text(step1))["system_owner_floor_warning"] is True
    assert store.count_active_system_owners() == 1

    # Step 2: A grants SystemOwner to B -- two owners now, warning clears.
    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        step2 = _call_grant_access_role(_SECOND_CALLER_SUBJECT, "SystemOwner")
    assert step2.is_error is False
    assert json.loads(_text(step2))["system_owner_floor_warning"] is False
    assert store.count_active_system_owners() == 2

    # Step 3: A grants SystemAdmin to C.
    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        step3 = _call_grant_access_role(_THIRD_CALLER_SUBJECT, "SystemAdmin")
    assert step3.is_error is False

    # Step 4: C (SystemAdmin) revokes SystemOwner from A -- widened RBAC,
    # not a self-target, floor check passes (2 -> 1).
    with _verified_actor(sub=_THIRD_CALLER_SUBJECT):
        step4 = _call_revoke_access_role(_FIRST_CALLER_SUBJECT, "SystemOwner")
    assert step4.is_error is False
    assert json.loads(_text(step4)) == {
        "principal_subject": _FIRST_CALLER_SUBJECT,
        "access_role": "SystemOwner",
        "revoked_by_subject": _THIRD_CALLER_SUBJECT,
        "system_owner_floor_warning": True,
    }
    assert store.count_active_system_owners() == 1
    assert AccessRole.SYSTEM_OWNER not in store.active_roles_for(
        (_FIRST_CALLER_SUBJECT, _ACTOR_ISSUER)
    )
    assert AccessRole.SYSTEM_OWNER in store.active_roles_for(
        (_SECOND_CALLER_SUBJECT, _ACTOR_ISSUER)
    )

    # Step 5: C attempts to revoke SystemOwner from B, the last remaining
    # owner -- rejected, count stays at 1, B keeps the role.
    with _verified_actor(sub=_THIRD_CALLER_SUBJECT):
        step5 = _call_revoke_access_role(_SECOND_CALLER_SUBJECT, "SystemOwner")
    assert step5.is_error is False
    assert _text(step5) == "error: This action would leave zero active SystemOwners."
    assert store.count_active_system_owners() == 1
    assert AccessRole.SYSTEM_OWNER in store.active_roles_for(
        (_SECOND_CALLER_SUBJECT, _ACTOR_ISSUER)
    )

    # AC-BI-007's "logged" half: at least the two count==1 moments (steps 1
    # and 4) each emitted a component="authz"/outcome="warning" log line.
    reset_for_tests()  # drain the emitter's queue and join its writer thread before reading
    lines = read_lines(resolve_default_log_path())
    warning_lines = [
        line
        for line in lines
        if line.get("component") == "authz" and line.get("outcome") == "warning"
    ]
    assert len(warning_lines) >= 2


def test_sole_owner_self_revoking_their_own_system_owner_role_is_blocked_before_the_floor_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005 extended to SystemOwner + PLAN.md §4 Slice 3's explicit ordering requirement.

    The sole SystemOwner both (a) is RBAC-eligible to revoke SystemOwner
    (their own role is in `_REVOKE_RBAC[SYSTEM_OWNER]`) and (b) is the one
    remaining owner (the floor check would also reject this) -- confirming
    `SelfGrantOrRevokeBlockedError` (not `SystemOwnerFloorViolationError`) is
    what actually fires proves `block_self_target` is evaluated *before*
    `enforce_system_owner_floor`, exactly as PLAN.md's Slice 3 proof
    requires: self-block wins whenever both conditions would otherwise
    independently apply.
    """
    configure()
    monkeypatch.setenv("PS_AUTH_ISSUER", _ACTOR_ISSUER)
    store = FakeAccessRoleStore()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        _call_list_access_roles()  # bootstraps FIRST_CALLER as the sole SystemOwner
        result = _call_revoke_access_role(_FIRST_CALLER_SUBJECT, "SystemOwner")

    assert result.is_error is False
    assert _text(result) == "error: You cannot grant or revoke your own access roles."
    # Untouched -- neither rule's rejection ever reaches the store mutation.
    assert store.count_active_system_owners() == 1
    assert AccessRole.SYSTEM_OWNER in store.active_roles_for((_FIRST_CALLER_SUBJECT, _ACTOR_ISSUER))


def test_revoke_of_system_owner_by_a_non_owner_non_admin_actor_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHANGES.md Appendix A: the widened revoke-SystemOwner RBAC is still a closed set --
    a bare PolicyManager (never SystemOwner or SystemAdmin) cannot revoke SystemOwner from anyone.
    """
    configure()
    monkeypatch.setenv("PS_AUTH_ISSUER", _ACTOR_ISSUER)
    store = FakeAccessRoleStore()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    with _verified_actor(sub=_FIRST_CALLER_SUBJECT):
        _call_list_access_roles()  # bootstraps FIRST_CALLER as SystemOwner
        _call_grant_access_role(_SECOND_CALLER_SUBJECT, "SystemOwner")  # two owners now
        _call_grant_access_role(_THIRD_CALLER_SUBJECT, "PolicyManager")

    with _verified_actor(sub=_THIRD_CALLER_SUBJECT):
        result = _call_revoke_access_role(_FIRST_CALLER_SUBJECT, "SystemOwner")

    assert result.is_error is False
    assert _text(result) == "error: You do not have the required access role for this action."
    assert AccessRole.SYSTEM_OWNER in store.active_roles_for((_FIRST_CALLER_SUBJECT, _ACTOR_ISSUER))
