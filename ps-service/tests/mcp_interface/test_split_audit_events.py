"""One tool per group: the audit event it emits keeps its action name, wherever the tool lives.

The MCP surface is spread over one module per tool group. These tests call one real tool of every
group in-process and pin the audit action names the call writes, so a tool that moves to a new
module cannot silently lose or rename its audit row. They reuse the fixtures of the per-tool test
files rather than redeclaring them. The registered set of action names itself is pinned by the
audit-action registry snapshot of the refactor; this file pins emission.

Limitations: the access-role store is the in-memory fake, which keeps its grant events as roster
rows rather than `audit_events` rows, so the access group is pinned through the gate row of its
audit-trail reader (`access.gate`) and the granted roster entry; the catalog group is pinned
through the `user.invite` row of `invite-user` (the in-memory runtime-config store writes no
`runtime_config.set` row); the cleanup group is pinned through the `access.gate` row of a denied
`release-capability-governance` call, because its own `capability.release_governance` row is
written at approval time, outside the tool call.
"""

from __future__ import annotations

import asyncio
import functools
import json
from typing import TYPE_CHECKING

import pytest
from api._gateway_fakes import installed_process_gateway
from audit._fakes import InMemoryAuditStore, audit_store_factory
from authz._fakes import FakeAccessRoleStore, FakeAuditStore
from graph_cleanup._fakes import SURVIVOR, ScriptedReleaseGraph
from invitations.test_client import (
    _RecordingTransport,  # pyright: ignore[reportPrivateUsage]  -- reuse `create_invitation`'s fake-transport double
)
from mcp.types import CallToolResult, TextContent

from mcp_interface.test_access_role_tools import (
    _ACTOR_ISSUER,  # pyright: ignore[reportPrivateUsage]  -- reuse the access-role tool tests' caller fixtures
    _FIRST_CALLER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _SECOND_CALLER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _call_grant_access_role,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _call_list_access_roles,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _call_list_audit_events,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp_interface.test_access_role_tools import (
    _fake_store_factory as _access_store_factory,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp_interface.test_access_role_tools import (
    _override as _access_override,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp_interface.test_access_role_tools import (
    _server as _access_server,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp_interface.test_access_role_tools import (
    _verified_actor as _access_actor,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp_interface.test_create_policy_draft_gateway import (
    _create as _create_policy_draft,  # pyright: ignore[reportPrivateUsage]  -- reuse the gateway-rig tool call
)
from mcp_interface.test_create_policy_draft_gateway import (
    _setup as _setup_policy_rig,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp_interface.test_create_policy_draft_gateway import (
    _verified_actor as _policy_actor,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp_interface.test_cypher_tool import (
    _FakeGraphHandle,  # pyright: ignore[reportPrivateUsage]  -- reuse the cypher tool test's graph fake
    _FakeQueryResult,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _install_graph,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _override,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _server,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp_interface.test_graph_cleanup_release_governance_tool import (
    _Env as _CleanupEnv,  # pyright: ignore[reportPrivateUsage]  -- reuse the release-governance tool test's environment
)
from mcp_interface.test_ingest_regulation_tool import (
    _CELEX,  # pyright: ignore[reportPrivateUsage]  -- reuse the curated-catalog fixture
    _SHORT_NAME,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _call_ingest_regulation,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _configure_complete_llm_env,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _use_real_pipeline_stages,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp_interface.test_near_miss_tools import (
    _call_near_misses_resolve,  # pyright: ignore[reportPrivateUsage]  -- reuse the near-miss tool test's call helper
    _script_keep_separate,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.invitations.client import create_invitation
from ps_service.logging import configure
from ps_service.mcp_interface import catalog_tools

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def audit_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryAuditStore:
    """Route every audit row of the call to memory, registering ids with the process rig."""
    configure()

    def _register(event_id: str) -> None:
        installed_process_gateway().rig.store.audit_rows.add(event_id)

    store = InMemoryAuditStore(event_id_sink=_register)
    _access_override(monkeypatch, audit_store_factory=audit_store_factory(store))
    return store


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_ingestion_group_ingest_regulation_audits_submit_then_complete(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _use_real_pipeline_stages(monkeypatch)
    _access_override(monkeypatch, audit_store_factory=audit_store_factory(audit_store))

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert [row.action for row in audit_store.rows] == [
        "ingestion_run.submit",
        "ingestion_run.complete",
    ]


def test_near_miss_group_keep_separate_audits_near_miss_resolve(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _script_keep_separate(monkeypatch)
    installed_process_gateway().rig.graphs.open("policy_system").seed_node(
        ("PendingReview",), "review_aaa", {"kind": "Capability"}
    )

    result = _call_near_misses_resolve("review_aaa", "keep-separate")

    assert json.loads(_text(result))["decision"] == "keep-separate"
    assert [row.action for row in audit_store.rows] == ["near_miss.resolve"]


def test_catalog_group_invite_user_audits_user_invite(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    monkeypatch.setenv("PS_AUTHENTIK_API_TOKEN", "test-authentik-token")
    monkeypatch.setenv("PS_AUTHENTIK_BASE_URL", "https://authentik.example.com")
    transport = _RecordingTransport(json.dumps({"pk": "tok-abc"}).encode())
    monkeypatch.setattr(
        catalog_tools,
        "create_invitation",
        functools.partial(create_invitation, transport=transport),
    )

    result = asyncio.run(_access_server().call_tool("invite-user", {"email": "target@example.com"}))

    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    assert [row.action for row in audit_store.rows] == ["user.invite"]


def test_cypher_group_cypher_audits_query_cypher(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    handle = _FakeGraphHandle(
        result=_FakeQueryResult(header=[[0, "id"]], result_set=[["a"]]),
    )
    _install_graph(monkeypatch, handle)
    _override(monkeypatch, audit_store_factory=audit_store_factory(audit_store))

    result = asyncio.run(_server().call_tool("cypher", {"query": "MATCH (n) RETURN n.id"}))

    assert isinstance(result, CallToolResult)
    assert json.loads(_text(result))["row_count"] == 1
    assert [row.action for row in audit_store.rows] == ["query.cypher"]


def test_access_group_grant_succeeds_and_a_denied_audit_read_audits_access_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    monkeypatch.setenv("PS_AUTH_ISSUER", _ACTOR_ISSUER)
    roles = FakeAccessRoleStore(expected_owner=(_FIRST_CALLER_SUBJECT, _ACTOR_ISSUER))
    trail = FakeAuditStore()
    _access_override(
        monkeypatch,
        role_store_factory=_access_store_factory(roles),
        audit_store_factory=_access_store_factory(trail),
    )

    with _access_actor(sub=_FIRST_CALLER_SUBJECT):
        _call_list_access_roles()
        granted = _call_grant_access_role(_SECOND_CALLER_SUBJECT, "SystemAdmin")
    with _access_actor(sub="stranger"):
        denied = _call_list_audit_events()

    assert granted.is_error is False
    assert (_SECOND_CALLER_SUBJECT, "SystemAdmin") in {
        (row.principal_subject, row.access_role.value) for row in roles.list_all_assignments()
    }
    assert _text(denied).startswith("error: You do not have the required access role")
    assert trail.standalone_actions == ["access.gate"]


def test_policy_lifecycle_group_create_policy_draft_audits_policy_create_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, audit = _setup_policy_rig(monkeypatch)

    with _policy_actor(sub="policy-author"):
        result = _create_policy_draft()

    assert result.is_error is False
    assert [row.action for row in audit.rows] == ["policy.create_draft"]


def test_graph_cleanup_group_a_denied_release_governance_audits_access_gate(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    env = _CleanupEnv(monkeypatch, ScriptedReleaseGraph())

    text = env.call("release-capability-governance", {"capability_id": SURVIVOR}, sub="stranger")

    assert text.startswith("error: You do not have the required access role")
    assert [row.action for row in audit_store.rows] == ["access.gate"]


_PER_GROUP: dict[str, Callable[..., None]] = {
    "ingestion": test_ingestion_group_ingest_regulation_audits_submit_then_complete,
    "near_miss": test_near_miss_group_keep_separate_audits_near_miss_resolve,
    "catalog": test_catalog_group_invite_user_audits_user_invite,
    "cypher": test_cypher_group_cypher_audits_query_cypher,
    "access_audit": test_access_group_grant_succeeds_and_a_denied_audit_read_audits_access_gate,
    "policy_lifecycle": test_policy_lifecycle_group_create_policy_draft_audits_policy_create_draft,
    "graph_cleanup": test_graph_cleanup_group_a_denied_release_governance_audits_access_gate,
}


def test_every_tool_group_has_an_emission_test() -> None:
    assert sorted(_PER_GROUP) == [
        "access_audit",
        "catalog",
        "cypher",
        "graph_cleanup",
        "ingestion",
        "near_miss",
        "policy_lifecycle",
    ]
