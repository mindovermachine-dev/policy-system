"""The Graph Write Gateway's allow-list of node labels and relationship types.

Derived, never hand-written: the labels and relationship types of `DOMAIN_SCHEMA` plus the three
named, closed exception sets of the domain schema's vocabulary exceptions (system-minted edges,
operational labels, native structural labels). Native graphs have no exemption and there is no
runtime registration: a new name is a change to the schema or to those named sets. When this
derivation and the named sets ever disagree, the named sets win (container architecture).

The restore allow-lists are independent hand-written lists and are not derived from this one.
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
    """Check that a write may name `label` as a node label.

    Args:
        label: Node label requested by a write.

    Returns:
        `label`, unchanged, when it is in `ALLOWED_NODE_LABELS`.

    Raises:
        UnlistedNameError: `label` is not allow-listed; the message does not echo the label.
    """
    if label not in ALLOWED_NODE_LABELS:
        raise UnlistedNameError(_UNLISTED_LABEL_MESSAGE)
    return label


def require_allowed_relationship_type(relationship_type: str) -> str:
    """Check that a write may name `relationship_type` as a relationship type.

    Args:
        relationship_type: Relationship type requested by a write.

    Returns:
        `relationship_type`, unchanged, when it is in `ALLOWED_RELATIONSHIP_TYPES`.

    Raises:
        UnlistedNameError: `relationship_type` is not allow-listed; the message does not echo it.
    """
    if relationship_type not in ALLOWED_RELATIONSHIP_TYPES:
        raise UnlistedNameError(_UNLISTED_TYPE_MESSAGE)
    return relationship_type
