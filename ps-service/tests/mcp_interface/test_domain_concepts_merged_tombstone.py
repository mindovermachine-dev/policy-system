"""The `domain_concepts` tool text documents the Capability `merged` tombstone (issue #190).

The packaged copy is what a model-driven skill reads, so the `merged` status and
the `MERGED_INTO` edge must be present in the text the tool returns.
"""

from __future__ import annotations

import asyncio

from mcp.types import CallToolResult, TextContent

from ps_service.mcp_interface import mcp_server


def _domain_text() -> str:
    mcp_server._domain_concepts_path.cache_clear()  # pyright: ignore[reportPrivateUsage]  # reset module-internal cache so the real packaged file is read
    outcome = asyncio.run(mcp_server.server.call_tool("domain_concepts", {}))
    assert isinstance(outcome, CallToolResult)
    texts = [c.text for c in outcome.content if isinstance(c, TextContent)]
    assert len(texts) == 1
    return texts[0]


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
