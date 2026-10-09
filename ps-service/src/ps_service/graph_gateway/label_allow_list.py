"""The Graph Write Gateway's label allow-list (issue #206, AC-BI-001).

Derived, never hand-written: the labels and relationship types of `DOMAIN_SCHEMA` plus the three
named, closed exception sets of the domain schema's vocabulary exceptions (system-minted edges,
operational labels, native structural labels). Native graphs have no exemption and there is no
runtime registration: a new name is a change to the schema or to those named sets. When this
derivation and the named sets ever disagree, the named sets win (container architecture).

The restore allow-lists are independent hand-written lists and are not touched here.
"""

from __future__ import annotations

from ps_service.domain_schema import DOMAIN_SCHEMA
from ps_service.domain_schema.vocabulary_exceptions import (
    CELLAR_ELI_NATIVE_LABELS,
    OPERATIONAL_LABELS,
    SYSTEM_MINTED_EDGE_TYPES,
)
from ps_service.graph_gateway.errors import UnlistedNameError

ALLOWED_NODE_LABELS: frozenset[str] = frozenset(
    {node.label for node in DOMAIN_SCHEMA.nodes}
    | set(OPERATIONAL_LABELS)
    | set(CELLAR_ELI_NATIVE_LABELS)
)
"""Node labels a write may name."""

ALLOWED_RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {edge.type for edge in DOMAIN_SCHEMA.edges} | set(SYSTEM_MINTED_EDGE_TYPES)
)
"""Relationship types a write may name."""

_UNLISTED_LABEL_MESSAGE = "a node label is outside the gateway's allow-list"
_UNLISTED_TYPE_MESSAGE = "a relationship type is outside the gateway's allow-list"


def require_allowed_node_label(label: str) -> str:
    """Return `label` if it is allow-listed, else raise `UnlistedNameError` (label not echoed)."""
    if label not in ALLOWED_NODE_LABELS:
        raise UnlistedNameError(_UNLISTED_LABEL_MESSAGE)
    return label


def require_allowed_relationship_type(relationship_type: str) -> str:
    """Return `relationship_type` if allow-listed, else raise `UnlistedNameError`."""
    if relationship_type not in ALLOWED_RELATIONSHIP_TYPES:
        raise UnlistedNameError(_UNLISTED_TYPE_MESSAGE)
    return relationship_type
