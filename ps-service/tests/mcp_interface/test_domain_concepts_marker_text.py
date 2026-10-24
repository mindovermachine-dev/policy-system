"""The domain-concepts document describes the merge markers that replaced the Capability tombstone.

These are properties of the hand-written document, served verbatim by the
`psdomain://concepts` resource (the `get-domain-concepts` tool renders the slim schema from code;
its status and edge assertions live with the schema tests). The prose is hand-written, so it is
read through `read_domain_concepts()`.
"""

from __future__ import annotations

import asyncio

from mcp.types import CallToolResult, TextContent

from mcp_interface._environment import build_test_server
from ps_service.domain_schema import DOMAIN_SCHEMA, EnumType
from ps_service.mcp_interface import cypher_tools


def _domain_text() -> str:
    cypher_tools._domain_concepts_path.cache_clear()  # pyright: ignore[reportPrivateUsage]  # reset module-internal cache so the real packaged file is read
    return cypher_tools.read_domain_concepts()


def _tool_text() -> str:
    outcome = asyncio.run(build_test_server().call_tool("get-domain-concepts", {}))
    assert isinstance(outcome, CallToolResult)
    return "".join(c.text for c in outcome.content if isinstance(c, TextContent))


def test_capability_status_enum_is_active_or_deprecated_everywhere() -> None:
    capability = next(node for node in DOMAIN_SCHEMA.nodes if node.label == "Capability")
    status = next(prop for prop in capability.properties if prop.name == "status")

    assert isinstance(status.type, EnumType)
    assert status.type.values == ("active", "deprecated")
    assert "status: enum(active|deprecated)\n" in _tool_text()
    assert "`active` \\| `deprecated` |" in _domain_text()


def test_no_merge_redirect_edge_exists_in_the_schema_the_tool_or_the_document() -> None:
    assert all(edge.type != "MERGED_INTO" for edge in DOMAIN_SCHEMA.edges)
    assert "MERGED_INTO" not in _tool_text()
    assert "MERGED_INTO" not in _domain_text()


def test_capability_lifecycle_documents_the_merged_capability_marker() -> None:
    text = _domain_text()

    assert "`MergedCapability`" in text
    assert "deleting the absorbed Capability" in text
    assert "`merged_into`" in text


def test_capability_status_property_documents_that_an_absorbed_capability_is_deleted() -> None:
    text = _domain_text()

    assert "An absorbed Capability is deleted, not marked" in text
    assert "filter on `status = 'active'`" not in text


def test_obligation_lifecycle_documents_the_merged_obligation_marker() -> None:
    text = _domain_text()

    assert "`MergedObligation`" in text
    assert "never counted as an Obligation" in text


def test_obligation_cleanup_merge_is_documented_as_a_delete_not_a_tombstone() -> None:
    text = _domain_text()

    assert "A Compliance Officer cleanup merge of two Obligations under the same Role" in text
    assert "deletes the absorbed Obligation" in text
