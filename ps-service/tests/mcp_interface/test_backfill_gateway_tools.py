"""Backfills write through the Graph Write Gateway, only when there is something to fill.

`get-policy` resolves the gateway lazily: a read of an already-backfilled tree works with no
gateway installed, a read that needs a fill answers `error:` without one and writes nothing. Every
write tool (the transitions here) acquires the gateway before the service call, so without
one they answer `error:`, write no audit row and log nothing, whether or not a backfill is needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

import pytest
from api._gateway_fakes import installed_process_gateway
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors the other lifecycle tool tests
    FakeAccessRoleStore,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent
from policy_lifecycle._gateway_fakes import (
    PolicyGraphSeeder,
    PolicyReadGraph,
    SeedControl,
    SeedStandard,
)

from mcp_interface._environment import graph_factory_from_connect
from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
    _server,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)
from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Generator

    from graph_gateway._fakes import GatewayRig

_ACTOR = "policy-author"
_ISSUER = "https://issuer.example.com/"
_POLICY = "pol_data_protection_policy_aaaaaa"
_GRAPH = "policy_system"
_NO_GATEWAY = "error: graph writes are unavailable"


@contextlib.contextmanager
def _verified_actor(*, sub: str, iss: str = _ISSUER) -> Generator[None]:
    access_token = AccessToken(
        token="test-token", client_id="test-client", scopes=[], subject=sub, claims={"iss": iss}
    )
    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(token)


class _Connection:
    def __init__(self, handle: PolicyReadGraph) -> None:
        self._handle = handle

    def select_graph(self, name: str) -> PolicyReadGraph:
        del name
        return self._handle


class _AuditStore:
    """Records every audit call: none of these tools may make one without a gateway."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def record(self, *args: object, **kwargs: object) -> str:
        self.calls.append((args, kwargs))
        return "unused"

    def record_standalone(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def _setup(monkeypatch: pytest.MonkeyPatch, *, legacy: bool) -> tuple[GatewayRig, _AuditStore]:
    """Seed a Policy tree (`legacy`: no version, no Standard/Control status) and wire the tools."""
    configure()
    rig = installed_process_gateway().rig
    graph = rig.graphs.open(_GRAPH)
    PolicyGraphSeeder(graph).seed_policy(
        _POLICY,
        version=None if legacy else "1",
        owner=(_ACTOR, _ISSUER),
        standards=(
            SeedStandard(
                "std_1",
                "Encryption",
                status=None if legacy else "draft",
                controls=(SeedControl("ctl_1", "Rotation", status=None if legacy else "draft"),),
            ),
        ),
        legacy_edges=False,
    )
    audit = _AuditStore()
    handle = PolicyReadGraph(graph)

    def _connect(_config: object) -> _Connection:
        return _Connection(handle)

    def _audit_store(_config: object) -> _AuditStore:
        return audit

    def _access_role_store(_config: object, **_kwargs: object) -> FakeAccessRoleStore:
        return FakeAccessRoleStore()

    _override(monkeypatch, graph_factory=graph_factory_from_connect(_connect))
    _override(monkeypatch, audit_store_factory=_audit_store)
    _override(monkeypatch, role_store_factory=_access_role_store)
    return rig, audit


def _call(tool: str, arguments: dict[str, object]) -> CallToolResult:
    result = asyncio.run(_server().call_tool(tool, arguments))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_get_policy_of_a_backfilled_tree_works_with_no_gateway_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that has nothing to fill needs no gateway."""
    rig, _ = _setup(monkeypatch, legacy=False)
    install_graph_write_gateway(None)

    with _verified_actor(sub=_ACTOR):
        result = _call("get-policy", {"policy_id": _POLICY})

    assert json.loads(_text(result))["policy_id"] == _POLICY
    assert rig.store.groups == []


def test_get_policy_that_needs_a_fill_with_no_gateway_answers_an_error_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`get-policy` of a tree needing a fill answers the no-gateway error and writes nothing."""
    rig, graph_audit = _setup(monkeypatch, legacy=True)
    install_graph_write_gateway(None)

    with _verified_actor(sub=_ACTOR):
        result = _call("get-policy", {"policy_id": _POLICY})

    assert _text(result).startswith(_NO_GATEWAY)
    assert rig.store.groups == []
    assert graph_audit.calls == []


def test_get_policy_of_a_legacy_tree_fills_it_in_one_group_and_answers_the_filled_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy tree is filled once in a single group; the second read fills nothing."""
    rig, audit = _setup(monkeypatch, legacy=True)

    with _verified_actor(sub=_ACTOR):
        first = _call("get-policy", {"policy_id": _POLICY})
        second = _call("get-policy", {"policy_id": _POLICY})

    body = json.loads(_text(first))
    assert body["version"] == "1"
    assert body["standards"][0]["status"] == "draft"
    assert body["standards"][0]["controls"][0]["status"] == "draft"
    assert _text(first) == _text(second)
    (group,) = rig.store.groups  # the second read filled nothing
    assert rig.store.audit_link_of(group) is None
    assert audit.calls == []


def test_get_policy_backfill_failure_is_the_prefixed_persistence_error_in_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing backfill append answers the prefixed persistence error, not a raw driver error."""
    rig, _ = _setup(monkeypatch, legacy=True)
    rig.store.fail_appends()

    with _verified_actor(sub=_ACTOR):
        result = _call("get-policy", {"policy_id": _POLICY})

    assert _text(result).startswith("error: FalkorDB write failed: ")


@pytest.mark.parametrize("legacy", [True, False], ids=["needs-backfill", "backfilled"])
@pytest.mark.parametrize(
    "tool",
    ["propose-policy", "approve-policy", "reject-policy", "revert-policy-to-draft"],
)
def test_a_transition_with_no_gateway_answers_an_error_writes_no_audit_row_and_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, tool: str, *, legacy: bool
) -> None:
    """The gateway is acquired before the service call, so not even a `rejected` row exists."""
    rig, audit = _setup(monkeypatch, legacy=legacy)
    install_graph_write_gateway(None)

    with _verified_actor(sub=_ACTOR):
        result = _call(tool, {"policy_id": _POLICY})

    assert _text(result).startswith(_NO_GATEWAY)
    assert audit.calls == []
    assert rig.store.groups == []


def test_a_standard_edit_on_a_tree_that_needs_a_fill_with_no_gateway_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A standard edit needing a fill, with no gateway, answers the no-gateway error."""
    rig, audit = _setup(monkeypatch, legacy=True)
    install_graph_write_gateway(None)

    with _verified_actor(sub=_ACTOR):
        result = _call(
            "update-standard-draft", {"standard_id": "std_1", "fields": {"description": "x"}}
        )

    assert _text(result).startswith(_NO_GATEWAY)
    assert audit.calls == []
    assert rig.store.groups == []
