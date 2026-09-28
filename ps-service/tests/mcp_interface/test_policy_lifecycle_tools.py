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
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

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
        if "SUPPORTED_BY]->(s:Standard {id: $standard_id})" in q:
            props = cast("dict[str, object]", p["properties"])
            policy = self._policies[cast("str", p["policy_id"])]
            policy.standards[cast("str", p["standard_id"])] = _E2EStandardState(
                title=cast("str", props["title"]), status=cast("str", props["status"])
            )
            return _FakeQueryResult()
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
