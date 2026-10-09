"""Tests for `ps_service.change_monitor.trigger`.

Increment 9 (§3 tests 13, 14, 19): regulation + directive succession happy
path -- exercised against the real `ps_service.ingestion.pipeline.
ingest_regulatory_instrument` entry point (not a spy: the already-injected
`FakeAdapter`/`FakeGraph` doubles are its real boundaries), asserted via the
resulting write order and exactly one `link_superseded_by` log entry
carrying the re-ingest's `run_id`.

Increment 10a (§3 test 15): the `national_transposition` guard (AC-010) --
both limbs raise `NationalTranspositionNotSupportedError` before any write.
The guard runs on the `fresh` path only; `resume` and `already_processed`
are covered offline (no adapter call at all).

Increment 10b (§3 tests 16, 17, 18): atomicity + idempotency + crash
recovery (AC-007/008) -- a failed re-ingest writes nothing, an
`already_processed` re-trigger is a no-op with no log entry, and a crash
between ingest and succession is resumable.
"""

from __future__ import annotations

from datetime import date

import pytest

from change_monitor._fakes import (
    FakeAdapter,
    FakeGraph,
    FakeQueryResult,
    MakeEmitter,
    ReadLines,
)
from ps_service.change_monitor.errors import (
    ChangeMonitorStateError,
    NationalTranspositionNotSupportedError,
)
from ps_service.change_monitor.trigger import trigger_reingestion, will_reingest
from ps_service.ingestion.adapters.errors import CellarFetchError
from ps_service.ingestion.models import (
    FetchedRegulatoryInstrumentStructure,
    InstrumentType,
    ReachabilityCount,
    RegulatoryInstrumentMetadata,
)

_IDENTIFIER = "32024R2847"
_PRIOR_ID = "CRA-1.0"
_NEW_ID = "CRA-2.0"

# `verify_structural_graph_reachable`'s label set (RegulatoryInstrument plus
# `graph_writer._KNOWN_ELEMENT_TYPES`) -- used to build the expected
# `ingest_counts` dict the real pipeline produces against the canned
# `_ingest_completion_results` below.
_REACHABILITY_LABELS = (
    "RegulatoryInstrument",
    "TITLE",
    "CHAPTER",
    "SECTION",
    "ARTICLE",
    "PARAGRAPH",
    "ANNEX",
    "RECITAL",
)


class _FailingIngestFetchAdapter:
    """`MetadataFetchingAdapter` stand-in whose guard fetch succeeds and ingest fetch fails.

    Drives AC-007's "a failed re-ingest writes nothing":
    `fetch_regulatory_instrument_metadata` (the guard's metadata-only fetch)
    returns the canned structure's metadata, so the guard passes;
    `fetch_regulatory_instrument_structure` (the real ingest's own first
    line) raises, so the failure is distinct from the guard failing.
    """

    def __init__(self, structure: FetchedRegulatoryInstrumentStructure, error: Exception) -> None:
        self._structure = structure
        self._error = error
        self.structure_calls: list[str] = []
        self.metadata_calls: list[str] = []

    def fetch_regulatory_instrument_metadata(self, identifier: str) -> RegulatoryInstrumentMetadata:
        self.metadata_calls.append(identifier)
        return self._structure.metadata

    def fetch_regulatory_instrument_structure(
        self, identifier: str
    ) -> FetchedRegulatoryInstrumentStructure:
        self.structure_calls.append(identifier)
        raise self._error


def _structure(
    instrument_type: InstrumentType = "regulation",
) -> FetchedRegulatoryInstrumentStructure:
    """A minimal canned structure for the given `instrument_type`.

    Empty `nodes`/`edges` means the real `persist_native_structural_graph`
    issues zero additional `graph.query` calls, keeping
    `_ingest_completion_results` a fixed, small, order-independent list.
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


def _ingest_completion_results() -> list[FakeQueryResult]:
    """Canned results for the real `ingest_regulatory_instrument`'s own graph traffic.

    Appended after `_preflight`'s 3 scripted rows so the `fresh` path can
    exercise the real pipeline end to end against a `FakeGraph`:
    `register_regulatory_instrument_version` issues 1 write whose result is
    never read (any `FakeQueryResult` satisfies it);
    `persist_native_structural_graph` issues 0 further queries against
    `_structure()`'s empty `nodes=()/edges=()`;
    `verify_structural_graph_reachable` issues 1 `_count_nodes` read for the
    `RegulatoryInstrument` label (total=1, no orphan check for that label)
    plus a `_count_nodes` (total=1) + `_count_orphans` (0) pair for each of
    the other 7 `_REACHABILITY_LABELS` (15 reads total). Every label's pair
    is scripted identically so the resulting `IngestResult.counts` dict
    comes out the same regardless of `_KNOWN_ELEMENT_TYPES` (a frozenset)'s
    iteration order.
    """
    unread_register_result = FakeQueryResult([])
    regulatory_instrument_total = FakeQueryResult([[1]])
    element_type_pairs = [
        result for _ in range(7) for result in (FakeQueryResult([[1]]), FakeQueryResult([[0]]))
    ]
    return [unread_register_result, regulatory_instrument_total, *element_type_pairs]


def _fresh_graph(prior_instrument_type: str) -> FakeGraph:
    """A `FakeGraph` primed for the `fresh` path: no completed edge, no new node, one prior.

    Also carries `_ingest_completion_results` queued behind the 3 preflight
    rows, for the tests where the guard passes and the real re-ingest runs;
    left unconsumed (harmless) on the tests where a guard limb or the
    adapter double raises first.
    """
    return FakeGraph(
        [
            FakeQueryResult([]),  # is_succession_complete -> None
            FakeQueryResult([]),  # new_node_exists -> None
            FakeQueryResult([[_PRIOR_ID, prior_instrument_type]]),  # find_prior_instrument
            *_ingest_completion_results(),
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
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    graph = _fresh_graph(prior_instrument_type)
    adapter = FakeAdapter({_IDENTIFIER: _structure()})

    outcome = trigger_reingestion(
        _IDENTIFIER, "CRA", "2.0", adapter=adapter, graph=graph, emitter=emitter
    )
    emitter.flush()

    assert outcome.outcome == "superseded"
    assert isinstance(outcome.run_id, str) and outcome.run_id  # real ingest mints a fresh run_id
    assert outcome.ingest_counts == {
        label: ReachabilityCount(total=1, reachable=1) for label in _REACHABILITY_LABELS
    }
    assert outcome.prior_regulatory_instrument_id == _PRIOR_ID
    assert outcome.new_regulatory_instrument_id == _NEW_ID


def test_fresh_guard_uses_metadata_fetch_not_structure_fetch(make_emitter: MakeEmitter) -> None:
    """AC-BI-004/011: the guard fetches metadata only; just the ingest fetches structure."""
    emitter, _ = make_emitter()
    adapter = FakeAdapter({_IDENTIFIER: _structure("regulation")})

    outcome = trigger_reingestion(
        _IDENTIFIER,
        "CRA",
        "2.0",
        adapter=adapter,
        graph=_fresh_graph("regulation"),
        emitter=emitter,
    )
    emitter.flush()

    assert outcome.outcome == "superseded"
    assert adapter.metadata_calls == [_IDENTIFIER]  # the guard
    assert adapter.structure_calls == [_IDENTIFIER]  # the ingest's own fetch only


@pytest.mark.parametrize("prior_instrument_type", ["regulation", "directive"])
def test_fresh_path_call_order(
    prior_instrument_type: str,
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    graph = _fresh_graph(prior_instrument_type)

    trigger_reingestion(
        _IDENTIFIER,
        "CRA",
        "2.0",
        adapter=FakeAdapter({_IDENTIFIER: _structure()}),
        graph=graph,
        emitter=emitter,
    )

    # `graph.writes` filters to only the write-clause queries, so this
    # proves the real ingest's own MERGE lands first, followed by this
    # component's two succession-bookkeeping writes -- without needing to
    # know how many read-only reachability queries ran in between.
    writes = [call.query for call in graph.writes]
    assert len(writes) == 3
    assert "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties" in writes[0]
    assert "SET n.version = $new_version" in writes[1]
    assert "MERGE (prior)-[:SUPERSEDED_BY]->(new)" in writes[2]
    assert graph.writes[1].params == {"new_id": _NEW_ID, "new_version": "2.0"}
    assert graph.writes[2].params == {"prior_id": _PRIOR_ID, "new_id": _NEW_ID}


def test_fresh_path_emits_exactly_one_link_superseded_by_entry(
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    emitter, log_path = make_emitter()

    outcome = trigger_reingestion(
        _IDENTIFIER,
        "CRA",
        "2.0",
        adapter=FakeAdapter({_IDENTIFIER: _structure()}),
        graph=_fresh_graph("regulation"),
        emitter=emitter,
    )
    emitter.flush()

    # the real ingest emits its own 4 stage-completion entries (component
    # "ingestion"); this component's own entry is the *one*
    # `link_superseded_by` among them, carrying the re-ingest's `run_id`.
    lines = read_lines(log_path)
    assert len(lines) == 5
    link_entries = [line for line in lines if line["action"] == "link_superseded_by"]
    assert len(link_entries) == 1
    entry = link_entries[0]
    assert entry["component"] == "change_monitor"
    assert entry["entity_id"] == [_PRIOR_ID, _NEW_ID]
    assert entry["outcome"] == "superseded"
    assert entry["run_id"] == outcome.run_id


# --- Increment 10a: national_transposition guard (§3 test 15, AC-010) ---


def test_national_transposition_prior_node_rejected(
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """Limb 1: a `national_transposition` prior node is rejected before any write."""
    emitter, log_path = make_emitter()
    graph = _fresh_graph("national_transposition")
    adapter = FakeAdapter({_IDENTIFIER: _structure()})

    with pytest.raises(NationalTranspositionNotSupportedError) as excinfo:
        trigger_reingestion(
            _IDENTIFIER, "CRA", "2.0", adapter=adapter, graph=graph, emitter=emitter
        )
    emitter.flush()

    assert "#41" in str(excinfo.value)
    assert "#46" in str(excinfo.value)
    # limb 1 fires before the guard's metadata fetch
    assert adapter.calls == []
    assert adapter.metadata_calls == []
    assert adapter.structure_calls == []
    assert graph.writes == []
    assert read_lines(log_path) == []


def test_national_transposition_fetched_metadata_rejected(
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """Limb 2: metadata (metadata-only guard fetch) typed `national_transposition` is rejected."""
    emitter, log_path = make_emitter()
    graph = _fresh_graph("regulation")  # prior looks fine -> limb 1 passes
    adapter = FakeAdapter({_IDENTIFIER: _structure("national_transposition")})

    with pytest.raises(NationalTranspositionNotSupportedError) as excinfo:
        trigger_reingestion(
            _IDENTIFIER, "CRA", "2.0", adapter=adapter, graph=graph, emitter=emitter
        )
    emitter.flush()

    assert "#41" in str(excinfo.value)
    assert "#46" in str(excinfo.value)
    assert adapter.metadata_calls == [_IDENTIFIER]  # the guard's metadata fetch
    assert adapter.structure_calls == []  # ingest never reached
    assert graph.writes == []
    assert read_lines(log_path) == []


# --- Increment 10b: atomicity + idempotency + crash recovery (§3 tests 16-18) ---


def test_reingest_failure_writes_nothing(
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """§3 test 16 (AC-007): a failing re-ingest propagates; no bookkeeping write happens."""
    emitter, log_path = make_emitter()
    graph = _fresh_graph("regulation")
    adapter = _FailingIngestFetchAdapter(_structure(), CellarFetchError("CELLAR unreachable"))

    with pytest.raises(CellarFetchError):
        trigger_reingestion(
            _IDENTIFIER, "CRA", "2.0", adapter=adapter, graph=graph, emitter=emitter
        )
    emitter.flush()

    # the guard's metadata fetch succeeds (limb 2 passes); the real ingest's
    # own structure fetch (its first line) then fails
    assert adapter.metadata_calls == [_IDENTIFIER]
    assert adapter.structure_calls == [_IDENTIFIER]
    assert graph.writes == []  # no SET n.version, no SUPERSEDED_BY -- prior stays active
    assert read_lines(log_path) == []


def test_idempotent_retrigger_is_noop(
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """§3 test 17 (AC-008): `already_processed` -- no ingest, no write, no log entry."""
    emitter, log_path = make_emitter()
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
    # characterization (AC-BI-007): the guard is not reached on the no-op path
    assert adapter.calls == []
    assert adapter.metadata_calls == []
    assert adapter.structure_calls == []
    assert graph.writes == []
    assert len(graph.calls) == 1  # only the completed-succession probe
    assert read_lines(log_path) == []


def test_crash_between_ingest_and_succession_is_resumable(
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """§3 test 18 (AC-007/008, flaw 2): a crashed `fresh` run completes on the next call."""
    emitter, log_path = make_emitter()
    adapter = FakeAdapter({_IDENTIFIER: _structure()})
    graph = _resume_graph()

    outcome = trigger_reingestion(
        _IDENTIFIER, "CRA", "2.0", adapter=adapter, graph=graph, emitter=emitter
    )
    emitter.flush()

    assert outcome.outcome == "superseded"
    assert outcome.run_id is None  # no IngestResult on the resume path
    assert outcome.ingest_counts is None
    # `resume` neither re-ingests nor consults the guard: no adapter fetch at all
    assert adapter.calls == []
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
    assert adapter.calls == []  # still no fetch of either kind across both calls
    assert adapter.metadata_calls == []
    assert adapter.structure_calls == []


def test_resume_does_not_consult_guard_even_when_prior_is_national_transposition(
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    """AC-BI-006/010: `resume` skips the guard, even for a `national_transposition` prior."""
    emitter, log_path = make_emitter()
    adapter = FakeAdapter({_IDENTIFIER: _structure()})
    graph = _resume_graph("national_transposition")

    outcome = trigger_reingestion(
        _IDENTIFIER, "CRA", "2.0", adapter=adapter, graph=graph, emitter=emitter
    )
    emitter.flush()

    assert outcome.outcome == "superseded"
    assert outcome.run_id is None
    assert adapter.calls == []
    assert adapter.metadata_calls == []
    assert adapter.structure_calls == []
    assert len(graph.writes) == 1
    assert "MERGE (prior)-[:SUPERSEDED_BY]->(new)" in graph.writes[0].query
    lines = read_lines(log_path)
    assert len(lines) == 1
    assert lines[0]["action"] == "link_superseded_by"


# --- Issue #195: run id pass-through and the read-only `will_reingest` probe ---


def test_trigger_reingestion_passes_the_given_run_id_to_the_ingest(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()

    outcome = trigger_reingestion(
        _IDENTIFIER,
        "CRA",
        "2.0",
        adapter=FakeAdapter({_IDENTIFIER: _structure()}),
        graph=_fresh_graph("regulation"),
        emitter=emitter,
        run_id="caller-minted-run-id",
    )

    assert outcome.run_id == "caller-minted-run-id"


def test_trigger_reingestion_without_run_id_keeps_minting_its_own(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    adapter = FakeAdapter({_IDENTIFIER: _structure()})

    first = trigger_reingestion(
        _IDENTIFIER,
        "CRA",
        "2.0",
        adapter=adapter,
        graph=_fresh_graph("regulation"),
        emitter=emitter,
    )
    second = trigger_reingestion(
        _IDENTIFIER,
        "CRA",
        "2.0",
        adapter=adapter,
        graph=_fresh_graph("regulation"),
        emitter=emitter,
    )

    assert first.run_id and second.run_id and first.run_id != second.run_id


@pytest.mark.parametrize(
    ("graph", "expected"),
    [
        (_fresh_graph("regulation"), True),
        (_resume_graph(), False),
        (_already_processed_graph(), False),
    ],
    ids=["fresh", "resume", "already_processed"],
)
def test_will_reingest_is_true_only_for_the_fresh_state_and_writes_nothing(
    graph: FakeGraph, *, expected: bool
) -> None:
    assert will_reingest(graph, "CRA", "2.0") is expected
    assert graph.writes == []


def test_will_reingest_raises_when_the_graph_has_no_single_active_prior() -> None:
    with pytest.raises(ChangeMonitorStateError):
        will_reingest(
            FakeGraph([FakeQueryResult([]), FakeQueryResult([]), FakeQueryResult([])]), "CRA", "2.0"
        )
