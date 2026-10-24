"""`find-duplicate-obligations` for a granted `ComplianceOfficer`."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from authz._fakes import (
    FakeAccessRoleStore,  # pyright: ignore[reportPrivateUsage]  -- importable test package
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
    _server,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)
from ps_service.authz.models import AccessRole
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    import pytest

_OWNER = "owner"
_OFFICER = "officer"
_ISSUER = "https://issuer.example.com/"


@dataclass
class _Result:
    result_set: list[object]


class _Graph:
    def __init__(self) -> None:
        self.params: list[dict[str, object] | None] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del q
        self.params.append(params)
        return _Result(
            [
                ["role_m", "Manufacturer", "obl_1", "Notify the authority", "R1", "Art. 6(1)"],
                ["role_m", "Manufacturer", "obl_2", "notify the authority", "R2", "Art. 6(2)"],
                ["role_i", "Importer", "obl_3", "Notify the authority", "R3", "Art. 21"],
            ]
        )


@contextlib.contextmanager
def _actor(sub: str) -> Generator[None]:
    token = AccessToken(token="t", client_id="c", scopes=[], subject=sub, claims={"iss": _ISSUER})
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def _store_factory(store: object) -> Callable[..., object]:
    def _factory(*_args: object, **_kwargs: object) -> object:
        return store

    return _factory


def _call(name: str, args: dict[str, object] | None = None) -> str:
    result = asyncio.run(_server().call_tool(name, args or {}))
    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _officer_store() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=(_OWNER, _ISSUER))
    store.bootstrap_first_owner((_OWNER, _ISSUER))
    store.grant(
        actor=(_OWNER, _ISSUER),
        target=(_OFFICER, _ISSUER),
        access_role=AccessRole.COMPLIANCE_OFFICER,
    )
    return store


def test_compliance_officer_gets_the_ac_bi_004_wire_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    graph = _Graph()
    _override(monkeypatch, role_store_factory=_store_factory(_officer_store()))

    def _open_graph(_config: object) -> _Graph:
        return graph

    _override(monkeypatch, graph_factory=_open_graph)

    with _actor(_OFFICER):
        body = json.loads(_call("find-duplicate-obligations", {"role_id": "role_m"}))

    assert graph.params == [{"role_id": "role_m"}]
    assert body == {
        "groups": [
            {
                "role_id": "role_m",
                "role_name": "Manufacturer",
                "basis": "identical_text",
                "members": [
                    {
                        "id": "obl_1",
                        "text": "Notify the authority",
                        "requirements": [{"requirement_id": "R1", "source_ref": "Art. 6(1)"}],
                    },
                    {
                        "id": "obl_2",
                        "text": "notify the authority",
                        "requirements": [{"requirement_id": "R2", "source_ref": "Art. 6(2)"}],
                    },
                ],
            }
        ]
    }


def test_unreachable_graph_returns_the_sanitised_error(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _override(monkeypatch, role_store_factory=_store_factory(_officer_store()))

    def _boom(_config: object) -> object:
        message = "connection refused to 10.0.0.1:6379"
        raise ConnectionError(message)

    _override(monkeypatch, graph_factory=_boom)

    with _actor(_OFFICER):
        text = _call("find-duplicate-obligations")

    assert text == "error: the policy graph database is not reachable"
