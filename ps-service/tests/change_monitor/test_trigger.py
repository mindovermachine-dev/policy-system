"""Tests for `ps_service.change_monitor.trigger`.

Increment 9 (§3 tests 13, 14, 19): regulation + directive succession happy
path via a spy on `ingest_regulatory_instrument`, asserted call order,
exactly one `link_superseded_by` log entry carrying the re-ingest's `run_id`.

Increment 10a (§3 test 15): the `national_transposition` guard (AC-010) --
both limbs raise `NationalTranspositionNotSupportedError` before any write.

Increment 10b (§3 tests 16, 17, 18): atomicity + idempotency + crash
recovery (AC-007/008) -- a failed re-ingest writes nothing, an
`already_processed` re-trigger is a no-op with no log entry, and a crash
between ingest and succession is resumable.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from change_monitor._fakes import (
    FakeAdapter,
    FakeGraph,
    FakeQueryResult,
    MakeEmitter,
    ReadLines,
)
from ps_service.change_monitor import trigger as trigger_module
from ps_service.change_monitor.errors import NationalTranspositionNotSupportedError
from ps_service.change_monitor.trigger import trigger_reingestion
from ps_service.ingestion.adapters.errors import CellarFetchError
from ps_service.ingestion.models import (
    FetchedRegulatoryInstrumentStructure,
    IngestResult,
    InstrumentType,
    ReachabilityCount,
    RegulatoryInstrumentMetadata,
)

if TYPE_CHECKING:
    from ps_service.ingestion.adapters.base import IngestionAdapter
    from ps_service.ingestion.falkordb_client import GraphHandle
    from ps_service.logging import LogEmitter

_IDENTIFIER = "32024R2847"
_PRIOR_ID = "CRA-1.0"
_NEW_ID = "CRA-2.0"
_RUN_ID = "run-abc-123"


class _IngestSpy:
    """Stand-in for `ingest_regulatory_instrument`: records the call, returns a canned result.

    Also captures `len(graph.calls)` at invocation time, so a test can prove
    the re-ingest ran *after* `_preflight`'s reads and *before* any
    succession write.
    """

    def __init__(self, result: IngestResult) -> None:
        self._result = result
        self.calls: list[tuple[str, str, str, int]] = []

    def __call__(
        self,
        identifier: str,
        short_name: str,
        *,
        version: str,
        adapter: IngestionAdapter,
        graph: GraphHandle,
        emitter: LogEmitter | None = None,
    ) -> IngestResult:
        _ = (adapter, emitter)
        graph_calls_so_far = len(getattr(graph, "calls", []))
        self.calls.append((identifier, short_name, version, graph_calls_so_far))
        return self._result


class _RaisingIngest:
    """Stand-in for `ingest_regulatory_instrument` that always raises (AC-007).

    Records the call so a test can prove the re-ingest was reached and then
    failed -- and that nothing was written afterwards.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls: list[str] = []

    def __call__(
        self,
        identifier: str,
        short_name: str,
        *,
        version: str,
        adapter: IngestionAdapter,
        graph: GraphHandle,
        emitter: LogEmitter | None = None,
    ) -> IngestResult:
        _ = (short_name, version, adapter, graph, emitter)
        self.calls.append(identifier)
        raise self._error


def _structure(
    instrument_type: InstrumentType = "regulation",
) -> FetchedRegulatoryInstrumentStructure:
    """A minimal canned structure for the given `instrument_type`.

    On the `fresh` / `resume` paths the AC-010 guard fetches this once; the
    ingest spy means the pipeline's own fetch never runs.
    """
    metadata = RegulatoryInstrumentMetadata(
        title="Fixture",
        jurisdiction="EU",
        effective_date=date(2027, 12, 11),
        version="1.0",
        status="active",
        source_type="external",
        instrument_type=instrument_type,
    )
    return FetchedRegulatoryInstrumentStructure(metadata=metadata, nodes=(), edges=())


def _fresh_graph(prior_instrument_type: str) -> FakeGraph:
    """A `FakeGraph` primed for the `fresh` path: no completed edge, no new node, one prior."""
    return FakeGraph(
        [
            FakeQueryResult([]),  # is_succession_complete -> None
            FakeQueryResult([]),  # new_node_exists -> None
            FakeQueryResult([[_PRIOR_ID, prior_instrument_type]]),  # find_prior_instrument
        ]
    )


def _resume_graph(prior_instrument_type: str = "regulation") -> FakeGraph:
    """A `FakeGraph` modelling a crash: new node exists + active, prior still active, no edge."""
    return FakeGraph(
        [
            FakeQueryResult([]),  # is_succession_complete -> None
            FakeQueryResult([["active"]]),  # new_node_exists -> "active"
            FakeQueryResult([[_PRIOR_ID, prior_instrument_type]]),  # find_prior_instrument
        ]
    )


def _already_processed_graph() -> FakeGraph:
    """A `FakeGraph` where succession into the new node is already complete."""
    return FakeGraph([FakeQueryResult([[_PRIOR_ID]])])  # is_succession_complete -> prior id


@pytest.mark.parametrize("prior_instrument_type", ["regulation", "directive"])
def test_fresh_succession_happy_path(
    prior_instrument_type: str,
    monkeypatch: pytest.MonkeyPatch,
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    result = IngestResult(
        regulatory_instrument_id=_NEW_ID,
        run_id=_RUN_ID,
        counts={"ARTICLE": ReachabilityCount(total=1, reachable=1)},
    )
    spy = _IngestSpy(result)
    monkeypatch.setattr(trigger_module, "ingest_regulatory_instrument", spy)
    graph = _fresh_graph(prior_instrument_type)
    adapter = FakeAdapter({_IDENTIFIER: _structure()})

    outcome = trigger_reingestion(
        _IDENTIFIER, "CRA", "2.0", adapter=adapter, graph=graph, emitter=emitter
    )
    emitter.flush()

    assert outcome.outcome == "superseded"
    assert outcome.run_id == _RUN_ID
    assert outcome.ingest_counts == result.counts
    assert outcome.prior_regulatory_instrument_id == _PRIOR_ID
    assert outcome.new_regulatory_instrument_id == _NEW_ID


@pytest.mark.parametrize("prior_instrument_type", ["regulation", "directive"])
def test_fresh_path_call_order(
    prior_instrument_type: str,
    monkeypatch: pytest.MonkeyPatch,
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    spy = _IngestSpy(IngestResult(regulatory_instrument_id=_NEW_ID, run_id=_RUN_ID, counts={}))
    monkeypatch.setattr(trigger_module, "ingest_regulatory_instrument", spy)
    graph = _fresh_graph(prior_instrument_type)

    trigger_reingestion(
        _IDENTIFIER,
        "CRA",
        "2.0",
        adapter=FakeAdapter({_IDENTIFIER: _structure()}),
        graph=graph,
        emitter=emitter,
    )

    # re-ingest ran after the 3 preflight reads, before any write
    assert spy.calls == [(_IDENTIFIER, "CRA", "2.0", 3)]
    queries = [call.query for call in graph.calls]
    assert len(queries) == 5
    assert "SET n.version = $new_version" in queries[3]
    assert "MERGE (prior)-[:SUPERSEDED_BY]->(new)" in queries[4]
    assert graph.calls[3].params == {"new_id": _NEW_ID, "new_version": "2.0"}
    assert graph.calls[4].params == {"prior_id": _PRIOR_ID, "new_id": _NEW_ID}


def test_fresh_path_emits_exactly_one_link_superseded_by_entry(
    monkeypatch: pytest.MonkeyPatch,
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    emitter, log_path = make_emitter()
    spy = _IngestSpy(IngestResult(regulatory_instrument_id=_NEW_ID, run_id=_RUN_ID, counts={}))
    monkeypatch.setattr(trigger_module, "ingest_regulatory_instrument", spy)

    trigger_reingestion(
        _IDENTIFIER,
        "CRA",
        "2.0",
        adapter=FakeAdapter({_IDENTIFIER: _structure()}),
        graph=_fresh_graph("regulation"),
        emitter=emitter,
    )
    emitter.flush()

    # the spy emits nothing, so the trigger's one entry is the whole log
    lines = read_lines(log_path)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["component"] == "change_monitor"
    assert entry["action"] == "link_superseded_by"
    assert entry["entity_id"] == [_PRIOR_ID, _NEW_ID]
    assert entry["outcome"] == "superseded"
    assert entry["run_id"] == _RUN_ID


# --- Increment 10a: national_transposition guard (§3 test 15, AC-010) ---


def test_national_transposition_prior_node_rejected(
    monkeypatch: pytest.MonkeyPatch,
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """Limb 1: a `national_transposition` prior node is rejected before any write."""
    emitter, log_path = make_emitter()
    spy = _IngestSpy(IngestResult(regulatory_instrument_id=_NEW_ID, run_id=_RUN_ID, counts={}))
    monkeypatch.setattr(trigger_module, "ingest_regulatory_instrument", spy)
    graph = _fresh_graph("national_transposition")
    adapter = FakeAdapter({_IDENTIFIER: _structure()})

    with pytest.raises(NationalTranspositionNotSupportedError) as excinfo:
        trigger_reingestion(
            _IDENTIFIER, "CRA", "2.0", adapter=adapter, graph=graph, emitter=emitter
        )
    emitter.flush()

    assert "#41" in str(excinfo.value)
    assert "#46" in str(excinfo.value)
    assert spy.calls == []
    assert adapter.calls == []  # limb 1 fires before the belt-and-braces fetch
    assert graph.writes == []
    assert read_lines(log_path) == []


def test_national_transposition_fetched_metadata_rejected(
    monkeypatch: pytest.MonkeyPatch,
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """Limb 2 (belt-and-braces): fetched metadata typed `national_transposition` is rejected."""
    emitter, log_path = make_emitter()
    spy = _IngestSpy(IngestResult(regulatory_instrument_id=_NEW_ID, run_id=_RUN_ID, counts={}))
    monkeypatch.setattr(trigger_module, "ingest_regulatory_instrument", spy)
    graph = _fresh_graph("regulation")  # prior looks fine -> limb 1 passes
    adapter = FakeAdapter({_IDENTIFIER: _structure("national_transposition")})

    with pytest.raises(NationalTranspositionNotSupportedError) as excinfo:
        trigger_reingestion(
            _IDENTIFIER, "CRA", "2.0", adapter=adapter, graph=graph, emitter=emitter
        )
    emitter.flush()

    assert "#41" in str(excinfo.value)
    assert "#46" in str(excinfo.value)
    assert adapter.calls == [_IDENTIFIER]  # the single guard fetch
    assert spy.calls == []
    assert graph.writes == []
    assert read_lines(log_path) == []


# --- Increment 10b: atomicity + idempotency + crash recovery (§3 tests 16-18) ---


def test_reingest_failure_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """§3 test 16 (AC-007): a failing re-ingest propagates; no bookkeeping write happens."""
    emitter, log_path = make_emitter()
    raising = _RaisingIngest(CellarFetchError("CELLAR unreachable"))
    monkeypatch.setattr(trigger_module, "ingest_regulatory_instrument", raising)
    graph = _fresh_graph("regulation")
    adapter = FakeAdapter({_IDENTIFIER: _structure()})

    with pytest.raises(CellarFetchError):
        trigger_reingestion(
            _IDENTIFIER, "CRA", "2.0", adapter=adapter, graph=graph, emitter=emitter
        )
    emitter.flush()

    assert raising.calls == [_IDENTIFIER]  # the re-ingest was reached, then failed
    assert graph.writes == []  # no SET n.version, no SUPERSEDED_BY -- prior stays active
    assert read_lines(log_path) == []


def test_idempotent_retrigger_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """§3 test 17 (AC-008): `already_processed` -- no ingest, no write, no log entry."""
    emitter, log_path = make_emitter()
    spy = _IngestSpy(IngestResult(regulatory_instrument_id=_NEW_ID, run_id=_RUN_ID, counts={}))
    monkeypatch.setattr(trigger_module, "ingest_regulatory_instrument", spy)
    graph = _already_processed_graph()
    adapter = FakeAdapter({_IDENTIFIER: _structure()})

    outcome = trigger_reingestion(
        _IDENTIFIER, "CRA", "2.0", adapter=adapter, graph=graph, emitter=emitter
    )
    emitter.flush()

    assert outcome.outcome == "already_processed"
    assert outcome.run_id is None
    assert outcome.ingest_counts is None
    assert outcome.prior_regulatory_instrument_id == _PRIOR_ID
    assert outcome.new_regulatory_instrument_id == _NEW_ID
    assert spy.calls == []
    assert adapter.calls == []  # guard is not reached on the no-op path
    assert graph.writes == []
    assert len(graph.calls) == 1  # only the completed-succession probe
    assert read_lines(log_path) == []


def test_crash_between_ingest_and_succession_is_resumable(
    monkeypatch: pytest.MonkeyPatch,
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """§3 test 18 (AC-007/008, flaw 2): a crashed `fresh` run completes on the next call."""
    emitter, log_path = make_emitter()
    spy = _IngestSpy(IngestResult(regulatory_instrument_id=_NEW_ID, run_id=_RUN_ID, counts={}))
    monkeypatch.setattr(trigger_module, "ingest_regulatory_instrument", spy)
    adapter = FakeAdapter({_IDENTIFIER: _structure()})
    graph = _resume_graph()

    outcome = trigger_reingestion(
        _IDENTIFIER, "CRA", "2.0", adapter=adapter, graph=graph, emitter=emitter
    )
    emitter.flush()

    assert outcome.outcome == "superseded"
    assert outcome.run_id is None  # no IngestResult on the resume path
    assert outcome.ingest_counts is None
    assert spy.calls == []  # the node is already there -- no re-ingest
    assert len(graph.writes) == 1  # exactly the one fused MERGE...SET
    assert "MERGE (prior)-[:SUPERSEDED_BY]->(new)" in graph.writes[0].query
    assert graph.writes[0].params == {"prior_id": _PRIOR_ID, "new_id": _NEW_ID}

    lines = read_lines(log_path)
    assert len(lines) == 1
    assert lines[0]["action"] == "link_superseded_by"
    assert lines[0]["entity_id"] == [_PRIOR_ID, _NEW_ID]
    assert lines[0]["outcome"] == "superseded"
    assert "run_id" not in lines[0]  # no bound run context on the resume path

    # A third call, with succession now complete, is a pure no-op.
    graph_complete = _already_processed_graph()
    outcome_again = trigger_reingestion(
        _IDENTIFIER, "CRA", "2.0", adapter=adapter, graph=graph_complete, emitter=emitter
    )
    assert outcome_again.outcome == "already_processed"
    assert graph_complete.writes == []
