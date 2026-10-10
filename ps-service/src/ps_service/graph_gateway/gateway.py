"""The Graph Write Gateway: log-first writes of graph mutations (issue #206).

A caller submits a `MutationGroup`. The gateway appends it to the Postgres mutation log (the
commit point), applies the logged entries to FalkorDB in batches and moves the applied marker
forward, then reports a `GroupOutcome`. Apply always reads the log beyond the marker, so a retry
or a replay takes effect once per entry.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import redis.exceptions

from ps_service.dependency_health import FALKORDB, mark_healthy
from ps_service.graph_gateway.applier import apply_entries
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.entry_codec import encode_primitive
from ps_service.graph_gateway.errors import (
    GraphApplyBlockedError,
    GraphApplyError,
    GraphDigestError,
    GraphLogPersistenceError,
    GraphLogUnavailableError,
    GraphReplayError,
    GraphReplayGatedError,
    GraphReplayStoppedError,
    GraphUnavailableError,
    GraphWriteRejectedError,
    StagedGroupNotCommittedError,
    UnexpectedGraphReplyError,
)
from ps_service.graph_gateway.gateway_log import emit_gateway_event
from ps_service.graph_gateway.graph_locks import GraphLockRegistry
from ps_service.graph_gateway.models import (
    CatchUpResult,
    GraphLogGroupDraft,
    GroupOutcome,
    RecoveryResult,
    ReplayReport,
    StartupReplayReport,
)
from ps_service.graph_gateway.noop_filter import select_effective_primitives
from ps_service.graph_gateway.reconciler import DEFAULT_STOP_TIMEOUT_SECONDS, GraphReconciler
from ps_service.graph_gateway.replay import GraphReplayer
from ps_service.graph_gateway.replay_gate import ReplayGate
from ps_service.graph_gateway.retry import GraphCallGuard, system_sleep
from ps_service.graph_gateway.staged_submission import StagedSubmission
from ps_service.graph_gateway.startup_retry import retry_through_outage
from ps_service.graph_gateway.validation import require_preconditions, validate_group

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    import psycopg
    from psycopg.rows import TupleRow

    from ps_service.graph_gateway.models import AppendedGroup, MutationGroup, Primitive
    from ps_service.graph_gateway.store import GraphLogStore
    from ps_service.ingestion.falkordb_client import GraphHandle
    from ps_service.logging import LogEmitter

_CHECKPOINT_FAILURES = (
    GraphDigestError,
    UnexpectedGraphReplyError,
    GraphLogPersistenceError,
    GraphLogUnavailableError,
    redis.exceptions.RedisError,
)
"""What taking or storing a checkpoint may raise without failing the committed group."""

DEFAULT_BATCH_SIZE = 500
DEFAULT_REPLAY_PAGE_SIZE = 5000
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_INITIAL_BACKOFF_SECONDS = 0.2
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_RECONCILER_MAX_BACKOFF_SECONDS = 30.0
DEFAULT_STARTUP_REPLAY_MAX_BACKOFF_SECONDS = 30.0


@dataclass(frozen=True)
class GatewaySettings:
    """Tunables of the gateway."""

    batch_size: int = DEFAULT_BATCH_SIZE
    """Rows per `UNWIND` query."""
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    """Tries of one transient-failure-prone step: the first plus `max_attempts - 1` retries."""
    initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS
    """Wait before the first retry."""
    backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER
    """Each further wait is the previous one times this (no jitter: the schedule is fixed)."""
    reconciler_max_backoff_seconds: float = DEFAULT_RECONCILER_MAX_BACKOFF_SECONDS
    """Longest wait between background reconciler passes (the pass count is unbounded)."""
    startup_replay_max_backoff_seconds: float = DEFAULT_STARTUP_REPLAY_MAX_BACKOFF_SECONDS
    """Longest wait before the startup replay is tried again after an infrastructure outage."""
    replay_page_size: int = DEFAULT_REPLAY_PAGE_SIZE
    """Log entries a replay reads per query, so a long log is never held in memory at once."""

    def __post_init__(self) -> None:
        """Reject values that would make the retry schedule meaningless."""
        if self.batch_size < 1:
            message = "batch_size must be at least 1"
            raise ValueError(message)
        if self.max_attempts < 1:
            message = "max_attempts must be at least 1"
            raise ValueError(message)
        if self.initial_backoff_seconds < 0 or self.backoff_multiplier < 1:
            message = "backoff must be non-negative and must not shrink"
            raise ValueError(message)
        if self.reconciler_max_backoff_seconds < 0:
            message = "reconciler_max_backoff_seconds must be non-negative"
            raise ValueError(message)
        if self.startup_replay_max_backoff_seconds < 0:
            message = "startup_replay_max_backoff_seconds must be non-negative"
            raise ValueError(message)
        if self.replay_page_size < 1:
            message = "replay_page_size must be at least 1"
            raise ValueError(message)


class GraphWriteGateway:
    """Single chokepoint for graph writes: validate, log, apply, report."""

    def __init__(
        self,
        log_store: GraphLogStore,
        graph_opener: Callable[[str], GraphHandle],
        settings: GatewaySettings | None = None,
        emitter: LogEmitter | None = None,
        sleep: Callable[[float], None] | None = None,
        reconciler_wait: Callable[[float], bool] | None = None,
    ) -> None:
        """Take the log store, a graph opener (graph name to handle) and optional tunables.

        `sleep` waits out a retry backoff and `reconciler_wait` the pause between background
        reconciler passes (it returns True to stop the reconciler); tests inject both,
        production uses the real clock and an interruptible `Event.wait`.
        """
        self._log_store = log_store
        self._graph_opener = graph_opener
        self._settings = settings if settings is not None else GatewaySettings()
        self._emitter = emitter
        self._guard = GraphCallGuard(
            self._settings, sleep if sleep is not None else system_sleep, emitter
        )
        self._locks = GraphLockRegistry()
        self._blocked: set[str] = set()
        self._gate = ReplayGate()
        self._stop_replay = threading.Event()
        self._replayer = GraphReplayer(
            log_store,
            graph_opener,
            batch_size=self._settings.batch_size,
            page_size=self._settings.replay_page_size,
            should_stop=self._stop_replay.is_set,
        )
        self._reconciler = GraphReconciler(
            self.catch_up, settings=self._settings, emitter=emitter, wait=reconciler_wait
        )

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        """Log what `group` changes, apply it to its graph and return what happened to it.

        A group none of whose mutations changes the graph is not logged and is reported as
        `unchanged`. The graph's lock is held from the state read to the marker advance, so
        groups for one graph never interleave (see `graph_locks` for the lock order).
        """
        self._gate.require_open(group.graph)
        with self._locks.lock_for(group.graph), self._failure_logged(group.graph):
            return self._submit_locked(group)

    def submit_group_in_transaction(
        self, cur: psycopg.Cursor[TupleRow], group: MutationGroup
    ) -> StagedSubmission:
        """Stage `group` on the caller's transaction; the caller commits, then `complete()`s.

        For writers whose audit row is recorded on `cur`: the group is appended on the same
        transaction (linked by `group.audit_event_id`), so the audit row and the log group commit
        or roll back together. The graph's lock is held until the returned submission is
        completed, aborted or its `with` block ends; a rejection or an append failure releases it
        before raising. One in-transaction submission per transaction; a group none of whose
        mutations changes the graph appends nothing, holds no lock and reports `unchanged`.

        Raises:
            GraphWriteRejectedError: the group was refused (nothing appended, lock released).
        """
        self._gate.require_open(group.graph)
        lock = self._locks.lock_for(group.graph)
        lock.acquire()
        handed_over = False
        try:
            with self._failure_logged(group.graph):
                staged = self._stage_in_transaction(cur, group, lock.release)
            handed_over = staged.status == "staged"
        finally:
            if not handed_over:
                lock.release()
        return staged

    def catch_up(self, graph: str) -> CatchUpResult:
        """Apply every logged entry of `graph` beyond its applied marker, in order.

        Idempotent. While FalkorDB stays unreachable the result says `caught_up=False` (the
        entries stay in the log and the background reconciler keeps trying); a success also
        unblocks a graph that an earlier permanent failure blocked.

        Raises:
            GraphLogUnavailableError: the log could not be read through the retry budget.
            GraphApplyError: FalkorDB refuses an entry; the graph stays blocked.
        """
        self._gate.require_open(graph)
        return self._catch_up(graph, honour_gate=True)

    def _catch_up(self, graph: str, *, honour_gate: bool) -> CatchUpResult:
        """Apply the pending entries of `graph` under its lock (startup replay is not gated)."""
        with self._locks.lock_for(graph), self._failure_logged(graph, "catch_up"):
            if honour_gate:
                self._gate.require_open(graph)
            try:
                self._apply_pending(graph)
            except GraphUnavailableError as exc:
                self._reconciler.register(graph)
                self._log_catch_up(graph, "pending", exc)
            else:
                self._log_catch_up(graph, "success")
            return self._standing(graph)

    def replay_graph(self, graph: str) -> ReplayReport:
        """Rebuild `graph` from its log, in sequence order, and verify it against a checkpoint.

        The graph's lock is held throughout. The applied marker only moves forward, to the log's
        head. No checkpoint is recorded: a digest of a rebuilt graph would vouch for nothing.
        """
        self._gate.begin(graph)
        try:
            with self._locks.lock_for(graph):
                report = self._replayer.replay(graph)
        except GraphReplayStoppedError as exc:
            emit_gateway_event(
                "replay_graph",
                "stopped",
                {"graph": graph, "last_position": exc.position},
                emitter=self._emitter,
            )
            raise
        except GraphReplayError as exc:
            self._gate.fail(graph, exc.position)
            emit_gateway_event(
                "replay_graph",
                "failure",
                {
                    "graph": graph,
                    "error_class": type(exc).__name__,
                    "failed_position": exc.position,
                },
                emitter=self._emitter,
            )
            raise
        self._gate.complete(graph)
        fields: dict[str, str | int | float] = {
            "graph": graph,
            "last_position": report.head,
            "unverified_entries": report.unverified_entries,
            "pages": report.pages,
        }
        if report.verified_position is not None:
            fields["verified_position"] = report.verified_position
        emit_gateway_event("replay_graph", "success", fields, emitter=self._emitter)
        return report

    def stop_replay(self) -> None:
        """Ask every replay of this gateway to end at its next page boundary (shutdown).

        A stopped replay raises `GraphReplayStoppedError`, leaves its progress record in the
        graph and keeps the graph closed to writes as incomplete. The request is permanent for
        this gateway: a restarted service builds a new one and resumes.
        """
        self._stop_replay.set()

    def hold_for_startup_replay(self) -> None:
        """Close every graph to writes until `startup_replay` has dealt with it.

        The service calls this synchronously before it accepts a request, then runs
        `startup_replay` in the background; `startup_replay` also calls it, so a direct caller
        needs nothing else.
        """
        self._gate.begin_all_pending()

    def startup_replay(self) -> StartupReplayReport:
        """Look at every logged graph and rebuild, resume or catch up the ones that need it.

        A graph is judged by probing FalkorDB (see `GraphReplayer.classify`), never by its
        applied marker. Each graph opens for writes as soon as it is dealt with; a graph whose
        replay failed (or is recorded as failed) stays closed and is reported in `gated`, and
        the others carry on. When every logged graph is dealt with the startup hold ends, graphs
        the log has never seen included. An infrastructure error (Postgres or FalkorDB down)
        propagates with the hold still in place, so a caller can retry: each graph resumes from
        its progress record.

        Raises:
            GraphLogUnavailableError: the log could not be read.
            redis.exceptions.RedisError: FalkorDB could not be reached or refused a query.
        """
        self.hold_for_startup_replay()
        buckets: dict[str, list[str]] = {
            "replayed": [],
            "resumed": [],
            "caught_up": [],
            "untouched": [],
            "gated": [],
        }
        for graph in self._log_store.logged_graphs():
            outcome = self._startup_graph(graph)
            buckets[outcome].append(graph)
            if outcome != "gated":
                self._gate.release(graph)
            emit_gateway_event("startup_replay", outcome, {"graph": graph}, emitter=self._emitter)
        self._gate.end_all_pending()
        return StartupReplayReport(**{name: tuple(graphs) for name, graphs in buckets.items()})

    def run_startup_replay(
        self, wait: Callable[[float], bool] | None = None
    ) -> StartupReplayReport:
        """Run `startup_replay`, trying again through an infrastructure outage until it ends.

        The service's background task runs this. `wait(delay)` pauses between tries and returns
        True when the retrying should end (default: an interruptible wait that `stop_replay`
        cuts short); the last outage error is then raised. A graph already dealt with by an
        earlier try is found healthy by the next one, so only the last try's report is returned.

        Raises:
            GraphLogUnavailableError, redis.exceptions.RedisError: the outage outlived a stop.
        """

        def log_retry(attempt: int, delay: float, error: BaseException) -> None:
            emit_gateway_event(
                "startup_replay",
                "retry",
                {
                    "attempt": attempt,
                    "backoff_seconds": delay,
                    "error_class": type(error).__name__,
                },
                emitter=self._emitter,
            )

        return retry_through_outage(
            self.startup_replay,
            settings=self._settings,
            wait=wait if wait is not None else self._stop_replay.wait,
            on_retry=log_retry,
        )

    def gated_graphs(self) -> tuple[str, ...]:
        """Names of the graphs that refuse writes for good until someone acts, sorted.

        A graph whose replay failed, or whose logged entry FalkorDB refuses. A graph that is merely
        waiting for the startup replay is not listed (`graph_replay` covers it in `/ready`).
        """
        return tuple(sorted(self._gate.failed_graphs() | self._blocked))

    def _startup_graph(self, graph: str) -> str:
        """Deal with one logged graph; return the `StartupReplayReport` list it belongs in."""
        action = self._replayer.classify(graph)
        if action == "untouched":
            return "untouched"
        if action == "catch_up":
            return self._startup_catch_up(graph)
        try:
            self.replay_graph(graph)
        except GraphReplayError:
            return "gated"  # replay_graph recorded the failure in the gate
        except GraphReplayGatedError as exc:  # the graph's record says an earlier replay failed
            self._gate.fail(graph, exc.position or 0)
            return "gated"
        return "resumed" if action == "resume" else "replayed"

    def _startup_catch_up(self, graph: str) -> str:
        """Apply the entries a non-empty graph is missing, as `recover` does."""
        try:
            result = self._catch_up(graph, honour_gate=False)
        except GraphApplyError, GraphLogUnavailableError:
            return "gated"
        return "caught_up" if result.caught_up else "gated"

    def recover(self) -> RecoveryResult:
        """Catch up every graph whose log is ahead of its applied marker (startup recovery).

        Synchronous; the service calls it once at startup, off the event loop. A graph that
        cannot be applied (FalkorDB down, or an entry it refuses) does not stop the others and
        does not raise: it is reported in `gated`, and writes to it fail closed until it is
        applied (the background reconciler retries a graph that is merely down; a refused one
        stays blocked). Nothing is started when nothing is pending.

        Raises:
            GraphLogUnavailableError: the log could not be read to find the lagging graphs.
        """
        recovered: list[str] = []
        gated: list[str] = []
        for graph in self._log_store.graphs_with_pending_entries():
            try:
                result = self.catch_up(graph)
            except (GraphApplyError, GraphLogUnavailableError) as exc:
                gated.append(graph)
                emit_gateway_event(
                    "startup_recovery",
                    "failure",
                    {"graph": graph, "error_class": type(exc).__name__},
                    emitter=self._emitter,
                )
                continue
            (recovered if result.caught_up else gated).append(graph)
            emit_gateway_event(
                "startup_recovery",
                "success" if result.caught_up else "pending",
                {"graph": graph},
                emitter=self._emitter,
            )
        return RecoveryResult(recovered=tuple(recovered), gated=tuple(gated))

    def is_caught_up(self, graph: str) -> bool:
        """Answer True only if every committed entry of `graph` is applied.

        A read-only predicate, so it does not retry: when the log cannot be read it raises
        `GraphLogUnavailableError` instead of answering.
        """
        if self._gate.is_gated(graph):
            return False
        applied = self._log_store.read_applied_position(graph)
        return applied == self._log_store.last_position(graph)

    @property
    def is_reconciling(self) -> bool:
        """Whether the background reconciler thread is running."""
        return self._reconciler.is_running

    def stop_reconciler(self, timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> bool:
        """Stop the background reconciler, waiting up to `timeout` seconds; True if it ended."""
        return self._reconciler.stop(timeout)

    def _standing(self, graph: str) -> CatchUpResult:
        """Report where `graph`'s marker and log stand."""
        applied = self._log_store.read_applied_position(graph)
        last = self._log_store.last_position(graph)
        return CatchUpResult(
            graph=graph, applied_position=applied, last_position=last, caught_up=applied == last
        )

    def _log_catch_up(self, graph: str, outcome: str, error: BaseException | None = None) -> None:
        fields: dict[str, str | int | float] = {"graph": graph}
        if error is not None:
            fields["error_class"] = type(error.__cause__ or error).__name__
        emit_gateway_event("catch_up", outcome, fields, emitter=self._emitter)

    def _stage_in_transaction(
        self, cur: psycopg.Cursor[TupleRow], group: MutationGroup, release: Callable[[], None]
    ) -> StagedSubmission:
        """Validate and append `group` on `cur`; the caller holds the graph's lock."""
        effective = self._stage(group)
        if not effective:
            return StagedSubmission(
                status="unchanged", finish=lambda: self._unchanged(group), release=lambda: None
            )
        appended = self._log_store.append_group(
            cur, _draft(group.graph, effective), audit_event_id=group.audit_event_id
        )
        return StagedSubmission(
            status="staged",
            finish=lambda: self._finish_staged(
                appended,
                len(effective),
                group.audit_event_id,
                checkpoint_requested=group.checkpoint_requested,
            ),
            release=release,
        )

    def _finish_staged(
        self,
        appended: AppendedGroup,
        entry_count: int,
        audit_event_id: str,
        *,
        checkpoint_requested: bool,
    ) -> GroupOutcome:
        """Apply the now committed entries and report; the lock is still held."""
        if self._log_store.last_position(appended.graph) < appended.last_position:
            message = f"the staged group of graph {appended.graph} is not in the log yet"
            raise StagedGroupNotCommittedError(message)
        with self._failure_logged(appended.graph):
            return self._apply_committed(
                appended, entry_count, audit_event_id, checkpoint_requested=checkpoint_requested
            )

    def _submit_locked(self, group: MutationGroup) -> GroupOutcome:
        """Stage, log and apply `group`; the caller holds the graph's lock."""
        effective = self._stage(group)
        if not effective:
            return self._unchanged(group)
        draft = _draft(group.graph, effective)
        appended = self._guard.call(
            group.graph,
            lambda: self._log_store.append_group_standalone(
                draft, audit_event_id=group.audit_event_id
            ),
        )
        return self._apply_committed(
            appended,
            len(effective),
            group.audit_event_id,
            checkpoint_requested=group.checkpoint_requested,
        )

    def _stage(self, group: MutationGroup) -> tuple[Primitive, ...]:
        """Reject `group` before anything is logged, else return its effective primitives.

        A rejection is recorded with the error class only.
        """
        try:
            self._gate.require_open(group.graph)
            self._require_not_blocked(group.graph)
            self._catch_up_before_write(group.graph)
            validate_group(group)
            require_preconditions(group, self._read_last_position)
            return self._guard.call(
                group.graph,
                lambda: select_effective_primitives(
                    lambda: self._graph_opener(group.graph),
                    group,
                    batch_size=self._settings.batch_size,
                ),
            )
        except GraphWriteRejectedError as exc:
            emit_gateway_event(
                "submit_rejected",
                "failure",
                {"graph": group.graph, "error_class": type(exc).__name__},
                emitter=self._emitter,
            )
            raise

    @contextlib.contextmanager
    def _failure_logged(self, graph: str, action: str = "apply_group") -> Generator[None]:
        """Log a sanitized failure of `graph` (class, and position if known), then re-raise."""
        try:
            yield
        except (GraphUnavailableError, GraphLogUnavailableError, GraphApplyError) as exc:
            fields: dict[str, str | int | float] = {
                "graph": graph,
                "error_class": type(exc).__name__,
            }
            if isinstance(exc, GraphApplyError) and exc.position is not None:
                fields["first_position"] = exc.position
            emit_gateway_event(action, "failure", fields, emitter=self._emitter)
            raise

    def _catch_up_before_write(self, graph: str) -> None:
        """Apply the entries a lagging graph still owes, before this write reads or logs anything.

        The check reads the marker and the last position on every write, so it also covers a
        process that restarted with unapplied entries. If FalkorDB cannot take them the write is
        refused with nothing logged and the graph goes to the reconciler.
        """
        lagging = self._guard.call(
            graph,
            lambda: (
                self._log_store.read_applied_position(graph) != self._log_store.last_position(graph)
            ),
        )
        if not lagging:
            return
        try:
            self._apply_pending(graph)
        except GraphUnavailableError:
            self._reconciler.register(graph)
            raise

    def _read_last_position(self, graph: str) -> int:
        """Read the log's last position for `graph`, retrying while Postgres is unreachable."""
        return self._guard.call(graph, lambda: self._log_store.last_position(graph))

    def _unchanged(self, group: MutationGroup) -> GroupOutcome:
        """Report a group that changes nothing: no log entry, no apply."""
        emit_gateway_event(
            "apply_group",
            "unchanged",
            {
                "graph": group.graph,
                "entry_count": 0,
                "audit_event_id": group.audit_event_id,
            },
            emitter=self._emitter,
        )
        return GroupOutcome(
            graph=group.graph,
            first_position=None,
            last_position=None,
            status="unchanged",
            checkpoint=_checkpoint_state(requested=group.checkpoint_requested, recorded=False),
        )

    def _apply_committed(
        self,
        appended: AppendedGroup,
        entry_count: int,
        audit_event_id: str,
        *,
        checkpoint_requested: bool,
    ) -> GroupOutcome:
        """Apply what is logged for the group's graph and report how the group stands.

        Past the retry budget a group that is already committed is not an error: it is reported
        as `committed_apply_pending` and its entries stay in the log beyond the marker. A
        requested checkpoint is taken here, under the graph's lock and only once the group is
        applied, so the digest belongs exactly to the group's last position.
        """
        status: Literal["applied", "committed_apply_pending"] = "applied"
        try:
            self._apply_pending(appended.graph)
        except (GraphUnavailableError, GraphLogUnavailableError) as exc:
            status = "committed_apply_pending"
            self._reconciler.register(appended.graph)
            self._log_group(appended, entry_count, audit_event_id, "pending", exc)
        else:
            self._log_group(appended, entry_count, audit_event_id, "success")
        recorded = (
            checkpoint_requested and status == "applied" and self._record_checkpoint(appended)
        )
        return GroupOutcome(
            graph=appended.graph,
            first_position=appended.first_position,
            last_position=appended.last_position,
            status=status,
            checkpoint=_checkpoint_state(requested=checkpoint_requested, recorded=recorded),
            checkpoint_position=appended.last_position if recorded else None,
        )

    def _record_checkpoint(self, appended: AppendedGroup) -> bool:
        """Store the digest of the graph after the group at the group's last position.

        The group is committed and applied by now, so a failure here does not fail it: it is
        logged by error class (position and graph only) and reported as `not_recorded`.
        """
        fields: dict[str, str | int | float] = {
            "graph": appended.graph,
            "last_position": appended.last_position,
        }
        try:
            digest = canonical_digest(self._graph_opener(appended.graph))
            self._log_store.record_digest_checkpoint(appended.graph, appended.last_position, digest)
        except _CHECKPOINT_FAILURES as exc:
            fields["error_class"] = type(exc).__name__
            emit_gateway_event("checkpoint", "failure", fields, emitter=self._emitter)
            return False
        emit_gateway_event("checkpoint", "success", fields, emitter=self._emitter)
        return True

    def _apply_pending(self, graph: str) -> None:
        """Apply every logged entry beyond the graph's applied marker, advancing the marker.

        Transient failures are retried, each attempt resuming from the marker. A permanent
        failure blocks the graph (see `_require_not_blocked`).

        Raises:
            GraphUnavailableError: FalkorDB stayed unreachable through the retry budget.
            GraphLogUnavailableError: the log could not be read through the retry budget.
            GraphApplyError: FalkorDB refused a query; the graph is now blocked.
        """
        applied_through = 0

        def apply_pass() -> None:
            nonlocal applied_through
            applied_through = self._log_store.read_applied_position(graph)
            pending = self._log_store.read_entries(graph, after_position=applied_through)

            def advance_marker(position: int) -> None:
                nonlocal applied_through
                self._log_store.advance_applied_position(graph, position)
                applied_through = position

            apply_entries(
                self._graph_opener(graph),
                pending,
                batch_size=self._settings.batch_size,
                on_run_applied=advance_marker,
            )

        try:
            self._guard.call(graph, apply_pass, position=lambda: applied_through + 1)
        except GraphApplyError:
            self._blocked.add(graph)
            raise
        self._blocked.discard(graph)
        mark_healthy(FALKORDB)

    def _require_not_blocked(self, graph: str) -> None:
        """Refuse to take a group for a graph that holds an entry FalkorDB cannot apply.

        Keeps AC-BI-011 (nothing new is accepted while an entry is unapplied) without spinning:
        the graph stays blocked until the process restarts or `catch_up` applies the entry.
        """
        if graph in self._blocked:
            raise GraphApplyBlockedError(graph)

    def _log_group(
        self,
        appended: AppendedGroup,
        entry_count: int,
        audit_event_id: str,
        outcome: str,
        error: BaseException | None = None,
    ) -> None:
        """Log how a group stands: graph and positions only, plus the error class if it is late."""
        fields: dict[str, str | int | float] = {
            "graph": appended.graph,
            "first_position": appended.first_position,
            "last_position": appended.last_position,
            "entry_count": entry_count,
            "audit_event_id": audit_event_id,
        }
        if error is not None:
            fields["error_class"] = type(error.__cause__ or error).__name__
        emit_gateway_event("apply_group", outcome, fields, emitter=self._emitter)


def _checkpoint_state(
    *, requested: bool, recorded: bool
) -> Literal["not_requested", "recorded", "not_recorded"]:
    """Name how a group's checkpoint request ended."""
    if recorded:
        return "recorded"
    return "not_recorded" if requested else "not_requested"


def _draft(graph: str, primitives: tuple[Primitive, ...]) -> GraphLogGroupDraft:
    """Encode `primitives` as the log group that records them."""
    return GraphLogGroupDraft(
        graph=graph, entries=tuple(encode_primitive(primitive) for primitive in primitives)
    )
