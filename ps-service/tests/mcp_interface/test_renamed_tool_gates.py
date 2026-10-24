"""The ingestion, near-miss, restore, query and unmerge tools keep their declared access gate.

Each row pins a tool, keyed by its current name, to the shared gate constant it must use. A tool
that is missing from the gate mapping, or that maps to another gate, fails its own test id, so a
rename cannot silently drop or loosen a gate.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mcp_interface.test_gate_declarations import find_undeclared_tools
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.mcp_server import TOOL_GATES
from ps_service.mcp_interface.tool_core import (
    CLEANUP_GATE,
    COMPLIANCE_OFFICER_GATE,
    NEAR_MISS_MERGE_GATE,
    OWN_APPROVAL_READ_GATE,
    PLAIN_READ_GATE,
)

if TYPE_CHECKING:
    from ps_service.mcp_interface.tool_gates import ToolGate

_EXPECTED_GATES: dict[str, ToolGate] = {
    "ingest-regulation": COMPLIANCE_OFFICER_GATE,
    "start-ingestion": COMPLIANCE_OFFICER_GATE,
    "get-ingestion-status": COMPLIANCE_OFFICER_GATE,
    "check-regulations": COMPLIANCE_OFFICER_GATE,
    "restore-instrument": COMPLIANCE_OFFICER_GATE,
    "check-instrument-ingestion-status": PLAIN_READ_GATE,
    "list-near-misses": PLAIN_READ_GATE,
    "get-domain-concepts": PLAIN_READ_GATE,
    "run-cypher": PLAIN_READ_GATE,
    "resolve-near-miss": NEAR_MISS_MERGE_GATE,
    "check-near-miss-approval": OWN_APPROVAL_READ_GATE,
    "unmerge-entities": CLEANUP_GATE,
}


@pytest.mark.parametrize(("name", "gate"), _EXPECTED_GATES.items(), ids=list(_EXPECTED_GATES))
def test_tool_is_declared_with_its_gate(name: str, gate: ToolGate) -> None:
    assert name in TOOL_GATES
    assert TOOL_GATES[name] is gate


def test_gate_mapping_covers_exactly_the_registered_tools() -> None:
    registered = {tool.name for tool in asyncio.run(mcp_server.server.list_tools())}

    assert find_undeclared_tools(mcp_server.server, TOOL_GATES) == set()
    assert set(TOOL_GATES) - registered == set()
