"""The Capability `status` value that means "in service", read from the domain schema."""

from __future__ import annotations

from ps_service.domain_schema import DOMAIN_SCHEMA, EnumType

__all__ = ["active_capability_status"]


def active_capability_status() -> str:
    """Return the Capability `status` enum's active value (its first member).

    A Capability with no `status` property counts as active; readers default to this value so the
    literal lives only in the schema.

    Returns:
        The active status value.

    Raises:
        TypeError: the schema's Capability `status` property is not an enum.
    """
    node = next(n for n in DOMAIN_SCHEMA.nodes if n.label == "Capability")
    status = next(p for p in node.properties if p.name == "status")
    if not isinstance(status.type, EnumType):  # pragma: no cover -- the schema declares an enum
        message = "the Capability status property is not an enum"
        raise TypeError(message)
    return status.type.values[0]
