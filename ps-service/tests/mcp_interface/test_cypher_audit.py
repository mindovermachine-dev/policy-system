"""One `query.cypher` audit row per `cypher` tool call; nothing from any other caller.

The tool writes the row (`applied`, `rejected` or `failed`) with details limited to `row_count`,
`truncated` and an enumerated `reason_code`, never query text or database error text. The row
actor is the verified caller, or the bypass sentinel under the local-test bypass. In-process
callers of `execute_cypher_query` (and `handle_mcp_tool_call` called directly) write no row, and
the query engine does not depend on the audit package.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from audit._fakes import InMemoryAuditStore, audit_store_factory
from authz._fakes import FakeAccessRoleStore
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from mcp_interface import test_check_instrument_ingestion_status_tool as ingestion_status_support
from mcp_interface.test_access_role_tools import (
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  # reuse the sibling test module's fixtures verbatim
)
from mcp_interface.test_cypher_tool import (
    _FakeGraphHandle,  # pyright: ignore[reportPrivateUsage]  # reuse the sibling test module's fixtures verbatim
    _FakeQueryResult,  # pyright: ignore[reportPrivateUsage]  # reuse the sibling test module's fixtures verbatim
    _install_graph,  # pyright: ignore[reportPrivateUsage]  # reuse the sibling test module's fixtures verbatim
)
from ps_service.audit.actor import LOCAL_TEST_BYPASS_AUDIT_ACTOR
from ps_service.audit.errors import AuditPostgresUnavailableError
from ps_service.audit.models import AuditQueryFilters, is_known_resource_type, resolve_details_model
from ps_service.authz.service import list_audit_events
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.audit_actions import QueryCypherDetails
from ps_service.query_engine import cypher_query

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path as _Path

    type ReadLines = Callable[[_Path], list[dict[str, object]]]

_CALLER = "caller-1"
_ISSUER = "https://issuer.example.com/"
_CANARY = "CANARY-7f3a91c4"
_OWNER = ("owner", _ISSUER)
_MATCH = "MATCH (n) RETURN n.id"
_ROWS = _FakeQueryResult(header=[[0, "id"]], result_set=[["a"], ["b"], ["c"], ["d"]])


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> InMemoryAuditStore:
    """Route the tool's audit rows to memory."""
    configure()
    store = InMemoryAuditStore()
    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", audit_store_factory(store))
    return store


def _cypher(query: str, *, sub: str | None = _CALLER) -> CallToolResult:
    if sub is None:
        result = asyncio.run(mcp_server.server.call_tool("cypher", {"query": query}))
    else:
        with _verified_actor(sub=sub):
            result = asyncio.run(mcp_server.server.call_tool("cypher", {"query": query}))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _only_row(store: InMemoryAuditStore):
    (row,) = store.rows
    return row


def test_successful_query_writes_one_applied_row_with_row_count_and_truncated_false(
    monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore
) -> None:
    _install_graph(monkeypatch, _FakeGraphHandle(result=_ROWS))
    result = _cypher(_MATCH)
    assert result.is_error is False
    row = _only_row(audit)
    assert (row.action, row.outcome) == ("query.cypher", "applied")
    assert (row.actor_subject, row.actor_issuer) == (_CALLER, _ISSUER)
    assert (row.resource_type, row.resource_id) == ("query", "cypher")
    assert row.details == {"row_count": 4, "truncated": False}


def test_row_cap_truncation_is_recorded_as_truncated_true(
    monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore
) -> None:
    monkeypatch.setenv("PS_QUERY_ROW_CAP", "2")
    _install_graph(monkeypatch, _FakeGraphHandle(result=_ROWS))
    _cypher(_MATCH)
    assert _only_row(audit).details == {"row_count": 2, "truncated": True}


def test_write_clause_query_writes_one_rejected_row_with_write_clause_reason(
    monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore
) -> None:
    _install_graph(monkeypatch, _FakeGraphHandle(result=_ROWS))
    result = _cypher("CREATE (n:Policy)")
    assert _text(result).startswith("error: ")
    row = _only_row(audit)
    assert (row.action, row.outcome) == ("query.cypher", "rejected")
    assert row.details == {"reason_code": "write_clause"}


def test_failing_query_writes_one_failed_row_with_query_failed_reason(
    monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore
) -> None:
    _install_graph(monkeypatch, _FakeGraphHandle(error=RuntimeError("syntax error near x")))
    result = _cypher(_MATCH)
    assert _text(result).startswith("error: ")
    row = _only_row(audit)
    assert (row.action, row.outcome) == ("query.cypher", "failed")
    assert row.details == {"reason_code": "query_failed"}


def test_unseeded_graph_writes_one_failed_row_with_graph_unseeded_reason(
    monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore
) -> None:
    unseeded = ingestion_status_support._FakeGraphHandle(  # pyright: ignore[reportPrivateUsage]  # reuse the sibling test module's fixtures verbatim
        ingested_celex=(), unseeded=True
    )
    ingestion_status_support._install_graph(monkeypatch, unseeded)  # pyright: ignore[reportPrivateUsage]  # reuse the sibling test module's fixtures verbatim
    result = _cypher(_MATCH)
    assert _text(result).startswith("error: ")
    row = _only_row(audit)
    assert (row.action, row.outcome) == ("query.cypher", "failed")
    assert row.details == {"reason_code": "graph_unseeded"}


def test_unreachable_graph_writes_one_failed_row_with_graph_unavailable_reason(
    monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore
) -> None:
    def _boom(_config: object) -> object:
        raise ConnectionError("refused")

    monkeypatch.setattr(mcp_server, "connect_from_config", _boom)
    result = _cypher(_MATCH)
    assert _text(result) == "error: the policy graph database is not reachable"
    row = _only_row(audit)
    assert (row.action, row.outcome) == ("query.cypher", "failed")
    assert row.details == {"reason_code": "graph_unavailable"}


def test_cypher_with_unloadable_config_writes_no_row_and_keeps_its_result_and_log_line(
    monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore, read_lines: ReadLines
) -> None:
    emitter = configure()
    monkeypatch.setenv("PS_FALKORDB_PORT", "not-a-port")
    result = _cypher(_MATCH)
    emitter.flush()
    assert _text(result) == "error: the policy graph database is not reachable"
    assert audit.rows == []
    unavailable = [
        e for e in read_lines(resolve_default_log_path()) if e.get("outcome") == "unavailable"
    ]
    assert len(unavailable) == 1


def test_audit_write_failure_leaves_the_cypher_result_unchanged(
    monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore
) -> None:
    _install_graph(monkeypatch, _FakeGraphHandle(result=_ROWS))
    expected = json.loads(_text(_cypher(_MATCH)))
    audit.rows.clear()
    audit.fail_on_action["query.cypher"] = AuditPostgresUnavailableError("db host 10.1.2.3 down")
    result = _cypher(_MATCH)
    assert result.is_error is False
    assert json.loads(_text(result)) == expected
    assert audit.rows == []


def test_a_call_with_no_resolvable_actor_writes_no_row_and_keeps_its_result(
    monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore
) -> None:
    _install_graph(monkeypatch, _FakeGraphHandle(result=_ROWS))
    result = _cypher(_MATCH, sub=None)
    assert result.is_error is False
    assert json.loads(_text(result))["row_count"] == 4
    assert audit.rows == []


def test_row_details_contain_only_row_count_truncated_and_reason_code(
    monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore
) -> None:
    _install_graph(monkeypatch, _FakeGraphHandle(result=_ROWS))
    _cypher(_MATCH)
    _cypher("CREATE (n)")
    assert audit.rows
    for row in audit.rows:
        assert set(row.details) <= {"row_count", "truncated", "reason_code"}


def test_query_cypher_details_reject_an_undeclared_field() -> None:
    with pytest.raises(ValidationError):
        QueryCypherDetails.model_validate({"row_count": 1, "query": "MATCH (n) RETURN n"})
    with pytest.raises(ValidationError):
        InMemoryAuditStore().record_standalone(
            actor_subject="a",
            actor_issuer="b",
            action="query.cypher",
            resource_type="query",
            resource_id="cypher",
            outcome="applied",
            details={"row_count": 1, "query": "x"},
        )


def test_query_cypher_is_registered_with_the_query_resource_type() -> None:
    assert resolve_details_model("query.cypher") is QueryCypherDetails
    assert is_known_resource_type("query")


def test_handle_mcp_tool_call_returns_exactly_what_it_returned_before_the_refactor() -> None:
    configure()
    graph = _FakeGraphHandle(result=_ROWS)
    ok = mcp_server.handle_mcp_tool_call(_MATCH, graph=graph, timeout_ms=100, row_cap=2)
    assert ok == {"columns": ["id"], "rows": [["a"], ["b"]], "row_count": 2, "truncated": True}
    rejected = mcp_server.handle_mcp_tool_call("CREATE (n)", graph=graph, timeout_ms=1, row_cap=2)
    assert isinstance(rejected, str)
    assert rejected.startswith("error: ")


def test_cypher_row_is_returned_by_list_audit_events_for_actor_and_action(
    monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore
) -> None:
    _install_graph(monkeypatch, _FakeGraphHandle(result=_ROWS))
    _cypher(_MATCH)
    roles = FakeAccessRoleStore(expected_owner=_OWNER)
    roles.bootstrap_first_owner(_OWNER)
    page = list_audit_events(
        _OWNER,
        filters=AuditQueryFilters(
            actor_subject=_CALLER, action="query.cypher", resource_type="query"
        ),
        cursor=None,
        page_size=10,
        access_role_store=roles,
        audit_store=audit,
    )
    assert [(e.actor_subject, e.action, e.resource_type, e.resource_id) for e in page.events] == [
        (_CALLER, "query.cypher", "query", "cypher")
    ]


# --- bypass, canary, in-process callers -------------------------------------------------


def test_cypher_under_the_local_test_bypass_records_the_bypass_sentinel_as_actor(
    monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _install_graph(monkeypatch, _FakeGraphHandle(result=_ROWS))
    _cypher(_MATCH, sub=None)
    row = _only_row(audit)
    assert (row.actor_subject, row.actor_issuer) == (
        LOCAL_TEST_BYPASS_AUDIT_ACTOR,
        LOCAL_TEST_BYPASS_AUDIT_ACTOR,
    )


def _dump(store: InMemoryAuditStore) -> str:
    return repr(store.rows)


@pytest.mark.parametrize("shape", ["success", "rejection", "failure"])
def test_a_canary_in_the_query_text_appears_in_no_audit_row_on_success_rejection_or_failure(
    shape: str, monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore
) -> None:
    if shape == "failure":
        _install_graph(monkeypatch, _FakeGraphHandle(error=RuntimeError("boom")))
    else:
        _install_graph(monkeypatch, _FakeGraphHandle(result=_ROWS))
    query = (
        f"CREATE (n {{x: '{_CANARY}'}})"
        if shape == "rejection"
        else f"MATCH (n) WHERE n.x = '{_CANARY}' RETURN n.id"
    )
    _cypher(query)
    assert len(audit.rows) == 1
    assert _CANARY not in _dump(audit)


def test_a_canary_in_the_database_error_text_appears_in_no_audit_row(
    monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore
) -> None:
    _install_graph(monkeypatch, _FakeGraphHandle(error=RuntimeError(f"bad token {_CANARY}")))
    result = _cypher(_MATCH)
    assert _CANARY in _text(result)  # the caller still sees the engine's message
    assert len(audit.rows) == 1
    assert _CANARY not in _dump(audit)


def test_check_instrument_ingestion_status_writes_no_query_cypher_row(
    monkeypatch: pytest.MonkeyPatch, audit: InMemoryAuditStore
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    handle = ingestion_status_support._FakeGraphHandle(ingested_celex=("32016R0679",))  # pyright: ignore[reportPrivateUsage]  # reuse the sibling test module's fixtures verbatim
    ingestion_status_support._install_graph(monkeypatch, handle)  # pyright: ignore[reportPrivateUsage]  # reuse the sibling test module's fixtures verbatim
    result = ingestion_status_support._call_check_instrument_ingestion_status(["32016R0679"])  # pyright: ignore[reportPrivateUsage]  # reuse the sibling test module's fixtures verbatim
    assert result.is_error is False
    assert [row for row in audit.rows if row.action == "query.cypher"] == []


def test_handle_mcp_tool_call_called_directly_writes_no_query_cypher_row(
    audit: InMemoryAuditStore,
) -> None:
    mcp_server.handle_mcp_tool_call(
        _MATCH, graph=_FakeGraphHandle(result=_ROWS), timeout_ms=100, row_cap=10
    )
    assert audit.rows == []


def test_execute_cypher_query_module_does_not_depend_on_the_audit_package() -> None:
    source = Path(cypher_query.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    assert [m for m in imported if m.startswith("ps_service.audit")] == []
