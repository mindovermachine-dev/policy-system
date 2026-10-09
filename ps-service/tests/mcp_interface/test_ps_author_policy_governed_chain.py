"""Stateful create -> propose -> approve -> detect chain for `ps-author-policy` (#185, S8).

Closes the AC-007 bug at fake level: after a fork of an approved Policy is
created, proposed and approved through the REAL MCP tools, the skill's own
branch-detection query (`GOVERNED_BY` + `SUPERSEDED_BY`, SKILL.md step 3) must
find the new Policy as the Capability's governor -- never "no governing
Policy". One stateful fake graph holds the `GOVERNED_BY` map, the
`SUPERSEDED_BY` pairs and the Policy trees; it models the contract of the
guarded statements per CHANGES.md Appendix A5 (a row and a mutation only when
the guard holds). The fake cannot validate Cypher text -- that is the
`falkordb_live` file's job (`policy_lifecycle/test_governed_by_live.py`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors test_policy_lifecycle_tools.py's own convention
    FakeAccessRoleStore,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from ps_service.authz.models import AccessRole
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server
from ps_service.query_engine.cypher_query import (
    _SEED_CHECK_QUERY,  # pyright: ignore[reportPrivateUsage]  -- pins the exact seed-check query text, mirrors test_cypher_tool.py
)

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence

    import pytest

_ISSUER = "https://issuer.example.com/"
_AUTHOR = "policy-author"
_MANAGER = "policy-manager"
_GRANTER = ("system-admin-tool", "iss")

_CAP_NAME = "Data Protection"
_CAP_ID = "cap_data_protection"
_PRIOR_ID = "pol_prior"


@dataclass
class _Policy:
    title: str
    status: str
    version: str
    owner_subject: str
    owner_issuer: str
    standards: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])


class _Result:
    def __init__(
        self, header: Sequence[object] | None = None, rows: list[object] | None = None
    ) -> None:
        self.header: list[object] = list(header or [])
        self.result_set = rows or []


class _ChainGraph:
    """In-memory graph honouring the contract of every statement the chain issues."""

    def __init__(self) -> None:
        self.policies: dict[str, _Policy] = {}
        self.governed: dict[str, str] = {}
        self.capabilities: dict[str, str] = {_CAP_ID: _CAP_NAME}
        self.superseded: list[tuple[str, str]] = []
        self.unmatched: list[str] = []

    # --- skill queries (the cypher tool) ---------------------------------

    def _skill_query(self, q: str) -> _Result | None:
        name_match = re.search(r"\{name: '([^']*)'\}", q)
        if name_match is None:
            return None
        cap_id = next((i for i, n in self.capabilities.items() if n == name_match[1]), None)
        if "GOVERNED_BY" not in q:
            rows: list[object] = [[name_match[1], cap_id]] if cap_id else []
            return _Result([[0, "capability_name"], [0, "capability_id"]], rows)
        header = [[0, "policy_id"], [0, "status"], [0, "fork_id"], [0, "fork_status"]]
        governor = self.governed.get(cap_id or "")
        if governor is None:
            return _Result(header, [])
        forks = [new for prior, new in self.superseded if prior == governor]
        gov_status = self.policies[governor].status
        if not forks:
            return _Result(header, [[governor, gov_status, None, None]])
        return _Result(header, [[governor, gov_status, f, self.policies[f].status] for f in forks])

    # --- policy_lifecycle statements --------------------------------------

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _Result:
        del timeout
        p = params or {}
        if q == _SEED_CHECK_QUERY:
            return _Result([[0, "c"]], [[1]])
        skill = self._skill_query(q)
        if skill is not None:
            return skill
        if "coalesce(p.version" in q or "IS NULL" in q:
            return _Result()
        if "DELETE r" in q:
            return self._repoint(p)
        if "RETURN prior.id, collect(cap.id)" in q:
            return self._fork_governance(p)
        if "WHERE prior.status = 'approved'" in q:
            return self._approved_prior(p)
        if "RETURN cap.id, g.id" in q:
            requested = cast("list[str]", p["capability_ids"])
            return _Result(
                rows=[[c, self.governed.get(c)] for c in requested if c in self.capabilities]
            )
        if "FOREACH (c IN caps | MERGE (c)-[:GOVERNED_BY]->(p))" in q:
            return self._guarded_create(p)
        if q.strip() == "MATCH (p:Policy {id: $policy_id}) RETURN p.id, p.title":
            policy = self.policies.get(cast("str", p["policy_id"]))
            return _Result(rows=[[p["policy_id"], policy.title]] if policy else [])
        if "MERGE (p:Policy {id: $policy_id}) SET p += $properties" in q:
            self._merge_policy(p)
            return _Result()
        if "MERGE (prior)-[:SUPERSEDED_BY]->(new)" in q:
            self.superseded.append((cast("str", p["prior_id"]), cast("str", p["new_id"])))
            return _Result()
        if "RETURN s.id, properties(s), c.id, properties(c)" in q:
            return self._fork_tree(cast("str", p["policy_id"]))
        if "SUPPORTED_BY]->(s:Standard {id: $standard_id})" in q:
            props = cast("dict[str, str]", p["properties"])
            self.policies[cast("str", p["policy_id"])].standards[cast("str", p["standard_id"])] = (
                props["title"],
                props["status"],
            )
            return _Result()
        if "s.id, s.title, s.status, c.id" in q:
            return self._tree(cast("str", p["policy_id"]))
        if "SET p.status = $target_status" in q:
            self._cascade(cast("str", p["policy_id"]), cast("str", p["target_status"]))
            return _Result()
        self.unmatched.append(q)
        return _Result()

    def _merge_policy(self, p: Mapping[str, object]) -> None:
        props = cast("dict[str, str]", p["properties"])
        self.policies[cast("str", p["policy_id"])] = _Policy(
            title=props["title"],
            status=props["status"],
            version=props["version"],
            owner_subject=props["owner_subject"],
            owner_issuer=props["owner_issuer"],
        )

    def _guarded_create(self, p: Mapping[str, object]) -> _Result:
        ids = cast("list[str]", p["capability_ids"])
        if not all(i in self.capabilities and i not in self.governed for i in ids):
            return _Result()
        self._merge_policy(p)
        for cap_id in ids:
            self.governed[cap_id] = cast("str", p["policy_id"])
        return _Result(rows=[[p["policy_id"]]])

    def _fork_governance(self, p: Mapping[str, object]) -> _Result:
        prior = next((a for a, b in self.superseded if b == p["policy_id"]), None)
        if prior is None:
            return _Result()
        caps = [c for c, g in self.governed.items() if g == prior]
        return _Result(rows=[[prior, caps]])

    def _approved_prior(self, p: Mapping[str, object]) -> _Result:
        for prior, new in self.superseded:
            if new == p["successor_policy_id"] and self.policies[prior].status == "approved":
                return _Result(rows=[[prior]])
        return _Result()

    def _repoint(self, p: Mapping[str, object]) -> _Result:
        ids = cast("list[str]", p["capability_ids"])
        if sum(self.governed.get(c) == p["prior_id"] for c in ids) != p["expected"]:
            return _Result()
        for cap_id in ids:
            self.governed[cap_id] = cast("str", p["policy_id"])
        self._cascade(cast("str", p["policy_id"]), cast("str", p["target_status"]))
        return _Result(rows=[[p["policy_id"]]])

    def _cascade(self, policy_id: str, target: str) -> None:
        policy = self.policies[policy_id]
        policy.status = target
        policy.standards = {i: (t, target) for i, (t, _s) in policy.standards.items()}

    def _fork_tree(self, policy_id: str) -> _Result:
        policy = self.policies.get(policy_id)
        if policy is None:
            return _Result()
        rows: list[object] = []
        for std_id, (title, status) in policy.standards.items():
            props: dict[str, object] = {"id": std_id, "title": title, "status": status}
            rows.append([std_id, props, None, None])
        return _Result(rows=rows)

    def _tree(self, policy_id: str) -> _Result:
        policy = self.policies.get(policy_id)
        if policy is None:
            return _Result()
        head = [
            policy_id,
            policy.title,
            policy.status,
            policy.version,
            policy.owner_subject,
            policy.owner_issuer,
        ]
        if not policy.standards:
            return _Result(rows=[[*head, None, None, None, None, None, None, None]])
        return _Result(
            rows=[
                [*head, std_id, title, status, None, None, None, None]
                for std_id, (title, status) in policy.standards.items()
            ]
        )


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

    monkeypatch.setattr(mcp_server, "connect_from_config", _connect)
    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", _audit)
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _role_store)


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


def _seed_approved_prior(graph: _ChainGraph) -> None:
    graph.policies[_PRIOR_ID] = _Policy(
        "Data Protection Policy", "approved", "1", _AUTHOR, _ISSUER, {"std_1": ("Std", "approved")}
    )
    graph.governed[_CAP_ID] = _PRIOR_ID


def test_fork_create_propose_approve_then_detection_finds_the_new_governed_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-007: the second run no longer re-reports "no governing Policy"."""
    configure()
    graph = _ChainGraph()
    _seed_approved_prior(graph)
    _install(monkeypatch, graph)

    assert _detect() == [[_PRIOR_ID, "approved", None, None]]

    with _actor(_AUTHOR):
        fork = _call(
            "create-policy-draft",
            {"title": "Data Protection Policy v2", "supersedes_policy_id": _PRIOR_ID},
        )
    fork_id = cast("str", fork["policy_id"])
    # Edges stay on the prior while the fork is a draft: skill resumes it.
    assert graph.governed == {_CAP_ID: _PRIOR_ID}
    assert _detect() == [[_PRIOR_ID, "approved", fork_id, "draft"]]

    with _actor(_AUTHOR):
        _call("propose-policy", {"policy_id": fork_id})
    assert _detect() == [[_PRIOR_ID, "approved", fork_id, "proposed"]]

    with _actor(_MANAGER):
        approved = _call("approve-policy", {"policy_id": fork_id})

    assert approved["governed_capability_ids"] == [_CAP_ID]
    assert graph.governed == {_CAP_ID: fork_id}
    # Detection now names the approved successor as governor -- not "no governing Policy".
    assert _detect() == [[fork_id, "approved", None, None]]
    assert graph.policies[_PRIOR_ID].status == "deprecated"
    assert graph.unmatched == []


def test_fresh_create_with_capability_ids_is_found_by_detection_as_a_governing_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    graph = _ChainGraph()
    _install(monkeypatch, graph)
    assert _detect() == []

    with _actor(_AUTHOR):
        created = _call(
            "create-policy-draft",
            {"title": "Data Protection Policy", "capability_ids": [_CAP_ID]},
        )

    policy_id = cast("str", created["policy_id"])
    # Second run takes the existing `status == "draft"` GOVERNED_BY branch (resume).
    assert _detect() == [[policy_id, "draft", None, None]]
    assert graph.unmatched == []
