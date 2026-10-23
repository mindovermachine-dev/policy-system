"""Tests for the registered `create-policy-draft` MCP tool.

`_verified_actor` is a local copy of `test_access_role_tools.py`'s own
helper -- binds a real `AccessToken`
onto the MCP SDK's own auth contextvar that `get_access_token()`
(`_resolve_policy_lifecycle_actor`) reads.
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
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

import pytest
import redis.exceptions
from api._gateway_fakes import installed_process_gateway
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package (has `__init__.py`); mirrors `test_access_role_tools.py`'s own cross-package import convention
    FakeAccessRoleStore,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent
from policy_lifecycle._gateway_fakes import (
    FlippingRead,
    PolicyGraphSeeder,
    PolicyReadGraph,
    SeedControl,
    SeedStandard,
)

from ps_service.authz.models import AccessRole
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server, tool_core

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping
    from pathlib import Path

    from graph_gateway._fakes import GatewayRig

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


class _QueryResult(Protocol):
    """The part of a query result the lifecycle reads: its rows."""

    @property
    def result_set(self) -> list[object]: ...


class _GraphHandleDouble(Protocol):
    """Structural shape both `_FakeGraph` (write-flow tests) and `_TreeFakeGraph`
    (`get-policy`'s read-flow tests) satisfy -- lets `_install_graph`/
    `_FakeFalkorDB` accept either without a shared base class. The lifecycle rig's read double
    satisfies it too.
    """

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _QueryResult: ...


class _FakeGraph:
    """Fake FalkorDB graph handle for the reads of a create that never reaches the gateway.

    It answers the title-collision and Capability-governor reads and refuses anything else: every
    write is a Graph Write Gateway group, never a statement through this handle.
    """

    def __init__(
        self,
        *,
        existing: tuple[str, str] | None = None,
        governors: dict[str, str | None] | None = None,
    ) -> None:
        self._existing = existing
        # `{capability_id: governing_policy_id | None}`; absent ids do not exist.
        self._governors = governors if governors is not None else {}
        self.total_queries = 0

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        del timeout
        self.total_queries += 1
        if "RETURN p.id, p.title" in q:
            rows: list[object] = [[self._existing[0], self._existing[1]]] if self._existing else []
            return _FakeQueryResult(result_set=rows)
        if "RETURN cap.id, g.id" in q:
            ids = cast("list[str]", (params or {})["capability_ids"])
            return _FakeQueryResult(
                result_set=[[i, self._governors[i]] for i in ids if i in self._governors]
            )
        raise AssertionError(f"not a read this double answers: {q}")


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

    monkeypatch.setattr(tool_core, "connect_from_config", _connect_from_config)


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

    def record(self, *args: object, **kwargs: object) -> str:
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

    def record_standalone_with_event_id(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        details: Mapping[str, object],
    ) -> str:
        """`record_standalone`, returning an id the process gateway's log accepts."""
        self.record_standalone(
            actor_subject=actor_subject,
            actor_issuer=actor_issuer,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            details=details,
        )
        event_id = str(uuid.uuid4())
        installed_process_gateway().rig.store.audit_rows.add(event_id)
        return event_id

    def query(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError


def _install_audit_store(monkeypatch: pytest.MonkeyPatch, store: _FakeAuditStore) -> None:
    def _factory(_config: object) -> _FakeAuditStore:
        return store

    monkeypatch.setattr(tool_core, "PsycopgAuditStore", _factory)


def _call_create_policy_draft(
    title: str = _TITLE, *, capability_ids: list[str] | None = None
) -> CallToolResult:
    arguments: dict[str, object] = {"title": title}
    if capability_ids is not None:
        arguments["capability_ids"] = capability_ids
    result = asyncio.run(mcp_server.server.call_tool("create-policy-draft", arguments))
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
        "governed_capability_ids": [],
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
    """The gateway failure is the persistence error, `FalkorDB write failed: ` prefixed."""
    configure()
    rig = installed_process_gateway().rig
    rig.graphs.open("policy_system").fail_on_write(redis.exceptions.ConnectionError("boom"))
    _install_graph(monkeypatch, _FakeGraph())
    audit_store = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit_store)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_create_policy_draft()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: FalkorDB write failed: ")
    assert "boom" not in text
    # Distinct from the duplicate-title error string above.
    assert "already exists" not in text
    assert [call.outcome for call in audit_store.calls] == ["applied", "failed"]


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
    """Under `PS_SERVICE_LOCAL_TEST_BYPASS=true`, with no
    verified bearer token bound at all, the created Policy's owner is
    `(LOCAL_TEST_PRINCIPAL_ID, LOCAL_TEST_PRINCIPAL_ID)`.
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


# --- `create-policy-draft` `capability_ids` argument -----------


def test_capability_ids_claims_capabilities_and_echoes_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    rig = installed_process_gateway().rig
    graph = rig.graphs.open("policy_system")
    PolicyGraphSeeder(graph).seed_capability("cap_a")
    PolicyGraphSeeder(graph).seed_capability("cap_b")
    audit_store = _FakeAuditStore()
    _install_graph(monkeypatch, PolicyReadGraph(graph))
    _install_audit_store(monkeypatch, audit_store)

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_create_policy_draft(capability_ids=["cap_a", "cap_b", "cap_a"])

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["governed_capability_ids"] == ["cap_a", "cap_b"]
    assert {key[2] for key in graph.edges if key[0] == "GOVERNED_BY"} == {"cap_a", "cap_b"}
    assert {key[4] for key in graph.edges if key[0] == "GOVERNED_BY"} == {body["policy_id"]}
    assert audit_store.calls[0].details["capability_ids"] == ("cap_a", "cap_b")


def test_empty_capability_ids_behaves_like_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    graph = _FakeGraph()
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_create_policy_draft(capability_ids=[])

    assert json.loads(_text(result))["governed_capability_ids"] == []
    edges = installed_process_gateway().rig.graphs.open("policy_system").edges
    assert not any(key[0] == "GOVERNED_BY" for key in edges)


def test_unknown_capability_id_surfaces_as_error_string_with_no_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    graph = _FakeGraph(governors={})
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_create_policy_draft(capability_ids=["cap_missing"])

    text = _text(result)
    assert text.startswith("error: ")
    assert "no Capability exists" in text
    assert "cap_missing" in text
    assert installed_process_gateway().rig.store.groups == []


def test_already_governed_capability_id_surfaces_as_error_string_with_no_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    graph = _FakeGraph(governors={"cap_a": "pol_other"})
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_create_policy_draft(capability_ids=["cap_a"])

    text = _text(result)
    assert text.startswith("error: ")
    assert "already governed" in text
    assert "pol_other" not in text
    assert installed_process_gateway().rig.store.groups == []


def test_blank_capability_id_entry_is_rejected_before_any_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    graph = _FakeGraph()
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT), pytest.raises(Exception, match="capability_ids"):
        _call_create_policy_draft(capability_ids=[""])

    assert graph.total_queries == 0


def test_capability_ids_without_authenticated_caller_is_refused_before_graph_is_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the pre-existing authentication gate, not new behaviour.

    `create-policy-draft` is not role-gated; a caller with no verified
    identity who passes `capability_ids` is refused with the same fixed
    message as without them, the graph is never touched and nothing is
    audited. `capability_ids` adds no role gate.
    """
    configure()
    graph = _FakeGraph(governors={"cap_a": None})
    audit_store = _FakeAuditStore()
    _install_graph(monkeypatch, graph)
    _install_audit_store(monkeypatch, audit_store)

    result = _call_create_policy_draft(capability_ids=["cap_a"])

    assert _text(result) == (
        "error: this action requires a real authenticated caller "
        "(the local-test bypass counts as one)"
    )
    assert graph.total_queries == 0
    assert audit_store.calls == []


# --- `create-policy-draft` `standards` argument --


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
        "governed_capability_ids": [],
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


def _install_stateful_rig(monkeypatch: pytest.MonkeyPatch) -> GatewayRig:
    """Read through the lifecycle read double over the process rig's graph.

    Every write -- creates, draft edits and the transition cascades -- is a gateway group on that
    graph, so a create -> get -> propose -> approve round trip runs over one graph.
    """
    rig = installed_process_gateway().rig
    _install_graph(monkeypatch, PolicyReadGraph(rig.graphs.open("policy_system")))
    return rig


def test_create_policy_draft_with_standards_creates_full_tree_all_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real gap this slice closes: a Policy created via the MCP tool
    surface can now have Standard/Control children attached at creation,
    verified by reading it straight back via `get-policy`.
    """
    configure()
    _install_stateful_rig(monkeypatch)
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
    """A Policy created via
    `create-policy-draft` WITH a Standard attached at creation satisfies
    `propose-policy`'s >=1-Standard completeness gate, so
    the owner can propose their own complete Draft
    through the tool surface alone.
    """
    configure()
    _install_stateful_rig(monkeypatch)
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


# --- `create-policy-draft`'s `supersedes_policy_id` fork --
#
# `_MANAGER_SUBJECT`/`_install_manager_role` (used below) are defined once,
# near the `approve-policy` tests further down this module -- reused here
# unchanged (Python resolves module-level names at call time, so their
# later position in the file is not an ordering problem).


def test_create_policy_draft_with_supersedes_policy_id_success_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine round trip against the shared stateful fake
    graph -- create a Policy, propose/approve it, then fork a successor from
    it, proving the response carries the new Policy id and every forked
    child id, and that a returned child id is
    IMMEDIATELY usable in a following real `update-standard-draft` call.
    """
    configure()
    _install_stateful_rig(monkeypatch)
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

        # "Usable immediately" proof: a real second call.
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
    """The fork path logs
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
        _install_rig_graph(monkeypatch, approved_prior)
        success_result = asyncio.run(
            mcp_server.server.call_tool(
                "create-policy-draft",
                {
                    "title": "Data Protection Policy v2",
                    "supersedes_policy_id": _EXISTING_POLICY_ID,
                },
            )
        )

        _install_rig_graph(monkeypatch, _draft_fixture())
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
        assert "principal" not in line
        assert isinstance(line["timestamp"], float)


# --- `get-policy` -----------------------------------------

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

    monkeypatch.setattr(tool_core, "PsycopgAccessRoleStore", _factory)


def _call_get_policy(policy_id: str = _EXISTING_POLICY_ID) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("get-policy", {"policy_id": policy_id}))
    assert isinstance(result, CallToolResult)
    return result


def test_get_policy_owner_reads_own_draft_successfully(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _TreeFakeGraph(_draft_fixture()))

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_get_policy()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["policy_id"] == _EXISTING_POLICY_ID
    assert body["status"] == "draft"
    assert body["owner_subject"] == _ACTOR_SUBJECT
    assert body["standards"] == []


def test_get_policy_non_owner_reads_a_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_graph(monkeypatch, _TreeFakeGraph(_draft_fixture()))

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_get_policy()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["policy_id"] == _EXISTING_POLICY_ID
    assert body["status"] == "draft"
    assert body["owner_subject"] == _ACTOR_SUBJECT


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


# --- `update-policy-draft` ----------------------------


def _install_rig_graph(
    monkeypatch: pytest.MonkeyPatch, policy: _PolicyFixture | None
) -> GatewayRig:
    """Seed `policy` (no children) into the process gateway rig's `policy_system` graph.

    `update-policy-draft` writes through the Graph Write Gateway (the process gateway
    the package's autouse fixture installs), so the graph it reads is the rig graph the gateway
    writes. The read handle is the lifecycle read double over it (it refuses a write statement).
    """
    rig = installed_process_gateway().rig
    graph = rig.graphs.open("policy_system")
    if policy is not None:
        PolicyGraphSeeder(graph).seed_policy(
            policy.id,
            title=policy.title,
            status=policy.status,
            version=policy.version,
            owner=(policy.owner_subject, policy.owner_issuer),
            legacy_edges=False,
        )
    _install_graph(monkeypatch, PolicyReadGraph(graph))
    return rig


def _install_tree_rig(
    monkeypatch: pytest.MonkeyPatch,
    *,
    owner_subject: str = _ACTOR_SUBJECT,
    owner_issuer: str = _ACTOR_ISSUER,
    standard_status: str | None = "draft",
    control_status: str | None = "draft",
    with_control: bool = False,
) -> GatewayRig:
    """Seed the existing Policy with its Standard (and optionally a Control) into the rig.

    The Standard and Control tools write through the Graph Write Gateway, so the
    graph they read is the rig graph the gateway writes. `standard_status=None` /
    `control_status=None` seed a legacy node with no `status` (the backfill fixtures).
    """
    controls = (
        (SeedControl(_EXISTING_CONTROL_ID, "Key Rotation Check", status=control_status),)
        if with_control
        else ()
    )
    rig = installed_process_gateway().rig
    graph = rig.graphs.open("policy_system")
    PolicyGraphSeeder(graph).seed_policy(
        _EXISTING_POLICY_ID,
        title=_TITLE,
        owner=(owner_subject, owner_issuer),
        standards=(
            SeedStandard(
                _EXISTING_STANDARD_ID,
                "Encryption Standard",
                status=standard_status,
                controls=controls,
            ),
        ),
        legacy_edges=False,
    )
    _install_graph(monkeypatch, PolicyReadGraph(graph))
    return rig


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
    _install_rig_graph(monkeypatch, _draft_fixture())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_policy_draft(fields={"description": "revised text"})

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {"policy_id": _EXISTING_POLICY_ID, "updated_fields": ["description"]}


def test_update_policy_draft_applies_only_supplied_fields_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine round trip: update -> get, against the same rig graph,
    showing the patched field actually persists and only the supplied field changed.

    The Policy is seeded rather than created.
    """
    configure()
    rig = _install_rig_graph(monkeypatch, _draft_fixture())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())
    policy_id = _EXISTING_POLICY_ID

    with _verified_actor(sub=_ACTOR_SUBJECT):
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
    # `get-policy`'s own response shape never echoes back arbitrary
    # content fields like `description` -- the round trip proves the write
    # landed by confirming the node holds it, the Policy is still readable,
    # still owned by the same actor, and still `draft` (i.e. the PATCH didn't
    # corrupt anything else in the process).
    body = json.loads(_text(get_result))
    assert body["policy_id"] == policy_id
    assert body["status"] == "draft"
    assert body["owner_subject"] == _ACTOR_SUBJECT
    assert rig.graphs.open("policy_system").nodes[("Policy", policy_id)]["description"] == (
        "revised text"
    )


def test_update_policy_draft_unknown_field_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_rig_graph(monkeypatch, _draft_fixture())
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
    _install_rig_graph(monkeypatch, _draft_fixture())
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
    _install_rig_graph(monkeypatch, policy)
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
    _install_rig_graph(monkeypatch, None)
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
    """The gateway failure is the module's persistence error, `FalkorDB write failed: ` prefixed.

    The gateway-mapped message is used rather than a generic "not reachable" text; the tool still
    answers `error: ...` and never leaks detail.
    """
    configure()
    rig = _install_rig_graph(monkeypatch, _draft_fixture())
    rig.graphs.open("policy_system").fail_on_write(redis.exceptions.ConnectionError("boom"))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_policy_draft(fields={"description": "x"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: FalkorDB write failed: ")
    assert "boom" not in text
    assert "not a patchable field" not in text


def test_update_policy_draft_log_triad_carries_action_node_id_and_timestamp_but_no_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """`_run_mcp_action`'s log triad threads
    `entity_id` -- proven here for both a success call and a deliberate
    `PolicyNotFoundError` failure, plus the `started` entry every call emits.
    """
    emitter = configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_rig_graph(monkeypatch, _draft_fixture())
        success_result = _call_update_policy_draft(fields={"description": "x"})

        _install_rig_graph(monkeypatch, None)
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
        assert "principal" not in line
        assert isinstance(line["timestamp"], float)


# --- `add-standard-to-draft` --------------------------


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
    _install_rig_graph(monkeypatch, _draft_fixture())
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
    """A genuine round trip: add Standard -> get-policy, against the same
    rig graph the gateway writes, showing the new Standard's id genuinely round-trips into a
    second, real call.

    The Policy is seeded rather than created.
    """
    configure()
    _install_rig_graph(monkeypatch, _draft_fixture())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        add_result = _call_add_standard_to_draft(policy_id=_EXISTING_POLICY_ID)
        assert add_result.is_error is False
        new_standard_id = json.loads(_text(add_result))["standard_id"]

        get_result = _call_get_policy(policy_id=_EXISTING_POLICY_ID)

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
    _install_rig_graph(monkeypatch, _draft_fixture())
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
    _install_rig_graph(monkeypatch, _draft_fixture())
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
    _install_rig_graph(monkeypatch, _draft_fixture())
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
    _install_rig_graph(monkeypatch, policy)
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
    _install_rig_graph(monkeypatch, None)
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
    """The gateway failure is the persistence error, `FalkorDB write failed: ` prefixed."""
    configure()
    rig = _install_rig_graph(monkeypatch, _draft_fixture())
    rig.graphs.open("policy_system").fail_on_write(redis.exceptions.ConnectionError("boom"))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_standard_to_draft()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: FalkorDB write failed: ")
    assert "boom" not in text
    assert "not a patchable field" not in text


def test_add_standard_to_draft_log_triad_carries_action_node_id_and_timestamp_but_no_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """The log triad carries `entity_id=policy_id` --
    the parent, since it's known before the call, matching `add-standard-to-
    draft`'s own rule (the new Standard's id isn't known until
    `body()` returns, and `_run_mcp_action`'s `entity_id` is fixed first).
    """
    emitter = configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_rig_graph(monkeypatch, _draft_fixture())
        success_result = _call_add_standard_to_draft()

        _install_rig_graph(monkeypatch, None)
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
        assert "principal" not in line
        assert isinstance(line["timestamp"], float)


# --- `update-standard-draft` --------------------------

_EXISTING_STANDARD_ID = "std_encryption_standard_aaaaaa"


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
    _install_tree_rig(monkeypatch)
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
    """Real round trip: add Standard -> update-standard-draft,
    against the same rig graph the gateway writes, proving the new Standard's id genuinely
    round-trips into a second, real call -- not just a determinism check.

    The Policy is seeded rather than created.
    """
    configure()
    _install_rig_graph(monkeypatch, _draft_fixture())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        add_result = _call_add_standard_to_draft(policy_id=_EXISTING_POLICY_ID)
        assert add_result.is_error is False
        new_standard_id = json.loads(_text(add_result))["standard_id"]

        update_result = _call_update_standard_draft(
            standard_id=new_standard_id, fields={"description": "updated"}
        )

    assert update_result.is_error is False
    assert json.loads(_text(update_result)) == {
        "standard_id": new_standard_id,
        "policy_id": _EXISTING_POLICY_ID,
        "title": "Encryption Standard",
        "status": "draft",
    }


def test_update_standard_draft_unknown_field_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_tree_rig(monkeypatch)
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
    _install_tree_rig(monkeypatch)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_update_standard_draft(fields={"description": "hijacked"})

    assert result.is_error is False
    assert _text(result) == "error: you do not have access to this Policy"


def test_update_standard_draft_non_draft_status_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_tree_rig(monkeypatch, standard_status="proposed")
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
    _install_rig_graph(monkeypatch, None)
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
    """A missing Standard's own error text
    differs from a missing Policy's (`update-policy-draft`'s own error).
    """
    configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_rig_graph(monkeypatch, None)
        standard_result = _call_update_standard_draft(standard_id="std_missing")

        _install_rig_graph(monkeypatch, None)
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
    """The gateway failure is the persistence error, `FalkorDB write failed: ` prefixed."""
    configure()
    rig = _install_tree_rig(monkeypatch)
    rig.graphs.open("policy_system").fail_on_write(redis.exceptions.ConnectionError("boom"))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_standard_draft(fields={"description": "x"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: FalkorDB write failed: ")
    assert "boom" not in text
    assert "not a patchable field" not in text


def test_update_standard_draft_log_triad_carries_action_node_id_and_timestamp_but_no_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    emitter = configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_tree_rig(monkeypatch)
        success_result = _call_update_standard_draft(fields={"description": "x"})

        _install_rig_graph(monkeypatch, None)
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
        assert "principal" not in line
        assert isinstance(line["timestamp"], float)


def test_update_standard_draft_backfills_legacy_null_status_before_gating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy Standard with a `NULL` own
    `status` (e.g. minted before any backfilling call, such as via the
    internal-seed adapter) is still draft-eligible once backfilled by
    `_read_standard_with_parent_backfilled`, not spuriously rejected.
    """
    configure()
    rig = _install_tree_rig(monkeypatch, standard_status=None)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_standard_draft(fields={"description": "now editable"})

    assert result.is_error is False
    standard = rig.graphs.open("policy_system").nodes[("Standard", _EXISTING_STANDARD_ID)]
    assert standard["status"] == "draft"


# --- `add-control-to-draft` ----------------------------


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
    _install_tree_rig(monkeypatch)
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
    """The one deliberate divergence from `add-standard-to-draft`'s
    own `"draft"` default -- easy to copy-paste wrong.
    """
    configure()
    rig = _install_tree_rig(monkeypatch)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_control_to_draft()

    assert result.is_error is False
    new_control_id = json.loads(_text(result))["control_id"]
    props = rig.graphs.open("policy_system").nodes[("Control", new_control_id)]
    assert props["implementation_status"] == "planned"
    assert props["status"] == "draft"


def test_add_control_to_draft_response_id_usable_in_get_policy_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine round trip: add Standard -> add Control ->
    get-policy, against the same rig graph the gateway writes, showing the new Control's id
    genuinely round-trips into a real call.

    The Policy is seeded rather than created.
    """
    configure()
    _install_rig_graph(monkeypatch, _draft_fixture())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        add_standard_result = _call_add_standard_to_draft(policy_id=_EXISTING_POLICY_ID)
        assert add_standard_result.is_error is False
        new_standard_id = json.loads(_text(add_standard_result))["standard_id"]

        add_control_result = _call_add_control_to_draft(standard_id=new_standard_id)
        assert add_control_result.is_error is False
        new_control_id = json.loads(_text(add_control_result))["control_id"]

        get_result = _call_get_policy(policy_id=_EXISTING_POLICY_ID)

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
    _install_tree_rig(monkeypatch)
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
    """`fields.type` is rejected -- `control_type` is
    the single path to set a Control's type at creation.
    """
    configure()
    _install_tree_rig(monkeypatch)
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
    _install_tree_rig(monkeypatch)
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
    _install_tree_rig(monkeypatch)
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
    _install_tree_rig(monkeypatch)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_add_control_to_draft()

    assert result.is_error is False
    assert _text(result) == "error: you do not have access to this Policy"


def test_add_control_to_draft_non_draft_status_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_tree_rig(monkeypatch, standard_status="proposed")
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
    _install_rig_graph(monkeypatch, None)
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
    """The gateway failure is the persistence error, `FalkorDB write failed: ` prefixed."""
    configure()
    rig = _install_tree_rig(monkeypatch)
    rig.graphs.open("policy_system").fail_on_write(redis.exceptions.ConnectionError("boom"))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_control_to_draft()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: FalkorDB write failed: ")
    assert "boom" not in text
    assert "not a patchable field" not in text


def test_add_control_to_draft_log_triad_carries_action_node_id_and_timestamp_but_no_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """The log triad carries `entity_id=standard_id` --
    the parent, since it's known before the call (the new Control's id isn't
    known until `body()` returns).
    """
    emitter = configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_tree_rig(monkeypatch)
        success_result = _call_add_control_to_draft()

        _install_rig_graph(monkeypatch, None)
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
        assert "principal" not in line
        assert isinstance(line["timestamp"], float)


def test_add_control_to_draft_backfills_legacy_null_status_before_gating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy Standard with a `NULL` own
    `status` is still draft-eligible once backfilled by
    `_read_standard_with_parent_backfilled`, not spuriously rejected.
    """
    configure()
    rig = _install_tree_rig(monkeypatch, standard_status=None)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_add_control_to_draft()

    assert result.is_error is False
    standard = rig.graphs.open("policy_system").nodes[("Standard", _EXISTING_STANDARD_ID)]
    assert standard["status"] == "draft"


# --- `update-control-draft` ----------------------------

_EXISTING_CONTROL_ID = "ctrl_key_rotation_check_aaaaaa"


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
    _install_tree_rig(monkeypatch, with_control=True)
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
    """The task's own required proof: a REAL Policy -> Standard -> Control chain, built through
    the tool surface (`add-standard-to-draft` -> `add-control-to-draft`) onto a seeded Policy,
    then `update-control-draft` called against that same live rig graph -- not a one-hop
    stand-in, not a fixture asserted into existence. `find_control_with_parent`'s own two-hop
    `MATCH (p:Policy)-[:SUPPORTED_BY]->(s:Standard)-[:IMPLEMENTED_BY]->(c:Control)` query is the
    only thing that can resolve `update-control-draft`'s ownership here, since only the Policy
    node carries an owner.

    The Policy is seeded rather than created.
    """
    configure()
    _install_rig_graph(monkeypatch, _draft_fixture())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        add_standard_result = _call_add_standard_to_draft(policy_id=_EXISTING_POLICY_ID)
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
        "policy_id": _EXISTING_POLICY_ID,
        "title": "Key Rotation Check",
        "status": "draft",
    }


def test_update_control_draft_ownership_traverses_two_hops_end_to_end_rejects_non_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same real chain as above, but the caller isn't the root Policy's
    owner -- rejected purely via the two-hop traversal, since nothing
    Standard/Control-level carries an ownership concept in this graph either.
    """
    configure()
    rig = _install_rig_graph(monkeypatch, _draft_fixture())
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        add_standard_result = _call_add_standard_to_draft(policy_id=_EXISTING_POLICY_ID)
        new_standard_id = json.loads(_text(add_standard_result))["standard_id"]

        add_control_result = _call_add_control_to_draft(standard_id=new_standard_id)
        new_control_id = json.loads(_text(add_control_result))["control_id"]
    logged = len(rig.store.groups)

    with _verified_actor(sub=_OTHER_SUBJECT):
        update_result = _call_update_control_draft(
            control_id=new_control_id, fields={"description": "hijacked"}
        )

    assert update_result.is_error is False
    assert _text(update_result) == "error: you do not have access to this Policy"
    assert len(rig.store.groups) == logged


def test_update_control_draft_unknown_field_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_tree_rig(monkeypatch, with_control=True)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_control_draft(fields={"title": "sneaky rename"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "not a patchable field" in text


def test_update_control_draft_type_field_is_patchable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike `add-control-to-draft` (which rejects `fields.type`), `type` IS
    patchable through `update-control-draft` -- the only post-creation path
    to change a Control's type.
    """
    configure()
    _install_tree_rig(monkeypatch, with_control=True)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_control_draft(fields={"type": "automated"})

    assert result.is_error is False


def test_update_control_draft_non_owner_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_tree_rig(monkeypatch, with_control=True)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_OTHER_SUBJECT):
        result = _call_update_control_draft(fields={"description": "hijacked"})

    assert result.is_error is False
    assert _text(result) == "error: you do not have access to this Policy"


def test_update_control_draft_non_draft_status_returns_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_tree_rig(monkeypatch, with_control=True, control_status="proposed")
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
    _install_rig_graph(monkeypatch, None)
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
    """Extended to the third node type:
    a missing Control's own error text differs from both `update-standard-
    draft`'s and `update-policy-draft`'s own missing-node errors.
    """
    configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_rig_graph(monkeypatch, None)
        control_result = _call_update_control_draft(control_id="ctrl_missing")

        _install_rig_graph(monkeypatch, None)
        standard_result = _call_update_standard_draft(standard_id="std_missing")

        _install_rig_graph(monkeypatch, None)
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
    """The gateway failure is the persistence error, `FalkorDB write failed: ` prefixed."""
    configure()
    rig = _install_tree_rig(monkeypatch, with_control=True)
    rig.graphs.open("policy_system").fail_on_write(redis.exceptions.ConnectionError("boom"))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_control_draft(fields={"description": "x"})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: FalkorDB write failed: ")
    assert "boom" not in text
    assert "not a patchable field" not in text


def test_update_control_draft_log_triad_carries_action_node_id_and_timestamp_but_no_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    emitter = configure()
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        _install_tree_rig(monkeypatch, with_control=True)
        success_result = _call_update_control_draft(fields={"description": "x"})

        _install_rig_graph(monkeypatch, None)
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
        assert "principal" not in line
        assert isinstance(line["timestamp"], float)


def test_update_control_draft_backfills_legacy_null_status_before_gating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two-hop case: a legacy Control with a
    `NULL` own `status` (e.g. minted before any backfilling call, such as via
    the internal-seed adapter) is still draft-eligible once backfilled by
    `_read_control_with_parent_backfilled`, not spuriously rejected.
    """
    configure()
    rig = _install_tree_rig(monkeypatch, with_control=True, control_status=None)
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_control_draft(fields={"description": "now editable"})

    assert result.is_error is False
    control = rig.graphs.open("policy_system").nodes[("Control", _EXISTING_CONTROL_ID)]
    assert control["status"] == "draft"


# --- `propose-policy` -------------------------------------


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


def _install_fixture_rig(
    monkeypatch: pytest.MonkeyPatch,
    policy: _PolicyFixture | None,
    *,
    fork_of: tuple[str, tuple[str, ...]] | None = None,
) -> GatewayRig:
    """Seed `policy` (with its tree) into the process gateway rig and read through its double.

    The transition tools write through the Graph Write Gateway, so the graph they
    read is the rig graph the gateway writes. `fork_of=(prior_id, capability_ids)` makes the
    Policy a fork of a `deprecated` prior (not auto-deprecated) that governs `capability_ids`.
    """
    rig = installed_process_gateway().rig
    graph = rig.graphs.open("policy_system")
    seeder = PolicyGraphSeeder(graph)
    if policy is not None:
        seeder.seed_policy(
            policy.id,
            title=policy.title,
            status=policy.status,
            version=policy.version,
            owner=(policy.owner_subject, policy.owner_issuer),
            standards=tuple(
                SeedStandard(
                    standard.id,
                    standard.title,
                    status=standard.status,
                    controls=tuple(
                        SeedControl(
                            control.id,
                            control.title,
                            status=control.status,
                            control_type=control.control_type,
                        )
                        for control in standard.controls
                    ),
                )
                for standard in policy.standards
            ),
            legacy_edges=False,
        )
    if fork_of is not None:
        assert policy is not None
        prior_id, capability_ids = fork_of
        seeder.seed_policy(prior_id, status="deprecated", legacy_edges=False)
        seeder.seed_supersedes(prior_id, policy.id, legacy=False)
        for capability_id in capability_ids:
            seeder.seed_capability(capability_id)
            seeder.seed_governs(capability_id, prior_id, legacy=False)
    _install_graph(monkeypatch, PolicyReadGraph(graph))
    return rig


def _call_propose_policy(policy_id: str = _EXISTING_POLICY_ID) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("propose-policy", {"policy_id": policy_id}))
    assert isinstance(result, CallToolResult)
    return result


def test_propose_policy_tool_is_registered_and_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_fixture_rig(monkeypatch, _proposable_fixture())
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_propose_policy()

    assert result.is_error is False


def test_propose_policy_success_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    rig = _install_fixture_rig(monkeypatch, _proposable_fixture())
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
    assert len(rig.store.groups) == 1
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].outcome == "applied"
    assert audit_store.calls[0].action == "policy.propose"


def test_propose_policy_non_owner_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_fixture_rig(monkeypatch, _proposable_fixture(owner_subject=_OTHER_SUBJECT))
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
    _install_fixture_rig(monkeypatch, _proposable_fixture(standards=()))
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
    _install_fixture_rig(monkeypatch, _proposable_fixture(status="proposed"))
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
    _install_fixture_rig(monkeypatch, None)
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
    rig = _install_fixture_rig(monkeypatch, _proposable_fixture())
    rig.graphs.open("policy_system").fail_on_write(redis.exceptions.ConnectionError("boom"))
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_propose_policy()

    assert result.is_error is False
    assert _text(result).startswith("error: FalkorDB write failed: ")


def test_propose_policy_without_a_real_authenticated_caller_and_no_bypass_is_refused() -> None:
    configure()

    result = _call_propose_policy()

    assert result.is_error is False
    assert _text(result) == (
        "error: this action requires a real authenticated caller "
        "(the local-test bypass counts as one)"
    )


# --- `approve-policy` -------------------------------------

_MANAGER_SUBJECT = "policy-manager"


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
    _install_fixture_rig(monkeypatch, _approvable_fixture())
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_approve_policy()

    assert result.is_error is False


def test_approve_policy_success_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    rig = _install_fixture_rig(monkeypatch, _approvable_fixture())
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
        "governed_capability_ids": [],
    }
    assert len(rig.store.groups) == 1
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].outcome == "applied"
    assert audit_store.calls[0].action == "policy.approve"


def test_approve_policy_fork_returns_governed_capability_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    rig = _install_fixture_rig(
        monkeypatch, _approvable_fixture(), fork_of=("prior_old", ("cap_a", "cap_b"))
    )
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_approve_policy()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["governed_capability_ids"] == ["cap_a", "cap_b"]
    assert len(rig.store.groups) == 1


def test_approve_policy_governance_conflict_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    rig = _install_fixture_rig(
        monkeypatch, _approvable_fixture(), fork_of=("prior_old", ("cap_a",))
    )
    graph = rig.graphs.open("policy_system")

    def _third_party_takes_cap_a() -> None:
        del graph.edges[next(k for k in graph.edges if k[0] == "GOVERNED_BY")]
        PolicyGraphSeeder(graph).seed_governs("cap_a", "pol_third", legacy=False)

    _install_graph(
        monkeypatch, FlippingRead(graph, _third_party_takes_cap_a, marker="collect(cap.id)")
    )
    audit_store = _FakeAuditStore()
    _install_audit_store(monkeypatch, audit_store)
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_approve_policy()

    text = _text(result)
    assert text.startswith("error: ")
    assert "nothing was changed" in text
    assert [call.outcome for call in audit_store.calls] == ["applied", "failed"]
    assert rig.store.groups == []


def test_approve_policy_owner_self_approval_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_fixture_rig(monkeypatch, _approvable_fixture())
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
    _install_fixture_rig(monkeypatch, _approvable_fixture())
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
    _install_fixture_rig(monkeypatch, _approvable_fixture(status="draft"))
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
    _install_fixture_rig(monkeypatch, None)
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
    rig = _install_fixture_rig(monkeypatch, _approvable_fixture())
    rig.graphs.open("policy_system").fail_on_write(redis.exceptions.ConnectionError("boom"))
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_approve_policy()

    assert result.is_error is False
    assert _text(result).startswith("error: FalkorDB write failed: ")


def test_approve_policy_without_a_real_authenticated_caller_and_no_bypass_is_refused() -> None:
    configure()

    result = _call_approve_policy()

    assert result.is_error is False
    assert _text(result) == (
        "error: this action requires a real authenticated caller "
        "(the local-test bypass counts as one)"
    )


# --- `reject-policy` ---------------------------------------
#
# Mirrors `approve-policy`'s own test list exactly (same
# `_ApproveFakeGraph`/`_approvable_fixture`/`_install_manager_role`
# fixtures, reused unchanged), proving the tool-layer wrapper is genuinely
# the same pattern, not reimplemented differently.


def _call_reject_policy(policy_id: str = _EXISTING_POLICY_ID) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("reject-policy", {"policy_id": policy_id}))
    assert isinstance(result, CallToolResult)
    return result


def test_reject_policy_tool_is_registered_and_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    _install_fixture_rig(monkeypatch, _approvable_fixture())
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_reject_policy()

    assert result.is_error is False


def test_reject_policy_success_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    rig = _install_fixture_rig(monkeypatch, _approvable_fixture())
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
    assert len(rig.store.groups) == 1
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].outcome == "applied"
    assert audit_store.calls[0].action == "policy.reject"


def test_reject_policy_owner_self_rejection_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    _install_fixture_rig(monkeypatch, _approvable_fixture())
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
    _install_fixture_rig(monkeypatch, _approvable_fixture())
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
    _install_fixture_rig(monkeypatch, _approvable_fixture(status="draft"))
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
    _install_fixture_rig(monkeypatch, None)
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
    rig = _install_fixture_rig(monkeypatch, _approvable_fixture())
    rig.graphs.open("policy_system").fail_on_write(redis.exceptions.ConnectionError("boom"))
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_manager_role(monkeypatch)

    with _verified_actor(sub=_MANAGER_SUBJECT):
        result = _call_reject_policy()

    assert result.is_error is False
    assert _text(result).startswith("error: FalkorDB write failed: ")


def test_reject_policy_without_a_real_authenticated_caller_and_no_bypass_is_refused() -> None:
    configure()

    result = _call_reject_policy()

    assert result.is_error is False
    assert _text(result) == (
        "error: this action requires a real authenticated caller "
        "(the local-test bypass counts as one)"
    )


# --- `revert-policy-to-draft` ------------------------------
#
# Mirrors `propose-policy`'s own test list (owner-only, no
# `access_role_store` involved at all) rather than `approve-policy`'s
# `PolicyManager`-gated shape. The non-owner test below deliberately grants
# the non-owner caller `PolicyManager` to prove this tool's own action is
# genuinely owner-only, not role-gated.


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
    _install_fixture_rig(monkeypatch, _revertable_fixture())
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_revert_policy_to_draft()

    assert result.is_error is False


def test_revert_policy_to_draft_success_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    rig = _install_fixture_rig(monkeypatch, _revertable_fixture())
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
    assert len(rig.store.groups) == 1
    assert len(audit_store.calls) == 1
    assert audit_store.calls[0].outcome == "applied"
    assert audit_store.calls[0].action == "policy.revert"


def test_revert_policy_to_draft_non_owner_policy_manager_surfaces_as_its_own_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `PolicyManager` who is not the owner is still rejected -- no RBAC gate exists here."""
    configure()
    _install_fixture_rig(monkeypatch, _revertable_fixture(owner_subject=_OTHER_SUBJECT))
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
    _install_fixture_rig(monkeypatch, _revertable_fixture(status="draft"))
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
    _install_fixture_rig(monkeypatch, None)
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
    rig = _install_fixture_rig(monkeypatch, _revertable_fixture())
    rig.graphs.open("policy_system").fail_on_write(redis.exceptions.ConnectionError("boom"))
    _install_audit_store(monkeypatch, _FakeAuditStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_revert_policy_to_draft()

    assert result.is_error is False
    assert _text(result).startswith("error: FalkorDB write failed: ")


def test_revert_policy_to_draft_no_auth_caller_and_no_bypass_is_refused() -> None:
    configure()

    result = _call_revert_policy_to_draft()

    assert result.is_error is False
    assert _text(result) == (
        "error: this action requires a real authenticated caller "
        "(the local-test bypass counts as one)"
    )


# --- Dedicated local-test-bypass verification ---
#
# `test_bypass_active_creates_a_policy_owned_by_the_bypass_principal` above
# already covers `create-policy-draft`'s `actor_subject`/
# `owner_subject` half through the tool's own success response
# and its recorded audit row. The two tests below close the remaining gaps:
# (1) the `owner_issuer` graph property -- not part of the tool's own
# success response shape (`{"policy_id", "title", "status", "version",
# "owner_subject"}` has no `owner_issuer` key) -- is also the bypass
# principal, confirmed by inspecting the graph write's own params directly;
# (2) the regression guard that `approve-policy`/`reject-policy` are ALWAYS
# rejected by `block_self_approval` under the bypass, because the bypass
# makes approve/reject not exercisable locally by design, not a defect.


def test_bypass_sets_owner_subject_and_owner_issuer_to_the_bypass_principal_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new Policy's `owner_issuer` graph property -- not just
    `owner_subject` (already covered by
    `test_bypass_active_creates_a_policy_owned_by_the_bypass_principal` above)
    -- is also `LOCAL_TEST_PRINCIPAL_ID` under the bypass, proving the
    synthetic owner pair is genuinely `(LOCAL_TEST_PRINCIPAL_ID,
    LOCAL_TEST_PRINCIPAL_ID)`, not a real issuer paired with a synthetic
    subject.
    """
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _install_graph(monkeypatch, _FakeGraph())
    _install_audit_store(monkeypatch, _FakeAuditStore())

    result = _call_create_policy_draft()

    assert result.is_error is False
    policy_id = json.loads(_text(result))["policy_id"]
    properties = (
        installed_process_gateway().rig.graphs.open("policy_system").nodes[("Policy", policy_id)]
    )
    assert properties["owner_subject"] == LOCAL_TEST_PRINCIPAL_ID
    assert properties["owner_issuer"] == LOCAL_TEST_PRINCIPAL_ID


def _bypass_owned_proposed_fixture() -> _PolicyFixture:
    """A `"proposed"` Policy owned by the bypass principal pair."""
    return _PolicyFixture(
        id=_EXISTING_POLICY_ID,
        title=_TITLE,
        status="proposed",
        version="1",
        owner_subject=LOCAL_TEST_PRINCIPAL_ID,
        owner_issuer=LOCAL_TEST_PRINCIPAL_ID,
    )


def _install_bypass_manager_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grants `PolicyManager` to the bypass principal pair itself.

    Needed so `approve-policy`/`reject-policy`'s own RBAC gate
    (`check_role`) passes -- the point of these regression
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
    """Regression guard: under the bypass,
    `_resolve_policy_lifecycle_actor` returns the SAME fixed
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
    _install_fixture_rig(monkeypatch, _bypass_owned_proposed_fixture())
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_bypass_manager_role(monkeypatch)

    result = _call_approve_policy()

    assert result.is_error is False
    assert _text(result) == "error: you cannot approve or reject a Policy you own"


def test_bypass_reject_policy_is_always_rejected_by_self_approval_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors `test_bypass_approve_policy_is_always_rejected_by_self_approval_block`
    above for `reject-policy` -- same bypass-forced `owner == actor` reasoning,
    not a defect.
    """
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _install_fixture_rig(monkeypatch, _bypass_owned_proposed_fixture())
    _install_audit_store(monkeypatch, _FakeAuditStore())
    _install_bypass_manager_role(monkeypatch)

    result = _call_reject_policy()

    assert result.is_error is False
    assert _text(result) == "error: you cannot approve or reject a Policy you own"


# --- Bypass proofs ---
#
# Every other test for these six tools uses `_verified_actor`, so none exercises
# the local-test bypass. Each test below proves BOTH halves of the bypass
# behaviour for its own tool:
# `_resolve_policy_lifecycle_actor` resolves the fixed `LOCAL_TEST_PRINCIPAL_ID`
# pair as `actor` under the bypass (no `_verified_actor` context is entered
# anywhere in these tests), AND `require_owner`'s `(sub, iss)` tuple-equality
# check passes against a node whose recorded owner is that exact same pair --
# i.e. a draft "created under the bypass" (which
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
    _install_rig_graph(monkeypatch, fixture)
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
    _install_rig_graph(monkeypatch, fixture)
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
    _install_tree_rig(
        monkeypatch, owner_subject=LOCAL_TEST_PRINCIPAL_ID, owner_issuer=LOCAL_TEST_PRINCIPAL_ID
    )
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
    _install_tree_rig(
        monkeypatch, owner_subject=LOCAL_TEST_PRINCIPAL_ID, owner_issuer=LOCAL_TEST_PRINCIPAL_ID
    )
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
    _install_tree_rig(
        monkeypatch,
        owner_subject=LOCAL_TEST_PRINCIPAL_ID,
        owner_issuer=LOCAL_TEST_PRINCIPAL_ID,
        with_control=True,
    )
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    result = _call_update_control_draft(fields={"description": "revised text"})

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["control_id"] == _EXISTING_CONTROL_ID
    assert body["policy_id"] == _EXISTING_POLICY_ID


def test_bypass_create_policy_draft_supersedes_fork_owned_and_editable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sixth bypass-proof tool: `create-policy-draft` with `supersedes_policy_id`.

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
    _install_stateful_rig(monkeypatch)
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


# --- Unexpected-error hardening ---
#
# Sibling tool suites (`test_near_miss_tools.py`, `test_restore_instrument_
# tool.py`, `test_ingest_regulation_tool.py`, `test_check_regulations_tool.py`,
# `test_get_catalog_listing_tool.py`) each have a dedicated test proving an
# unclassified exception from the underlying call never crosses the MCP
# boundary raw -- `_run_mcp_action`'s residual safety net sanitises it to the
# fixed `_UNEXPECTED_ERROR_MESSAGE` string. Each test below does the same for
# one of these tools: it raises a raw exception carrying a fake
# host:port string from the underlying service call and asserts neither the
# host nor the port text leaks into the returned error string.


def test_create_policy_draft_unexpected_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike a gateway failure (`test_graph_unavailable_surfaces_as_its_own_distinct_error`
    above, the named persistence error), a non-gateway exception (e.g. a driver bug, a malformed
    response) is genuinely unclassified and must fall through to `_run_mcp_action`'s residual
    safety net instead of a named message. Scripted for real via the log store's failure, not by
    replacing the tool's own delegate.
    """
    configure()
    rig = installed_process_gateway().rig
    rig.store.fail_appends(error=RuntimeError("connection refused to db-internal-7.prod:6379"))
    _install_graph(monkeypatch, _FakeGraph())
    _install_audit_store(monkeypatch, _FakeAuditStore())

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
    """See `test_create_policy_draft_unexpected_error_is_sanitized` above:
    same non-`RedisError`-escapes-the-catch shape, this tool's own fixture.
    """
    configure()
    rig = _install_rig_graph(monkeypatch, _draft_fixture())
    rig.store.fail_appends(error=RuntimeError("connection refused to db-internal-7.prod:6379"))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

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
    """See `test_create_policy_draft_unexpected_error_is_sanitized` above."""
    configure()
    rig = _install_rig_graph(monkeypatch, _draft_fixture())
    rig.store.fail_appends(error=RuntimeError("connection refused to db-internal-7.prod:6379"))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

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
    """See `test_create_policy_draft_unexpected_error_is_sanitized` above."""
    configure()
    rig = _install_tree_rig(monkeypatch)
    rig.store.fail_appends(error=RuntimeError("connection refused to db-internal-7.prod:6379"))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

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
    """See `test_create_policy_draft_unexpected_error_is_sanitized` above."""
    configure()
    rig = _install_tree_rig(monkeypatch)
    rig.store.fail_appends(error=RuntimeError("connection refused to db-internal-7.prod:6379"))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

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
    """See `test_create_policy_draft_unexpected_error_is_sanitized` above."""
    configure()
    rig = _install_tree_rig(monkeypatch, with_control=True)
    rig.store.fail_appends(error=RuntimeError("connection refused to db-internal-7.prod:6379"))
    _install_access_role_store(monkeypatch, FakeAccessRoleStore())

    with _verified_actor(sub=_ACTOR_SUBJECT):
        result = _call_update_control_draft(fields={"description": "x"})

    assert result.is_error is False
    text = _text(result)
    assert text == "error: an unexpected error occurred"
    assert "db-internal-7.prod" not in text
    assert "6379" not in text
