"""The replay-progress record: a sentinel node inside the graph being rebuilt.

A replay that dies half way leaves a graph that is non-empty while the applied marker (which only
moves forward and is moved once, at the end) says everything is applied, so nothing outside the
graph can tell. The sentinel `(:GraphReplayState {position, state, kind, verified})` can: it is
written before the first page, advanced after each page, and removed when the replay completes.
It lives in the graph it describes, so wiping the graph wipes the record too. It is bookkeeping,
not graph content: the digest and the emptiness probe skip its label, and no writer can reach it
(the label is not on the allow-list).

`position` is the last log position applied, `state` is `in_progress` or `failed`, `kind` names
the failure of a failed replay and `verified` is the checkpoint position the graph was compared
with (-1: not compared yet).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from ps_service.graph_gateway.cypher import (
    REPLAY_STATE_DELETE,
    REPLAY_STATE_READ,
    REPLAY_STATE_WRITE,
)
from ps_service.graph_gateway.errors import UnexpectedGraphReplyError

if TYPE_CHECKING:
    from ps_service.graph_gateway.connection import WritableGraph

type ReplayStateName = Literal["in_progress", "failed"]

NOT_VERIFIED = -1
_REPLY_MESSAGE = "the graph answered a replay state read with an unexpected shape"
_ROW_WIDTH = 4


@dataclass(frozen=True)
class ReplayState:
    """What the sentinel says about a replay of this graph.

    Attributes:
        position: The last log position applied.
        state: `in_progress` or `failed`.
        kind: Names the failure of a failed replay.
        verified: The checkpoint position the graph was compared with, or `NOT_VERIFIED` (-1)
            when not compared yet.
    """

    position: int
    state: ReplayStateName
    kind: str
    verified: int


def read_replay_state(graph: WritableGraph) -> ReplayState | None:
    """Return the sentinel of `graph`, or None when no replay is recorded (or the key is missing).

    Args:
        graph: The graph holding the sentinel.

    Returns:
        The recorded state, or `None` when the graph holds no sentinel.

    Raises:
        UnexpectedGraphReplyError: the graph answered something that is not a sentinel.
    """
    rows = graph.query(REPLAY_STATE_READ).result_set
    if not rows:
        return None
    row = rows[0]
    if not isinstance(row, list) or len(cast("list[object]", row)) != _ROW_WIDTH:
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    position, state, kind, verified = cast("list[object]", row)
    if (
        not isinstance(position, int)
        or state not in ("in_progress", "failed")
        or not isinstance(kind, str)
        or not isinstance(verified, int)
    ):
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    return ReplayState(position=position, state=state, kind=kind, verified=verified)


def write_replay_state(graph: WritableGraph, state: ReplayState) -> None:
    """Create the sentinel of `graph` or move it to `state`.

    Args:
        graph: The graph that holds the sentinel.
        state: The progress to record.

    Side Effects:
        Creates or updates the sentinel node in `graph`.
    """
    graph.query(
        REPLAY_STATE_WRITE,
        {
            "position": state.position,
            "state": state.state,
            "kind": state.kind,
            "verified": state.verified,
        },
    )


def delete_replay_state(graph: WritableGraph) -> None:
    """Remove the sentinel of `graph` (a no-op when there is none).

    Args:
        graph: The graph holding the sentinel.

    Side Effects:
        Deletes the sentinel node from `graph`.
    """
    graph.query(REPLAY_STATE_DELETE)
