"""Values Company Merge generates at write time, fixed before a group is submitted.

Purpose: a node's `created_at` (and, for a PendingReview, its uuid) must not be drawn by the
Cypher writer at the moment of the write. A gateway group is logged and may be replayed, so a
value drawn at apply time would differ between the first application and a replay. Every writer
therefore draws its values from a `ValueSource` while it builds the group, and the group carries
them as ordinary literals: applying the group twice leaves one node, and a replay of the log
reproduces the same values.

`SYSTEM_VALUES` is the production source (the UTC clock and `uuid4`). A test injects its own
`ValueSource` to make a run deterministic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["SYSTEM_VALUES", "ValueSource"]


@dataclass(frozen=True)
class ValueSource:
    """Where a writer draws the values it generates: the current instant and fresh ids.

    Attributes:
        now: Returns the instant a created node is stamped with; `created_at` is its ISO-8601
            text.
        new_id: Returns a fresh unique id (a PendingReview's).
    """

    now: Callable[[], datetime]
    """The instant a created node is stamped with (`created_at` is its ISO-8601 text)."""
    new_id: Callable[[], str]
    """A fresh unique id (a PendingReview's)."""


SYSTEM_VALUES = ValueSource(now=lambda: datetime.now(UTC), new_id=lambda: uuid.uuid4().hex)
"""The production source: the current UTC instant and a fresh `uuid4` hex string per call."""
