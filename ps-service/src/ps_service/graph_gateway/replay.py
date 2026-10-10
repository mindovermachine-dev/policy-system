"""Rebuild a graph from the mutation log and verify it against a checkpoint (issue #207).

The replay reads the log in pages, in sequence order, and applies it through the same applier the
live path uses, so a replayed log and a live apply cannot disagree. A page is cut at the
checkpoint position so the digest is taken exactly there. It never records a checkpoint itself:
a digest of a graph rebuilt from the log would only vouch for the replay.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, NoReturn

from ps_service.graph_gateway.applier import apply_decoded
from ps_service.graph_gateway.cypher import GRAPH_HOLDS_A_NODE
from ps_service.graph_gateway.digest import EMPTY_GRAPH_DIGEST, canonical_digest
from ps_service.graph_gateway.entry_codec import decode_entry
from ps_service.graph_gateway.errors import (
    GraphDigestMismatchError,
    GraphLogCorruptEntryError,
    GraphLogEntryDecodeError,
    GraphLogGapError,
    GraphReplayError,
    GraphReplayGatedError,
    GraphReplayStoppedError,
    GraphReplayUnverifiableError,
)
from ps_service.graph_gateway.index_manager import IdIndexes
from ps_service.graph_gateway.models import ReplayReport
from ps_service.graph_gateway.replay_state import (
    NOT_VERIFIED,
    ReplayState,
    delete_replay_state,
    read_replay_state,
    write_replay_state,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ps_service.graph_gateway.models import DigestCheckpoint, GraphLogEntry, Primitive
    from ps_service.graph_gateway.replay_state import ReplayStateName
    from ps_service.graph_gateway.store import GraphLogStore
    from ps_service.ingestion.falkordb_client import GraphHandle

type StartupAction = Literal["resume", "replay", "catch_up", "untouched"]
"""What startup does for a graph: finish a replay, rebuild it, apply what is missing, or nothing."""
type _Decoded = list[tuple[int, Primitive]]


class GraphReplayer:
    """Replays one graph's log into FalkorDB. The caller holds the graph's lock."""

    def __init__(
        self,
        log_store: GraphLogStore,
        graph_opener: Callable[[str], GraphHandle],
        *,
        batch_size: int,
        page_size: int,
        should_stop: Callable[[], bool],
    ) -> None:
        """Take the log, a graph opener, the rows per `UNWIND` query and the entries per read.

        `should_stop` is asked before every page; once it says True the replay ends there.
        """
        self._log_store = log_store
        self._graph_opener = graph_opener
        self._batch_size = batch_size
        self._page_size = page_size
        self._should_stop = should_stop

    def replay(self, graph: str) -> ReplayReport:
        """Apply the whole log of `graph` in position order and verify what a checkpoint covers.

        The log is applied up to the highest checkpoint at or below its head, the rebuilt graph
        is compared with that checkpoint, and only then does the rest follow, so a mismatch
        stops the replay with nothing applied past the sequence it names. Progress is recorded
        in the graph after every page (`replay_state`), so an interrupted replay resumes there.

        Raises:
            GraphDigestMismatchError: the rebuilt graph differs from the checkpoint.
            GraphLogGapError: the log skips a position.
            GraphLogCorruptEntryError: an entry does not decode.
            GraphReplayUnverifiableError: a resumed replay is past its checkpoint unverified.
            GraphReplayGatedError: the graph's earlier replay failed and is recorded as failed.
            GraphReplayStoppedError: a stop was requested; the record lets a later run resume.
        """
        head = self._log_store.last_position(graph)
        if head == 0:
            return ReplayReport(graph=graph, head=0, unverified_entries=0)
        handle = self._graph_opener(graph)
        try:
            return self._replay_recorded(graph, head, handle)
        except GraphReplayError as exc:
            write_replay_state(
                handle,
                ReplayState(
                    position=exc.position,
                    state="failed",
                    kind=type(exc).__name__,
                    verified=_recorded_verified(handle),
                ),
            )
            raise

    def _replay_recorded(self, graph: str, head: int, handle: GraphHandle) -> ReplayReport:
        """Apply the log of `graph` up to `head`, recording progress; failures propagate."""
        indexes = IdIndexes(handle)
        checkpoint = self._log_store.read_highest_checkpoint_at_or_below(graph, head)
        verify_at = checkpoint.position if checkpoint is not None else 0
        progress = self._start_or_resume(handle, graph, checkpoint)
        pages = 0
        while progress.applied < head:
            if self._should_stop():
                raise GraphReplayStoppedError(graph, progress.applied)
            limit = self._page_limit(progress.applied + 1, head, verify_at)
            entries = self._log_store.read_entries(
                graph, after_position=progress.applied, limit=limit
            )
            pages += 1
            progress = self._advance(handle, graph, entries, progress, limit, indexes)
            if checkpoint is not None and progress.applied == verify_at != progress.verified:
                self._verify(handle, checkpoint)
                progress = progress.verified_at(verify_at)
                write_replay_state(handle, progress.record())
        self._log_store.advance_applied_position(graph, head)
        delete_replay_state(handle)
        return ReplayReport(
            graph=graph,
            head=head,
            verified_position=verify_at or None,
            unverified_entries=head - verify_at,
            pages=pages,
            resumed_from=progress.resumed_from,
        )

    def classify(self, graph: str) -> StartupAction:
        """Decide, by probing FalkorDB, what startup must do for `graph` (it has log entries).

        The applied marker cannot say: it only moves forward, so a wiped or half rebuilt graph
        still reads "applied". A progress record means a replay to resume (or one recorded as
        failed). Without one, a graph that holds nodes is only caught up if it is behind its
        log; an empty graph is rebuilt, unless its log nets to empty and a checkpoint at the
        head says so.
        """
        handle = self._graph_opener(graph)
        if read_replay_state(handle) is not None:
            return "resume"
        head = self._log_store.last_position(graph)
        applied = self._log_store.read_applied_position(graph)
        if handle.query(GRAPH_HOLDS_A_NODE).result_set:
            return "catch_up" if applied < head else "untouched"
        return "untouched" if self._nets_to_empty(graph, head, applied) else "replay"

    def _nets_to_empty(self, graph: str, head: int, applied: int) -> bool:
        """Whether an empty graph is what the log leaves: applied through the head and vouched."""
        if applied != head:
            return False
        checkpoint = self._log_store.read_digest_checkpoint(graph, head)
        return checkpoint is not None and checkpoint.canonical_digest == EMPTY_GRAPH_DIGEST

    def _start_or_resume(
        self, handle: GraphHandle, graph: str, checkpoint: DigestCheckpoint | None
    ) -> _Progress:
        """Open the progress record of a fresh replay, or pick up the one an earlier run left.

        A resumed run first settles the verification its predecessor may have left open (see the
        rule table in the CHANGES of issue #207, A2).
        """
        recorded = read_replay_state(handle)
        if recorded is None:
            fresh = _Progress(applied=0, verified=NOT_VERIFIED, resumed_from=0)
            write_replay_state(handle, fresh.record())
            return fresh
        if recorded.state == "failed":
            raise GraphReplayGatedError(graph, recorded.position, failed=True)
        progress = _Progress(
            applied=recorded.position,
            verified=recorded.verified,
            resumed_from=recorded.position + 1,
        )
        if checkpoint is None or progress.applied < checkpoint.position:
            return progress
        if progress.verified == checkpoint.position:
            return progress
        if progress.applied > checkpoint.position:
            self._fail_unverifiable(checkpoint)
        self._verify(handle, checkpoint)
        verified = progress.verified_at(checkpoint.position)
        write_replay_state(handle, verified.record())
        return verified

    def _fail_unverifiable(self, checkpoint: DigestCheckpoint) -> NoReturn:
        """Raise: the resumed replay is past its checkpoint unverified (`replay` records it)."""
        raise GraphReplayUnverifiableError(checkpoint.graph, checkpoint.position)

    def _advance(
        self,
        handle: GraphHandle,
        graph: str,
        entries: Sequence[GraphLogEntry],
        progress: _Progress,
        expected: int,
        indexes: IdIndexes,
    ) -> _Progress:
        """Apply one page and record where the replay now stands."""
        applied = self._apply_page(handle, graph, entries, progress.applied + 1, expected, indexes)
        moved = progress.moved_to(applied - 1)
        write_replay_state(handle, moved.record())
        return moved

    def _page_limit(self, next_position: int, head: int, verify_at: int) -> int:
        """Entries to read next: a full page, cut at the checkpoint and at the head."""
        limit = min(self._page_size, head - next_position + 1)
        if next_position <= verify_at:
            limit = min(limit, verify_at - next_position + 1)
        return limit

    def _apply_page(
        self,
        handle: GraphHandle,
        graph: str,
        entries: Sequence[GraphLogEntry],
        first: int,
        expected: int,
        indexes: IdIndexes,
    ) -> int:
        """Apply the sound start of a page, then raise if the page is not sound; return the next.

        Raises:
            GraphLogGapError: the page skips a position or ends early.
            GraphLogCorruptEntryError: an entry does not decode.
        """
        decoded, failure = _decode_page(graph, entries, first, expected)
        if decoded:
            apply_decoded(
                handle,
                decoded,
                batch_size=self._batch_size,
                on_run_applied=_ignore,
                indexes=indexes,
            )
        if failure is not None:
            raise failure
        return first + len(decoded)

    def _verify(self, handle: GraphHandle, checkpoint: DigestCheckpoint) -> None:
        """Raise if the rebuilt graph's digest is not the checkpoint's."""
        actual = canonical_digest(handle)
        if actual != checkpoint.canonical_digest:
            raise GraphDigestMismatchError(
                checkpoint.graph,
                checkpoint.position,
                expected=checkpoint.canonical_digest,
                actual=actual,
            )


def _recorded_verified(handle: GraphHandle) -> int:
    """The checkpoint position the progress record says was verified (-1: none, or no record)."""
    recorded = read_replay_state(handle)
    return recorded.verified if recorded is not None else NOT_VERIFIED


@dataclass(frozen=True)
class _Progress:
    """Where a replay stands: the last position applied and the checkpoint it matched."""

    applied: int
    verified: int
    resumed_from: int

    def moved_to(self, applied: int) -> _Progress:
        return replace(self, applied=applied)

    def verified_at(self, position: int) -> _Progress:
        return replace(self, verified=position)

    def record(self, *, state: ReplayStateName = "in_progress", kind: str = "") -> ReplayState:
        """The sentinel that says this."""
        return ReplayState(position=self.applied, state=state, kind=kind, verified=self.verified)


def _decode_page(
    graph: str, entries: Sequence[GraphLogEntry], first: int, expected: int
) -> tuple[_Decoded, GraphReplayError | None]:
    """Decode the entries of a page that are contiguous from `first` and decode cleanly.

    Returns them with the error that ended the page early (None when the page is whole). A page
    shorter than the `expected` count means the log ends before its own head: a gap.
    """
    decoded: _Decoded = []
    for offset, entry in enumerate(entries):
        if entry.position != first + offset:
            return decoded, GraphLogGapError(graph, first + offset)
        try:
            decoded.append((entry.position, decode_entry(entry)))
        except GraphLogEntryDecodeError as exc:
            failure = GraphLogCorruptEntryError(graph, entry.position)
            failure.__cause__ = exc
            return decoded, failure
    if len(entries) < expected:
        return decoded, GraphLogGapError(graph, first + len(entries))
    return decoded, None


def _ignore(position: int) -> None:
    """Replay moves the applied marker once, at the end, so it ignores per-run progress."""
    del position
