"""Tests for the registered `create-policy-draft` MCP tool (issue #134, S12).

`_verified_actor` is a local copy of `test_access_role_tools.py`'s own
helper (issue #131's originating pattern) -- binds a real `AccessToken`
onto the MCP SDK's own auth contextvar that `get_access_token()`
(`_resolve_principal`/`_resolve_policy_lifecycle_actor`) reads.
`_install_graph` mirrors `test_cypher_tool.py`'s own
`connect_from_config`-monkeypatch pattern for installing a fake FalkorDB
graph handle; `_install_audit_store` mirrors `test_access_role_tools.py`'s
own `_fake_store_factory` pattern, monkeypatched onto
`mcp_server.PsycopgAuditStore` instead of `PsycopgAccessRoleStore`. This
tool is the first policy-lifecycle one to need both a fake graph handle and
a fake `AuditStore` in the same call.

Real FalkorDB/Postgres are not reachable in this sandbox -- every test here
is hermetic, against `_FakeGraph`/`_FakeAuditStore`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

import pytest
import redis.exceptions
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package (has `__init__.py`); mirrors `test_access_role_tools.py`'s own cross-package import convention
    FakeAccessRoleStore,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from ps_service.authz.models import AccessRole
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping
    from pathlib import Path

    type ReadLines = Callable[[Path], list[dict[str, object]]]

_ACTOR_SUBJECT = "policy-author"
_ACTOR_ISSUER = "https://issuer.example.com/"
_TITLE = "Data Protection Policy"
_EXISTING_POLICY_ID = "pol_data_protection_policy_aaaaaa"


@contextlib.contextmanager
def _verified_actor(*, sub: str, iss: str = _ACTOR_ISSUER) -> Generator[None]:
    access_token = AccessToken(
        token="test-token", client_id="test-client", scopes=[], subject=sub, claims={"iss": iss}
    )
    token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        yield
    finally:
        auth_context_var.reset(token)


@dataclass
class _FakeQueryResult:
    result_set: list[object] = field(default_factory=list)


class _GraphHandleDouble(Protocol):
    """Structural shape both `_FakeGraph` (write-flow tests) and `_TreeFakeGraph`
    (`get-policy`'s read-flow tests, added S14) satisfy -- lets `_install_graph`/
    `_FakeFalkorDB` accept either without a shared base class.
    """

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult: ...


class _FakeGraph:
    """Fake FalkorDB graph handle: existence-check read, plus recorded writes."""

    def __init__(self, *, existing: tuple[str, str] | None = None) -> None:
        self._existing = existing
        self.write_queries: list[str] = []
        self.raise_on_write: Exception | None = None

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        del params, timeout
        if "RETURN p.id, p.title" in q:
            rows: list[object] = [[self._existing[0], self._existing[1]]] if self._existing else []
            return _FakeQueryResult(result_set=rows)
        self.write_queries.append(q)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


class _FakeFalkorDB:
    """Stands in for the eager `falkordb.FalkorDB` client (mirrors `test_cypher_tool.py`)."""

    def __init__(self, handle: _GraphHandleDouble) -> None:
        self._handle = handle

    def select_graph(self, name: str) -> _GraphHandleDouble:
        del name
        return self._handle


def _install_graph(monkeypatch: pytest.MonkeyPatch, handle: _GraphHandleDouble) -> None:
    def _connect_from_config(_config: object) -> _FakeFalkorDB:
        return _FakeFalkorDB(handle)

    monkeypatch.setattr(mcp_server, "connect_from_config", _connect_from_config)


@dataclass
class _RecordedAuditCall:
    actor_subject: str
    actor_issuer: str
    action: str
    resource_id: str
    outcome: str
    details: Mapping[str, object]


class _FakeAuditStore:
    """Records every `record_standalone` call -- this tool never calls `record`/`query`."""

    def __init__(self) -> None:
        self.calls: list[_RecordedAuditCall] = []

    def record(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError

    def record_standalone(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        details: Mapping[str, object],
    ) -> None:
        del resource_type
        self.calls.append(
            _RecordedAuditCall(
                actor_subject=actor_subject,
                actor_issuer=actor_issuer,
                action=action,
                resource_id=resource_id,
                outcome=outcome,
                details=details,
            )
        )

    def query(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError


def _install_audit_store(monkeypatch: pytest.MonkeyPatch, store: _FakeAuditStore) -> None:
    def _factory(_config: object) -> _FakeAuditStore:
        return store

    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", _factory)


def _call_create_policy_draft(title: str = _TITLE) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("create-policy-draft", {"title": title}))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_tool_is_registered_and_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _FakeGraph())
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_create_policy_draft()

    assert result.is_error is False


def test_success_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _FakeGraph())
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_create_policy_draft()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {
        "policy_id": body["policy_id"],
        "title": _TITLE,
        "status": "draft",
        "version": "1",
        "owner_subject": _ACTOR_SUBJECT,
        "standard_ids": [],
        "control_ids": [],
        "superseded_policy_id": None,
    }
    assert body["policy_id"].startswith("pol_data_protection_policy_")


def test_duplicate_title_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _FakeGraph(existing=(_EXISTING_POLICY_ID, _TITLE)))
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_create_policy_draft()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "already exists" in text
    assert _EXISTING_POLICY_ID in text


def test_graph_unavailable_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    graph = _FakeGraph()
    graph.raise_on_write = redis.exceptions.ConnectionError("boom")
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_create_policy_draft()

    assert result.is_error is False
    text = _text(result)
    assert text == "error: the policy graph database is not reachable"
    # Distinct from the duplicate-title error string above.
    assert "already exists" not in text


def test_without_a_real_authenticated_caller_and_no_bypass_is_refused() -> None:
    configure()

    result = _call_create_policy_draft()

    assert result.is_error is False
    assert _text(result) == (
        "error: this action requires a real authenticated caller "
        "(the local-test bypass counts as one)"
    )


def test_bypass_active_creates_a_policy_owned_by_the_bypass_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-018 / D-11: under `PS_SERVICE_LOCAL_TEST_BYPASS=true`, with no
    verified bearer token bound at all, the created Policy's owner is
    `(LOCAL_TEST_PRINCIPAL_ID, LOCAL_TEST_PRINCIPAL_ID)` -- CHANGES.md
    finding 2's real bypass-toggling pattern (`test_main.py:626`/`:2004`),
    not the stale `test_main.py:358-387` range PLAN.md itself cites.
    """
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    audit_store = _FakeAuditStore()
    _install_graph(monkeypatch, _FakeGraph())
    _install_audit_store(monkeypatch, audit_store)

    result = _call_create_policy_draft()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["owner_subject"] == LOCAL_TEST_PRINCIPAL_ID
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].actor_subject == LOCAL_TEST_PRINCIPAL_ID
    assert audit_store.calls[0].actor_issuer == LOCAL_TEST_PRINCIPAL_ID


# --- `create-policy-draft` `standards` argument (issue #134, S12B gap fix) --


def test_create_policy_draft_without_standards_argument_still_works_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward compatibility: omitting `standards` entirely still produces the
    exact same title-only, zero-Standard draft this tool always produced.
    """
    configure()
    _install_graph(monkeypatch, _FakeGraph())
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = asyncio.run(mcp_server.server.call_tool("create-policy-draft", {"title": _TITLE}))
    assert isinstance(result, CallToolResult)

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {
        "policy_id": body["policy_id"],
        "title": _TITLE,
        "status": "draft",
        "version": "1",
        "owner_subject": _ACTOR_SUBJECT,
        "standard_ids": [],
        "control_ids": [],
        "superseded_policy_id": None,
    }


def test_create_policy_draft_with_malformed_standards_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _FakeGraph())
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = asyncio.run(
            mcp_server.server.call_tool(
                "create-policy-draft",
                {"title": _TITLE, "standards": [{"controls": []}]},
            )
        )
    assert isinstance(result, CallToolResult)

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "standards[0].title" in text


@dataclass
class _E2EControlState:
    title: str
    status: str
    control_type: str


@dataclass
class _E2EStandardState:
    title: str
    status: str
    controls: dict[str, _E2EControlState] = field(default_factory=dict)


@dataclass
class _E2EPolicyState:
    title: str
    status: str
    version: str
    owner_subject: str
    owner_issuer: str
    standards: dict[str, _E2EStandardState] = field(default_factory=dict)


class _StatefulFakeGraph:
    """Minimal in-memory FalkorDB double that actually persists across calls.

    Unlike `_FakeGraph` (write-only recorder) and `_TreeFakeGraph` (read-only
    canned fixture), this fake implements just enough of `graph_writer`'s own
    Cypher shapes -- existence check, Policy/Standard/Control write, tree
    read, cascade write -- to prove a real create -> get -> propose round
    trip end to end, matched on each query's own distinguishing exact text
    or substring. Every other query (`backfill_governance_status`'s three
    `SET ... IS NULL` statements) is a no-op, safe here because every node
    this fake ever creates already has a non-`NULL` `status`/`version`.
    """

    def __init__(self) -> None:
        self._policies: dict[str, _E2EPolicyState] = {}

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        del timeout
        p = params or {}
        if q.strip() == "MATCH (p:Policy {id: $policy_id}) RETURN p.id, p.title":
            policy_id = cast("str", p["policy_id"])
            policy = self._policies.get(policy_id)
            if policy is None:
                return _FakeQueryResult(result_set=[])
            return _FakeQueryResult(result_set=[[policy_id, policy.title]])
        if "MERGE (p:Policy {id: $policy_id}) SET p += $properties" in q:
            props = cast("dict[str, object]", p["properties"])
            self._policies[cast("str", p["policy_id"])] = _E2EPolicyState(
                title=cast("str", props["title"]),
                status=cast("str", props["status"]),
                version=cast("str", props["version"]),
                owner_subject=cast("str", props["owner_subject"]),
                owner_issuer=cast("str", props["owner_issuer"]),
            )
            return _FakeQueryResult()
        if "RETURN s.id, properties(s), c.id, properties(c)" in q:
            # `read_policy_tree_for_fork` (issue #136, Slice 6) -- checked
            # BEFORE the `find_standard_with_parent`/`SUPPORTED_BY]->
            # (s:Standard {id: $standard_id})` branches below, own
            # distinguishing `RETURN` clause, no substring overlap.
            policy_id = cast("str", p["policy_id"])
            policy = self._policies.get(policy_id)
            if policy is None or not policy.standards:
                return _FakeQueryResult(result_set=[])
            fork_rows: list[object] = []
            for standard_id, standard in policy.standards.items():
                standard_props: dict[str, object] = {
                    "id": standard_id,
                    "title": standard.title,
                    "status": standard.status,
                }
                if not standard.controls:
                    fork_rows.append([standard_id, standard_props, None, None])
                    continue
                for control_id, control in standard.controls.items():
                    control_props: dict[str, object] = {
                        "id": control_id,
                        "title": control.title,
                        "status": control.status,
                        "type": control.control_type,
                    }
                    fork_rows.append([standard_id, standard_props, control_id, control_props])
            return _FakeQueryResult(result_set=fork_rows)
        if "RETURN p.id, p.owner_subject, p.owner_issuer, p.status, s.status, s.title" in q:
            # `find_standard_with_parent` (issue #136, Slice 3) -- checked BEFORE the
            # write-shaped branch below, since this read query's own text also
            # contains that branch's `SUPPORTED_BY]->(s:Standard {id: $standard_id})`
            # matching substring.
            target_standard_id = cast("str", p["standard_id"])
            for policy_id, policy in self._policies.items():
                standard = policy.standards.get(target_standard_id)
                if standard is not None:
                    return _FakeQueryResult(
                        result_set=[
                            [
                                policy_id,
                                policy.owner_subject,
                                policy.owner_issuer,
                                policy.status,
                                standard.status,
                                standard.title,
                            ]
                        ]
                    )
            return _FakeQueryResult(result_set=[])
        if "SUPPORTED_BY]->(s:Standard {id: $standard_id})" in q:
            props = cast("dict[str, object]", p["properties"])
            policy = self._policies[cast("str", p["policy_id"])]
            policy.standards[cast("str", p["standard_id"])] = _E2EStandardState(
                title=cast("str", props["title"]), status=cast("str", props["status"])
            )
            return _FakeQueryResult()
        if "RETURN s.id, p.id, p.owner_subject, p.owner_issuer, p.status, c.status, c.title" in q:
            # `find_control_with_parent` (issue #136, Slice 5) -- checked
            # BEFORE the write-shaped branch below, since this read query's
            # own text also contains that branch's
            # `IMPLEMENTED_BY]->(c:Control {id: $control_id})` matching
            # substring (the same routing-collision risk Slice 3 flagged for
            # `find_standard_with_parent`, one hop deeper).
            target_control_id = cast("str", p["control_id"])
            for policy_id, policy in self._policies.items():
                for standard_id, standard in policy.standards.items():
                    control = standard.controls.get(target_control_id)
                    if control is not None:
                        return _FakeQueryResult(
                            result_set=[
                                [
                                    standard_id,
                                    policy_id,
                                    policy.owner_subject,
                                    policy.owner_issuer,
                                    policy.status,
                                    control.status,
                                    control.title,
                                ]
                            ]
                        )
            return _FakeQueryResult(result_set=[])
        if "IMPLEMENTED_BY]->(c:Control {id: $control_id})" in q:
            props = cast("dict[str, object]", p["properties"])
            standard = self._find_standard(cast("str", p["standard_id"]))
            standard.controls[cast("str", p["control_id"])] = _E2EControlState(
                title=cast("str", props["title"]),
                status=cast("str", props["status"]),
                control_type=cast("str", props["type"]),
            )
            return _FakeQueryResult()
        if "s.id, s.title, s.status, c.id" in q:
            policy_id = cast("str", p["policy_id"])
            policy = self._policies.get(policy_id)
            if policy is None:
                return _FakeQueryResult(result_set=[])
            return _FakeQueryResult(result_set=self._tree_rows(policy_id, policy))
        if "SET p.status = $target_status" in q:
            policy = self._policies[cast("str", p["policy_id"])]
            target = cast("str", p["target_status"])
            policy.status = target
            for standard in policy.standards.values():
                standard.status = target
                for control in standard.controls.values():
                    control.status = target
            return _FakeQueryResult()
        return _FakeQueryResult()

    def _find_standard(self, standard_id: str) -> _E2EStandardState:
        for policy in self._policies.values():
            if standard_id in policy.standards:
                return policy.standards[standard_id]
        raise AssertionError(f"no standard {standard_id}")

    def _tree_rows(self, policy_id: str, policy: _E2EPolicyState) -> list[object]:
        head = [
            policy_id,
            policy.title,
            policy.status,
            policy.version,
            policy.owner_subject,
            policy.owner_issuer,
        ]
        if not policy.standards:
            return [[*head, None, None, None, None, None, None, None]]
        rows: list[object] = []
        for standard_id, standard in policy.standards.items():
            standard_head = [standard_id, standard.title, standard.status]
            if not standard.controls:
                rows.append([*head, *standard_head, None, None, None, None])
                continue
            for control_id, control in standard.controls.items():
                rows.append(
                    [
                        *head,
                        *standard_head,
                        control_id,
                        control.title,
                        control.status,
                        control.control_type,
                    ]
                )
        return rows


def test_create_policy_draft_with_standards_creates_full_tree_all_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real gap this slice closes: a Policy created via the MCP tool
    surface can now have Standard/Control children attached at creation,
    verified by reading it straight back via `get-policy`.
    """
    configure()
    graph = _StatefulFakeGraph()
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        create_result = asyncio.run(
            mcp_server.server.call_tool(
                "create-policy-draft",
                {
                    "title": _TITLE,
                    "standards": [
                        {
                            "title": "Encryption Standard",
                            "controls": [
                                {"title": "Key Rotation", "control_type": "automated"},
                                {"title": "Access Review"},
                            ],
                        },
                        {"title": "Logging Standard"},
                    ],
                },
            )
        )
        assert isinstance(create_result, CallToolResult)
        assert create_result.is_error is False
        created = json.loads(_text(create_result))
        policy_id = created["policy_id"]

        get_result = _call_get_policy(policy_id=policy_id)

    assert get_result.is_error is False
    body = json.loads(_text(get_result))
    standards_by_title = {s["title"]: s for s in body["standards"]}
    assert set(standards_by_title) == {"Encryption Standard", "Logging Standard"}

    encryption = standards_by_title["Encryption Standard"]
    assert encryption["status"] == "draft"
    controls_by_title = {c["title"]: c for c in encryption["controls"]}
    assert controls_by_title["Key Rotation"] == {
        "control_id": controls_by_title["Key Rotation"]["control_id"],
        "title": "Key Rotation",
        "control_type": "automated",
        "status": "draft",
    }
    assert controls_by_title["Access Review"]["control_type"] == "manual"
    assert controls_by_title["Access Review"]["status"] == "draft"

    logging_standard = standards_by_title["Logging Standard"]
    assert logging_standard["status"] == "draft"
    assert logging_standard["controls"] == []


def test_create_policy_draft_with_standards_then_propose_policy_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end proof the gap is closed: a Policy created via
    `create-policy-draft` WITH a Standard attached at creation now satisfies
    `propose-policy`'s >=1-Standard completeness gate (AC-BI-013), so
    AC-BI-003 (the owner proposes their own complete Draft) is exercisable
    through the tool surface alone -- previously impossible, since
    `create-policy-draft` could only ever produce a zero-Standard Policy.
    """
    configure()
    graph = _StatefulFakeGraph()
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        create_result = asyncio.run(
            mcp_server.server.call_tool(
                "create-policy-draft",
                {"title": _TITLE, "standards": [{"title": "Encryption Standard"}]},
            )
        )
        assert isinstance(create_result, CallToolResult)
        assert create_result.is_error is False
        policy_id = json.loads(_text(create_result))["policy_id"]

        propose_result = _call_propose_policy(policy_id=policy_id)

    assert propose_result.is_error is False
    propose_body = json.loads(_text(propose_result))
    assert propose_body["status"] == "proposed"


# --- `create-policy-draft`'s `supersedes_policy_id` fork (issue #136, Slice 6) --
#
# `_MANAGER_SUBJECT`/`_install_manager_role` (used below) are defined once,
# near the `approve-policy` tests further down this module -- reused here
# unchanged (Python resolves module-level names at call time, so their
# later position in the file is not an ordering problem).


def test_create_policy_draft_with_supersedes_policy_id_success_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005/010: a genuine round trip against the shared stateful fake
    graph -- create a Policy, propose/approve it, then fork a successor from
    it, proving the response carries the new Policy id and every forked
    child id, and (CHANGES.md Appendix D) that a returned child id is
    IMMEDIATELY usable in a following real `update-standard-draft` call.
    """
    configure()
    graph = _StatefulFakeGraph()
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        prior_result = asyncio.run(
            mcp_server.server.call_tool(
                "create-policy-draft",
                {"title": _TITLE, "standards": [{"title": "Encryption Standard"}]},
            )
        )
        assert isinstance(prior_result, CallToolResult)
        assert prior_result.is_error is False
        prior_id = json.loads(_text(prior_result))["policy_id"]

        propose_result = _call_propose_policy(policy_id=prior_id)
        assert propose_result.is_error is False

    with _verified_actor(sub=_MANAGER_SUBJECT):
        approve_result = _call_approve_policy(policy_id=prior_id)
        assert approve_result.is_error is False

    with _verified_actor(sub=_ACTOR_SUBJECT):
        fork_result = asyncio.run(
            mcp_server.server.call_tool(
                "create-policy-draft",
                {"title": "Data Protection Policy v2", "supersedes_policy_id": prior_id},
            )
        )
        assert isinstance(fork_result, CallToolResult)
        assert fork_result.is_error is False
        body = json.loads(_text(fork_result))
        assert body["policy_id"] != prior_id
        assert body["status"] == "draft"
        assert body["version"] == "2"
        assert body["superseded_policy_id"] == prior_id
        assert len(body["standard_ids"]) == 1
        new_standard_id = body["standard_ids"][0]
        assert new_standard_id != "std_encryption_standard_" + prior_id[len("pol_") :]

        # AC-BI-010's own "usable immediately" proof: a real second call.
        update_result = _call_update_standard_draft(
            standard_id=new_standard_id, fields={"description": "revised"}
        )

    assert update_result.is_error is False


def test_create_policy_draft_supersedes_missing_prior_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _TreeFakeGraph(None))
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = asyncio.run(
            mcp_server.server.call_tool(
                "create-policy-draft",
                {"title": _TITLE, "supersedes_policy_id": "pol_missing"},
            )
        )
    assert isinstance(result, CallToolResult)

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no Policy exists" in text


def test_create_policy_draft_supersedes_non_approved_prior_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _TreeFakeGraph(_draft_fixture()))
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = asyncio.run(
            mcp_server.server.call_tool(
                "create-policy-draft",
                {"title": _TITLE, "supersedes_policy_id": _EXISTING_POLICY_ID},
            )
        )
    assert isinstance(result, CallToolResult)

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "cannot be superseded" in text
    assert "'draft'" in text


def test_create_policy_draft_supersede_log_triad_carries_entity_id(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """CHANGES.md finding #9 (Appendix G): the fork path logs
    `entity_id=supersedes_policy_id` (the prior, known before `body()`
    returns, unlike the not-yet-minted successor id) -- proven for both a
    successful fork and a deliberate `PolicySupersedePriorNotApprovedError`
    failure, both against the SAME prior id.
    """
    emitter = configure()
    _install_audit_store(monkeypatch, _FakeAuditStore())

    approved_prior = _PolicyFixture(
        id=_EXISTING_POLICY_ID,
        title=_TITLE,
        status="approved",
        version="1",
        owner_subject=_ACTOR_SUBJECT,
        owner_issuer=_ACTOR_ISSUER,
    )

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_graph(monkeypatch, _TreeFakeGraph(approved_prior))
        success_result = asyncio.run(
            mcp_server.server.call_tool(
                "create-policy-draft",
                {
                    "title": "Data Protection Policy v2",
                    "supersedes_policy_id": _EXISTING_POLICY_ID,
                },
            )
        )

        _install_graph(monkeypatch, _TreeFakeGraph(_draft_fixture()))
        failure_result = asyncio.run(
            mcp_server.server.call_tool(
                "create-policy-draft",
                {"title": _TITLE, "supersedes_policy_id": _EXISTING_POLICY_ID},
            )
        )

    assert isinstance(success_result, CallToolResult)
    assert isinstance(failure_result, CallToolResult)
    assert success_result.is_error is False
    assert _text(failure_result).startswith("error: ")

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    action_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "create_policy_draft"
    ]
    entity_lines = [line for line in action_lines if line.get("entity_id") == _EXISTING_POLICY_ID]

    assert {line["outcome"] for line in entity_lines} == {"started", "succeeded", "failed"}
    for line in entity_lines:
        assert line["principal"] == _ACTOR_SUBJECT
        assert isinstance(line["timestamp"], float)


# --- `get-policy` (issue #134, S14) -----------------------------------------

_OTHER_SUBJECT = "someone-else"
_GRANTER = ("system-admin-tool", "iss")


@dataclass
class _ControlFixture:
    id: str
    title: str
    status: str
    control_type: str


@dataclass
class _StandardFixture:
    id: str
    title: str
    status: str
    controls: tuple[_ControlFixture, ...] = field(default_factory=tuple)


@dataclass
class _PolicyFixture:
    id: str
    title: str
    status: str
    version: str
    owner_subject: str
    owner_issuer: str
    standards: tuple[_StandardFixture, ...] = field(default_factory=tuple)


class _TreeFakeGraph:
    """Fake FalkorDB graph handle for `get-policy`'s tests.

    Answers `graph_writer.read_policy_tree`'s single `RETURN` query with
    canned rows built from `_policy` (matched by a distinguishing column-list
    substring); every other query (i.e. each of
    `backfill_governance_status`'s three `SET` statements) is a no-op, since
    these tests always seed already-backfilled fixtures.
    """

    def __init__(self, policy: _PolicyFixture | None) -> None:
        self._policy = policy

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        del params, timeout
        if "s.id, s.title, s.status, c.id" not in q:
            return _FakeQueryResult()
        if self._policy is None:
            return _FakeQueryResult(result_set=[])
        return _FakeQueryResult(result_set=self._rows())

    def _rows(self) -> list[object]:
        policy = self._policy
        assert policy is not None
        head = [
            policy.id,
            policy.title,
            policy.status,
            policy.version,
            policy.owner_subject,
            policy.owner_issuer,
        ]
        if not policy.standards:
            return [[*head, None, None, None, None, None, None, None]]
        rows: list[object] = []
        for standard in policy.standards:
            standard_head = [standard.id, standard.title, standard.status]
            if not standard.controls:
                rows.append([*head, *standard_head, None, None, None, None])
                continue
            for control in standard.controls:
                rows.append(
                    [
                        *head,
                        *standard_head,
                        control.id,
                        control.title,
                        control.status,
                        control.control_type,
                    ]
                )
        return rows


def _draft_fixture(*, owner_subject: str = _ACTOR_SUBJECT) -> _PolicyFixture:
    return _PolicyFixture(
        id=_EXISTING_POLICY_ID,
        title=_TITLE,
        status="draft",
        version="1",
        owner_subject=owner_subject,
        owner_issuer=_ACTOR_ISSUER,
    )


def _install_access_role_store(monkeypatch: pytest.MonkeyPatch, store: object) -> None:
    def _factory(_config: object, **_kwargs: object) -> object:
        return store

    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _factory)


def _call_get_policy(policy_id: str = _EXISTING_POLICY_ID) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("get-policy", {"policy_id": policy_id}))
    assert isinstance(result, CallToolResult)
    return result


def test_get_policy_owner_reads_own_draft_successfully(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _TreeFakeGraph(_draft_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_get_policy()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["policy_id"] == _EXISTING_POLICY_ID
    assert body["status"] == "draft"
    assert body["owner_subject"] == _ACTOR_SUBJECT
    assert body["standards"] == []


def test_get_policy_system_owner_reads_anyones_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _TreeFakeGraph(_draft_fixture()))
    store = FakeAccessRoleStore()
    store.grant(
        actor=_GRANTER, target=(_OTHER_SUBJECT, _ACTOR_ISSUER), access_role=AccessRole.SYSTEM_OWNER
    )
    _install_access_role_store(monkeypatch, store)

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_get_policy()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["policy_id"] == _EXISTING_POLICY_ID


def test_get_policy_system_admin_reads_anyones_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _TreeFakeGraph(_draft_fixture()))
    store = FakeAccessRoleStore()
    store.grant(
        actor=_GRANTER, target=(_OTHER_SUBJECT, _ACTOR_ISSUER), access_role=AccessRole.SYSTEM_ADMIN
    )
    _install_access_role_store(monkeypatch, store)

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_get_policy()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["policy_id"] == _EXISTING_POLICY_ID


def test_get_policy_non_owner_policy_manager_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _TreeFakeGraph(_draft_fixture()))
    store = FakeAccessRoleStore()
    store.grant(
        actor=_GRANTER,
        target=(_OTHER_SUBJECT, _ACTOR_ISSUER),
        access_role=AccessRole.POLICY_MANAGER,
    )
    _install_access_role_store(monkeypatch, store)

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_get_policy()

    assert result.is_error is False
    assert _text(result) == "error: you do not have access to this Policy"


def test_get_policy_non_owner_authenticated_user_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _TreeFakeGraph(_draft_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_get_policy()

    assert result.is_error is False
    assert _text(result) == "error: you do not have access to this Policy"


@pytest.mark.parametrize("status", ["proposed", "approved", "deprecated"])
def test_get_policy_non_draft_policy_readable_by_any_authenticated_caller(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    configure()
    policy = _PolicyFixture(
        id=_EXISTING_POLICY_ID,
        title=_TITLE,
        status=status,
        version="1",
        owner_subject=_ACTOR_SUBJECT,
        owner_issuer=_ACTOR_ISSUER,
    )
    _install_graph(monkeypatch, _TreeFakeGraph(policy))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_get_policy()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["status"] == status


def test_get_policy_nonexistent_policy_returns_not_found_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _TreeFakeGraph(None))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_get_policy(policy_id="pol_missing")

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no Policy exists" in text


def test_get_policy_without_a_real_authenticated_caller_and_no_bypass_is_refused() -> None:
    configure()

    result = _call_get_policy()

    assert result.is_error is False
    assert _text(result) == (
        "error: this action requires a real authenticated caller "
        "(the local-test bypass counts as one)"
    )


def test_get_policy_with_standards_and_controls_returns_full_nested_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _PolicyFixture(
        id=_EXISTING_POLICY_ID,
        title=_TITLE,
        status="draft",
        version="1",
        owner_subject=_ACTOR_SUBJECT,
        owner_issuer=_ACTOR_ISSUER,
        standards=(
            _StandardFixture(
                id="std_1",
                title="Encryption Standard",
                status="draft",
                controls=(
                    _ControlFixture(
                        id="ctrl_1", title="Key Rotation", status="draft", control_type="automated"
                    ),
                ),
            ),
        ),
    )
    configure()
    _install_graph(monkeypatch, _TreeFakeGraph(policy))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_get_policy()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["standards"] == [
        {
            "standard_id": "std_1",
            "title": "Encryption Standard",
            "status": "draft",
            "controls": [
                {
                    "control_id": "ctrl_1",
                    "title": "Key Rotation",
                    "control_type": "automated",
                    "status": "draft",
                }
            ],
        }
    ]


# --- `update-policy-draft` (issue #136, Slice 1) ----------------------------


class _UpdatePolicyDraftFakeGraph(_TreeFakeGraph):
    """Extends `_TreeFakeGraph` with `update_policy_fields`'s own writes.

    Every other query (the tree read, each of `backfill_governance_status`'s
    three `SET` statements) is handled by `_TreeFakeGraph.query` unchanged;
    only `update_policy_fields`'s own two distinguishing shapes
    (`$set_properties` map-merge, `= null` single-field clear) are
    intercepted here.
    """

    def __init__(self, policy: _PolicyFixture | None) -> None:
        super().__init__(policy)
        self.write_queries: list[str] = []
        self.write_params: list[dict[str, object] | None] = []
        self.raise_on_write: Exception | None = None

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        if "$set_properties" not in q and "= null" not in q:
            return super().query(q, params, timeout)
        self.write_queries.append(q)
        self.write_params.append(params)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def _call_update_policy_draft(
    *, policy_id: str = _EXISTING_POLICY_ID, fields: dict[str, object] | None = None
) -> CallToolResult:
    args: dict[str, object] = {"policy_id": policy_id}
    if fields is not None:
        args["fields"] = fields
    result = asyncio.run(mcp_server.server.call_tool("update-policy-draft", args))
    assert isinstance(result, CallToolResult)
    return result


def test_update_policy_draft_success_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _UpdatePolicyDraftFakeGraph(_draft_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_policy_draft(fields={"description": "revised text"})

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {"policy_id": _EXISTING_POLICY_ID, "updated_fields": ["description"]}


def test_update_policy_draft_applies_only_supplied_fields_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-008, proven as a genuine round trip: create -> update -> get,
    against the same stateful fake graph, showing the patched field
    actually persists and only the supplied field changed.
    """
    configure()
    graph = _StatefulFakeGraph()
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        create_result = _call_create_policy_draft()
        policy_id = json.loads(_text(create_result))["policy_id"]

        update_result = _call_update_policy_draft(
            policy_id=policy_id, fields={"description": "revised text"}
        )
        get_result = _call_get_policy(policy_id=policy_id)

    assert update_result.is_error is False
    assert json.loads(_text(update_result)) == {
        "policy_id": policy_id,
        "updated_fields": ["description"],
    }
    assert get_result.is_error is False
    # `get-policy`'s own response shape (S13) never echoes back arbitrary
    # content fields like `description` -- the round trip proves the write
    # landed by confirming the Policy is still readable, still owned by the
    # same actor, and still `draft` (i.e. the PATCH didn't corrupt anything
    # else in the process).
    body = json.loads(_text(get_result))
    assert body["policy_id"] == policy_id
    assert body["status"] == "draft"
    assert body["owner_subject"] == _ACTOR_SUBJECT


def test_update_policy_draft_unknown_field_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _UpdatePolicyDraftFakeGraph(_draft_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_policy_draft(fields={"title": "sneaky rename"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "not a patchable field" in text


def test_update_policy_draft_non_owner_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _UpdatePolicyDraftFakeGraph(_draft_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_update_policy_draft(fields={"description": "hijacked"})

    assert result.is_error is False
    assert _text(result) == "error: you do not have access to this Policy"


def test_update_policy_draft_non_draft_status_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _PolicyFixture(
        id=_EXISTING_POLICY_ID,
        title=_TITLE,
        status="proposed",
        version="1",
        owner_subject=_ACTOR_SUBJECT,
        owner_issuer=_ACTOR_ISSUER,
    )
    configure()
    _install_graph(monkeypatch, _UpdatePolicyDraftFakeGraph(policy))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_policy_draft(fields={"description": "too late"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "cannot edit a Policy in status 'proposed'" in text


def test_update_policy_draft_missing_policy_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _UpdatePolicyDraftFakeGraph(None))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_policy_draft(policy_id="pol_missing", fields={"description": "x"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no Policy exists" in text


def test_update_policy_draft_graph_unavailable_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _UpdatePolicyDraftFakeGraph(_draft_fixture())
    graph.raise_on_write = redis.exceptions.ConnectionError("boom")
    _install_graph(monkeypatch, graph)
    configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_policy_draft(fields={"description": "x"})

    assert result.is_error is False
    text = _text(result)
    assert text == "error: the policy graph database is not reachable"
    assert "not a patchable field" not in text


def test_update_policy_draft_log_triad_carries_actor_action_node_id_and_timestamp(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """CHANGES.md finding #9: `_run_mcp_action`'s log triad now threads
    `entity_id` -- proven here for both a success call and a deliberate
    `PolicyNotFoundError` failure, plus the `started` entry every call emits.
    """
    emitter = configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_graph(monkeypatch, _UpdatePolicyDraftFakeGraph(_draft_fixture()))
        success_result = _call_update_policy_draft(fields={"description": "x"})

        _install_graph(monkeypatch, _UpdatePolicyDraftFakeGraph(None))
        failure_result = _call_update_policy_draft(
            policy_id="pol_missing", fields={"description": "x"}
        )

    assert success_result.is_error is False
    assert _text(failure_result).startswith("error: ")

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    action_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "update_policy_draft"
    ]
    # 3 for the success call (started/succeeded), 2 for the not-found
    # failure (started/failed) -- but since the failure case's `policy_id`
    # ("pol_missing") differs from the success case's, filter to each.
    success_lines = [line for line in action_lines if line.get("entity_id") == _EXISTING_POLICY_ID]
    failure_lines = [line for line in action_lines if line.get("entity_id") == "pol_missing"]

    assert {line["outcome"] for line in success_lines} == {"started", "succeeded"}
    assert {line["outcome"] for line in failure_lines} == {"started", "failed"}
    for line in (*success_lines, *failure_lines):
        assert line["principal"] == _ACTOR_SUBJECT
        assert isinstance(line["timestamp"], float)


# --- `add-standard-to-draft` (issue #136, Slice 2) --------------------------


class _AddStandardToDraftFakeGraph(_TreeFakeGraph):
    """Extends `_TreeFakeGraph` with `add_standard_to_policy`'s own write.

    Every other query (the tree read, each of `backfill_governance_status`'s
    three `SET` statements) is handled by `_TreeFakeGraph.query` unchanged;
    only `add_standard_to_policy`'s own distinguishing `MERGE ... Standard`
    shape is intercepted here.
    """

    def __init__(self, policy: _PolicyFixture | None) -> None:
        super().__init__(policy)
        self.write_queries: list[str] = []
        self.write_params: list[dict[str, object] | None] = []
        self.raise_on_write: Exception | None = None

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        if "SUPPORTED_BY]->(s:Standard {id: $standard_id})" not in q:
            return super().query(q, params, timeout)
        self.write_queries.append(q)
        self.write_params.append(params)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def _call_add_standard_to_draft(
    *,
    policy_id: str = _EXISTING_POLICY_ID,
    title: str = "Encryption Standard",
    fields: dict[str, object] | None = None,
) -> CallToolResult:
    args: dict[str, object] = {"policy_id": policy_id, "title": title}
    if fields is not None:
        args["fields"] = fields
    result = asyncio.run(mcp_server.server.call_tool("add-standard-to-draft", args))
    assert isinstance(result, CallToolResult)
    return result


def test_add_standard_to_draft_success_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _AddStandardToDraftFakeGraph(_draft_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_standard_to_draft()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {
        "standard_id": body["standard_id"],
        "policy_id": _EXISTING_POLICY_ID,
        "title": "Encryption Standard",
        "status": "draft",
    }
    assert body["standard_id"].startswith("std_encryption_standard_")


def test_add_standard_to_draft_response_id_usable_in_get_policy_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-009, proven as a genuine round trip: create Policy -> add Standard
    -> get-policy, against the same stateful fake graph, showing the new
    Standard's id genuinely round-trips into a second, real call.

    `update-standard-draft` (the more direct AC-BI-009 chained call) is
    Slice 3's own tool, not yet implemented -- `get-policy` is the read this
    slice uses to prove usability instead, per the task's own guidance.
    """
    configure()
    graph = _StatefulFakeGraph()
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        create_result = _call_create_policy_draft()
        policy_id = json.loads(_text(create_result))["policy_id"]

        add_result = _call_add_standard_to_draft(policy_id=policy_id)
        assert add_result.is_error is False
        new_standard_id = json.loads(_text(add_result))["standard_id"]

        get_result = _call_get_policy(policy_id=policy_id)

    assert get_result.is_error is False
    body = json.loads(_text(get_result))
    standards_by_id = {s["standard_id"]: s for s in body["standards"]}
    assert new_standard_id in standards_by_id
    assert standards_by_id[new_standard_id]["title"] == "Encryption Standard"
    assert standards_by_id[new_standard_id]["status"] == "draft"


def test_add_standard_to_draft_unknown_field_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _AddStandardToDraftFakeGraph(_draft_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_standard_to_draft(fields={"title": "sneaky rename"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "not a patchable field" in text


def test_add_standard_to_draft_invalid_implementation_status_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _AddStandardToDraftFakeGraph(_draft_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_standard_to_draft(fields={"implementation_status": "bogus"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "must be one of" in text


def test_add_standard_to_draft_non_owner_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _AddStandardToDraftFakeGraph(_draft_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_add_standard_to_draft()

    assert result.is_error is False
    assert _text(result) == "error: you do not have access to this Policy"


def test_add_standard_to_draft_non_draft_status_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _PolicyFixture(
        id=_EXISTING_POLICY_ID,
        title=_TITLE,
        status="proposed",
        version="1",
        owner_subject=_ACTOR_SUBJECT,
        owner_issuer=_ACTOR_ISSUER,
    )
    configure()
    _install_graph(monkeypatch, _AddStandardToDraftFakeGraph(policy))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_standard_to_draft()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "cannot edit a Policy in status 'proposed'" in text


def test_add_standard_to_draft_missing_policy_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _AddStandardToDraftFakeGraph(None))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_standard_to_draft(policy_id="pol_missing")

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no Policy exists" in text


def test_add_standard_to_draft_graph_unavailable_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _AddStandardToDraftFakeGraph(_draft_fixture())
    graph.raise_on_write = redis.exceptions.ConnectionError("boom")
    _install_graph(monkeypatch, graph)
    configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_standard_to_draft()

    assert result.is_error is False
    text = _text(result)
    assert text == "error: the policy graph database is not reachable"
    assert "not a patchable field" not in text


def test_add_standard_to_draft_log_triad_carries_actor_action_node_id_and_timestamp(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """CHANGES.md finding #9: the log triad carries `entity_id=policy_id` --
    the parent, since it's known before the call, matching `add-standard-to-
    draft`'s own §1(Appendix G) rule (the new Standard's id isn't known until
    `body()` returns, and `_run_mcp_action`'s `entity_id` is fixed first).
    """
    emitter = configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_graph(monkeypatch, _AddStandardToDraftFakeGraph(_draft_fixture()))
        success_result = _call_add_standard_to_draft()

        _install_graph(monkeypatch, _AddStandardToDraftFakeGraph(None))
        failure_result = _call_add_standard_to_draft(policy_id="pol_missing")

    assert success_result.is_error is False
    assert _text(failure_result).startswith("error: ")

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    action_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface"
        and line.get("action") == "add_standard_to_draft"
    ]
    success_lines = [line for line in action_lines if line.get("entity_id") == _EXISTING_POLICY_ID]
    failure_lines = [line for line in action_lines if line.get("entity_id") == "pol_missing"]

    assert {line["outcome"] for line in success_lines} == {"started", "succeeded"}
    assert {line["outcome"] for line in failure_lines} == {"started", "failed"}
    for line in (*success_lines, *failure_lines):
        assert line["principal"] == _ACTOR_SUBJECT
        assert isinstance(line["timestamp"], float)


# --- `update-standard-draft` (issue #136, Slice 3) --------------------------

_EXISTING_STANDARD_ID = "std_encryption_standard_aaaaaa"


@dataclass
class _StandardParentFixture:
    policy_id: str
    owner_subject: str
    owner_issuer: str
    policy_status: str
    standard_status: str | None
    standard_title: str = "Encryption Standard"


class _UpdateStandardDraftFakeGraph:
    """Fake FalkorDB graph handle for `update-standard-draft`'s tests (issue #136, Slice 3).

    Mirrors `test_service.py`'s own `_StandardWithParentFakeGraph`: models one
    Standard's parent-Policy owner/status plus the Standard's own (possibly
    `None`, pre-backfill) status as a single mutable fixture, so the same
    instance answers `find_standard_with_parent`'s read both before and after
    `backfill_governance_status`'s own `s.status IS NULL` write actually
    mutates `standard_status` (CHANGES.md finding #2).
    """

    def __init__(
        self, fixture: _StandardParentFixture | None, *, raise_on_write: Exception | None = None
    ) -> None:
        self._fixture = fixture
        self.write_queries: list[str] = []
        self.write_params: list[dict[str, object] | None] = []
        self.raise_on_write = raise_on_write

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        del timeout
        fixture = self._fixture
        if "RETURN p.id, p.owner_subject, p.owner_issuer, p.status, s.status, s.title" in q:
            if fixture is None:
                return _FakeQueryResult(result_set=[])
            return _FakeQueryResult(
                result_set=[
                    [
                        fixture.policy_id,
                        fixture.owner_subject,
                        fixture.owner_issuer,
                        fixture.policy_status,
                        fixture.standard_status,
                        fixture.standard_title,
                    ]
                ]
            )
        if "s.status IS NULL" in q and "SET s.status = p.status" in q:
            if fixture is not None and fixture.standard_status is None:
                fixture.standard_status = fixture.policy_status
            return _FakeQueryResult()
        if "$set_properties" not in q and "= null" not in q:
            return _FakeQueryResult()
        self.write_queries.append(q)
        self.write_params.append(params)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def _standard_parent_fixture(
    *, owner_subject: str = _ACTOR_SUBJECT, status: str = "draft"
) -> _StandardParentFixture:
    return _StandardParentFixture(
        policy_id=_EXISTING_POLICY_ID,
        owner_subject=owner_subject,
        owner_issuer=_ACTOR_ISSUER,
        policy_status="draft",
        standard_status=status,
    )


def _call_update_standard_draft(
    *, standard_id: str = _EXISTING_STANDARD_ID, fields: dict[str, object] | None = None
) -> CallToolResult:
    args: dict[str, object] = {"standard_id": standard_id}
    if fields is not None:
        args["fields"] = fields
    result = asyncio.run(mcp_server.server.call_tool("update-standard-draft", args))
    assert isinstance(result, CallToolResult)
    return result


def test_update_standard_draft_success_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _UpdateStandardDraftFakeGraph(_standard_parent_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_standard_draft(fields={"description": "revised text"})

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {
        "standard_id": _EXISTING_STANDARD_ID,
        "policy_id": _EXISTING_POLICY_ID,
        "title": "Encryption Standard",
        "status": "draft",
    }


def test_update_standard_draft_response_id_usable_in_update_standard_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-009, real round trip (CHANGES.md Appendix D): create Policy ->
    add Standard -> update-standard-draft, against the same stateful fake
    graph, proving the new Standard's id genuinely round-trips into a
    second, real call -- not just a determinism check.
    """
    configure()
    graph = _StatefulFakeGraph()
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        create_result = _call_create_policy_draft()
        policy_id = json.loads(_text(create_result))["policy_id"]

        add_result = _call_add_standard_to_draft(policy_id=policy_id)
        assert add_result.is_error is False
        new_standard_id = json.loads(_text(add_result))["standard_id"]

        update_result = _call_update_standard_draft(
            standard_id=new_standard_id, fields={"description": "updated"}
        )

    assert update_result.is_error is False
    assert json.loads(_text(update_result)) == {
        "standard_id": new_standard_id,
        "policy_id": policy_id,
        "title": "Encryption Standard",
        "status": "draft",
    }


def test_update_standard_draft_unknown_field_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _UpdateStandardDraftFakeGraph(_standard_parent_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_standard_draft(fields={"title": "sneaky rename"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "not a patchable field" in text


def test_update_standard_draft_non_owner_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _UpdateStandardDraftFakeGraph(_standard_parent_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_update_standard_draft(fields={"description": "hijacked"})

    assert result.is_error is False
    assert _text(result) == "error: you do not have access to this Policy"


def test_update_standard_draft_non_draft_status_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(
        monkeypatch, _UpdateStandardDraftFakeGraph(_standard_parent_fixture(status="proposed"))
    )
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_standard_draft(fields={"description": "too late"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "cannot edit a Policy in status 'proposed'" in text


def test_update_standard_draft_missing_standard_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _UpdateStandardDraftFakeGraph(None))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_standard_draft(standard_id="std_missing")

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no Standard exists" in text


def test_update_standard_draft_missing_standard_returns_distinct_error_from_missing_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-012's 'distinct' requirement: a missing Standard's own error text
    differs from a missing Policy's (`update-policy-draft`'s own error).
    """
    configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_graph(monkeypatch, _UpdateStandardDraftFakeGraph(None))
        standard_result = _call_update_standard_draft(standard_id="std_missing")

        _install_graph(monkeypatch, _UpdatePolicyDraftFakeGraph(None))
        policy_result = _call_update_policy_draft(
            policy_id="pol_missing", fields={"description": "x"}
        )

    standard_text = _text(standard_result)
    policy_text = _text(policy_result)
    assert standard_text != policy_text
    assert "Standard" in standard_text
    assert "Policy" in policy_text


def test_update_standard_draft_graph_unavailable_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _UpdateStandardDraftFakeGraph(
        _standard_parent_fixture(), raise_on_write=redis.exceptions.ConnectionError("boom")
    )
    _install_graph(monkeypatch, graph)
    configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_standard_draft(fields={"description": "x"})

    assert result.is_error is False
    text = _text(result)
    assert text == "error: the policy graph database is not reachable"
    assert "not a patchable field" not in text


def test_update_standard_draft_log_triad_carries_actor_action_node_id_and_timestamp(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    emitter = configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_graph(monkeypatch, _UpdateStandardDraftFakeGraph(_standard_parent_fixture()))
        success_result = _call_update_standard_draft(fields={"description": "x"})

        _install_graph(monkeypatch, _UpdateStandardDraftFakeGraph(None))
        failure_result = _call_update_standard_draft(
            standard_id="std_missing", fields={"description": "x"}
        )

    assert success_result.is_error is False
    assert _text(failure_result).startswith("error: ")

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    action_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface"
        and line.get("action") == "update_standard_draft"
    ]
    success_lines = [
        line for line in action_lines if line.get("entity_id") == _EXISTING_STANDARD_ID
    ]
    failure_lines = [line for line in action_lines if line.get("entity_id") == "std_missing"]

    assert {line["outcome"] for line in success_lines} == {"started", "succeeded"}
    assert {line["outcome"] for line in failure_lines} == {"started", "failed"}
    for line in (*success_lines, *failure_lines):
        assert line["principal"] == _ACTOR_SUBJECT
        assert isinstance(line["timestamp"], float)


def test_update_standard_draft_backfills_legacy_null_status_before_gating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHANGES.md finding #2 (High): a legacy Standard with a `NULL` own
    `status` (e.g. minted before any backfilling call, such as via the
    internal-seed adapter) is still draft-eligible once backfilled by
    `_read_standard_with_parent_backfilled`, not spuriously rejected.
    """
    configure()
    fixture = _StandardParentFixture(
        policy_id=_EXISTING_POLICY_ID,
        owner_subject=_ACTOR_SUBJECT,
        owner_issuer=_ACTOR_ISSUER,
        policy_status="draft",
        standard_status=None,
    )
    _install_graph(monkeypatch, _UpdateStandardDraftFakeGraph(fixture))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_standard_draft(fields={"description": "now editable"})

    assert result.is_error is False
    assert fixture.standard_status == "draft"


# --- `add-control-to-draft` (issue #136, Slice 4) ----------------------------


class _AddControlToDraftFakeGraph:
    """Fake FalkorDB graph handle for `add-control-to-draft`'s tests (issue #136, Slice 4).

    Mirrors `_UpdateStandardDraftFakeGraph` exactly for the read/backfill
    half -- PLAN.md §1.4's own correction: `add-control-to-draft` shares
    `update-standard-draft`'s (Slice 3) one-hop `find_standard_with_parent`
    ownership shape, never a two-hop traversal (that belongs to
    `update-control-draft` alone, Slice 5). Adds `add_control_to_standard`'s
    own distinguishing `MERGE ... Control` write branch.
    """

    def __init__(
        self, fixture: _StandardParentFixture | None, *, raise_on_write: Exception | None = None
    ) -> None:
        self._fixture = fixture
        self.write_queries: list[str] = []
        self.write_params: list[dict[str, object] | None] = []
        self.raise_on_write = raise_on_write

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        del timeout
        fixture = self._fixture
        if "RETURN p.id, p.owner_subject, p.owner_issuer, p.status, s.status, s.title" in q:
            if fixture is None:
                return _FakeQueryResult(result_set=[])
            return _FakeQueryResult(
                result_set=[
                    [
                        fixture.policy_id,
                        fixture.owner_subject,
                        fixture.owner_issuer,
                        fixture.policy_status,
                        fixture.standard_status,
                        fixture.standard_title,
                    ]
                ]
            )
        if "s.status IS NULL" in q and "SET s.status = p.status" in q:
            if fixture is not None and fixture.standard_status is None:
                fixture.standard_status = fixture.policy_status
            return _FakeQueryResult()
        if "IMPLEMENTED_BY]->(c:Control {id: $control_id})" not in q:
            return _FakeQueryResult()
        self.write_queries.append(q)
        self.write_params.append(params)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def _call_add_control_to_draft(
    *,
    standard_id: str = _EXISTING_STANDARD_ID,
    title: str = "Key Rotation Check",
    control_type: str | None = None,
    fields: dict[str, object] | None = None,
) -> CallToolResult:
    args: dict[str, object] = {"standard_id": standard_id, "title": title}
    if control_type is not None:
        args["control_type"] = control_type
    if fields is not None:
        args["fields"] = fields
    result = asyncio.run(mcp_server.server.call_tool("add-control-to-draft", args))
    assert isinstance(result, CallToolResult)
    return result


def test_add_control_to_draft_success_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _AddControlToDraftFakeGraph(_standard_parent_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_control_to_draft()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {
        "control_id": body["control_id"],
        "standard_id": _EXISTING_STANDARD_ID,
        "policy_id": _EXISTING_POLICY_ID,
        "title": "Key Rotation Check",
        "status": "draft",
    }
    assert body["control_id"].startswith("ctrl_key_rotation_check_")


def test_add_control_to_draft_defaults_implementation_status_planned_not_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-007: the one deliberate divergence from `add-standard-to-draft`'s
    own `"draft"` default -- easy to copy-paste wrong.
    """
    configure()
    graph = _AddControlToDraftFakeGraph(_standard_parent_fixture())
    _install_graph(monkeypatch, graph)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_control_to_draft()

    assert result.is_error is False
    params = cast("dict[str, object]", graph.write_params[0])
    props = cast("dict[str, object]", params["properties"])
    assert props["implementation_status"] == "planned"
    assert props["status"] == "draft"


def test_add_control_to_draft_response_id_usable_in_get_policy_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-009-shaped, proven as a genuine round trip: create Policy -> add
    Standard -> add Control -> get-policy, against the same stateful fake
    graph, showing the new Control's id genuinely round-trips into a real
    call. `update-control-draft` (the more direct chained call) is Slice 5's
    own tool, not yet implemented -- `get-policy` is the read this slice uses
    instead, per the task's own guidance.
    """
    configure()
    graph = _StatefulFakeGraph()
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        create_result = _call_create_policy_draft()
        policy_id = json.loads(_text(create_result))["policy_id"]

        add_standard_result = _call_add_standard_to_draft(policy_id=policy_id)
        assert add_standard_result.is_error is False
        new_standard_id = json.loads(_text(add_standard_result))["standard_id"]

        add_control_result = _call_add_control_to_draft(standard_id=new_standard_id)
        assert add_control_result.is_error is False
        new_control_id = json.loads(_text(add_control_result))["control_id"]

        get_result = _call_get_policy(policy_id=policy_id)

    assert get_result.is_error is False
    body = json.loads(_text(get_result))
    standard = next(s for s in body["standards"] if s["standard_id"] == new_standard_id)
    controls_by_id = {c["control_id"]: c for c in standard["controls"]}
    assert new_control_id in controls_by_id
    assert controls_by_id[new_control_id]["title"] == "Key Rotation Check"
    assert controls_by_id[new_control_id]["status"] == "draft"


def test_add_control_to_draft_unknown_field_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _AddControlToDraftFakeGraph(_standard_parent_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_control_to_draft(fields={"title": "sneaky rename"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "not a patchable field" in text


def test_add_control_to_draft_fields_type_key_rejected_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHANGES.md finding #8: `fields.type` is rejected -- `control_type` is
    the single path to set a Control's type at creation.
    """
    configure()
    _install_graph(monkeypatch, _AddControlToDraftFakeGraph(_standard_parent_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_control_to_draft(fields={"type": "automated"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "not a patchable field" in text


def test_add_control_to_draft_invalid_implementation_status_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _AddControlToDraftFakeGraph(_standard_parent_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_control_to_draft(fields={"implementation_status": "bogus"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "must be one of" in text


def test_add_control_to_draft_invalid_control_type_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _AddControlToDraftFakeGraph(_standard_parent_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_control_to_draft(control_type="bogus")

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "control_type" in text


def test_add_control_to_draft_non_owner_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _AddControlToDraftFakeGraph(_standard_parent_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_add_control_to_draft()

    assert result.is_error is False
    assert _text(result) == "error: you do not have access to this Policy"


def test_add_control_to_draft_non_draft_status_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(
        monkeypatch, _AddControlToDraftFakeGraph(_standard_parent_fixture(status="proposed"))
    )
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_control_to_draft()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "cannot edit a Policy in status 'proposed'" in text


def test_add_control_to_draft_missing_standard_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _AddControlToDraftFakeGraph(None))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_control_to_draft(standard_id="std_missing")

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no Standard exists" in text


def test_add_control_to_draft_graph_unavailable_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _AddControlToDraftFakeGraph(_standard_parent_fixture())
    graph.raise_on_write = redis.exceptions.ConnectionError("boom")
    _install_graph(monkeypatch, graph)
    configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_control_to_draft()

    assert result.is_error is False
    text = _text(result)
    assert text == "error: the policy graph database is not reachable"
    assert "not a patchable field" not in text


def test_add_control_to_draft_log_triad_carries_actor_action_node_id_and_timestamp(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """CHANGES.md finding #9: the log triad carries `entity_id=standard_id` --
    the parent, since it's known before the call (the new Control's id isn't
    known until `body()` returns).
    """
    emitter = configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_graph(monkeypatch, _AddControlToDraftFakeGraph(_standard_parent_fixture()))
        success_result = _call_add_control_to_draft()

        _install_graph(monkeypatch, _AddControlToDraftFakeGraph(None))
        failure_result = _call_add_control_to_draft(standard_id="std_missing")

    assert success_result.is_error is False
    assert _text(failure_result).startswith("error: ")

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    action_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "add_control_to_draft"
    ]
    success_lines = [
        line for line in action_lines if line.get("entity_id") == _EXISTING_STANDARD_ID
    ]
    failure_lines = [line for line in action_lines if line.get("entity_id") == "std_missing"]

    assert {line["outcome"] for line in success_lines} == {"started", "succeeded"}
    assert {line["outcome"] for line in failure_lines} == {"started", "failed"}
    for line in (*success_lines, *failure_lines):
        assert line["principal"] == _ACTOR_SUBJECT
        assert isinstance(line["timestamp"], float)


def test_add_control_to_draft_backfills_legacy_null_status_before_gating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHANGES.md finding #2 (High): a legacy Standard with a `NULL` own
    `status` is still draft-eligible once backfilled by
    `_read_standard_with_parent_backfilled`, not spuriously rejected.
    """
    configure()
    fixture = _StandardParentFixture(
        policy_id=_EXISTING_POLICY_ID,
        owner_subject=_ACTOR_SUBJECT,
        owner_issuer=_ACTOR_ISSUER,
        policy_status="draft",
        standard_status=None,
    )
    _install_graph(monkeypatch, _AddControlToDraftFakeGraph(fixture))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_control_to_draft()

    assert result.is_error is False
    assert fixture.standard_status == "draft"


# --- `update-control-draft` (issue #136, Slice 5) ----------------------------

_EXISTING_CONTROL_ID = "ctrl_key_rotation_check_aaaaaa"


@dataclass
class _ControlParentFixture:
    """The two-hop analogue of `_StandardParentFixture` (Slice 3): a Control's
    own status/title plus its ROOT Policy's owner/status -- neither the
    Control nor its parent Standard carries an ownership field of its own.
    """

    standard_id: str
    policy_id: str
    owner_subject: str
    owner_issuer: str
    policy_status: str
    control_status: str | None
    control_title: str = "Key Rotation Check"


class _UpdateControlDraftFakeGraph:
    """Fake FalkorDB graph handle for `update-control-draft`'s tests (issue #136, Slice 5).

    Mirrors `_UpdateStandardDraftFakeGraph` (Slice 3) exactly, one hop
    deeper: models the Control's ROOT-Policy owner/status plus the Control's
    own (possibly `None`, pre-backfill) status as a single mutable fixture,
    so the same instance answers `find_control_with_parent`'s two-hop read
    both before and after `backfill_governance_status`'s own
    `c.status IS NULL` write actually mutates `control_status` (CHANGES.md
    finding #2).
    """

    def __init__(
        self, fixture: _ControlParentFixture | None, *, raise_on_write: Exception | None = None
    ) -> None:
        self._fixture = fixture
        self.write_queries: list[str] = []
        self.write_params: list[dict[str, object] | None] = []
        self.raise_on_write = raise_on_write

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        del timeout
        fixture = self._fixture
        if "RETURN s.id, p.id, p.owner_subject, p.owner_issuer, p.status, c.status, c.title" in q:
            if fixture is None:
                return _FakeQueryResult(result_set=[])
            return _FakeQueryResult(
                result_set=[
                    [
                        fixture.standard_id,
                        fixture.policy_id,
                        fixture.owner_subject,
                        fixture.owner_issuer,
                        fixture.policy_status,
                        fixture.control_status,
                        fixture.control_title,
                    ]
                ]
            )
        if "c.status IS NULL" in q and "SET c.status = p.status" in q:
            if fixture is not None and fixture.control_status is None:
                fixture.control_status = fixture.policy_status
            return _FakeQueryResult()
        if "$set_properties" not in q and "= null" not in q:
            return _FakeQueryResult()
        self.write_queries.append(q)
        self.write_params.append(params)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def _control_parent_fixture(
    *, owner_subject: str = _ACTOR_SUBJECT, status: str = "draft"
) -> _ControlParentFixture:
    return _ControlParentFixture(
        standard_id=_EXISTING_STANDARD_ID,
        policy_id=_EXISTING_POLICY_ID,
        owner_subject=owner_subject,
        owner_issuer=_ACTOR_ISSUER,
        policy_status="draft",
        control_status=status,
    )


def _call_update_control_draft(
    *, control_id: str = _EXISTING_CONTROL_ID, fields: dict[str, object] | None = None
) -> CallToolResult:
    args: dict[str, object] = {"control_id": control_id}
    if fields is not None:
        args["fields"] = fields
    result = asyncio.run(mcp_server.server.call_tool("update-control-draft", args))
    assert isinstance(result, CallToolResult)
    return result


def test_update_control_draft_success_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _UpdateControlDraftFakeGraph(_control_parent_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_control_draft(fields={"description": "revised text"})

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {
        "control_id": _EXISTING_CONTROL_ID,
        "standard_id": _EXISTING_STANDARD_ID,
        "policy_id": _EXISTING_POLICY_ID,
        "title": "Key Rotation Check",
        "status": "draft",
    }


def test_update_control_draft_ownership_traverses_two_hops_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task's own required proof: a REAL Policy -> Standard -> Control
    chain, built entirely through the tool surface
    (`create-policy-draft` -> `add-standard-to-draft` -> `add-control-to-
    draft`), then `update-control-draft` called against that same live
    `_StatefulFakeGraph` -- not a one-hop stand-in, not a fixture asserted
    into existence. `find_control_with_parent`'s own two-hop `MATCH (p:Policy)
    -[:SUPPORTED_BY]->(s:Standard)-[:IMPLEMENTED_BY]->(c:Control)` query is
    the only thing that can resolve `update-control-draft`'s ownership here,
    since `_StatefulFakeGraph` never stores an "owner" concept anywhere
    except on the Policy node itself.
    """
    configure()
    graph = _StatefulFakeGraph()
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        create_result = _call_create_policy_draft()
        policy_id = json.loads(_text(create_result))["policy_id"]

        add_standard_result = _call_add_standard_to_draft(policy_id=policy_id)
        assert add_standard_result.is_error is False
        new_standard_id = json.loads(_text(add_standard_result))["standard_id"]

        add_control_result = _call_add_control_to_draft(standard_id=new_standard_id)
        assert add_control_result.is_error is False
        new_control_id = json.loads(_text(add_control_result))["control_id"]

        update_result = _call_update_control_draft(
            control_id=new_control_id, fields={"description": "updated via two-hop traversal"}
        )

    assert update_result.is_error is False
    assert json.loads(_text(update_result)) == {
        "control_id": new_control_id,
        "standard_id": new_standard_id,
        "policy_id": policy_id,
        "title": "Key Rotation Check",
        "status": "draft",
    }


def test_update_control_draft_ownership_traverses_two_hops_end_to_end_rejects_non_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same real chain as above, but the caller isn't the root Policy's
    owner -- rejected purely via the two-hop traversal, since nothing
    Standard/Control-level carries an ownership concept in this fake either.
    """
    configure()
    graph = _StatefulFakeGraph()
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        create_result = _call_create_policy_draft()
        policy_id = json.loads(_text(create_result))["policy_id"]

        add_standard_result = _call_add_standard_to_draft(policy_id=policy_id)
        new_standard_id = json.loads(_text(add_standard_result))["standard_id"]

        add_control_result = _call_add_control_to_draft(standard_id=new_standard_id)
        new_control_id = json.loads(_text(add_control_result))["control_id"]

    with _verified_actor(sub=_OTHER_SUBJECT):
        update_result = _call_update_control_draft(
            control_id=new_control_id, fields={"description": "hijacked"}
        )

    assert update_result.is_error is False
    assert _text(update_result) == "error: you do not have access to this Policy"


def test_update_control_draft_unknown_field_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _UpdateControlDraftFakeGraph(_control_parent_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_control_draft(fields={"title": "sneaky rename"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "not a patchable field" in text


def test_update_control_draft_type_field_is_patchable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike `add-control-to-draft` (CHANGES.md finding #8), `type` IS
    patchable through `update-control-draft` -- the only post-creation path
    to change a Control's type.
    """
    configure()
    _install_graph(monkeypatch, _UpdateControlDraftFakeGraph(_control_parent_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_control_draft(fields={"type": "automated"})

    assert result.is_error is False


def test_update_control_draft_non_owner_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _UpdateControlDraftFakeGraph(_control_parent_fixture()))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_update_control_draft(fields={"description": "hijacked"})

    assert result.is_error is False
    assert _text(result) == "error: you do not have access to this Policy"


def test_update_control_draft_non_draft_status_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(
        monkeypatch, _UpdateControlDraftFakeGraph(_control_parent_fixture(status="proposed"))
    )
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_control_draft(fields={"description": "too late"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "cannot edit a Policy in status 'proposed'" in text


def test_update_control_draft_missing_control_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _UpdateControlDraftFakeGraph(None))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_control_draft(control_id="ctrl_missing")

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no Control exists" in text


def test_update_control_draft_missing_control_error_distinct_from_standard_and_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-012's 'distinct' requirement, extended to the third node type:
    a missing Control's own error text differs from both `update-standard-
    draft`'s and `update-policy-draft`'s own missing-node errors.
    """
    configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_graph(monkeypatch, _UpdateControlDraftFakeGraph(None))
        control_result = _call_update_control_draft(control_id="ctrl_missing")

        _install_graph(monkeypatch, _UpdateStandardDraftFakeGraph(None))
        standard_result = _call_update_standard_draft(standard_id="std_missing")

        _install_graph(monkeypatch, _UpdatePolicyDraftFakeGraph(None))
        policy_result = _call_update_policy_draft(
            policy_id="pol_missing", fields={"description": "x"}
        )

    control_text = _text(control_result)
    standard_text = _text(standard_result)
    policy_text = _text(policy_result)
    assert len({control_text, standard_text, policy_text}) == 3
    assert "Control" in control_text
    assert "Standard" in standard_text
    assert "Policy" in policy_text


def test_update_control_draft_graph_unavailable_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _UpdateControlDraftFakeGraph(
        _control_parent_fixture(), raise_on_write=redis.exceptions.ConnectionError("boom")
    )
    _install_graph(monkeypatch, graph)
    configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_control_draft(fields={"description": "x"})

    assert result.is_error is False
    text = _text(result)
    assert text == "error: the policy graph database is not reachable"
    assert "not a patchable field" not in text


def test_update_control_draft_log_triad_carries_actor_action_node_id_and_timestamp(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    emitter = configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_graph(monkeypatch, _UpdateControlDraftFakeGraph(_control_parent_fixture()))
        success_result = _call_update_control_draft(fields={"description": "x"})

        _install_graph(monkeypatch, _UpdateControlDraftFakeGraph(None))
        failure_result = _call_update_control_draft(
            control_id="ctrl_missing", fields={"description": "x"}
        )

    assert success_result.is_error is False
    assert _text(failure_result).startswith("error: ")

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    action_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "update_control_draft"
    ]
    success_lines = [line for line in action_lines if line.get("entity_id") == _EXISTING_CONTROL_ID]
    failure_lines = [line for line in action_lines if line.get("entity_id") == "ctrl_missing"]

    assert {line["outcome"] for line in success_lines} == {"started", "succeeded"}
    assert {line["outcome"] for line in failure_lines} == {"started", "failed"}
    for line in (*success_lines, *failure_lines):
        assert line["principal"] == _ACTOR_SUBJECT
        assert isinstance(line["timestamp"], float)


def test_update_control_draft_backfills_legacy_null_status_before_gating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHANGES.md finding #2 (High), two-hop case: a legacy Control with a
    `NULL` own `status` (e.g. minted before any backfilling call, such as via
    the internal-seed adapter) is still draft-eligible once backfilled by
    `_read_control_with_parent_backfilled`, not spuriously rejected.
    """
    configure()
    fixture = _ControlParentFixture(
        standard_id=_EXISTING_STANDARD_ID,
        policy_id=_EXISTING_POLICY_ID,
        owner_subject=_ACTOR_SUBJECT,
        owner_issuer=_ACTOR_ISSUER,
        policy_status="draft",
        control_status=None,
    )
    _install_graph(monkeypatch, _UpdateControlDraftFakeGraph(fixture))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_control_draft(fields={"description": "now editable"})

    assert result.is_error is False
    assert fixture.control_status == "draft"


# --- `propose-policy` (issue #134, S16) -------------------------------------


class _ProposeFakeGraph(_TreeFakeGraph):
    """Extends `_TreeFakeGraph` with `cascade_status`'s single cascading write (S16).

    Every other query (the tree read, each of `backfill_governance_status`'s
    three `SET` statements) is handled by `_TreeFakeGraph.query` unchanged;
    only the cascade's own distinguishing `SET p.status = $target_status`
    substring is intercepted here.
    """

    def __init__(
        self, policy: _PolicyFixture | None, *, raise_on_write: Exception | None = None
    ) -> None:
        super().__init__(policy)
        self.write_queries: list[str] = []
        self.raise_on_write = raise_on_write

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        if "SET p.status = $target_status" not in q:
            return super().query(q, params, timeout)
        self.write_queries.append(q)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def _proposable_fixture(
    *,
    owner_subject: str = _ACTOR_SUBJECT,
    status: str = "draft",
    standards: tuple[_StandardFixture, ...] = (
        _StandardFixture(id="std_1", title="Encryption Standard", status="draft"),
    ),
) -> _PolicyFixture:
    return _PolicyFixture(
        id=_EXISTING_POLICY_ID,
        title=_TITLE,
        status=status,
        version="1",
        owner_subject=owner_subject,
        owner_issuer=_ACTOR_ISSUER,
        standards=standards,
    )


def _call_propose_policy(policy_id: str = _EXISTING_POLICY_ID) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("propose-policy", {"policy_id": policy_id}))
    assert isinstance(result, CallToolResult)
    return result


def test_propose_policy_tool_is_registered_and_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _ProposeFakeGraph(_proposable_fixture()))
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_propose_policy()

    assert result.is_error is False


def test_propose_policy_success_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    graph = _ProposeFakeGraph(_proposable_fixture())
    _install_graph(monkeypatch, graph)
    audit_store = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit_store)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_propose_policy()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {
        "policy_id": _EXISTING_POLICY_ID,
        "status": "proposed",
        "standard_ids": ["std_1"],
        "control_ids": [],
    }
    assert len(graph.write_queries) == 1
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].outcome == "applied"
    assert audit_store.calls[0].action == "policy.propose"


def test_propose_policy_non_owner_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(
        monkeypatch, _ProposeFakeGraph(_proposable_fixture(owner_subject=_OTHER_SUBJECT))
    )
    audit_store = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit_store)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_propose_policy()

    assert result.is_error is False
    assert _text(result) == "error: you do not have access to this Policy"
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].outcome == "rejected"


def test_propose_policy_zero_standards_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _ProposeFakeGraph(_proposable_fixture(standards=())))
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_propose_policy()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "at least one Standard" in text


def test_propose_policy_wrong_status_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _ProposeFakeGraph(_proposable_fixture(status="proposed")))
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_propose_policy()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "cannot propose" in text


def test_propose_policy_nonexistent_policy_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _ProposeFakeGraph(None))
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_propose_policy(policy_id="pol_missing")

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no Policy exists" in text


def test_propose_policy_graph_unavailable_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    graph = _ProposeFakeGraph(
        _proposable_fixture(), raise_on_write=redis.exceptions.ConnectionError("boom")
    )
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_propose_policy()

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"


def test_propose_policy_without_a_real_authenticated_caller_and_no_bypass_is_refused() -> None:
    configure()

    result = _call_propose_policy()

    assert result.is_error is False
    assert _text(result) == (
        "error: this action requires a real authenticated caller "
        "(the local-test bypass counts as one)"
    )


# --- `approve-policy` (issue #134, S18) -------------------------------------

_MANAGER_SUBJECT = "policy-manager"


class _ApproveFakeGraph(_TreeFakeGraph):
    """Extends `_TreeFakeGraph` with `cascade_status`'s single cascading write (S18).

    Mirrors `_ProposeFakeGraph` exactly -- none of this slice's own tests
    seed a `SUPERSEDED_BY` edge (that behavior is already fully covered at
    the service layer, `test_service.py`'s S17/S25 tests), so the
    `find_approved_prior` read this tool's own `approve_policy` call always
    issues after a successful cascade falls through to `_TreeFakeGraph.
    query`'s own default branch, which returns an empty `result_set` --
    exactly what "no prior" looks like.
    """

    def __init__(
        self, policy: _PolicyFixture | None, *, raise_on_write: Exception | None = None
    ) -> None:
        super().__init__(policy)
        self.write_queries: list[str] = []
        self.raise_on_write = raise_on_write

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        if "SET p.status = $target_status" not in q:
            return super().query(q, params, timeout)
        self.write_queries.append(q)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def _approvable_fixture(
    *,
    owner_subject: str = _ACTOR_SUBJECT,
    status: str = "proposed",
    standards: tuple[_StandardFixture, ...] = (
        _StandardFixture(id="std_1", title="Encryption Standard", status="proposed"),
    ),
) -> _PolicyFixture:
    return _PolicyFixture(
        id=_EXISTING_POLICY_ID,
        title=_TITLE,
        status=status,
        version="1",
        owner_subject=owner_subject,
        owner_issuer=_ACTOR_ISSUER,
        standards=standards,
    )


def _call_approve_policy(policy_id: str = _EXISTING_POLICY_ID) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("approve-policy", {"policy_id": policy_id}))
    assert isinstance(result, CallToolResult)
    return result


def _install_manager_role(
    monkeypatch: pytest.MonkeyPatch, *, subject: str = _MANAGER_SUBJECT
) -> None:
    store = FakeAccessRoleStore()
    store.grant(
        actor=_GRANTER, target=(subject, _ACTOR_ISSUER), access_role=AccessRole.POLICY_MANAGER
    )
    _install_access_role_store(monkeypatch, store)


def test_approve_policy_tool_is_registered_and_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _ApproveFakeGraph(_approvable_fixture()))
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_approve_policy()

    assert result.is_error is False


def test_approve_policy_success_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    graph = _ApproveFakeGraph(_approvable_fixture())
    _install_graph(monkeypatch, graph)
    audit_store = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit_store)
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_approve_policy()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {
        "policy_id": _EXISTING_POLICY_ID,
        "status": "approved",
        "standard_ids": ["std_1"],
        "control_ids": [],
        "auto_deprecated_policy_id": None,
    }
    assert len(graph.write_queries) == 1
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].outcome == "applied"
    assert audit_store.calls[0].action == "policy.approve"


def test_approve_policy_owner_self_approval_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _ApproveFakeGraph(_approvable_fixture()))
    audit_store = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit_store)
    _install_manager_role(monkeypatch, subject=_ACTOR_SUBJECT)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_approve_policy()

    assert result.is_error is False
    assert _text(result) == "error: you cannot approve or reject a Policy you own"
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].outcome == "rejected"


def test_approve_policy_non_policy_manager_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _ApproveFakeGraph(_approvable_fixture()))
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_approve_policy()

    assert result.is_error is False
    assert _text(result) == "error: You do not have the required access role for this action."


def test_approve_policy_wrong_status_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _ApproveFakeGraph(_approvable_fixture(status="draft")))
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_approve_policy()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "cannot approve" in text


def test_approve_policy_nonexistent_policy_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _ApproveFakeGraph(None))
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_approve_policy(policy_id="pol_missing")

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no Policy exists" in text


def test_approve_policy_graph_unavailable_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    graph = _ApproveFakeGraph(
        _approvable_fixture(), raise_on_write=redis.exceptions.ConnectionError("boom")
    )
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_approve_policy()

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"


def test_approve_policy_without_a_real_authenticated_caller_and_no_bypass_is_refused() -> None:
    configure()

    result = _call_approve_policy()

    assert result.is_error is False
    assert _text(result) == (
        "error: this action requires a real authenticated caller "
        "(the local-test bypass counts as one)"
    )


# --- `reject-policy` (issue #134, S20) ---------------------------------------
#
# Mirrors `approve-policy`'s own S18 test list exactly (same
# `_ApproveFakeGraph`/`_approvable_fixture`/`_install_manager_role`
# fixtures, reused unchanged), proving the tool-layer wrapper is genuinely
# the same pattern, not reimplemented differently.


def _call_reject_policy(policy_id: str = _EXISTING_POLICY_ID) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("reject-policy", {"policy_id": policy_id}))
    assert isinstance(result, CallToolResult)
    return result


def test_reject_policy_tool_is_registered_and_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _ApproveFakeGraph(_approvable_fixture()))
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_reject_policy()

    assert result.is_error is False


def test_reject_policy_success_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    graph = _ApproveFakeGraph(_approvable_fixture())
    _install_graph(monkeypatch, graph)
    audit_store = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit_store)
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_reject_policy()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {
        "policy_id": _EXISTING_POLICY_ID,
        "status": "draft",
        "standard_ids": ["std_1"],
        "control_ids": [],
    }
    assert len(graph.write_queries) == 1
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].outcome == "applied"
    assert audit_store.calls[0].action == "policy.reject"


def test_reject_policy_owner_self_rejection_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _ApproveFakeGraph(_approvable_fixture()))
    audit_store = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit_store)
    _install_manager_role(monkeypatch, subject=_ACTOR_SUBJECT)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_reject_policy()

    assert result.is_error is False
    assert _text(result) == "error: you cannot approve or reject a Policy you own"
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].outcome == "rejected"


def test_reject_policy_non_policy_manager_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _ApproveFakeGraph(_approvable_fixture()))
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_reject_policy()

    assert result.is_error is False
    assert _text(result) == "error: You do not have the required access role for this action."


def test_reject_policy_wrong_status_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _ApproveFakeGraph(_approvable_fixture(status="draft")))
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_reject_policy()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "cannot reject" in text


def test_reject_policy_nonexistent_policy_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _ApproveFakeGraph(None))
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_reject_policy(policy_id="pol_missing")

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no Policy exists" in text


def test_reject_policy_graph_unavailable_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    graph = _ApproveFakeGraph(
        _approvable_fixture(), raise_on_write=redis.exceptions.ConnectionError("boom")
    )
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_reject_policy()

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"


def test_reject_policy_without_a_real_authenticated_caller_and_no_bypass_is_refused() -> None:
    configure()

    result = _call_reject_policy()

    assert result.is_error is False
    assert _text(result) == (
        "error: this action requires a real authenticated caller "
        "(the local-test bypass counts as one)"
    )


# --- `revert-policy-to-draft` (issue #134, S22) ------------------------------
#
# Mirrors `propose-policy`'s own S16 test list (owner-only, no
# `access_role_store` involved at all) rather than `approve-policy`'s
# `PolicyManager`-gated shape. The non-owner test below deliberately grants
# the non-owner caller `PolicyManager` to prove this tool's own action is
# genuinely owner-only, not role-gated.


class _RevertFakeGraph(_TreeFakeGraph):
    """Extends `_TreeFakeGraph` with `cascade_status`'s single cascading write (S22)."""

    def __init__(
        self, policy: _PolicyFixture | None, *, raise_on_write: Exception | None = None
    ) -> None:
        super().__init__(policy)
        self.write_queries: list[str] = []
        self.raise_on_write = raise_on_write

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        if "SET p.status = $target_status" not in q:
            return super().query(q, params, timeout)
        self.write_queries.append(q)
        if self.raise_on_write is not None:
            raise self.raise_on_write
        return _FakeQueryResult()


def _revertable_fixture(
    *,
    owner_subject: str = _ACTOR_SUBJECT,
    status: str = "proposed",
    standards: tuple[_StandardFixture, ...] = (
        _StandardFixture(id="std_1", title="Encryption Standard", status="proposed"),
    ),
) -> _PolicyFixture:
    return _PolicyFixture(
        id=_EXISTING_POLICY_ID,
        title=_TITLE,
        status=status,
        version="1",
        owner_subject=owner_subject,
        owner_issuer=_ACTOR_ISSUER,
        standards=standards,
    )


def _call_revert_policy_to_draft(policy_id: str = _EXISTING_POLICY_ID) -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool("revert-policy-to-draft", {"policy_id": policy_id})
    )
    assert isinstance(result, CallToolResult)
    return result


def test_revert_policy_to_draft_tool_is_registered_and_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _RevertFakeGraph(_revertable_fixture()))
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_revert_policy_to_draft()

    assert result.is_error is False


def test_revert_policy_to_draft_success_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    graph = _RevertFakeGraph(_revertable_fixture())
    _install_graph(monkeypatch, graph)
    audit_store = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit_store)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_revert_policy_to_draft()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {
        "policy_id": _EXISTING_POLICY_ID,
        "status": "draft",
        "standard_ids": ["std_1"],
        "control_ids": [],
    }
    assert len(graph.write_queries) == 1
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].outcome == "applied"
    assert audit_store.calls[0].action == "policy.revert"


def test_revert_policy_to_draft_non_owner_policy_manager_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `PolicyManager` who is not the owner is still rejected -- no RBAC gate exists here."""
    configure()
    _install_graph(monkeypatch, _RevertFakeGraph(_revertable_fixture(owner_subject=_OTHER_SUBJECT)))
    audit_store = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit_store)
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_revert_policy_to_draft()

    assert result.is_error is False
    assert _text(result) == "error: you do not have access to this Policy"
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].outcome == "rejected"


def test_revert_policy_to_draft_wrong_status_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _RevertFakeGraph(_revertable_fixture(status="draft")))
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_revert_policy_to_draft()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "cannot revert" in text


def test_revert_policy_to_draft_nonexistent_policy_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _RevertFakeGraph(None))
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_revert_policy_to_draft(policy_id="pol_missing")

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "no Policy exists" in text


def test_revert_policy_to_draft_graph_unavailable_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    graph = _RevertFakeGraph(
        _revertable_fixture(), raise_on_write=redis.exceptions.ConnectionError("boom")
    )
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_revert_policy_to_draft()

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"


def test_revert_policy_to_draft_no_auth_caller_and_no_bypass_is_refused() -> None:
    configure()

    result = _call_revert_policy_to_draft()

    assert result.is_error is False
    assert _text(result) == (
        "error: this action requires a real authenticated caller "
        "(the local-test bypass counts as one)"
    )


# --- Dedicated local-test-bypass verification (issue #134, PLAN.md S28, AC-BI-018) -----
#
# `test_bypass_active_creates_a_policy_owned_by_the_bypass_principal` above
# (S12) already covers `create-policy-draft`'s `actor_subject`/
# `owner_subject` half of AC-BI-018 through the tool's own success response
# and its recorded audit row. The two tests below close the remaining gaps:
# (1) the `owner_issuer` graph property -- not part of the tool's own
# success response shape (`{"policy_id", "title", "status", "version",
# "owner_subject"}` has no `owner_issuer` key) -- is also the bypass
# principal, confirmed by inspecting the graph write's own params directly;
# (2) the regression guard that `approve-policy`/`reject-policy` are ALWAYS
# rejected by `block_self_approval` under the bypass, proving the issue's
# own documented limitation ("approve/reject are not exercisable locally
# under the bypass") is real and intended, not a defect.


class _ParamCapturingFakeGraph:
    """A `_FakeGraph` variant that also records each write's `params` (S28).

    `_FakeGraph` (S12, above) discards `params` entirely -- none of its own
    tests needed to inspect them, since the tool's own success response only
    surfaces `owner_subject`, not `owner_issuer`. AC-BI-018's literal
    requirement that BOTH halves of the owner pair are the bypass
    principal can only be confirmed by inspecting the
    `graph_writer.create_policy_draft` write's own params directly.
    """

    def __init__(self) -> None:
        self.write_params: list[dict[str, object]] = []

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        del timeout
        if "RETURN p.id, p.title" in q:
            return _FakeQueryResult(result_set=[])
        assert params is not None
        self.write_params.append(params)
        return _FakeQueryResult()


def test_bypass_sets_owner_subject_and_owner_issuer_to_the_bypass_principal_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-018 / D-11: the new Policy's `owner_issuer` graph property -- not just
    `owner_subject` (already covered by
    `test_bypass_active_creates_a_policy_owned_by_the_bypass_principal` above)
    -- is also `LOCAL_TEST_PRINCIPAL_ID` under the bypass, proving the
    synthetic owner pair is genuinely `(LOCAL_TEST_PRINCIPAL_ID,
    LOCAL_TEST_PRINCIPAL_ID)`, not a real issuer paired with a synthetic
    subject.
    """
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    graph = _ParamCapturingFakeGraph()
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())

    result = _call_create_policy_draft()

    assert result.is_error is False
    policy_write_params = next(params for params in graph.write_params if "properties" in params)
    properties = policy_write_params["properties"]
    assert isinstance(properties, dict)
    assert properties["owner_subject"] == LOCAL_TEST_PRINCIPAL_ID
    assert properties["owner_issuer"] == LOCAL_TEST_PRINCIPAL_ID


def _bypass_owned_proposed_fixture() -> _PolicyFixture:
    """A `"proposed"` Policy owned by the bypass principal pair (S28)."""
    return _PolicyFixture(
        id=_EXISTING_POLICY_ID,
        title=_TITLE,
        status="proposed",
        version="1",
        owner_subject=LOCAL_TEST_PRINCIPAL_ID,
        owner_issuer=LOCAL_TEST_PRINCIPAL_ID,
    )


def _install_bypass_manager_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grants `PolicyManager` to the bypass principal pair itself (S28).

    Needed so `approve-policy`/`reject-policy`'s own RBAC gate
    (`require_role`, AC-BI-008) passes -- the point of these regression
    tests is to prove `block_self_approval` is what rejects the call, not
    the RBAC gate.
    """
    store = FakeAccessRoleStore()
    store.grant(
        actor=_GRANTER,
        target=(LOCAL_TEST_PRINCIPAL_ID, LOCAL_TEST_PRINCIPAL_ID),
        access_role=AccessRole.POLICY_MANAGER,
    )
    _install_access_role_store(monkeypatch, store)


def test_bypass_approve_policy_is_always_rejected_by_self_approval_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard (AC-BI-018's own documented limitation): under the bypass,
    `_resolve_policy_lifecycle_actor` (D-11) returns the SAME fixed
    `(LOCAL_TEST_PRINCIPAL_ID, LOCAL_TEST_PRINCIPAL_ID)` pair for `actor` on
    every call, with no verified bearer token to vary it -- so a Policy
    created under the bypass always has `owner == actor` for any later call
    made under the same bypass. `block_self_approval` therefore ALWAYS
    rejects `approve-policy` here, by construction, not by accident. This is
    NOT a defect to "fix": it is the correct, conservative consequence of a
    bypass with no distinct real identities, and this test exists so a
    future change that accidentally makes self-approval succeed under the
    bypass (a far worse regression than today's correctly-inert limitation)
    is caught immediately.
    """
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _install_graph(monkeypatch, _ApproveFakeGraph(_bypass_owned_proposed_fixture()))
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_bypass_manager_role(monkeypatch)

    result = _call_approve_policy()

    assert result.is_error is False
    assert _text(result) == "error: you cannot approve or reject a Policy you own"


def test_bypass_reject_policy_is_always_rejected_by_self_approval_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors `test_bypass_approve_policy_is_always_rejected_by_self_approval_block`
    above for `reject-policy` -- same bypass-forced `owner == actor` reasoning
    (AC-BI-018's documented limitation), not a defect.
    """
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _install_graph(monkeypatch, _ApproveFakeGraph(_bypass_owned_proposed_fixture()))
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_bypass_manager_role(monkeypatch)

    result = _call_reject_policy()

    assert result.is_error is False
    assert _text(result) == "error: you cannot approve or reject a Policy you own"


# --- AC-BI-001 bypass proofs (Slice 7 gap closure) ---------------------------
#
# An independent Verify pass found that no test anywhere in this issue's diff
# actually exercises the local-test bypass against any of #136's six new
# tools -- every existing test for these six uses `_verified_actor`. Each
# test below proves BOTH halves of AC-BI-001 for its own tool:
# `_resolve_policy_lifecycle_actor` resolves the fixed `LOCAL_TEST_PRINCIPAL_ID`
# pair as `actor` under the bypass (no `_verified_actor` context is entered
# anywhere in these tests), AND `require_owner`'s `(sub, iss)` tuple-equality
# check passes against a node whose recorded owner is that exact same pair --
# i.e. a draft "created under the bypass" (which #134's own
# `test_bypass_active_creates_a_policy_owned_by_the_bypass_principal` already
# proves gets owner `(LOCAL_TEST_PRINCIPAL_ID, LOCAL_TEST_PRINCIPAL_ID)`) is
# genuinely editable under the bypass, not merely callable.


def test_bypass_update_policy_draft_treats_bypass_principal_as_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    fixture = _PolicyFixture(
        id=_EXISTING_POLICY_ID,
        title=_TITLE,
        status="draft",
        version="1",
        owner_subject=LOCAL_TEST_PRINCIPAL_ID,
        owner_issuer=LOCAL_TEST_PRINCIPAL_ID,
    )
    _install_graph(monkeypatch, _UpdatePolicyDraftFakeGraph(fixture))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    result = _call_update_policy_draft(fields={"description": "revised text"})

    assert result.is_error is False
    assert json.loads(_text(result)) == {
        "policy_id": _EXISTING_POLICY_ID,
        "updated_fields": ["description"],
    }


def test_bypass_add_standard_to_draft_treats_bypass_principal_as_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    fixture = _PolicyFixture(
        id=_EXISTING_POLICY_ID,
        title=_TITLE,
        status="draft",
        version="1",
        owner_subject=LOCAL_TEST_PRINCIPAL_ID,
        owner_issuer=LOCAL_TEST_PRINCIPAL_ID,
    )
    _install_graph(monkeypatch, _AddStandardToDraftFakeGraph(fixture))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    result = _call_add_standard_to_draft()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["policy_id"] == _EXISTING_POLICY_ID
    assert body["status"] == "draft"


def test_bypass_update_standard_draft_treats_bypass_principal_as_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    fixture = _StandardParentFixture(
        policy_id=_EXISTING_POLICY_ID,
        owner_subject=LOCAL_TEST_PRINCIPAL_ID,
        owner_issuer=LOCAL_TEST_PRINCIPAL_ID,
        policy_status="draft",
        standard_status="draft",
    )
    _install_graph(monkeypatch, _UpdateStandardDraftFakeGraph(fixture))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    result = _call_update_standard_draft(fields={"description": "revised text"})

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["standard_id"] == _EXISTING_STANDARD_ID
    assert body["policy_id"] == _EXISTING_POLICY_ID


def test_bypass_add_control_to_draft_treats_bypass_principal_as_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    fixture = _StandardParentFixture(
        policy_id=_EXISTING_POLICY_ID,
        owner_subject=LOCAL_TEST_PRINCIPAL_ID,
        owner_issuer=LOCAL_TEST_PRINCIPAL_ID,
        policy_status="draft",
        standard_status="draft",
    )
    _install_graph(monkeypatch, _AddControlToDraftFakeGraph(fixture))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    result = _call_add_control_to_draft()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["standard_id"] == _EXISTING_STANDARD_ID
    assert body["status"] == "draft"


def test_bypass_update_control_draft_treats_bypass_principal_as_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    fixture = _ControlParentFixture(
        standard_id=_EXISTING_STANDARD_ID,
        policy_id=_EXISTING_POLICY_ID,
        owner_subject=LOCAL_TEST_PRINCIPAL_ID,
        owner_issuer=LOCAL_TEST_PRINCIPAL_ID,
        policy_status="draft",
        control_status="draft",
    )
    _install_graph(monkeypatch, _UpdateControlDraftFakeGraph(fixture))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    result = _call_update_control_draft(fields={"description": "revised text"})

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["control_id"] == _EXISTING_CONTROL_ID
    assert body["policy_id"] == _EXISTING_POLICY_ID


def test_bypass_create_policy_draft_supersedes_fork_owned_and_editable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sixth AC-BI-001 tool: `create-policy-draft` with `supersedes_policy_id`.

    The superseded prior is created and approved by a real verified actor
    (irrelevant to what this test proves); only the fork call itself, and the
    follow-up edit of one of its forked children, run under the bypass with
    no `_verified_actor` context bound at all -- proving the fork's own new
    draft is owned by `LOCAL_TEST_PRINCIPAL_ID` AND genuinely editable under
    the bypass (mirrors `test_create_policy_draft_with_supersedes_policy_id_
    success_shape`'s own real round trip, ending under the bypass instead of
    under a verified actor).
    """
    configure()
    graph = _StatefulFakeGraph()
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        prior_result = asyncio.run(
            mcp_server.server.call_tool(
                "create-policy-draft",
                {"title": _TITLE, "standards": [{"title": "Encryption Standard"}]},
            )
        )
        assert isinstance(prior_result, CallToolResult)
        assert prior_result.is_error is False
        prior_id = json.loads(_text(prior_result))["policy_id"]

        propose_result = _call_propose_policy(policy_id=prior_id)
        assert propose_result.is_error is False

    with _verified_actor(sub=_MANAGER_SUBJECT):
        approve_result = _call_approve_policy(policy_id=prior_id)
        assert approve_result.is_error is False

    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")

    fork_result = asyncio.run(
        mcp_server.server.call_tool(
            "create-policy-draft",
            {"title": "Data Protection Policy v2", "supersedes_policy_id": prior_id},
        )
    )
    assert isinstance(fork_result, CallToolResult)
    assert fork_result.is_error is False
    body = json.loads(_text(fork_result))
    assert body["owner_subject"] == LOCAL_TEST_PRINCIPAL_ID
    assert len(body["standard_ids"]) == 1
    new_standard_id = body["standard_ids"][0]

    update_result = _call_update_standard_draft(
        standard_id=new_standard_id, fields={"description": "revised"}
    )

    assert update_result.is_error is False


# --- AC-BI-013 unexpected-error hardening (Slice 7 gap closure) -------------
#
# Sibling tool suites (`test_near_miss_tools.py`, `test_restore_instrument_
# tool.py`, `test_ingest_regulation_tool.py`, `test_check_regulations_tool.py`,
# `test_get_catalog_listing_tool.py`) each have a dedicated test proving an
# unclassified exception from the underlying call never crosses the MCP
# boundary raw -- `_run_mcp_action`'s residual safety net sanitises it to the
# fixed `_UNEXPECTED_ERROR_MESSAGE` string. None of #136's six new tools had
# an analogous test; each one below raises a raw exception carrying a fake
# host:port string from the underlying service call and asserts neither the
# host nor the port text leaks into the returned error string.


def test_create_policy_draft_unexpected_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _FakeGraph())
    _install_audit_store(monkeypatch, _FakeAuditStore())

    def _raise(**kwargs: object) -> object:
        del kwargs
        message = "connection refused to db-internal-7.prod:6379"
        raise RuntimeError(message)

    monkeypatch.setattr(mcp_server, "run_create_policy_draft", _raise)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_create_policy_draft()

    assert result.is_error is False
    text = _text(result)
    assert text == "error: an unexpected error occurred"
    assert "db-internal-7.prod" not in text
    assert "6379" not in text


def test_update_policy_draft_unexpected_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _FakeGraph())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    def _raise(**kwargs: object) -> object:
        del kwargs
        message = "connection refused to db-internal-7.prod:6379"
        raise RuntimeError(message)

    monkeypatch.setattr(mcp_server, "run_update_policy_draft", _raise)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_policy_draft(fields={"description": "x"})

    assert result.is_error is False
    text = _text(result)
    assert text == "error: an unexpected error occurred"
    assert "db-internal-7.prod" not in text
    assert "6379" not in text


def test_add_standard_to_draft_unexpected_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _FakeGraph())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    def _raise(**kwargs: object) -> object:
        del kwargs
        message = "connection refused to db-internal-7.prod:6379"
        raise RuntimeError(message)

    monkeypatch.setattr(mcp_server, "run_add_standard_to_draft", _raise)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_standard_to_draft()

    assert result.is_error is False
    text = _text(result)
    assert text == "error: an unexpected error occurred"
    assert "db-internal-7.prod" not in text
    assert "6379" not in text


def test_update_standard_draft_unexpected_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _FakeGraph())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    def _raise(**kwargs: object) -> object:
        del kwargs
        message = "connection refused to db-internal-7.prod:6379"
        raise RuntimeError(message)

    monkeypatch.setattr(mcp_server, "run_update_standard_draft", _raise)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_standard_draft(fields={"description": "x"})

    assert result.is_error is False
    text = _text(result)
    assert text == "error: an unexpected error occurred"
    assert "db-internal-7.prod" not in text
    assert "6379" not in text


def test_add_control_to_draft_unexpected_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _FakeGraph())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    def _raise(**kwargs: object) -> object:
        del kwargs
        message = "connection refused to db-internal-7.prod:6379"
        raise RuntimeError(message)

    monkeypatch.setattr(mcp_server, "run_add_control_to_draft", _raise)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_control_to_draft()

    assert result.is_error is False
    text = _text(result)
    assert text == "error: an unexpected error occurred"
    assert "db-internal-7.prod" not in text
    assert "6379" not in text


def test_update_control_draft_unexpected_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_graph(monkeypatch, _FakeGraph())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    def _raise(**kwargs: object) -> object:
        del kwargs
        message = "connection refused to db-internal-7.prod:6379"
        raise RuntimeError(message)

    monkeypatch.setattr(mcp_server, "run_update_control_draft", _raise)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_control_draft(fields={"description": "x"})

    assert result.is_error is False
    text = _text(result)
    assert text == "error: an unexpected error occurred"
    assert "db-internal-7.prod" not in text
    assert "6379" not in text
