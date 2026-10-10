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
import math
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
_READ_TEMPLATES = frozenset(
    {
        "node_state",
        "node_exists",
        "edge_state",
        "digest_node_scan",
        "digest_edge_scan",
        "graph_holds_a_node",
    }
)
_INDEX_TEMPLATES = frozenset({"list_indexes", "create_index"})
_STATE_TEMPLATES = frozenset({"replay_state_read", "replay_state_write", "replay_state_delete"})
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_IDENTIFIER = r"(?P<{name}>" + _NAME + ")"
_KEY_LIST = rf"(?P<KEYS>n\.{_NAME}(?:, n\.{_NAME})*)"


def _encode_exact_float(value: float) -> list[object]:
    """Mirror of the Cypher in `exact_floats`: `[s, digits of value * 2**s]`, value non-zero."""
    scale = 54 - math.frexp(value)[1]
    return [scale, f"{math.ldexp(value, scale):.6f}"]


def lossy_float(value: float) -> float:
    """A double as FalkorDB's reply carries it: printed with 15 significant digits, then parsed."""
    return float(f"{value:.15g}")


def lossy_reply(value: object) -> object:
    """A property value as FalkorDB's reply carries it (floats at 15 significant digits)."""
    if isinstance(value, float):
        return lossy_float(value)
    if isinstance(value, list):
        return [lossy_reply(item) for item in cast("list[object]", value)]
    return value


def property_columns(properties: dict[str, object]) -> list[object]:
    """The `pairs, scalars, float lists, mixed lists` columns of a row, as FalkorDB answers.

    Mirror of `exact_floats.PROPERTY_COLUMNS`: a list of floats travels only as exact integers;
    every other property travels as a lossy `[key, value]` pair.
    """
    pairs = [
        [key, lossy_reply(value)] for key, value in properties.items() if not _is_float_list(value)
    ]
    scalars = [
        [key, _encode_exact_float(cast("float", value))]
        for key, value in properties.items()
        if _is_nonzero_float(value)
    ]
    float_lists = [
        _list_column(key, cast("list[object]", value))
        for key, value in properties.items()
        if _is_float_list(value)
    ]
    mixed_lists = [
        _list_column(key, cast("list[object]", value))
        for key, value in properties.items()
        if isinstance(value, list)
        and not _is_float_list(cast("list[object]", value))
        and any(_is_nonzero_float(item) for item in cast("list[object]", value))
    ]
    return [pairs, scalars, float_lists, mixed_lists]


def _list_column(key: str, items: list[object]) -> list[object]:
    """One `[key, scales, integers]` entry of a list column."""
    scales, integers = _aligned_integers(items)
    return [key, scales, integers]


def _is_float_list(value: object) -> bool:
    """A non-empty list whose items are all floats (zero and negative zero included)."""
    if not isinstance(value, list):
        return False
    items = cast("list[object]", value)
    return bool(items) and all(isinstance(item, float) for item in items)


def _is_nonzero_float(value: object) -> bool:
    return isinstance(value, float) and value != 0.0


def _aligned_integers(items: list[object]) -> tuple[list[int], list[int]]:
    """Mirror of the list fragments for one list: scales and integers aligned with the items.

    A position that holds no non-zero float is `0` in the integers; a negative zero is `1` in
    the scales (a positive zero and anything else is `0`).
    """
    scales: list[int] = []
    integers: list[int] = []
    for item in items:
        if _is_nonzero_float(item):
            scale = 54 - math.frexp(cast("float", item))[1]
            scales.append(scale)
            integers.append(int(math.ldexp(cast("float", item), scale)))
        else:
            negative_zero = isinstance(item, float) and math.copysign(1.0, item) < 0
            scales.append(1 if negative_zero else 0)
            integers.append(0)
    return scales, integers


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
    checkpoint_fault: Fault | None = None
    """Raised by `record_digest_checkpoint` (the real store's write failing)."""
    append_attempts: int = 0
    """Standalone appends tried, failed ones included."""
    marker_advanced: threading.Event = field(default_factory=threading.Event)
    """Set whenever the applied marker moves (lets a test wait for the reconciler)."""
    report_head_extra: int = 0
    """Makes `last_position` claim this many entries more than the log holds (a torn read)."""
    page_sizes: list[int] = field(default_factory=list)
    """Entries each paged `read_entries(limit=...)` call returned."""
    page_reads: list[tuple[int, int]] = field(default_factory=list)
    """`(first, last)` position of each non-empty paged read."""
    on_paged_read: Callable[[], None] | None = None
    """Called after each paged `read_entries` (a test's way to act while a replay is mid-flight)."""
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

    def read_entries(
        self, graph: str, *, after_position: int = 0, limit: int | None = None
    ) -> tuple[GraphLogEntry, ...]:
        if self.read_fault is not None:
            self.read_fault.trigger()
        found = tuple(e for e in self.entries.get(graph, []) if e.position > after_position)
        found = found if limit is None else found[:limit]
        if limit is not None:
            self.page_sizes.append(len(found))
            if found:
                self.page_reads.append((found[0].position, found[-1].position))
            if self.on_paged_read is not None:
                self.on_paged_read()
        return found

    def tamper_gap(self, graph: str, position: int) -> None:
        """Remove the entry at `position` without renumbering (the real database forbids this)."""
        self.entries[graph] = [e for e in self.entries[graph] if e.position != position]

    def tamper_corrupt(self, graph: str, position: int) -> None:
        """Replace the entry at `position` by one that names an operation nobody knows."""
        self.entries[graph] = [
            e.model_copy(update={"content": {"op": "bogus"}}) if e.position == position else e
            for e in self.entries[graph]
        ]

    def last_position(self, graph: str) -> int:
        if self.read_fault is not None:
            self.read_fault.trigger()
        return self.logged_count(graph) + self.report_head_extra

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

    def logged_graphs(self) -> tuple[str, ...]:
        if self.read_fault is not None:
            self.read_fault.trigger()
        return tuple(sorted(graph for graph, entries in self.entries.items() if entries))

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
        if self.checkpoint_fault is not None:
            self.checkpoint_fault.trigger()
        checkpoint = DigestCheckpoint(
            graph=graph, position=position, canonical_digest=canonical_digest
        )
        if (graph, position) in self.checkpoints:  # insert-only, as the primary key makes it
            message = "failed to record the digest checkpoint"
            raise GraphLogPersistenceError(message)
        self.checkpoints[(graph, position)] = checkpoint
        return checkpoint

    def read_digest_checkpoint(self, graph: str, position: int) -> DigestCheckpoint | None:
        return self.checkpoints.get((graph, position))

    def read_highest_checkpoint_at_or_below(
        self, graph: str, position: int
    ) -> DigestCheckpoint | None:
        if self.read_fault is not None:
            self.read_fault.trigger()
        at_or_below = [
            checkpoint
            for (name, at), checkpoint in self.checkpoints.items()
            if name == graph and at <= position
        ]
        return max(at_or_below, key=lambda checkpoint: checkpoint.position, default=None)


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
    if template in _STATE_TEMPLATES:
        return "state"
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
    _internal_ids: dict[tuple[str, str], int] = field(default_factory=dict)
    _next_internal_id: int = 0
    _extra_labels: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    _edge_internal_ids: dict[tuple[str, str, str, str, str, str], int] = field(default_factory=dict)
    _next_edge_internal_id: int = 0
    upsert_order: list[tuple[str, str]] = field(default_factory=list)
    """`(label, id)` of every node upsert row, in the order the graph received them."""
    indexes: set[str] = field(default_factory=set)
    """Labels with an index on `id`."""
    labels_indexed_by_a_racing_writer: set[str] = field(default_factory=set)
    """Labels whose `CREATE INDEX` is answered `already indexed` (another writer won the race)."""
    read_fault: Fault | None = None
    write_fault: Fault | None = None
    index_fault: Fault | None = None
    replay_state: tuple[int, str, str, int] | None = None
    """The replay-progress sentinel as `(position, state, kind, verified)`; wiped with the graph.

    Its queries are of kind `state`: bookkeeping that is neither a data write (no write fault, no
    event, not counted in a statement budget) nor a data read.
    """
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
            "node_exists": _compile(cypher.NODE_EXISTS_TEMPLATE, ("L",)),
            "edge_state": _compile(cypher.EDGE_STATE_TEMPLATE, ("SL", "TL", "T")),
            "digest_node_scan": _compile(cypher.DIGEST_NODE_SCAN, ()),
            "digest_edge_scan": _compile(cypher.DIGEST_EDGE_SCAN, ()),
            "replay_state_read": _compile(cypher.REPLAY_STATE_READ, ()),
            "replay_state_write": _compile(cypher.REPLAY_STATE_WRITE, ()),
            "replay_state_delete": _compile(cypher.REPLAY_STATE_DELETE, ()),
            "graph_holds_a_node": _compile(cypher.GRAPH_HOLDS_A_NODE, ()),
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
        if kind == "state":
            return self._run_replay_state(name, params)
        if kind != "index":  # index housekeeping is not part of the log-then-graph story
            self.events.append(_EVENT_GRAPH_READ if kind == "read" else _EVENT_GRAPH_WRITE)
        if kind == "index":
            return self._run_index(name, names)
        if name == "upsert_node":
            self._apply_upsert_node(names["L"], rows)
        elif name == "node_state":
            return FakeQueryResult(self._read_nodes(names["L"], rows))
        elif name == "node_exists":
            return FakeQueryResult(
                [[row["id"]] for row in rows if (names["L"], str(row["id"])) in self.nodes]
            )
        elif name == "edge_state":
            return FakeQueryResult(self._read_edges(names, rows))
        elif name == "graph_holds_a_node":
            return FakeQueryResult(
                [[0]] if any(label != cypher.REPLAY_STATE_LABEL for label, _ in self.nodes) else []
            )
        elif name == "digest_node_scan":
            return FakeQueryResult(self._scan_nodes(params))
        elif name == "digest_edge_scan":
            return FakeQueryResult(self._scan_edges(params))
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

    def _run_replay_state(self, name: str, params: dict[str, object]) -> FakeQueryResult:
        """Read, write or delete the replay-progress sentinel."""
        if name == "replay_state_read":
            return FakeQueryResult([] if self.replay_state is None else [list(self.replay_state)])
        if name == "replay_state_delete":
            self.replay_state = None
            return FakeQueryResult()
        self.replay_state = (
            cast("int", params["position"]),
            cast("str", params["state"]),
            cast("str", params["kind"]),
            cast("int", params["verified"]),
        )
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
        self.replay_state = None
        self._internal_ids.clear()
        self._edge_internal_ids.clear()

    def _read_nodes(self, label: str, rows: list[dict[str, object]]) -> list[object]:
        """Answer the node state read: id, lossy `properties(n)` (with `id`) and exact columns."""
        answers: list[object] = []
        for row in rows:
            stored = self.nodes.get((label, str(row["id"])))
            if stored is not None:
                answers.append([row["id"], *property_columns({"id": row["id"], **stored})])
        return answers

    def seed_node(
        self, labels: tuple[str, ...], node_id: str, properties: dict[str, object]
    ) -> None:
        """Create a node that carries several labels (the gateway only ever writes one).

        The first label keys the node in `nodes`; the others are kept in `_extra_labels`, in
        the order given, as FalkorDB keeps them.
        """
        first, *rest = labels
        self.nodes[(first, node_id)] = dict(properties)
        self._extra_labels[(first, node_id)] = tuple(rest)
        self.internal_id(first, node_id)

    def internal_id(self, label: str, node_id: str) -> int:
        """The node's internal id: assigned once, in creation order, and never reused."""
        key = (label, node_id)
        if key not in self._internal_ids:
            self._internal_ids[key] = self._next_internal_id
            self._next_internal_id += 1
        return self._internal_ids[key]

    def _scan_nodes(self, params: dict[str, object]) -> list[object]:
        """Answer the digest node scan: rows after the `$after` cursor, at most `$limit`."""
        after, limit = cast("int", params["after"]), cast("int", params["limit"])
        scanned: list[tuple[int, list[object]]] = []
        for (label, node_id), stored in self.nodes.items():
            internal_id = self.internal_id(label, node_id)
            if internal_id <= after or label == cypher.REPLAY_STATE_LABEL:
                continue
            properties: dict[str, object] = {"id": node_id, **stored}
            scanned.append(
                (
                    internal_id,
                    [
                        internal_id,
                        [label, *self._extra_labels.get((label, node_id), ())],
                        *property_columns(properties),
                    ],
                )
            )
        return [row for _, row in sorted(scanned, key=lambda item: item[0])[:limit]]

    def _scan_edges(self, params: dict[str, object]) -> list[object]:
        """Answer the digest relationship scan: rows after the `$after` cursor, at most `$limit`."""
        after, limit = cast("int", params["after"]), cast("int", params["limit"])
        scanned: list[tuple[int, list[object]]] = []
        for key, stored in self.edges.items():
            kind, source_label, source_id, target_label, target_id, _ = key
            internal_id = self.edge_internal_id(key)
            if internal_id <= after:
                continue
            scanned.append(
                (
                    internal_id,
                    [
                        internal_id,
                        kind,
                        [source_label],
                        source_id,
                        [target_label],
                        target_id,
                        *property_columns(stored),
                    ],
                )
            )
        return [row for _, row in sorted(scanned, key=lambda item: item[0])[:limit]]

    def edge_internal_id(self, key: tuple[str, str, str, str, str, str]) -> int:
        """The relationship's internal id: assigned once, in creation order, never reused."""
        if key not in self._edge_internal_ids:
            self._edge_internal_ids[key] = self._next_edge_internal_id
            self._next_edge_internal_id += 1
        return self._edge_internal_ids[key]

    def _read_edges(self, names: dict[str, str], rows: list[dict[str, object]]) -> list[object]:
        """Answer the edge state read: endpoints and identity, then the property columns."""
        return [
            [
                row["source_id"],
                row["target_id"],
                row["identity"],
                *property_columns(self.edges[key]),
            ]
            for row in rows
            if (key := self._edge_key(names, row)) in self.edges
        ]

    def _apply_upsert_node(self, label: str, rows: list[dict[str, object]]) -> None:
        for row in rows:
            self.upsert_order.append((label, str(row["id"])))
            self.internal_id(label, str(row["id"]))  # a created node takes the next id
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
            self._internal_ids.pop((label, node_id), None)  # the id is gone, never reused
            for key in [
                key
                for key in self.edges
                if (key[1], key[2]) == (label, node_id) or (key[3], key[4]) == (label, node_id)
            ]:
                del self.edges[key]  # DETACH DELETE
                self._edge_internal_ids.pop(key, None)

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
            self.edge_internal_id(self._edge_key(names, row))
            properties = row["properties"]
            assert isinstance(properties, dict)
            edge.update(properties)  # pyright: ignore[reportUnknownArgumentType]

    def _apply_delete_edge(self, names: dict[str, str], rows: list[dict[str, object]]) -> None:
        for row in rows:
            self.edges.pop(self._edge_key(names, row), None)
            self._edge_internal_ids.pop(self._edge_key(names, row), None)


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
