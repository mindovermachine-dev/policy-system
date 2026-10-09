"""Typed models of the graph mutation log (issue #205).

Input drafts are the boundary validation (L2 Data Modeling): a malformed group never reaches
SQL. Models are frozen (L1 Immutability) and reject undeclared fields.
"""

from __future__ import annotations

import uuid  # pydantic resolves field annotations at runtime

from pydantic import BaseModel, ConfigDict, Field

_NON_BLANK = r"\S"
"""Pattern requiring at least one non-whitespace character."""


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
