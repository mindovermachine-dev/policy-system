"""Typed models of the graph mutation log (issue #205).

Input drafts are the boundary validation (L2 Data Modeling): a malformed group never reaches
SQL. Models are frozen (L1 Immutability) and reject undeclared fields.
"""

from __future__ import annotations

import math
import uuid  # pydantic resolves field annotations at runtime
from typing import TYPE_CHECKING, Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from collections.abc import Iterable

_NON_BLANK = r"\S"
"""Pattern requiring at least one non-whitespace character."""

IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
"""Shape of a label, relationship type or interpolated property key (second layer: Cypher)."""

RESERVED_NODE_PROPERTY_KEYS = frozenset({"id", "embedding"})
"""Keys a caller cannot place in `properties`: the gateway owns them."""

RESERVED_EDGE_PROPERTY_KEYS = frozenset({"id", "embedding", "identity"})
"""Keys a caller cannot place in an edge's `properties`."""

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


class GraphLogEntryDraft(BaseModel):
    """One resolved graph mutation, as submitted for appending (position is assigned on commit)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, pattern=_NON_BLANK)
    """The node label or relationship type written."""
    identity: str = Field(min_length=1, pattern=_NON_BLANK)
    """The identity of the node or relationship written."""
    content: dict[str, object]
    """The properties written."""
    embedding: tuple[float, ...] | None = Field(default=None, min_length=1)
    """The embedding of the entry, 64-bit values kept bit-identical (never narrowed to 32-bit)."""


class GraphLogGroupDraft(BaseModel):
    """A transaction group: the ordered entries of one graph that commit together or not at all."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str = Field(min_length=1, pattern=_NON_BLANK)
    """The graph the entries apply to."""
    entries: tuple[GraphLogEntryDraft, ...] = Field(min_length=1)
    """The entries, in submission order."""


class AppendedGroup(BaseModel):
    """The outcome of appending one group: its id and the positions its entries took."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: uuid.UUID
    graph: str
    first_position: int = Field(ge=1)
    last_position: int = Field(ge=1)


class GraphLogEntry(BaseModel):
    """One recorded log entry, as read back."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str
    position: int = Field(ge=1)
    group_id: uuid.UUID
    name: str
    identity: str
    content: dict[str, object]
    embedding: tuple[float, ...] | None = None


class GraphLogGroup(BaseModel):
    """One recorded group with its entries in position order, as read back by audit event id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: uuid.UUID
    graph: str
    first_position: int = Field(ge=1)
    last_position: int = Field(ge=1)
    audit_event_id: str
    entries: tuple[GraphLogEntry, ...]


class AppliedMarker(BaseModel):
    """The position up to which one graph reflects the log (never ahead of the last entry)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str = Field(min_length=1, pattern=_NON_BLANK)
    applied_position: int = Field(ge=0)


class DigestCheckpoint(BaseModel):
    """A canonical digest of one graph recorded at a log position; opaque to the store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str = Field(min_length=1, pattern=_NON_BLANK)
    position: int = Field(ge=0)
    canonical_digest: str = Field(min_length=1, pattern=_NON_BLANK)


def _is_valid_scalar(value: object) -> bool:
    """Return whether `value` is a graph-storable scalar: str, bool, int64 or finite float."""
    if isinstance(value, bool | str):
        return True
    if isinstance(value, int):
        return _INT64_MIN <= value <= _INT64_MAX
    return isinstance(value, float) and math.isfinite(value)


def _is_valid_property_value(value: object) -> bool:
    """Return whether `value` is a scalar or a non-empty list of one scalar type."""
    if isinstance(value, list | tuple):
        items = list(cast("Iterable[object]", value))  # narrowed to a list/tuple of unknowns
        return (
            bool(items)
            and len({type(item) for item in items}) == 1
            and all(_is_valid_scalar(item) for item in items)
        )
    return _is_valid_scalar(value)


def validate_property_map(
    properties: dict[str, object], reserved: frozenset[str]
) -> dict[str, object]:
    """Return `properties` with tuples turned into lists, or raise `ValueError` on a bad entry.

    Values must be scalars or flat homogeneous lists (no nested maps, no None) so the log entry
    and the graph hold exactly what the caller meant; `reserved` keys are owned by the gateway.
    """
    for key, value in properties.items():
        if not key.strip() or key in reserved:
            message = "a property key is blank or reserved"
            raise ValueError(message)
        if not _is_valid_property_value(value):
            message = f"property {key!r} is not a scalar or a flat homogeneous list"
            raise ValueError(message)
    return {
        key: list(cast("Iterable[object]", value)) if isinstance(value, tuple) else value
        for key, value in properties.items()
    }


class UpsertNode(BaseModel):
    """Create the node `(label, id)` or merge `properties` (and `embedding`) into it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["upsert_node"] = "upsert_node"
    label: str = Field(pattern=IDENTIFIER_PATTERN)
    id: str = Field(min_length=1, pattern=_NON_BLANK)
    """The caller-supplied node id; the gateway never generates one."""
    properties: dict[str, object] = Field(default_factory=dict)
    embedding: tuple[float, ...] | None = Field(default=None, min_length=1)
    """Stored on the node as `embedding`; omitted means keep whatever the node has."""

    @field_validator("properties")
    @classmethod
    def _check_properties(cls, properties: dict[str, object]) -> dict[str, object]:
        return validate_property_map(properties, RESERVED_NODE_PROPERTY_KEYS)


class NodeRef(BaseModel):
    """The caller-supplied identity of a node: its label and id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(pattern=IDENTIFIER_PATTERN)
    id: str = Field(min_length=1, pattern=_NON_BLANK)


class _EdgeBase(BaseModel):
    """What upserting and deleting an edge share: its type, caller identity and endpoints."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str = Field(pattern=IDENTIFIER_PATTERN)
    """The relationship type."""
    identity: str = Field(min_length=1, pattern=_NON_BLANK)
    """The caller-supplied edge identity: the key parallel edges are told apart by."""
    source: NodeRef
    target: NodeRef


class UpsertEdge(_EdgeBase):
    """Create the edge `(source)-[type {identity}]->(target)` or merge `properties` into it."""

    op: Literal["upsert_edge"] = "upsert_edge"
    properties: dict[str, object] = Field(default_factory=dict)

    @field_validator("properties")
    @classmethod
    def _check_properties(cls, properties: dict[str, object]) -> dict[str, object]:
        return validate_property_map(properties, RESERVED_EDGE_PROPERTY_KEYS)


class DeleteEdge(_EdgeBase):
    """Delete the edge `(source)-[type {identity}]->(target)`; a missing edge is a no-op."""

    op: Literal["delete_edge"] = "delete_edge"


class MergeProperty(BaseModel):
    """Merge `properties` into the existing node `(label, id)`; the node must exist."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["merge_property"] = "merge_property"
    label: str = Field(pattern=IDENTIFIER_PATTERN)
    id: str = Field(min_length=1, pattern=_NON_BLANK)
    properties: dict[str, object] = Field(min_length=1)

    @field_validator("properties")
    @classmethod
    def _check_properties(cls, properties: dict[str, object]) -> dict[str, object]:
        return validate_property_map(properties, RESERVED_NODE_PROPERTY_KEYS)


class RemoveProperty(BaseModel):
    """Remove the property `keys` from the node `(label, id)`; a missing node or key is a no-op.

    Keys are interpolated into Cypher (they cannot be parameterized), so each must match the
    identifier pattern here and again in the Cypher builder (F5).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["remove_property"] = "remove_property"
    label: str = Field(pattern=IDENTIFIER_PATTERN)
    id: str = Field(min_length=1, pattern=_NON_BLANK)
    keys: tuple[Annotated[str, Field(pattern=IDENTIFIER_PATTERN)], ...] = Field(min_length=1)

    @field_validator("keys")
    @classmethod
    def _check_keys(cls, keys: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(keys)) != len(keys) or RESERVED_NODE_PROPERTY_KEYS & set(keys):
            message = "property keys must be distinct and not reserved"
            raise ValueError(message)
        return keys


class DeleteNode(BaseModel):
    """Delete the node `(label, id)` with its relationships; a missing node is a no-op."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["delete_node"] = "delete_node"
    label: str = Field(pattern=IDENTIFIER_PATTERN)
    id: str = Field(min_length=1, pattern=_NON_BLANK)


Primitive = Annotated[
    UpsertNode | UpsertEdge | MergeProperty | RemoveProperty | DeleteNode | DeleteEdge,
    Field(discriminator="op"),
]
"""One graph mutation a caller may submit (tagged union on `op`; grows add-only)."""


class ExpectedPosition(BaseModel):
    """Precondition: the graph's log is at exactly `position` (0 means no entries yet)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    position: int = Field(ge=0)


Precondition = ExpectedPosition
"""One caller precondition on a group (a closed set that grows add-only; the gateway adds none)."""


class MutationGroup(BaseModel):
    """A transaction group for one graph: caller-supplied primitives plus the causing command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str = Field(min_length=1, pattern=_NON_BLANK)
    audit_event_id: str
    """The id of the audit event of the command that caused the group (a UUID string)."""
    primitives: tuple[Primitive, ...] = Field(min_length=1)
    """The mutations, in submission order."""
    preconditions: tuple[Precondition, ...] = ()
    """Conditions the caller needs to hold when the group is logged; none unless the caller asks."""

    @field_validator("audit_event_id")
    @classmethod
    def _check_audit_event_id(cls, audit_event_id: str) -> str:
        uuid.UUID(audit_event_id)
        return audit_event_id


class GroupOutcome(BaseModel):
    """What the caller learns about a submitted group."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str
    first_position: int | None = Field(ge=1)
    last_position: int | None = Field(ge=1)
    status: Literal["applied", "committed_apply_pending", "unchanged"]
    """`applied`: logged and applied. `committed_apply_pending`: logged, but FalkorDB stayed
    unreachable through the retry budget; the entries stay in the log and are applied on recovery
    (not an error). `unchanged`: every mutation was a no-op, nothing logged.
    """

    @model_validator(mode="after")
    def _positions_follow_status(self) -> GroupOutcome:
        """Positions exist exactly when something was logged."""
        logged = self.first_position is not None and self.last_position is not None
        unlogged = self.first_position is None and self.last_position is None
        if (self.status == "unchanged") != unlogged or not (logged or unlogged):
            message = "positions are set exactly when the group was logged"
            raise ValueError(message)
        return self


class CatchUpResult(BaseModel):
    """Where a graph stands after `catch_up`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str
    applied_position: int = Field(ge=0)
    last_position: int = Field(ge=0)
    caught_up: bool
    """True only if every entry in the log is applied (`applied_position == last_position`)."""


class RecoveryResult(BaseModel):
    """What startup recovery did: the graphs it caught up and the ones still gated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recovered: tuple[str, ...]
    """Graphs whose marker now equals their last logged position, in name order."""
    gated: tuple[str, ...]
    """Graphs still behind their log (FalkorDB down or refusing an entry): writes fail closed."""
