"""Stateful create -> propose -> approve -> detect chain for `ps-author-policy`.

Closes a fork-detection gap at fake level: after a fork of an approved Policy is
created, proposed and approved through the REAL MCP tools, the skill's own
branch-detection query (`GOVERNED_BY` + `SUPERSEDED_BY`, SKILL.md step 3) must
find the new Policy as the Capability's governor -- never "no governing
Policy". Every write of the chain is a Graph Write Gateway group over the
process gateway rig's graph; the handle the tools read through answers the lifecycle reads and the
skill's detection queries from that same graph. It cannot validate Cypher text -- that is the
`falkordb_live` file's job (`policy_lifecycle/test_governed_by_live.py`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from typing import TYPE_CHECKING, cast

from api._gateway_fakes import installed_process_gateway
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors test_policy_lifecycle_tools.py's own convention
    FakeAccessRoleStore,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent
from policy_lifecycle._gateway_fakes import PolicyGraphSeeder, PolicyReadGraph, SeedStandard

from ps_service.authz.models import AccessRole
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server, tool_core
from ps_service.query_engine.cypher_query import (
    _SEED_CHECK_QUERY,  # pyright: ignore[reportPrivateUsage]  -- pins the exact seed-check query text, mirrors test_cypher_tool.py
)

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    import pytest
    from graph_gateway._fakes import InMemoryGraph

_ISSUER = "https://issuer.example.com/"
_AUTHOR = "policy-author"
_MANAGER = "policy-manager"
_GRANTER = ("system-admin-tool", "iss")

_CAP_NAME = "Data Protection"
_CAP_ID = "cap_data_protection"
_PRIOR_ID = "pol_prior"


class _Result:
    def __init__(
        self, header: Sequence[object] | None = None, rows: list[object] | None = None
    ) -> None:
        self.header: list[object] = list(header or [])
        self.result_set = rows or []


class _ChainGraph(PolicyReadGraph):
    """The process gateway rig's graph, as the lifecycle tools and the skill's queries read it.

    Every write of the chain is a gateway group on that graph. This handle answers
    the lifecycle reads through the shared read double and the skill's two detection queries from
    the same graph; it refuses every statement it was not taught.
    """

    def __init__(self) -> None:
        super().__init__(installed_process_gateway().rig.graphs.open("policy_system"))

    @property
    def graph(self) -> InMemoryGraph:
        return self._graph

    def governed(self) -> dict[str, str]:
        """`{capability_id: governing_policy_id}` of the graph's `GOVERNED_BY` edges."""
        return {key[2]: key[4] for key in self._graph.edges if key[0] == "GOVERNED_BY"}

    def status(self, policy_id: str) -> object:
        return self._graph.nodes[("Policy", policy_id)]["status"]

    # --- skill queries (the cypher tool) ---------------------------------

    def _skill_query(self, q: str) -> _Result | None:
        name_match = re.search(r"\{name: '([^']*)'\}", q)
        if name_match is None:
            return None
        cap_id = next(
            (
                node_id
                for (label, node_id), props in self._graph.nodes.items()
                if label == "Capability" and props.get("name") == name_match[1]
            ),
            None,
        )
        if "GOVERNED_BY" not in q:
            rows: list[object] = [[name_match[1], cap_id]] if cap_id else []
            return _Result([[0, "capability_name"], [0, "capability_id"]], rows)
        header = [[0, "policy_id"], [0, "status"], [0, "fork_id"], [0, "fork_status"]]
        governor = self.governed().get(cap_id or "")
        if governor is None:
            return _Result(header, [])
        forks = [
            key[4] for key in self._graph.edges if key[0] == "SUPERSEDED_BY" and key[2] == governor
        ]
        gov_status = self.status(governor)
        if not forks:
            return _Result(header, [[governor, gov_status, None, None]])
        return _Result(header, [[governor, gov_status, f, self.status(f)] for f in forks])

    def query(  # type: ignore[override]
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _Result:
        if q == _SEED_CHECK_QUERY:
            return _Result([[0, "c"]], [[1]])
        skill = self._skill_query(q)
        if skill is not None:
            return skill
        return cast("_Result", super().query(q, params, timeout))


class _FakeFalkorDB:
    def __init__(self, handle: _ChainGraph) -> None:
        self._handle = handle

    def select_graph(self, name: str) -> _ChainGraph:
        del name
        return self._handle


class _AuditStore:
    def record(self, *args: object, **kwargs: object) -> str:
        raise NotImplementedError

    def record_standalone(self, **kwargs: object) -> None:
        del kwargs

    def record_standalone_with_event_id(self, **kwargs: object) -> str:
        """`record_standalone`, returning an id the process gateway's log accepts."""
        del kwargs
        event_id = str(uuid.uuid4())
        installed_process_gateway().rig.store.audit_rows.add(event_id)
        return event_id

    def query(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError


@contextlib.contextmanager
def _actor(sub: str) -> Generator[None]:
    token = auth_context_var.set(
        AuthenticatedUser(
            AccessToken(token="t", client_id="c", scopes=[], subject=sub, claims={"iss": _ISSUER})
        )
    )
    try:
        yield
    finally:
        auth_context_var.reset(token)


def _install(monkeypatch: pytest.MonkeyPatch, graph: _ChainGraph) -> None:
    roles = FakeAccessRoleStore()
    roles.grant(actor=_GRANTER, target=(_MANAGER, _ISSUER), access_role=AccessRole.POLICY_MANAGER)

    def _connect(_config: object) -> _FakeFalkorDB:
        return _FakeFalkorDB(graph)

    def _audit(_config: object) -> _AuditStore:
        return _AuditStore()

    def _role_store(_config: object, **_kwargs: object) -> object:
        return roles

    monkeypatch.setattr(tool_core, "connect_from_config", _connect)
    monkeypatch.setattr(tool_core, "PsycopgAuditStore", _audit)
    monkeypatch.setattr(tool_core, "PsycopgAccessRoleStore", _role_store)


def _call(tool: str, args: dict[str, object]) -> dict[str, object]:
    result = asyncio.run(mcp_server.server.call_tool(tool, args))
    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert not block.text.startswith("error:"), block.text
    return cast("dict[str, object]", json.loads(block.text))


def _detect() -> list[list[object]]:
    """The skill's step 3 detection query, verbatim, through the real `cypher` tool."""
    query = (
        f"MATCH (c:Capability {{name: '{_CAP_NAME}'}})-[:GOVERNED_BY]->(p:Policy) "
        "OPTIONAL MATCH (p)-[:SUPERSEDED_BY]->(f:Policy) "
        "RETURN p.id AS policy_id, p.status AS status, f.id AS fork_id, f.status AS fork_status"
    )
    return cast("list[list[object]]", _call("cypher", {"query": query})["rows"])


def _seed_approved_prior() -> None:
    seeder = PolicyGraphSeeder(installed_process_gateway().rig.graphs.open("policy_system"))
    seeder.seed_policy(
        _PRIOR_ID,
        title="Data Protection Policy",
        status="approved",
        owner=(_AUTHOR, _ISSUER),
        standards=(SeedStandard("std_1", "Std", status="approved"),),
        legacy_edges=False,
    )
    seeder.seed_capability(_CAP_ID, name=_CAP_NAME)
    seeder.seed_governs(_CAP_ID, _PRIOR_ID, legacy=False)


def test_fork_create_propose_approve_then_detection_finds_the_new_governed_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second run no longer re-reports "no governing Policy"."""
    configure()
    graph = _ChainGraph()
    _seed_approved_prior()
    _install(monkeypatch, graph)

    assert _detect() == [[_PRIOR_ID, "approved", None, None]]

    with _actor(_AUTHOR):
        fork = _call(
            "create-policy-draft",
            {"title": "Data Protection Policy v2", "supersedes_policy_id": _PRIOR_ID},
        )
    fork_id = cast("str", fork["policy_id"])
    # Edges stay on the prior while the fork is a draft: skill resumes it.
    assert graph.governed() == {_CAP_ID: _PRIOR_ID}
    assert _detect() == [[_PRIOR_ID, "approved", fork_id, "draft"]]

    with _actor(_AUTHOR):
        _call("propose-policy", {"policy_id": fork_id})
    assert _detect() == [[_PRIOR_ID, "approved", fork_id, "proposed"]]

    with _actor(_MANAGER):
        approved = _call("approve-policy", {"policy_id": fork_id})

    assert approved["governed_capability_ids"] == [_CAP_ID]
    assert graph.governed() == {_CAP_ID: fork_id}
    # Detection now names the approved successor as governor -- not "no governing Policy".
    assert _detect() == [[fork_id, "approved", None, None]]
    assert graph.status(_PRIOR_ID) == "deprecated"


def test_fresh_create_with_capability_ids_is_found_by_detection_as_a_governing_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    graph = _ChainGraph()
    _install(monkeypatch, graph)
    # The claim is a guarded gateway group: the Capability exists in the graph the
    # gateway writes.
    PolicyGraphSeeder(installed_process_gateway().rig.graphs.open("policy_system")).seed_capability(
        _CAP_ID, name=_CAP_NAME
    )
    assert _detect() == []

    with _actor(_AUTHOR):
        created = _call(
            "create-policy-draft",
            {"title": "Data Protection Policy", "capability_ids": [_CAP_ID]},
        )

    policy_id = cast("str", created["policy_id"])
    # Second run takes the existing `status == "draft"` GOVERNED_BY branch (resume).
    assert _detect() == [[policy_id, "draft", None, None]]
