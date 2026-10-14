"""Typed models of the graph mutation log and of the mutation groups the gateway accepts.

Input drafts and primitives are the boundary validation: a malformed group never reaches SQL or
the graph. Models are frozen and reject undeclared fields.
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
    """One resolved graph mutation, as submitted for appending (position is assigned on commit).

    Attributes:
        name: Node label or relationship type written; non-blank.
        identity: Identity of the node or relationship written; non-blank.
        content: The properties written.
        embedding: Embedding of the entry, or None; at least one value when given.
    """

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
    """A transaction group: the ordered entries of one graph that commit together or not at all.

    Attributes:
        graph: Name of the graph the entries apply to; non-blank.
        entries: The entries in submission order; at least one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str = Field(min_length=1, pattern=_NON_BLANK)
    """The graph the entries apply to."""
    entries: tuple[GraphLogEntryDraft, ...] = Field(min_length=1)
    """The entries, in submission order."""


class AppendedGroup(BaseModel):
    """The outcome of appending one group: its id and the positions its entries took.

    Attributes:
        group_id: Id assigned to the group.
        graph: Graph the group was appended to.
        first_position: Log position of the first entry; at least 1.
        last_position: Log position of the last entry; at least 1.
        swap_set_id: Swap set the group is a leg of, or None.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: uuid.UUID
    graph: str
    first_position: int = Field(ge=1)
    last_position: int = Field(ge=1)
    swap_set_id: uuid.UUID | None = None
    """The swap set the group was appended as a leg of (`None`: not part of a swap set)."""


class GraphLogEntry(BaseModel):
    """One recorded log entry, as read back.

    Attributes:
        graph: Graph the entry belongs to.
        position: Log position; at least 1.
        group_id: Group the entry was committed in.
        name: Node label or relationship type written.
        identity: Identity of the node or relationship written.
        content: The properties written.
        embedding: The embedding, or None.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str
    position: int = Field(ge=1)
    group_id: uuid.UUID
    name: str
    identity: str
    content: dict[str, object]
    embedding: tuple[float, ...] | None = None


class GraphLogGroup(BaseModel):
    """One recorded group with its entries in position order, as read back by audit event id.

    Attributes:
        group_id: Id of the group.
        graph: Graph the group applies to.
        first_position: Log position of the first entry; at least 1.
        last_position: Log position of the last entry; at least 1.
        audit_event_id: Audit event of the command that caused the group.
        entries: The entries in position order.
        swap_set_id: Swap set the group belongs to, or None.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: uuid.UUID
    graph: str
    first_position: int = Field(ge=1)
    last_position: int = Field(ge=1)
    audit_event_id: str
    entries: tuple[GraphLogEntry, ...]
    swap_set_id: uuid.UUID | None = None
    """The swap set the group belongs to (`None`: it was applied entry by entry)."""


type SwapSetId = uuid.UUID
"""The id of a swap set: shared by the logged groups that were applied together by swap."""


class SwapGroupRef(BaseModel):
    """A logged group that belongs to a swap set: where it sits in its graph's log.

    Attributes:
        group_id: Id of the group.
        graph: Graph the group applies to.
        first_position: Log position of the first entry; at least 1.
        last_position: Log position of the last entry; at least 1.
        swap_set_id: The swap set the group belongs to.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: uuid.UUID
    graph: str
    first_position: int = Field(ge=1)
    last_position: int = Field(ge=1)
    swap_set_id: uuid.UUID


class AppliedMarker(BaseModel):
    """The position up to which one graph reflects the log (never ahead of the last entry).

    Attributes:
        graph: Graph name; non-blank.
        applied_position: Highest log position applied to the graph; 0 when none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str = Field(min_length=1, pattern=_NON_BLANK)
    applied_position: int = Field(ge=0)


class DigestCheckpoint(BaseModel):
    """A canonical digest of one graph recorded at a log position; opaque to the store.

    Attributes:
        graph: Graph name; non-blank.
        position: Log position the digest belongs to; 0 or more.
        canonical_digest: The digest text; non-blank.
    """

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
    """Validate a property map and normalise its sequences to lists.

    Values must be scalars or flat homogeneous lists (no nested maps, no None) so the log entry
    and the graph hold exactly what the caller meant.

    Args:
        properties: Property name to value. A scalar is str, bool, int64 or a finite float; a
            list or tuple must be non-empty and hold one scalar type.
        reserved: Keys owned by the gateway; a caller may not set them.

    Returns:
        A new map in which every tuple is a list.

    Raises:
        ValueError: a key is blank or reserved, or a value is not a scalar or a flat homogeneous
            list.
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
    """Create the node `(label, id)` or merge `properties` (and `embedding`) into it.

    Attributes:
        op: Tag `upsert_node`.
        label: Node label; matches `IDENTIFIER_PATTERN`.
        id: Caller-supplied node id; non-blank.
        properties: Properties to set; keys `id` and `embedding` are reserved.
        embedding: Embedding to store, or None to keep the node's own; at least one value.
    """

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
    """The caller-supplied identity of a node: its label and id.

    Attributes:
        label: Node label; matches `IDENTIFIER_PATTERN`.
        id: Node id; non-blank.
    """

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
    """Create the edge `(source)-[type {identity}]->(target)` or merge `properties` into it.

    Both endpoints must exist in the graph or be created earlier in the same group.

    Attributes:
        op: Tag `upsert_edge`.
        properties: Properties to set; keys `id`, `embedding` and `identity` are reserved.
    """

    op: Literal["upsert_edge"] = "upsert_edge"
    properties: dict[str, object] = Field(default_factory=dict)

    @field_validator("properties")
    @classmethod
    def _check_properties(cls, properties: dict[str, object]) -> dict[str, object]:
        return validate_property_map(properties, RESERVED_EDGE_PROPERTY_KEYS)


class DeleteEdge(_EdgeBase):
    """Delete the edge `(source)-[type {identity}]->(target)`; a missing edge is a no-op."""

    op: Literal["delete_edge"] = "delete_edge"


class StampEdgeIdentity(_EdgeBase):
    """Give the identity-less edge `(source)-[type]->(target)` the `identity`, in place.

    For a graph holding edges that carry no `identity`: the stamped edge keeps its properties and
    its place, so a later `UpsertEdge` with this identity merges into it instead of creating a
    parallel edge. A missing or already identified edge is a no-op. Every identity-less edge of
    the type between the two nodes receives the identity.
    """

    op: Literal["stamp_edge_identity"] = "stamp_edge_identity"


class MergeProperty(BaseModel):
    """Merge `properties` into the existing node `(label, id)`; the node must exist.

    Attributes:
        op: Tag `merge_property`.
        label: Node label; matches `IDENTIFIER_PATTERN`.
        id: Node id; non-blank.
        properties: Properties to merge; at least one, keys `id` and `embedding` reserved.
    """

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
    identifier pattern here and again in the Cypher builder.

    Attributes:
        op: Tag `remove_property`.
        label: Node label; matches `IDENTIFIER_PATTERN`.
        id: Node id; non-blank.
        keys: Distinct property keys to remove; at least one, each matching `IDENTIFIER_PATTERN`,
            none of them `id` or `embedding`.
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
    """Delete the node `(label, id)` with its relationships; a missing node is a no-op.

    Attributes:
        op: Tag `delete_node`.
        label: Node label; matches `IDENTIFIER_PATTERN`.
        id: Node id; non-blank.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: Literal["delete_node"] = "delete_node"
    label: str = Field(pattern=IDENTIFIER_PATTERN)
    id: str = Field(min_length=1, pattern=_NON_BLANK)


Primitive = Annotated[
    UpsertNode
    | UpsertEdge
    | MergeProperty
    | RemoveProperty
    | DeleteNode
    | DeleteEdge
    | StampEdgeIdentity,
    Field(discriminator="op"),
]
"""One graph mutation a caller may submit (tagged union on `op`; grows add-only)."""


class ExpectedPosition(BaseModel):
    """Precondition: the graph's log is at exactly `position` (0 means no entries yet).

    Attributes:
        position: The log position the caller prepared the group against; 0 or more.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    position: int = Field(ge=0)


Precondition = ExpectedPosition
"""One caller precondition on a group (a closed set that grows add-only; the gateway adds none)."""


class MutationGroup(BaseModel):
    """A transaction group for one graph: caller-supplied primitives plus the causing command.

    Attributes:
        graph: Name of the graph the group applies to; non-blank.
        audit_event_id: Audit event of the causing command, or None.
        primitives: The mutations in submission order; at least one.
        preconditions: Conditions that must hold when the group is logged.
        checkpoint_requested: Whether to record a digest after the group is applied.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str = Field(min_length=1, pattern=_NON_BLANK)
    audit_event_id: str | None = None
    """The id of the audit event of the command that caused the group (a UUID string).

    `None` for a group no audit event caused (Policy Lifecycle's draft edits and status/version
    backfills, and the edge-identity stamp ahead of them, write no `audit_events` row): the group
    is logged with a null link.
    """
    primitives: tuple[Primitive, ...] = Field(min_length=1)
    """The mutations, in submission order."""
    preconditions: tuple[Precondition, ...] = ()
    """Conditions the caller needs to hold when the group is logged; none unless the caller asks."""
    checkpoint_requested: bool = False
    """Record the graph's canonical digest at this group's last position once it is applied.

    Off unless the caller asks: computing a digest reads the whole live graph, so no group takes
    one implicitly.
    """

    @field_validator("audit_event_id")
    @classmethod
    def _check_audit_event_id(cls, audit_event_id: str | None) -> str | None:
        if audit_event_id is not None:
            uuid.UUID(audit_event_id)
        return audit_event_id


class GraphLeg(BaseModel):
    """One graph's share of a `MultiGraphGroup`: primitives, preconditions, checkpoint request.

    Attributes:
        graph: Name of the graph; non-blank.
        primitives: The mutations of this graph in submission order; at least one.
        preconditions: Conditions that must hold on this graph when the set is logged.
        replace: Whether the leg replaces the graph's content; only upserts are then allowed.
        checkpoint_requested: Whether to record a digest of this graph once the leg is applied.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str = Field(min_length=1, pattern=_NON_BLANK)
    primitives: tuple[Primitive, ...] = Field(min_length=1)
    """The mutations of this graph, in submission order."""
    preconditions: tuple[Precondition, ...] = ()
    """Conditions the caller needs to hold on this graph when the set is logged."""
    replace: bool = False
    """Leave the graph holding exactly this leg's content: what the leg does not upsert is deleted.

    Only upserts may be given (the leg IS the content). The gateway expands the replacement under
    the graph's lock, at the set's position, into ordinary logged deletes ahead of the upserts, so
    replay rebuilds the same graph. Off: the leg merges into what the graph holds.
    """
    checkpoint_requested: bool = False
    """Record this graph's canonical digest at its last position once the leg is applied."""

    @model_validator(mode="after")
    def _replace_holds_only_upserts(self) -> GraphLeg:
        """Reject a replacing leg that holds anything but upserts: it is the content itself."""
        if self.replace and not all(
            isinstance(p, UpsertNode | UpsertEdge) for p in self.primitives
        ):
            message = "a replacing leg holds only UpsertNode and UpsertEdge primitives"
            raise ValueError(message)
        return self

    def as_group(self, audit_event_id: str) -> MutationGroup:
        """Express the leg as the single-graph group it is checked and logged as.

        Args:
            audit_event_id: Audit event of the causing command (a UUID string).

        Returns:
            A `MutationGroup` with the leg's graph, primitives, preconditions and checkpoint
            request.

        Raises:
            ValueError: `audit_event_id` is not a UUID string.
        """
        return MutationGroup(
            graph=self.graph,
            audit_event_id=audit_event_id,
            primitives=self.primitives,
            preconditions=self.preconditions,
            checkpoint_requested=self.checkpoint_requested,
        )


class MultiGraphGroup(BaseModel):
    """A transaction group over two or more graphs: one command, one log transaction.

    Every leg is checked before anything is logged and all legs are logged together or none is.
    The causing command's audit event id is required: a set is always one command's effect.

    Attributes:
        audit_event_id: Audit event of the causing command (a UUID string).
        legs: One leg per distinct graph; at least two.
        apply: `in_place` or `swap`; see the field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    audit_event_id: str
    """The id of the audit event of the command that caused the set (a UUID string)."""
    legs: tuple[GraphLeg, ...] = Field(min_length=2)
    """One leg per distinct graph; the gateway handles them in ascending graph-name order."""
    apply: Literal["in_place", "swap"] = "in_place"
    """How the logged entries reach FalkorDB.

    `in_place`: leg by leg, in ascending graph order. `swap`: each leg is built on a staged copy
    of its graph and all copies replace the live graphs together, so a reader sees none or all of
    the set. A swap needs a gateway built with a key port.
    """

    @field_validator("audit_event_id")
    @classmethod
    def _check_audit_event_id(cls, audit_event_id: str) -> str:
        uuid.UUID(audit_event_id)
        return audit_event_id

    @model_validator(mode="after")
    def _graphs_are_distinct(self) -> MultiGraphGroup:
        """Name each graph once, so one command never writes a graph twice in one transaction."""
        graphs = [leg.graph for leg in self.legs]
        if len(set(graphs)) != len(graphs):
            message = "a multi-graph group names each graph once"
            raise ValueError(message)
        return self


class GroupOutcome(BaseModel):
    """What the caller learns about a submitted group.

    Attributes:
        graph: Graph the group applied to.
        first_position: First log position the group took; None when nothing was logged.
        last_position: Last log position the group took; None when nothing was logged.
        status: Whether the group was applied, is pending apply, or changed nothing.
        checkpoint: Whether a digest was not requested, recorded, or requested but not recorded.
        checkpoint_position: Position of the recorded digest; set exactly when recorded.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str
    first_position: int | None = Field(ge=1)
    last_position: int | None = Field(ge=1)
    status: Literal["applied", "committed_apply_pending", "unchanged"]
    """`applied`: logged and applied. `committed_apply_pending`: logged, but FalkorDB stayed
    unreachable through the retry budget; the entries stay in the log and are applied on recovery
    (not an error). `unchanged`: every mutation was a no-op, nothing logged.
    """
    checkpoint: Literal["not_requested", "recorded", "not_recorded"] = "not_requested"
    """`recorded`: the digest was stored at `checkpoint_position`. `not_recorded`: one was asked
    for but could not be taken or stored (the group itself is committed all the same).
    """
    checkpoint_position: int | None = Field(default=None, ge=1)
    """The log position the recorded digest belongs to (set exactly when `checkpoint` is
    `recorded`).
    """

    @model_validator(mode="after")
    def _positions_follow_status(self) -> GroupOutcome:
        """Require positions exactly when something was logged."""
        logged = self.first_position is not None and self.last_position is not None
        unlogged = self.first_position is None and self.last_position is None
        if (self.status == "unchanged") != unlogged or not (logged or unlogged):
            message = "positions are set exactly when the group was logged"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _checkpoint_position_follows_checkpoint(self) -> GroupOutcome:
        """Require a checkpoint position exactly when a checkpoint was recorded."""
        if (self.checkpoint == "recorded") != (self.checkpoint_position is not None):
            message = "a checkpoint position is set exactly when a checkpoint was recorded"
            raise ValueError(message)
        return self


class MultiGraphOutcome(BaseModel):
    """What the caller learns about a submitted `MultiGraphGroup`: one outcome per graph.

    Attributes:
        audit_event_id: Audit event of the causing command.
        legs: Per-graph outcomes; see the field.
        status: Overall status; see the field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    audit_event_id: str
    legs: tuple[GroupOutcome, ...]
    """Per-graph outcomes, ordered by graph name (positions and status of each graph)."""
    status: Literal["applied", "committed_apply_pending", "unchanged"]
    """`unchanged`: no leg changed its graph, nothing logged. `committed_apply_pending`: logged, and
    at least one leg is still to be applied (it is, on recovery). Otherwise `applied`.
    """


class CatchUpResult(BaseModel):
    """Where a graph stands after `catch_up`.

    Attributes:
        graph: Graph name.
        applied_position: Highest log position applied to the graph.
        last_position: Last position in the graph's log; 0 when empty.
        caught_up: Whether every logged entry is applied.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str
    applied_position: int = Field(ge=0)
    last_position: int = Field(ge=0)
    caught_up: bool
    """True only if every entry in the log is applied (`applied_position == last_position`)."""


class RecoveryResult(BaseModel):
    """What startup recovery did: the graphs it caught up and the ones still gated.

    Attributes:
        recovered: Graphs caught up, in name order.
        gated: Graphs still behind their log, which refuse writes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    recovered: tuple[str, ...]
    """Graphs whose marker now equals their last logged position, in name order."""
    gated: tuple[str, ...]
    """Graphs still behind their log (FalkorDB down or refusing an entry): writes fail closed."""


class ReplayReport(BaseModel):
    """What a replay of one graph did and how much of it a checkpoint vouches for.

    Attributes:
        graph: Graph name.
        head: The log's last position when the replay started.
        verified_position: Position of the checkpoint compared with, or None.
        unverified_entries: Entries applied that no checkpoint vouches for.
        pages: Number of log reads the replay made.
        resumed_from: First position applied by a resumed run; 0 for a fresh replay.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str
    head: int = Field(ge=0)
    """The log's last position when the replay started; the graph now holds entries 1..head."""
    verified_position: int | None = Field(default=None, ge=1)
    """The position of the checkpoint the rebuilt graph was compared with (None: there is none)."""
    unverified_entries: int = Field(ge=0)
    """Entries after `verified_position` (all of them when it is None): applied, not vouched for."""
    pages: int = Field(default=0, ge=0)
    """How many reads of the log the replay made."""
    resumed_from: int = Field(default=0, ge=0)
    """First position applied by a run that resumed an interrupted replay (0: a fresh replay)."""


class StartupReplayReport(BaseModel):
    """How startup replay dealt with each logged graph; every graph is in exactly one list.

    Attributes:
        replayed: Graphs found empty and rebuilt from the log.
        resumed: Graphs whose interrupted replay was finished.
        caught_up: Non-empty graphs that got the entries they were missing.
        untouched: Graphs that needed nothing.
        gated: Graphs that stay closed to writes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    replayed: tuple[str, ...] = ()
    """Graphs found empty (wiped, lost, never applied) and rebuilt from the log."""
    resumed: tuple[str, ...] = ()
    """Graphs whose interrupted replay was found by its progress record and finished."""
    caught_up: tuple[str, ...] = ()
    """Non-empty graphs that were behind their log and got the missing entries."""
    untouched: tuple[str, ...] = ()
    """Graphs that needed nothing."""
    gated: tuple[str, ...] = ()
    """Graphs that stay closed to writes: a failed replay, or an apply refused or still pending."""
