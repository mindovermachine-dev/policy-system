"""AC-BI-001: `graph_reader`'s read vocabulary must equal internal_seed's
`NodeLabel`/`EdgeType`, or a future vocabulary addition silently drops data
again (issue #106's own root cause -- PracticeArea/RiskPath/COVERS/OWNS/
MITIGATED_BY/VERIFIED_BY were dropped for two release cycles with no test
catching it, per TASK.md).
"""

from __future__ import annotations

import typing

from ps_service.company_merge import graph_reader
from ps_service.ingestion.adapters.internal_seed.models import EdgeType, NodeLabel


def test_graph_reader_reads_every_internal_seed_node_label() -> None:
    assert (
        frozenset(typing.get_args(NodeLabel)) == graph_reader._HANDLED_NODE_LABELS  # pyright: ignore[reportPrivateUsage]
    )


def test_graph_reader_reads_every_internal_seed_edge_type() -> None:
    assert (
        frozenset(typing.get_args(EdgeType)) == graph_reader._HANDLED_EDGE_TYPES  # pyright: ignore[reportPrivateUsage]
    )
