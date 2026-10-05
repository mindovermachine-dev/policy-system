"""`find-capability-merge-candidates` for a granted `ComplianceOfficer` (issue #190, slice 6)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from authz._fakes import (
    FakeAccessRoleStore,  # pyright: ignore[reportPrivateUsage]  -- importable test package
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from ps_service.authz.models import AccessRole
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

_OWNER = "owner"
_OFFICER = "officer"
_ISSUER = "https://issuer.example.com/"


@dataclass
class _Result:
    result_set: list[object]


class _Graph:
    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del q, params
        return _Result(
            [
                ["cap_1", "Patch Management", None, "pol_a", "Policy A", "approved", 4],
                ["cap_2", "patch-management", None, None, None, None, 2],
                ["cap_3", "Other", None, None, None, None, 0],
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


def _opener_factory(opener: Callable[[object], object]) -> Callable[[], Callable[[object], object]]:
    def _factory() -> Callable[[object], object]:
        return opener

    return _factory


def _open_graph(_config: object) -> object:
    return _Graph()


def _call(name: str, args: dict[str, object] | None = None) -> str:
    result = asyncio.run(mcp_server.server.call_tool(name, args or {}))
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


def test_compliance_officer_gets_the_full_ac_bi_003_wire_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    store = _officer_store()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _store_factory(store))
    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
        _opener_factory(_open_graph),
    )

    with _actor(_OFFICER):
        body = json.loads(_call("find-capability-merge-candidates"))

    assert body == {
        "groups": [
            {
                "basis": "name",
                "merge_case": 2,
                "policies_distinct": False,
                "members": [
                    {
                        "id": "cap_1",
                        "name": "Patch Management",
                        "obligation_count": 4,
                        "governing_policy": {
                            "id": "pol_a",
                            "title": "Policy A",
                            "status": "approved",
                        },
                    },
                    {
                        "id": "cap_2",
                        "name": "patch-management",
                        "obligation_count": 2,
                        "governing_policy": None,
                    },
                ],
            }
        ]
    }


def test_unreachable_graph_returns_the_sanitised_error(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    store = _officer_store()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _store_factory(store))

    def _boom(_config: object) -> object:
        message = "connection refused to 10.0.0.1:6379"
        raise ConnectionError(message)

    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
        _opener_factory(_boom),
    )

    with _actor(_OFFICER):
        text = _call("find-capability-merge-candidates")

    assert text == "error: the policy graph database is not reachable"


@pytest.mark.parametrize("bad", [0.5, 0.2, 1.01])
def test_min_similarity_outside_the_bounds_is_rejected_at_the_schema(
    monkeypatch: pytest.MonkeyPatch, bad: float
) -> None:
    configure()
    store = _officer_store()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _store_factory(store))

    with _actor(_OFFICER), pytest.raises(ToolError):
        asyncio.run(
            mcp_server.server.call_tool("find-capability-merge-candidates", {"min_similarity": bad})
        )


def test_min_similarity_is_passed_through_and_embedding_groups_are_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    store = _officer_store()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _store_factory(store))

    class _EmbeddingGraph:
        def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
            del q, params
            return _Result(
                [
                    ["cap_1", "A", [1.0, 0.0], None, None, None, 0],
                    ["cap_2", "B", [0.9, 0.436], None, None, None, 0],
                ]
            )

    monkeypatch.setattr(
        "ps_service.graph_cleanup.dependencies.build_default_graph_cleanup_graph_opener",
        _opener_factory(lambda _config: _EmbeddingGraph()),
    )

    with _actor(_OFFICER):
        strict = json.loads(_call("find-capability-merge-candidates", {"min_similarity": 0.95}))
        loose = json.loads(_call("find-capability-merge-candidates", {"min_similarity": 0.85}))

    assert strict == {"groups": []}
    assert [g["basis"] for g in loose["groups"]] == ["embedding"]
