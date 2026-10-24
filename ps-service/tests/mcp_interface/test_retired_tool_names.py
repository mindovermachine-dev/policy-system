"""The tool list carries the verb-first names, and a retired name is an unknown tool.

Retired names are built by concatenation so this file never spells one; a repo-wide search for
them stays empty while the negative tests still exist. Calling a retired name must fail the
standard unknown-tool way before any store factory is opened or any audit row is written.
"""

from __future__ import annotations

import asyncio

import pytest
from audit._fakes import InMemoryAuditStore, audit_store_factory
from mcp.server.mcpserver.exceptions import ToolError

from mcp_interface._environment import build_test_server, touched
from ps_service.mcp_interface import mcp_server


def _joined(*parts: str, separator: str = "_") -> str:
    return separator.join(parts)


_RETIRED_BY_CURRENT_NAME = {
    "ingest-regulation": _joined("ingest", "regulation"),
    "start-ingestion": _joined("start", "ingestion"),
    "get-ingestion-status": _joined("get", "ingestion", "status"),
    "check-regulations": _joined("check", "regulations"),
    "list-near-misses": _joined("near", "misses", "list"),
    "resolve-near-miss": _joined("near", "misses", "resolve"),
    "restore-instrument": _joined("restore", "instrument"),
    "check-instrument-ingestion-status": _joined("check", "instrument", "ingestion", "status"),
    "get-domain-concepts": _joined("domain", "concepts"),
    "run-cypher": _joined("cy", "pher", separator=""),
    "unmerge-entities": _joined("un", "merge", separator=""),
}
_PAIRS = list(_RETIRED_BY_CURRENT_NAME.items())
_IDS = list(_RETIRED_BY_CURRENT_NAME)


def _listed_tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(mcp_server.server.list_tools())}


@pytest.mark.parametrize(("current", "retired"), _PAIRS, ids=_IDS)
def test_tool_list_has_the_current_name_and_not_the_retired_one(current: str, retired: str) -> None:
    listed = _listed_tool_names()

    assert current in listed
    assert retired not in listed


@pytest.mark.parametrize(("current", "retired"), _PAIRS, ids=_IDS)
def test_calling_a_retired_name_is_an_unknown_tool_with_no_dispatch(
    current: str, retired: str
) -> None:
    touched.clear()
    audit_store = InMemoryAuditStore()
    server = build_test_server(audit_store_factory=audit_store_factory(audit_store))

    with pytest.raises(ToolError, match=f"Unknown tool: {retired}"):
        asyncio.run(server.call_tool(retired, {}))

    assert touched == [], current
    assert audit_store.rows == [], current


_REMOVED_STATUS_TOOL = _joined("check", "near", "miss", "approval", separator="-")


def test_the_removed_status_tool_is_an_unknown_tool_with_no_dispatch() -> None:
    touched.clear()
    audit_store = InMemoryAuditStore()
    server = build_test_server(audit_store_factory=audit_store_factory(audit_store))

    assert _REMOVED_STATUS_TOOL not in _listed_tool_names()
    with pytest.raises(ToolError, match=f"Unknown tool: {_REMOVED_STATUS_TOOL}"):
        asyncio.run(server.call_tool(_REMOVED_STATUS_TOOL, {"pending_approval_id": "x"}))

    assert touched == []
    assert audit_store.rows == []
