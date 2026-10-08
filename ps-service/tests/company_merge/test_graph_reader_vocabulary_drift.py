"""AC-BI-001: `graph_reader`'s read vocabulary must equal internal_seed's
`NodeLabel`/`EdgeType`, or a future vocabulary addition silently drops data
again (issue #106's own root cause -- PracticeArea/RiskPath/COVERS/OWNS/
MITIGATED_BY/VERIFIED_BY were dropped for two release cycles with no test
catching it, per TASK.md).
"""

from __future__ import annotations

import typing

from ps_service.company_merge import graph_reader
from ps_service.domain_schema import DOMAIN_SCHEMA
from ps_service.domain_schema.vocabulary_exceptions import SYSTEM_MINTED_EDGE_TYPES, find_unpinned
from ps_service.ingestion.adapters.internal_seed.models import EdgeType, NodeLabel


def test_graph_reader_reads_every_internal_seed_node_label() -> None:
    assert (
        frozenset(typing.get_args(NodeLabel)) == graph_reader._HANDLED_NODE_LABELS  # pyright: ignore[reportPrivateUsage]
    )


def test_graph_reader_reads_every_internal_seed_edge_type() -> None:
    assert (
        frozenset(typing.get_args(EdgeType)) == graph_reader._HANDLED_EDGE_TYPES  # pyright: ignore[reportPrivateUsage]
    )


# AC-BI-015: the read set is also pinned to the code-defined domain schema.
_SCHEMA_LABELS = frozenset(node.label for node in DOMAIN_SCHEMA.nodes)
_SCHEMA_EDGE_TYPES = frozenset(edge.type for edge in DOMAIN_SCHEMA.edges)
_HANDLED_LABELS = graph_reader._HANDLED_NODE_LABELS  # pyright: ignore[reportPrivateUsage]  # pin test compares the module-private read set to the schema
_HANDLED_EDGES = graph_reader._HANDLED_EDGE_TYPES  # pyright: ignore[reportPrivateUsage]  # pin test compares the module-private read set to the schema


def test_handled_labels_equal_schema_labels() -> None:
    assert _HANDLED_LABELS == _SCHEMA_LABELS


def test_handled_edge_types_equal_schema_edges_minus_system_minted() -> None:
    assert _SCHEMA_EDGE_TYPES - frozenset(SYSTEM_MINTED_EDGE_TYPES) == _HANDLED_EDGES


def test_schema_name_missing_from_handled_set_fails_without_exception() -> None:
    reduced = _HANDLED_EDGES - {"COVERS"}

    assert find_unpinned(_SCHEMA_EDGE_TYPES, reduced, SYSTEM_MINTED_EDGE_TYPES) == ("COVERS",)
    assert find_unpinned(_SCHEMA_EDGE_TYPES, _HANDLED_EDGES, SYSTEM_MINTED_EDGE_TYPES) == ()


def test_handled_name_not_in_schema_fails() -> None:
    assert find_unpinned(_HANDLED_LABELS | {"Bogus"}, _SCHEMA_LABELS, ()) == ("Bogus",)
    assert find_unpinned(_HANDLED_LABELS, _SCHEMA_LABELS, ()) == ()
