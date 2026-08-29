"""Shared test doubles for the `ps_service.change_monitor` test package.

`tests/change_monitor/` is an importable package (it has an `__init__.py`),
so its per-file test modules share these hand-written doubles from here
instead of redeclaring them, mirroring `tests/company_merge/_fakes.py`.

`FakeGraph` / `FakeQueryResult` satisfy `ps_service.change_monitor.
falkordb_client.GraphHandle` / `GraphQueryResult` structurally: a test
scripts the rows each `query()` call returns and asserts the exact Cypher +
params afterwards, including "this component issued no write" (see
`FakeGraph.writes`).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import redis.exceptions

if TYPE_CHECKING:
    from pathlib import Path

    from ps_service.ingestion.models import FetchedRegulatoryInstrumentStructure
    from ps_service.logging import LogEmitter
    from ps_service.logging.emitter import TextSink

_WRITE_CLAUSES = ("MERGE", "CREATE", "SET ", "DELETE", "REMOVE")


class MakeEmitter(Protocol):
    """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

    def __call__(
        self, *, filename: str = ..., fallback: TextSink | None = ...
    ) -> tuple[LogEmitter, Path]: ...


class ReadLines(Protocol):
    """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

    def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


@dataclass(frozen=True, slots=True)
class RecordedQuery:
    """One `(query, params)` pair a `FakeGraph` was called with."""

    query: str
    params: dict[str, object] | None


class FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally: one scripted row list.

    Each row is itself a list of column values, in the query's `RETURN`
    order -- the shape a real `falkordb.QueryResult.result_set` has.
    """

    def __init__(self, rows: list[list[object]]) -> None:
        """Script the rows this result yields from its `result_set`."""
        self._rows = rows

    @property
    def result_set(self) -> list[object]:
        """The scripted rows, one list of column values per row."""
        return list(self._rows)


class FakeGraph:
    """Satisfies `GraphHandle` structurally, recording every `query()` call.

    `results` is consumed in order, one `FakeQueryResult` per `query()`
    call; once exhausted every further call yields an empty result. Every
    call is appended to `calls`, so a test can assert the exact Cypher and
    params, and `writes` lets it assert this component issued no write at
    all (the poll is read-only).
    """

    def __init__(self, results: list[FakeQueryResult] | None = None) -> None:
        """Prime the scripted results (default: always an empty result)."""
        self.calls: list[RecordedQuery] = []
        self._results: deque[FakeQueryResult] = deque(results or [])

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        """Record `(q, params)` and return the next scripted result."""
        self.calls.append(RecordedQuery(q, params))
        if self._results:
            return self._results.popleft()
        return FakeQueryResult([])

    @property
    def writes(self) -> list[RecordedQuery]:
        """Recorded calls whose Cypher contains a write clause."""
        return [call for call in self.calls if _is_write(call.query)]


def _is_write(query: str) -> bool:
    """Whether `query`'s Cypher contains a node/edge/property write clause."""
    upper = query.upper()
    return any(clause in upper for clause in _WRITE_CLAUSES)


class RaisingGraph:
    """Satisfies `GraphHandle`; every `query()` raises `redis.exceptions.RedisError`.

    The exact exception shape a real unreachable/failing FalkorDB instance
    raises mid-call -- drives `succession._execute_query`'s
    `SuccessionPersistenceError` + `mark_unhealthy` path.
    """

    def __init__(self, error: redis.exceptions.RedisError | None = None) -> None:
        """Prime the error each `query()` call raises (default: a connection error)."""
        self._error: redis.exceptions.RedisError = error or redis.exceptions.ConnectionError(
            "Error 111 connecting to FalkorDB"
        )
        self.calls: list[RecordedQuery] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        """Record `(q, params)` and raise the primed `RedisError`."""
        self.calls.append(RecordedQuery(q, params))
        raise self._error


class FakeAdapter:
    """Satisfies `ps_service.ingestion.adapters.base.IngestionAdapter` structurally.

    One canned `FetchedRegulatoryInstrumentStructure` per identifier;
    records every `fetch_regulatory_instrument_structure` call. In the
    Increment 9 trigger tests `ingest_regulatory_instrument` is replaced by
    a spy, so this adapter is only ever passed through, never invoked --
    Increment 10a's `national_transposition` guard is its first real caller.
    """

    def __init__(
        self, structures_by_identifier: dict[str, FetchedRegulatoryInstrumentStructure]
    ) -> None:
        """Prime the canned structures keyed by identifier."""
        self._structures_by_identifier = structures_by_identifier
        self.calls: list[str] = []

    def fetch_regulatory_instrument_structure(
        self, identifier: str
    ) -> FetchedRegulatoryInstrumentStructure:
        """Record `identifier` and return its canned structure."""
        self.calls.append(identifier)
        return self._structures_by_identifier[identifier]
