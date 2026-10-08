"""The domain-concepts document describes the Capability `merged` tombstone (issue #190).

These are properties of the hand-written document, served verbatim by the
`psdomain://concepts` resource (the `domain_concepts` tool now renders the slim
schema from code; its `merged` / `MERGED_INTO` assertions live with the schema
tests). The tables are still hand-written at this point, so they are read
through `read_domain_concepts()`.
"""

from __future__ import annotations

import asyncio

from mcp.types import CallToolResult, TextContent

from ps_service.domain_schema import DOMAIN_SCHEMA, EnumType
from ps_service.mcp_interface import mcp_server


def _domain_text() -> str:
    mcp_server._domain_concepts_path.cache_clear()  # pyright: ignore[reportPrivateUsage]  # reset module-internal cache so the real packaged file is read
    return mcp_server.read_domain_concepts()


def test_capability_status_enum_includes_merged() -> None:
    assert "`active` \\| `deprecated` \\| `merged`" in _domain_text()


def test_edge_catalog_has_merged_into_row() -> None:
    text = _domain_text()
    assert "| `MERGED_INTO` | Capability → Capability |" in text


def test_capability_relationships_list_merged_into_both_directions() -> None:
    text = _domain_text()
    assert "`MERGED_INTO` (outbound)" in text
    assert "`MERGED_INTO` (inbound)" in text


def test_diagram_draws_merged_into_edge() -> None:
    assert 'Capability -->|"MERGED_INTO"| Capability' in _domain_text()


def test_obligation_lifecycle_documents_the_merged_obligation_marker() -> None:
    text = _domain_text()

    assert "`MergedObligation`" in text
    assert "`merged_into`" in text
    assert "never counted as an Obligation" in text


def test_obligation_cleanup_merge_is_documented_as_a_delete_not_a_tombstone() -> None:
    text = _domain_text()

    assert "A Compliance Officer cleanup merge of two Obligations under the same Role" in text
    assert "deletes the absorbed Obligation" in text


def _tool_text() -> str:
    outcome = asyncio.run(mcp_server.server.call_tool("domain_concepts", {}))
    assert isinstance(outcome, CallToolResult)
    return "".join(c.text for c in outcome.content if isinstance(c, TextContent))


def test_capability_status_enum_includes_merged_in_schema_and_tool_output() -> None:
    """AC-BI-020: the tombstone status is a schema fact and visible through the tool."""
    capability = next(node for node in DOMAIN_SCHEMA.nodes if node.label == "Capability")
    status = next(prop for prop in capability.properties if prop.name == "status")

    assert isinstance(status.type, EnumType)
    assert "merged" in status.type.values
    assert "status: enum(active|deprecated|merged)" in _tool_text()


def test_merged_into_edge_in_schema_and_slim_output() -> None:
    """AC-BI-020: Capability -[MERGED_INTO]-> Capability, 0..1 : 0..*, in schema and tool output."""
    edges = {edge.key: edge for edge in DOMAIN_SCHEMA.edges}

    assert str(edges[("MERGED_INTO", "Capability", "Capability")].cardinality) == "0..1 : 0..*"
    assert "(:Capability)-[:MERGED_INTO]->(:Capability)  0..1 : 0..*" in _tool_text()
