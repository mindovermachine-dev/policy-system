"""Hand-written boundary fakes for the Graph Write Gateway tests (issue #206).

Postgres and FalkorDB are the only true boundaries. Each is replaced by a Protocol-conforming
in-memory fake injected through the gateway's constructor:

- `InMemoryGraphLogStore` implements the full `GraphLogStore` Protocol: gap-free positions,
  a forward-only applied marker capped at the last position, and a JSON round trip of every
  entry's content (as Postgres stores it).
- `InMemoryGraph` implements `GraphHandle.query` by interpreting ONLY the fixed query templates
  exported from `ps_service.graph_gateway.cypher`, so the fake cannot drift from the gateway.

Both append to one shared `events` list so a test can assert the order "log, then graph".

Import as `from graph_gateway._fakes import ...` (the cross-package idiom of `audit._fakes`).
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import redis.exceptions

from ps_service.graph_gateway import cypher
from ps_service.graph_gateway.errors import GraphLogPersistenceError, GraphLogUnavailableError
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.models import (
    AppendedGroup,
    AppliedMarker,
    DigestCheckpoint,
    GraphLogEntry,
    GraphLogGroup,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import psycopg
    from psycopg.rows import TupleRow

    from ps_service.graph_gateway.gateway import GatewaySettings
    from ps_service.graph_gateway.models import GraphLogGroupDraft
    from ps_service.logging import LogEmitter

_EVENT_LOG_APPEND = "log_append"
_EVENT_GRAPH_WRITE = "graph_write"
_EVENT_GRAPH_READ = "graph_read"
_READ_TEMPLATES = frozenset({"node_state", "edge_state"})
_INDEX_TEMPLATES = frozenset({"list_indexes", "create_index"})
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_IDENTIFIER = r"(?P<{name}>" + _NAME + ")"
_KEY_LIST = rf"(?P<KEYS>n\.{_NAME}(?:, n\.{_NAME})*)"


@dataclass
class Fault:
    """An error a fake raises: `times` more calls (None: every call until healed)."""

    error: Exception
    times: int | None = None

    def trigger(self) -> None:
        """Raise the error if calls remain to fail, counting this one."""
        if self.times == 0:
            return
        if self.times is not None:
            self.times -= 1
        raise self.error


@dataclass
class InMemoryGraphLogStore:
    """In-memory `GraphLogStore`; `events` may be shared with an `InMemoryGraph`."""

    events: list[str] = field(default_factory=list)
    entries: dict[str, list[GraphLogEntry]] = field(default_factory=dict)
    groups: list[GraphLogGroup] = field(default_factory=list)
    markers: dict[str, int] = field(default_factory=dict)
    checkpoints: dict[tuple[str, int], DigestCheckpoint] = field(default_factory=dict)
    audit_rows: set[str] = field(default_factory=set)
    """Ids of committed audit rows (what `graph_log.groups.audit_event_id` references)."""
    xact_locks: dict[str, threading.Lock] = field(default_factory=dict)
    """Stand-ins for `pg_advisory_xact_lock`: held from the append to commit or rollback."""
    append_gate: Callable[[str], None] | None = None
    """Called with the graph name when a standalone append starts (a test's thread barrier)."""
    read_fault: Fault | None = None
    append_fault: Fault | None = None
    append_attempts: int = 0
    """Standalone appends tried, failed ones included."""
    marker_advanced: threading.Event = field(default_factory=threading.Event)
    """Set whenever the applied marker moves (lets a test wait for the reconciler)."""
    marker_history: list[tuple[str, int]] = field(default_factory=list)
    """`(graph, position)` of every `advance_applied_position` call, in call order."""

    def fail_reads(self, times: int | None = None) -> None:
        """Make `read_entries`, `last_position` and `read_applied_position` report Postgres down."""
        self.read_fault = Fault(GraphLogUnavailableError(), times)

    def fail_appends(self, times: int | None = None, error: Exception | None = None) -> None:
        """Make standalone appends raise `error` (default: Postgres down) for `times` calls."""
        self.append_fault = Fault(error if error is not None else GraphLogUnavailableError(), times)

    def heal(self) -> None:
        """Stop failing reads and appends."""
        self.read_fault = None
        self.append_fault = None

    def logged_count(self, graph: str) -> int:
        """Entries logged for `graph`, without a fault check (test-side bookkeeping)."""
        return len(self.entries.get(graph, []))

    def begin(self) -> InMemoryTransaction:
        """Open a transaction shared by a caller's audit row and the log append."""
        return InMemoryTransaction(self)

    def append_group(
        self,
        cur: object,
        group: GraphLogGroupDraft,
        *,
        audit_event_id: str | None = None,
    ) -> AppendedGroup:
        """Stage `group` on the caller's transaction; it is visible only once that commits.

        Faithful to `PsycopgGraphLogStore.append_group`: the per-graph lock is taken here and
        held until the transaction ends, and an `audit_event_id` must name an audit row the
        same transaction (or an earlier one) recorded.
        """
        assert isinstance(cur, InMemoryTransaction), "append_group needs the caller's transaction"
        if audit_event_id is not None:
            uuid.UUID(audit_event_id)
            if audit_event_id not in cur.audit_rows and audit_event_id not in self.audit_rows:
                message = "the audit event id names no audit row"
                raise GraphLogPersistenceError(message)
        return cur.stage(group, audit_event_id)

    def append_group_standalone(
        self, group: GraphLogGroupDraft, *, audit_event_id: str | None = None
    ) -> AppendedGroup:
        if audit_event_id is not None:
            uuid.UUID(audit_event_id)
        self.append_attempts += 1
        if self.append_fault is not None:
            self.append_fault.trigger()
        if self.append_gate is not None:
            self.append_gate(group.graph)
        transaction = self.begin()
        appended = transaction.stage(group, audit_event_id)
        transaction.commit()
        return appended

    def read_entries(self, graph: str, *, after_position: int = 0) -> tuple[GraphLogEntry, ...]:
        if self.read_fault is not None:
            self.read_fault.trigger()
        return tuple(e for e in self.entries.get(graph, []) if e.position > after_position)

    def last_position(self, graph: str) -> int:
        if self.read_fault is not None:
            self.read_fault.trigger()
        return self.logged_count(graph)

    def graphs_with_pending_entries(self) -> tuple[str, ...]:
        if self.read_fault is not None:
            self.read_fault.trigger()
        return tuple(
            sorted(
                graph
                for graph in self.entries
                if self.logged_count(graph) > self.markers.get(graph, 0)
            )
        )

    def read_groups_by_audit_event(self, audit_event_id: str) -> tuple[GraphLogGroup, ...]:
        uuid.UUID(audit_event_id)
        return tuple(g for g in self.groups if g.audit_event_id == audit_event_id)

    def read_applied_position(self, graph: str) -> int:
        if self.read_fault is not None:
            self.read_fault.trigger()
        return self.markers.get(graph, 0)

    def advance_applied_position(self, graph: str, position: int) -> AppliedMarker:
        if position > self.logged_count(graph):
            message = "the applied position may not pass the last logged position"
            raise AssertionError(message)
        self.marker_history.append((graph, position))
        self.marker_advanced.set()
        self.markers[graph] = max(self.markers.get(graph, 0), position)
        return AppliedMarker(graph=graph, applied_position=self.markers[graph])

    def record_digest_checkpoint(
        self, graph: str, position: int, canonical_digest: str
    ) -> DigestCheckpoint:
        checkpoint = DigestCheckpoint(
            graph=graph, position=position, canonical_digest=canonical_digest
        )
        self.checkpoints[(graph, position)] = checkpoint
        return checkpoint

    def read_digest_checkpoint(self, graph: str, position: int) -> DigestCheckpoint | None:
        return self.checkpoints.get((graph, position))


@dataclass
class InMemoryTransaction:
    """The caller's open transaction: its audit rows and staged log groups commit or vanish.

    `cursor` is what a caller hands to `append_group` / `submit_group_in_transaction`; the real
    thing is a `psycopg.Cursor`, which the fake store accepts only in this form.
    """

    store: InMemoryGraphLogStore
    audit_rows: list[str] = field(default_factory=list)
    _staged: list[tuple[GraphLogGroup, str | None]] = field(default_factory=list)
    _held: dict[str, threading.Lock] = field(default_factory=dict)

    @property
    def cursor(self) -> psycopg.Cursor[TupleRow]:
        """This transaction typed as the cursor the real store takes."""
        return cast("psycopg.Cursor[TupleRow]", self)  # the fake store unwraps it again

    def record_audit(self) -> str:
        """Record an audit row on this transaction and return its id."""
        audit_event_id = str(uuid.uuid4())
        self.audit_rows.append(audit_event_id)
        return audit_event_id

    def stage(self, group: GraphLogGroupDraft, audit_event_id: str | None) -> AppendedGroup:
        """Take the graph's transaction lock and assign the group the next contiguous positions."""
        graph = group.graph
        if graph not in self._held:
            lock = self.store.xact_locks.setdefault(graph, threading.Lock())
            lock.acquire()
            self._held[graph] = lock
        staged_here = sum(len(g.entries) for g, _ in self._staged if g.graph == graph)
        first_position = self.store.logged_count(graph) + staged_here + 1
        group_id = uuid.uuid4()
        recorded = tuple(
            GraphLogEntry(
                graph=graph,
                position=first_position + offset,
                group_id=group_id,
                name=draft.name,
                identity=draft.identity,
                content=json.loads(json.dumps(draft.content)),
                embedding=draft.embedding,
            )
            for offset, draft in enumerate(group.entries)
        )
        last_position = recorded[-1].position
        self._staged.append(
            (
                GraphLogGroup(
                    group_id=group_id,
                    graph=graph,
                    first_position=first_position,
                    last_position=last_position,
                    audit_event_id="" if audit_event_id is None else audit_event_id,
                    entries=recorded,
                ),
                audit_event_id,
            )
        )
        self.store.events.append(_EVENT_LOG_APPEND)
        return AppendedGroup(
            group_id=group_id,
            graph=graph,
            first_position=first_position,
            last_position=last_position,
        )

    def commit(self) -> None:
        """Publish the audit rows and staged groups together, then release the graph locks."""
        self.store.audit_rows.update(self.audit_rows)
        for group, _ in self._staged:
            self.store.entries.setdefault(group.graph, []).extend(group.entries)
            self.store.groups.append(group)
        self._finish()

    def rollback(self) -> None:
        """Discard the audit rows and staged groups, then release the graph locks."""
        self._finish()

    def _finish(self) -> None:
        self.audit_rows = []
        self._staged = []
        for lock in self._held.values():
            lock.release()
        self._held = {}


@dataclass(frozen=True)
class FakeQueryResult:
    """Structural `GraphQueryResult`."""

    result_set: list[object] = field(default_factory=list)


@dataclass(frozen=True)
class RecordedQuery:
    """One query the fake graph interpreted: its kind (read/write/index), text and params."""

    kind: str
    text: str
    params: dict[str, object]
    template: str = ""
    """Name of the gateway template the query is an instance of (`upsert_node`, ...)."""
    labels: tuple[str, ...] = ()
    """The labels or type interpolated into the template, in template order."""


def _compile(template: str, placeholders: tuple[str, ...]) -> re.Pattern[str]:
    """Turn a gateway query template into a regex that captures its interpolated names."""
    pattern = re.escape(template)
    for name in placeholders:
        replacement = _KEY_LIST if name == "KEYS" else _IDENTIFIER.format(name=name)
        pattern = pattern.replace(re.escape("{" + name + "}"), replacement)
    return re.compile(f"^{pattern}$")


def _kind_of(template: str) -> str:
    if template in _READ_TEMPLATES:
        return "read"
    return "index" if template in _INDEX_TEMPLATES else "write"


def _rows(params: dict[str, object] | None) -> list[dict[str, object]]:
    rows = (params or {}).get("rows", [])
    assert isinstance(rows, list)
    return [row for row in rows if isinstance(row, dict)]  # pyright: ignore[reportUnknownVariableType]


@dataclass
class InMemoryGraph:
    """In-memory `GraphHandle` that interprets only the gateway's fixed query templates."""

    events: list[str] = field(default_factory=list)
    nodes: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    edges: dict[tuple[str, str, str, str, str, str], dict[str, object]] = field(
        default_factory=dict
    )
    queries: list[RecordedQuery] = field(default_factory=list)
    upsert_order: list[tuple[str, str]] = field(default_factory=list)
    """`(label, id)` of every node upsert row, in the order the graph received them."""
    indexes: set[str] = field(default_factory=set)
    """Labels with an index on `id`."""
    labels_indexed_by_a_racing_writer: set[str] = field(default_factory=set)
    """Labels whose `CREATE INDEX` is answered `already indexed` (another writer won the race)."""
    read_fault: Fault | None = None
    write_fault: Fault | None = None
    index_fault: Fault | None = None
    writes_before_failure: int | None = None
    """When set, this many more writes succeed and every later write raises `write_error`."""
    write_error: Exception | None = None
    _templates: dict[str, re.Pattern[str]] = field(
        default_factory=lambda: {
            "upsert_node": _compile(cypher.UPSERT_NODE_TEMPLATE, ("L",)),
            "merge_property": _compile(cypher.MERGE_PROPERTY_TEMPLATE, ("L",)),
            "remove_property": _compile(cypher.REMOVE_PROPERTY_TEMPLATE, ("L", "KEYS")),
            "delete_node": _compile(cypher.DELETE_NODE_TEMPLATE, ("L",)),
            "upsert_edge": _compile(cypher.UPSERT_EDGE_TEMPLATE, ("SL", "TL", "T")),
            "delete_edge": _compile(cypher.DELETE_EDGE_TEMPLATE, ("SL", "TL", "T")),
            "node_state": _compile(cypher.NODE_STATE_TEMPLATE, ("L",)),
            "edge_state": _compile(cypher.EDGE_STATE_TEMPLATE, ("SL", "TL", "T")),
            "list_indexes": _compile(cypher.LIST_INDEXES, ()),
            "create_index": _compile(cypher.CREATE_INDEX_TEMPLATE, ("L",)),
        }
    )

    def fail_on_read(self, error: Exception, times: int | None = None) -> None:
        """Make state reads raise `error` for `times` calls (None: until `heal`)."""
        self.read_fault = Fault(error, times)

    def fail_on_write(self, error: Exception, times: int | None = None) -> None:
        """Make write queries raise `error` for `times` calls; reads and index calls still work."""
        self.write_fault = Fault(error, times)

    def fail_on_index(self, error: Exception, times: int | None = None) -> None:
        """Make `CREATE INDEX` raise `error` for `times` calls."""
        self.index_fault = Fault(error, times)

    def fail_after_n_writes(self, count: int, error: Exception) -> None:
        """Let `count` more write queries succeed, then raise `error` from every write."""
        self.writes_before_failure = count
        self.write_error = error

    def heal(self) -> None:
        """Stop failing writes."""
        self.writes_before_failure = None
        self.write_error = None
        self.read_fault = None
        self.write_fault = None
        self.index_fault = None

    def _raise_if_write_fails(self) -> None:
        if self.write_fault is not None:
            self.write_fault.trigger()
        if self.writes_before_failure is None:
            return
        if self.writes_before_failure == 0:
            assert self.write_error is not None
            raise self.write_error
        self.writes_before_failure -= 1

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        for name, pattern in self._templates.items():
            match = pattern.match(q)
            if match is not None:
                return self._run(name, match.groupdict(), _rows(params), q, params or {})
        message = f"the fake graph interprets only gateway templates, got: {q}"
        raise AssertionError(message)

    def _run(
        self,
        name: str,
        names: dict[str, str],
        rows: list[dict[str, object]],
        text: str,
        params: dict[str, object],
    ) -> FakeQueryResult:
        kind = _kind_of(name)
        if kind == "read" and self.read_fault is not None:
            self.read_fault.trigger()
        if kind == "write":
            self._raise_if_write_fails()
        self.queries.append(RecordedQuery(kind, text, params, name, tuple(names.values())))
        if kind != "index":  # index housekeeping is not part of the log-then-graph story
            self.events.append(_EVENT_GRAPH_READ if kind == "read" else _EVENT_GRAPH_WRITE)
        if kind == "index":
            return self._run_index(name, names)
        if name == "upsert_node":
            self._apply_upsert_node(names["L"], rows)
        elif name == "node_state":
            return FakeQueryResult(self._read_nodes(names["L"], rows))
        elif name == "edge_state":
            return FakeQueryResult(self._read_edges(names, rows))
        elif name == "merge_property":
            self._apply_merge_property(names["L"], rows)
        elif name == "remove_property":
            self._apply_remove_property(names, rows)
        elif name == "delete_node":
            self._apply_delete_node(names["L"], rows)
        elif name == "upsert_edge":
            self._apply_upsert_edge(names, rows)
        else:
            self._apply_delete_edge(names, rows)
        return FakeQueryResult()

    def _run_index(self, name: str, names: dict[str, str]) -> FakeQueryResult:
        """Answer `CALL db.indexes()` in FalkorDB's column order, or create an `id` index."""
        if name == "list_indexes":
            return FakeQueryResult(
                [
                    [label, ["id"], {"id": ["RANGE"]}, {}, None, None, "NODE", "OPERATIONAL", {}]
                    for label in sorted(self.indexes)
                ]
            )
        label = names["L"]
        if self.index_fault is not None:
            self.index_fault.trigger()
        if label in self.labels_indexed_by_a_racing_writer or label in self.indexes:
            self.indexes.add(label)
            message = "Attribute 'id' is already indexed"
            raise redis.exceptions.ResponseError(message)
        self.indexes.add(label)
        return FakeQueryResult()

    def flush(self) -> None:
        """Drop all data and indexes (a flushed or swapped-in graph)."""
        self.nodes.clear()
        self.edges.clear()
        self.indexes.clear()

    def _read_nodes(self, label: str, rows: list[dict[str, object]]) -> list[object]:
        """Answer `RETURN n.id, properties(n)`: `properties` carries `id`, as FalkorDB's does."""
        return [
            [row["id"], {"id": row["id"], **self.nodes[(label, str(row["id"]))]}]
            for row in rows
            if (label, str(row["id"])) in self.nodes
        ]

    def _read_edges(self, names: dict[str, str], rows: list[dict[str, object]]) -> list[object]:
        """Answer the edge state read: endpoints and identity, then `properties(r)`."""
        return [
            [row["source_id"], row["target_id"], row["identity"], dict(self.edges[key])]
            for row in rows
            if (key := self._edge_key(names, row)) in self.edges
        ]

    def _apply_upsert_node(self, label: str, rows: list[dict[str, object]]) -> None:
        for row in rows:
            self.upsert_order.append((label, str(row["id"])))
            node = self.nodes.setdefault((label, str(row["id"])), {})
            properties = row["properties"]
            assert isinstance(properties, dict)
            node.update(properties)  # pyright: ignore[reportUnknownArgumentType]
            if row.get("embedding") is not None:
                node["embedding"] = row["embedding"]

    def _apply_merge_property(self, label: str, rows: list[dict[str, object]]) -> None:
        for row in rows:
            node = self.nodes.get((label, str(row["id"])))
            if node is None:
                continue  # MATCH finds nothing
            properties = row["properties"]
            assert isinstance(properties, dict)
            node.update(properties)  # pyright: ignore[reportUnknownArgumentType]

    def _apply_remove_property(self, names: dict[str, str], rows: list[dict[str, object]]) -> None:
        keys = [part.removeprefix("n.") for part in names["KEYS"].split(", ")]
        for row in rows:
            node = self.nodes.get((names["L"], str(row["id"])))
            if node is None:
                continue  # MATCH finds nothing
            for key in keys:
                node.pop(key, None)

    def _apply_delete_node(self, label: str, rows: list[dict[str, object]]) -> None:
        for row in rows:
            node_id = str(row["id"])
            if self.nodes.pop((label, node_id), None) is None:
                continue
            for key in [
                key
                for key in self.edges
                if (key[1], key[2]) == (label, node_id) or (key[3], key[4]) == (label, node_id)
            ]:
                del self.edges[key]  # DETACH DELETE

    def _edge_key(
        self, names: dict[str, str], row: dict[str, object]
    ) -> tuple[str, str, str, str, str, str]:
        return (
            names["T"],
            names["SL"],
            str(row["source_id"]),
            names["TL"],
            str(row["target_id"]),
            str(row["identity"]),
        )

    def _both_nodes_exist(self, names: dict[str, str], row: dict[str, object]) -> bool:
        return (names["SL"], str(row["source_id"])) in self.nodes and (
            names["TL"],
            str(row["target_id"]),
        ) in self.nodes

    def _apply_upsert_edge(self, names: dict[str, str], rows: list[dict[str, object]]) -> None:
        for row in rows:
            if not self._both_nodes_exist(names, row):
                continue  # MATCH finds nothing, so nothing is merged
            edge = self.edges.setdefault(self._edge_key(names, row), {"identity": row["identity"]})
            properties = row["properties"]
            assert isinstance(properties, dict)
            edge.update(properties)  # pyright: ignore[reportUnknownArgumentType]

    def _apply_delete_edge(self, names: dict[str, str], rows: list[dict[str, object]]) -> None:
        for row in rows:
            self.edges.pop(self._edge_key(names, row), None)


@dataclass
class InMemoryGraphs:
    """Opener for named graphs; every graph shares the `events` list."""

    events: list[str] = field(default_factory=list)
    graphs: dict[str, InMemoryGraph] = field(default_factory=dict)

    def open(self, name: str) -> InMemoryGraph:
        return self.graphs.setdefault(name, InMemoryGraph(events=self.events))


class SteppedWait:
    """A reconciler `wait` the test steps through: each backoff wait blocks until `resume()`.

    The reconciler calls it between passes; `await_wait()` returns once the reconciler is idle in
    one, so the test can inspect state deterministically. `stopped` makes the wait report that
    the reconciler was asked to stop.
    """

    def __init__(self, timeout: float = 5.0) -> None:
        self.delays: list[float] = []
        self.stopped = threading.Event()
        self._timeout = timeout
        self._arrived = threading.Semaphore(0)
        self._go = threading.Semaphore(0)

    def __call__(self, delay: float) -> bool:
        self.delays.append(delay)
        self._arrived.release()
        self._go.acquire(timeout=self._timeout)
        return self.stopped.is_set()

    def await_wait(self) -> None:
        """Block until the reconciler has finished a pass and is waiting."""
        assert self._arrived.acquire(timeout=self._timeout), "the reconciler never waited"

    def resume(self) -> None:
        """Let the current wait return so the reconciler runs its next pass."""
        self._go.release()


_RIGS: list[GatewayRig] = []


def close_all_rigs() -> None:
    """Stop the reconciler of every rig a test built (called by the autouse fixture)."""
    while _RIGS:
        _RIGS.pop().close()


class GatewayRig:
    """A gateway wired to fresh fakes sharing one `events` list.

    Unless a test injects `reconciler_wait` (or asks for the real one), the reconciler parks in
    its first backoff wait until `close()`, so a pending graph is never reconciled behind the
    test's back.
    """

    def __init__(
        self,
        emitter: LogEmitter | None = None,
        settings: GatewaySettings | None = None,
        reconciler_wait: Callable[[float], bool] | None = None,
        *,
        real_reconciler_wait: bool = False,
    ) -> None:
        self.events: list[str] = []
        self.store = InMemoryGraphLogStore(events=self.events)
        self.graphs = InMemoryGraphs(events=self.events)
        self._settings = settings
        self._emitter = emitter
        self._released = threading.Event()
        self._reconciler_wait = (
            reconciler_wait if reconciler_wait is not None or real_reconciler_wait else self._park
        )
        _RIGS.append(self)
        self.sleeps: list[float] = []
        """Every backoff the gateway asked to sleep, in order (no real time passes)."""
        self.gateway = self._new_gateway()

    def _park(self, delay: float) -> bool:
        """Hold the reconciler in its backoff until the rig closes (a bound, for safety)."""
        del delay
        return self._released.wait(timeout=30.0)

    def close(self) -> None:
        """Release a parked reconciler and stop it."""
        self._released.set()
        self.gateway.stop_reconciler()

    def _new_gateway(self) -> GraphWriteGateway:
        return GraphWriteGateway(
            log_store=self.store,
            graph_opener=self.graphs.open,
            settings=self._settings,
            emitter=self._emitter,
            sleep=self.sleeps.append,
            reconciler_wait=self._reconciler_wait,
        )

    def restart(self) -> GraphWriteGateway:
        """Replace the gateway by a fresh one over the same log and graphs (a process restart)."""
        self.gateway = self._new_gateway()
        return self.gateway
