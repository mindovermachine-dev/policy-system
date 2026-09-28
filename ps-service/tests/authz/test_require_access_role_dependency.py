"""Direct unit tests for `require_access_role` (issue #133, PLAN.md §0.5/§2.3, Slice 5).

`require_access_role` is a FastAPI dependency-factory with no REST route wired
to it (PLAN.md §0.5 -- Deliverables names only MCP tools + a skill for every
one of the six gated actions; a fabricated route would be scope creep beyond
the 15 ACs). It is proven here exactly as PLAN.md's own Slice 5 text
instructs: "construct a fake `Request`/`app.state.config`, call the returned
callable, assert it raises/passes" -- a plain function call against a bare
Starlette `Request` built from a minimal ASGI scope, never a `TestClient`
HTTP round trip (there is no route to hit).

`ps_service.api.dependencies.PsycopgAccessRoleStore` is monkeypatched exactly
the way `mcp_interface.mcp_server.PsycopgAccessRoleStore` already is in
`tests/mcp_interface/test_access_role_tools.py`/`test_catalog_source_authz_gate.py`
-- the same fakes (`tests/authz/_fakes.py`), proving `require_access_role`
drives the identical shared `ps_service.authz.service.require_role` function
those MCP tools call (AC-BI-012: one shared component, both surfaces).

Lives under `tests/authz/` (alongside `test_service.py`) rather than
`tests/api/`: `--import-mode=importlib` (workspace root `pyproject.toml`)
registers each package's own `__init__.py` in `sys.modules` only once
pytest starts collecting *inside* that package's directory, and full-suite
collection visits `tests/api/` before `tests/authz/` (alphabetical) --  a
file under `tests/api/` importing `from authz._fakes import ...` hits a real
`ModuleNotFoundError: No module named 'authz'` in a full-suite run (verified
directly), the same class of pre-existing ordering quirk this issue's own
task briefing already flags for `api`. Placing this file inside `tests/authz/`
itself sidesteps it the same way `test_service.py` already does.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from starlette.requests import Request

from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore
from ps_service.api import dependencies
from ps_service.api.dependencies import require_access_role
from ps_service.api.errors import AccessDeniedError, AuthorizationStoreUnavailableError
from ps_service.auth import Principal
from ps_service.authz.models import AccessRole

if TYPE_CHECKING:
    from collections.abc import Callable

_SUBJECT = "rest-caller"
_OTHER_SUBJECT = "rest-owner"
_ISSUER = "https://issuer.example.com/"


def _fake_store_factory(store: object) -> Callable[..., object]:
    """An `AccessRoleStore`-shaped factory returning the same fake store every call.

    Monkeypatched onto `dependencies.PsycopgAccessRoleStore` -- `require_access_role`'s
    returned dependency calls it as `PsycopgAccessRoleStore(config,
    audit_store=PsycopgAuditStore(config))` (issue #147), so this must accept (and
    ignore) both the positional `config` and the `audit_store` keyword.
    Mirrors `test_access_role_tools.py`'s own `_fake_store_factory` exactly.
    """

    def _factory(_config: object, **_kwargs: object) -> object:
        return store

    return _factory


def _fake_request(*, principal: Principal | None) -> Request:
    """A bare `Request` carrying only what `require_access_role` reads.

    `get_service_config` reads `request.app.state.config` -- the value
    itself is never inspected by `require_access_role` (the monkeypatched
    `PsycopgAccessRoleStore` factory below ignores it, exactly like
    `mcp_server.py`'s own `_fake_store_factory`), so a bare placeholder
    suffices. `get_principal` reads `request.scope["ps_principal"]`
    directly (the contract `RestAuthMiddleware` establishes, issue #58).
    """
    app = SimpleNamespace(state=SimpleNamespace(config=SimpleNamespace()))
    scope = {"type": "http", "app": app, "ps_principal": principal}
    return Request(scope)


def test_no_verified_principal_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """No verified `Principal` bound to the request -- fail closed, never a synthetic identity."""
    monkeypatch.setattr(
        dependencies, "PsycopgAccessRoleStore", _fake_store_factory(FakeAccessRoleStore())
    )
    dependency = require_access_role(AccessRole.SYSTEM_ADMIN)
    request = _fake_request(principal=None)

    with pytest.raises(AccessDeniedError) as exc_info:
        dependency(request)

    assert str(exc_info.value) == "You do not have the required access role for this action."


def test_principal_without_the_required_role_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """A verified principal holding no elevated role fails `require_access_role`'s gate."""
    store = FakeAccessRoleStore(expected_owner=(_OTHER_SUBJECT, _ISSUER))
    store.bootstrap_first_owner((_OTHER_SUBJECT, _ISSUER))  # a distinct SystemOwner already exists
    monkeypatch.setattr(dependencies, "PsycopgAccessRoleStore", _fake_store_factory(store))
    dependency = require_access_role(AccessRole.SYSTEM_ADMIN)
    request = _fake_request(principal=Principal(sub=_SUBJECT, iss=_ISSUER))

    with pytest.raises(AccessDeniedError):
        dependency(request)


def test_principal_holding_the_required_role_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A verified principal already holding the minimum role passes the dependency cleanly."""
    store = FakeAccessRoleStore()
    store.grant(
        actor=(_OTHER_SUBJECT, _ISSUER),
        target=(_SUBJECT, _ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )
    monkeypatch.setattr(dependencies, "PsycopgAccessRoleStore", _fake_store_factory(store))
    dependency = require_access_role(AccessRole.SYSTEM_ADMIN)
    request = _fake_request(principal=Principal(sub=_SUBJECT, iss=_ISSUER))

    assert dependency(request) is None  # passes cleanly -- never raises


def test_sole_system_owner_also_satisfies_a_system_admin_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAN.md §0.8's hierarchy fix reaches the REST dependency too: `SystemOwner` satisfies
    a `SystemAdmin` minimum -- proving `require_access_role` calls the identical
    `require_role` function `mcp_server.py`'s own gated tools call (AC-BI-012), not a
    parallel, potentially-divergent gating mechanism.
    """
    store = FakeAccessRoleStore(expected_owner=(_SUBJECT, _ISSUER))
    store.bootstrap_first_owner((_SUBJECT, _ISSUER))
    monkeypatch.setattr(dependencies, "PsycopgAccessRoleStore", _fake_store_factory(store))
    dependency = require_access_role(AccessRole.SYSTEM_ADMIN)
    request = _fake_request(principal=Principal(sub=_SUBJECT, iss=_ISSUER))

    assert dependency(request) is None


def test_store_outage_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-011: a simulated authz-store outage raises the distinct
    `AuthorizationStoreUnavailableError`, never a silent pass/default.
    """
    monkeypatch.setattr(
        dependencies, "PsycopgAccessRoleStore", _fake_store_factory(RaisingAccessRoleStore())
    )
    dependency = require_access_role(AccessRole.SYSTEM_ADMIN)
    request = _fake_request(principal=Principal(sub=_SUBJECT, iss=_ISSUER))

    with pytest.raises(AuthorizationStoreUnavailableError) as exc_info:
        dependency(request)

    assert str(exc_info.value) == "The authorization store is temporarily unavailable."
