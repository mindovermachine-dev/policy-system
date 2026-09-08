"""Shared test doubles for the ``ps_service.api`` test package.

``tests/api/`` is an importable package (it has an ``__init__.py``), so its
per-file test modules share these hand-written doubles from here instead of
redeclaring them — mirroring ``tests/company_merge/_fakes.py`` and
``tests/change_monitor/_fakes.py``.

The orchestration under test (`ps_service.api.ingestion_orchestration`) sequences
the four external-pipeline stages behind injected ``PipelineDependencies``. These
doubles let a test drive that sequence without a real graph, adapter, or LLM:

* :class:`FakeGraphHandle` / :class:`FakeQueryResult` satisfy the orchestration's
  local ``GraphHandle`` / ``_QueryResult`` Protocols structurally, recording every
  ``query()`` call.
* :class:`StageRecorder` plus the four ``Fake*Stage`` classes record call order and
  the ``regulatory_instrument_id`` each downstream stage consumed, and can be told
  to raise instead of returning.
* :func:`build_fake_pipeline_dependencies` assembles a ready-to-inject
  ``PipelineDependencies`` around a single shared recorder.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ps_service.api.change_check_orchestration import ChangeCheckDependencies
from ps_service.api.ingestion_orchestration import (
    GraphOpeners,
    PipelineAdapters,
    PipelineDependencies,
    PipelineStages,
)
from ps_service.change_monitor.models import PollReport
from ps_service.company_merge.models import MergeResult
from ps_service.domain_mapper.models import (
    DerivationResult,
    ExtractionResult,
    GovernanceDerivationResult,
)
from ps_service.ingestion.adapters.internal_seed.persist import InternalIngestResult
from ps_service.ingestion.models import IngestResult

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from ps_service.api.catalog import CatalogEntry
    from ps_service.api.change_check_orchestration import TriggerReingestionCall
    from ps_service.api.ingestion_orchestration import GraphHandle
    from ps_service.change_monitor.models import ReingestionOutcome, TrackedInstrumentNode
    from ps_service.config import ServiceConfig
    from ps_service.domain_mapper.models import ExtractionUnit
    from ps_service.ingestion.adapters.base import IngestionAdapter
    from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed
    from ps_service.ingestion.models import FetchedRegulatoryInstrumentStructure
    from ps_service.llm_interface.client import CompletionCaller, EmbeddingCaller
    from ps_service.logging import LogEmitter
    from ps_service.logging.emitter import TextSink


class MakeEmitter(Protocol):
    """Call shape of the shared ``make_emitter`` fixture (``tests/conftest.py``)."""

    def __call__(
        self, *, filename: str = ..., fallback: TextSink | None = ...
    ) -> tuple[LogEmitter, Path]: ...


class ReadLines(Protocol):
    """Call shape of the shared ``read_lines`` fixture (``tests/conftest.py``)."""

    def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


@dataclass(frozen=True, slots=True)
class RecordedQuery:
    """One ``(query, params)`` pair a :class:`FakeGraphHandle` was called with."""

    query: str
    params: dict[str, object] | None


class FakeQueryResult:
    """Satisfies the orchestration's ``_QueryResult`` Protocol: one scripted row list."""

    def __init__(self, rows: list[object] | None = None) -> None:
        """Script the rows this result yields from ``result_set``."""
        self._rows: list[object] = list(rows) if rows else []

    @property
    def result_set(self) -> list[object]:
        """The scripted rows."""
        return list(self._rows)


class FakeGraphHandle:
    """Satisfies the orchestration's ``GraphHandle`` Protocol, recording every ``query()`` call.

    ``results`` is consumed in order, one :class:`FakeQueryResult` per ``query()``
    call; once exhausted every further call yields an empty result.
    """

    def __init__(self, results: list[FakeQueryResult] | None = None) -> None:
        """Prime the scripted results (default: always an empty result)."""
        self.calls: list[RecordedQuery] = []
        self._results: deque[FakeQueryResult] = deque(results or [])

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        """Record ``(q, params)`` and return the next scripted result."""
        self.calls.append(RecordedQuery(q, params))
        return self._results.popleft() if self._results else FakeQueryResult([])


@dataclass(frozen=True, slots=True)
class StageCall:
    """One recorded pipeline-stage invocation."""

    stage: str
    regulatory_instrument_id: str | None
    kwargs: dict[str, object]


class StageRecorder:
    """Records the ordered sequence of pipeline-stage calls the orchestration made."""

    def __init__(self) -> None:
        """Start with an empty call log."""
        self.calls: list[StageCall] = []

    @property
    def order(self) -> list[str]:
        """The stage names in call order."""
        return [call.stage for call in self.calls]


class FakeIngestStage:
    """Stand-in for ``ingest_regulatory_instrument`` — records the call, returns a canned result."""

    def __init__(
        self, recorder: StageRecorder, *, rid: str = "CRA-1.0", error: Exception | None = None
    ) -> None:
        """Prime the recorder, the id to return, and an optional error to raise."""
        self._recorder = recorder
        self._rid = rid
        self._error = error

    def __call__(
        self,
        identifier: str,
        short_name: str,
        *,
        version: str,
        adapter: object,
        graph: GraphHandle,
        run_id: str | None = None,
        emitter: LogEmitter | None = None,
    ) -> IngestResult:
        """Record the call and return (or raise) a canned :class:`IngestResult`."""
        _ = (graph, emitter)
        self._recorder.calls.append(
            StageCall(
                "ingestion",
                None,
                {
                    "identifier": identifier,
                    "short_name": short_name,
                    "version": version,
                    "run_id": run_id,
                    "adapter": adapter,
                },
            )
        )
        if self._error is not None:
            raise self._error
        return IngestResult(
            regulatory_instrument_id=self._rid, run_id=run_id or "fake-run", counts={}
        )


class FakeExtractStage:
    """Stand-in for ``extract_roles_and_requirements``."""

    def __init__(self, recorder: StageRecorder, *, error: Exception | None = None) -> None:
        """Prime the recorder and an optional error to raise."""
        self._recorder = recorder
        self._error = error

    def __call__(
        self,
        regulatory_instrument_id: str,
        *,
        adapter: object,
        native_graph: GraphHandle,
        baseline_graph: GraphHandle,
        model: str,
        call_completion: CompletionCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> ExtractionResult:
        """Record the call and return (or raise) a canned :class:`ExtractionResult`."""
        _ = (adapter, native_graph, baseline_graph, call_completion, emitter)
        self._recorder.calls.append(
            StageCall("extraction", regulatory_instrument_id, {"model": model})
        )
        if self._error is not None:
            raise self._error
        return ExtractionResult(
            regulatory_instrument_id=regulatory_instrument_id,
            role_node_ids={},
            requirement_ids=(),
            candidate_count=0,
            skipped_unit_count=0,
            requirement_id_collisions=(),
        )


class FakeDeriveStage:
    """Stand-in for ``derive_obligations_and_capabilities``."""

    def __init__(
        self,
        recorder: StageRecorder,
        *,
        error: Exception | None = None,
        unmatched_obligation_ids: tuple[str, ...] = (),
    ) -> None:
        """Prime the recorder, an optional error to raise, and the canned
        ``unmatched_obligation_ids`` (issue #64 slice 9 -- lets a test exercise
        the derivation stage's summary with a non-empty count without
        scripting a real capability-derivation failure).
        """
        self._recorder = recorder
        self._error = error
        self._unmatched_obligation_ids = unmatched_obligation_ids

    def __call__(
        self,
        regulatory_instrument_id: str,
        *,
        baseline_graph: GraphHandle,
        model: str,
        call_completion: CompletionCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> DerivationResult:
        """Record the call and return (or raise) a canned :class:`DerivationResult`."""
        _ = (baseline_graph, call_completion, emitter)
        self._recorder.calls.append(
            StageCall("derivation", regulatory_instrument_id, {"model": model})
        )
        if self._error is not None:
            raise self._error
        return DerivationResult(
            regulatory_instrument_id=regulatory_instrument_id,
            obligation_node_ids=(),
            capability_node_ids=(),
            unmatched_requirement_ids=(),
            unmatched_obligation_ids=self._unmatched_obligation_ids,
        )


class FakeMergeStage:
    """Stand-in for ``merge_baseline_graph``."""

    def __init__(self, recorder: StageRecorder, *, error: Exception | None = None) -> None:
        """Prime the recorder and an optional error to raise."""
        self._recorder = recorder
        self._error = error

    def __call__(
        self,
        regulatory_instrument_id: str,
        *,
        baseline_graph: GraphHandle,
        single_tenant_graph: GraphHandle,
        embed_model: str,
        similarity_threshold: float | None,
        call_embedding: EmbeddingCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> MergeResult:
        """Record the call and return (or raise) a canned :class:`MergeResult`."""
        _ = (baseline_graph, single_tenant_graph, embed_model, call_embedding, emitter)
        self._recorder.calls.append(
            StageCall(
                "merge",
                regulatory_instrument_id,
                {"embed_model": embed_model, "similarity_threshold": similarity_threshold},
            )
        )
        if self._error is not None:
            raise self._error
        return MergeResult(
            regulatory_instrument_id=regulatory_instrument_id,
            obligation_ids=(),
            capability_canonical_ids=(),
            near_misses=(),
        )


class FakeIngestInternalStage:
    """Stand-in for ``internal_seed.persist.ingest_internal_regulatory_instrument``."""

    def __init__(
        self,
        recorder: StageRecorder,
        *,
        rid: str = "ENGPRAC-3.0",
        error: Exception | None = None,
    ) -> None:
        """Prime the recorder, the id to return, and an optional error to raise."""
        self._recorder = recorder
        self._rid = rid
        self._error = error

    def __call__(
        self,
        seed: InternalRegulationSeed,
        *,
        baseline_graph: GraphHandle,
        native_graph: GraphHandle,
        emitter: LogEmitter | None = None,
    ) -> InternalIngestResult:
        """Record the call and return (or raise) a canned :class:`InternalIngestResult`."""
        _ = (seed, baseline_graph, native_graph, emitter)
        self._recorder.calls.append(StageCall("internal_ingestion", None, {}))
        if self._error is not None:
            raise self._error
        return InternalIngestResult(
            regulatory_instrument_id=self._rid,
            role_count=0,
            requirement_count=0,
            obligation_count=0,
            capability_count=0,
        )


class FakeDeriveGovernanceStage:
    """Stand-in for ``derive_governance_artifacts`` (issue #54, S3)."""

    def __init__(self, recorder: StageRecorder, *, error: Exception | None = None) -> None:
        """Prime the recorder and an optional error to raise."""
        self._recorder = recorder
        self._error = error

    def __call__(
        self,
        regulatory_instrument_id: str,
        *,
        baseline_graph: GraphHandle,
        model: str,
        call_completion: CompletionCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> GovernanceDerivationResult:
        """Record the call and return (or raise) a canned :class:`GovernanceDerivationResult`."""
        _ = (baseline_graph, call_completion, emitter)
        self._recorder.calls.append(
            StageCall("governance_derivation", regulatory_instrument_id, {"model": model})
        )
        if self._error is not None:
            raise self._error
        return GovernanceDerivationResult(
            regulatory_instrument_id=regulatory_instrument_id,
            policy_node_ids=(),
            standard_node_ids=(),
            control_node_ids=(),
            unmatched_capability_ids=(),
        )


class FakeInternalSeedAdapter:
    """Stand-in for ``InternalSeedIngestionAdapter`` -- reads a real seed file off disk.

    Unlike :class:`FakeIngestionAdapter` (never invoked, since the faked
    ``ingest`` stage ignores its adapter), the internal pipeline's own
    orchestration genuinely calls ``adapter.read_seed(...)`` itself (to
    derive the graph ``short_name`` before any graph is opened) -- so this
    fake delegates to the real, already-tested ``InternalSeedIngestionAdapter``
    rather than raising, letting a route-level test exercise real parsing
    against a real fixture file while every downstream stage stays faked.
    """

    def read_seed(self, identifier: str) -> InternalRegulationSeed:
        """Delegate to the real adapter -- fixture parsing is not what these tests fake."""
        from ps_service.ingestion.adapters.internal_seed.adapter import (
            InternalSeedIngestionAdapter,
        )

        return InternalSeedIngestionAdapter().read_seed(identifier)


class FakeIngestionAdapter:
    """Satisfies ``ps_service.ingestion.adapters.base.IngestionAdapter`` structurally.

    Never invoked — the faked ingest stage ignores its adapter.
    """

    def fetch_regulatory_instrument_structure(
        self, identifier: str
    ) -> FetchedRegulatoryInstrumentStructure:
        """Fail loudly if the faked pipeline ever actually calls the adapter."""
        message = f"the faked ingest stage must not fetch {identifier!r}"
        raise AssertionError(message)


class FakeDomainMappingAdapter:
    """Satisfies ``ps_service.domain_mapper.adapters.base.DomainMappingAdapter`` structurally."""

    def read_native_units(self, graph: GraphHandle) -> tuple[ExtractionUnit, ...]:
        """Fail loudly if the faked pipeline ever actually calls the adapter."""
        _ = graph
        message = "the faked extract stage must not read native units"
        raise AssertionError(message)


@dataclass(frozen=True, slots=True)
class FakePipeline:
    """The assembled fake dependencies plus handles a test asserts against."""

    dependencies: PipelineDependencies
    recorder: StageRecorder
    native: FakeGraphHandle
    baseline: FakeGraphHandle
    single_tenant: FakeGraphHandle


def build_fake_pipeline_dependencies(
    *,
    rid: str = "CRA-1.0",
    ingest_error: Exception | None = None,
    extract_error: Exception | None = None,
    derive_error: Exception | None = None,
    merge_error: Exception | None = None,
    derive_unmatched_obligation_ids: tuple[str, ...] = (),
    internal_rid: str = "ENGPRAC-3.0",
    ingest_internal_error: Exception | None = None,
    derive_governance_error: Exception | None = None,
) -> FakePipeline:
    """Assemble a :class:`FakePipeline` around one shared :class:`StageRecorder`.

    Args:
        rid: The ``regulatory_instrument_id`` the fake ingest stage returns.
        ingest_error: If set, the ingest stage raises this instead of returning.
        extract_error: If set, the extract stage raises this.
        derive_error: If set, the derive stage raises this.
        merge_error: If set, the merge stage raises this.
        derive_unmatched_obligation_ids: Canned ``unmatched_obligation_ids`` for
            the fake derive stage's ``DerivationResult`` (issue #64 slice 9).
        internal_rid: The ``regulatory_instrument_id`` the fake
            ``ingest_internal`` stage returns (issue #54, S2).
        ingest_internal_error: If set, the ``ingest_internal`` stage raises
            this instead of returning.
        derive_governance_error: If set, the ``governance_derivation`` stage
            raises this instead of returning (issue #54, S3).

    Returns:
        A :class:`FakePipeline` whose ``dependencies`` can be passed straight into
        ``run_catalog_ingestion_pipeline``/``run_internal_ingestion_pipeline``.
    """
    recorder = StageRecorder()
    native = FakeGraphHandle()
    baseline = FakeGraphHandle()
    single_tenant = FakeGraphHandle()

    def _open_native(config: ServiceConfig, short_name: str) -> GraphHandle:
        _ = (config, short_name)
        return native

    def _open_baseline(config: ServiceConfig, short_name: str) -> GraphHandle:
        _ = (config, short_name)
        return baseline

    def _open_single_tenant(config: ServiceConfig) -> GraphHandle:
        _ = config
        return single_tenant

    dependencies = PipelineDependencies(
        graphs=GraphOpeners(
            native=_open_native, baseline=_open_baseline, single_tenant=_open_single_tenant
        ),
        stages=PipelineStages(
            ingest=FakeIngestStage(recorder, rid=rid, error=ingest_error),
            extract=FakeExtractStage(recorder, error=extract_error),
            derive=FakeDeriveStage(
                recorder,
                error=derive_error,
                unmatched_obligation_ids=derive_unmatched_obligation_ids,
            ),
            merge=FakeMergeStage(recorder, error=merge_error),
            ingest_internal=FakeIngestInternalStage(
                recorder, rid=internal_rid, error=ingest_internal_error
            ),
            derive_governance=FakeDeriveGovernanceStage(recorder, error=derive_governance_error),
        ),
        adapters=PipelineAdapters(
            ingestion=FakeIngestionAdapter,
            mapping=FakeDomainMappingAdapter,
            internal_seed=FakeInternalSeedAdapter,
        ),
    )
    return FakePipeline(
        dependencies=dependencies,
        recorder=recorder,
        native=native,
        baseline=baseline,
        single_tenant=single_tenant,
    )


# --- change-check fakes (issue #73, PLAN.md §4 Slice 2) ---------------------


def _never_open_native(config: ServiceConfig, short_name: str) -> GraphHandle:
    """Fail loudly if a Slice 2 test's faked sweep ever opens the native graph."""
    _ = config
    message = f"open_native must not be called in this test (short_name={short_name!r})"
    raise AssertionError(message)


def _never_trigger_reingestion(
    identifier: str,
    short_name: str,
    new_version: str,
    *,
    adapter: IngestionAdapter,
    graph: GraphHandle,
    emitter: LogEmitter | None = None,
) -> ReingestionOutcome:
    """Fail loudly if a faked sweep with no scripted ``reingestion_result`` calls this.

    Mirrors :class:`FakeIngestionAdapter`'s own "never invoked" pattern.
    The default wiring (no ``reingestion_result`` passed to
    :func:`build_fake_change_check_dependencies`) -- used by every Slice 2
    test (no findings ever reach the `finding_ids` branch) and by Slice 3's
    D5 "no curated catalog entry" test (the missing-entry short-circuit
    happens before `trigger_reingestion` would ever be called).
    """
    _ = (adapter, graph, emitter)
    message = (
        "trigger_reingestion must not be called in this test "
        f"(identifier={identifier!r}, short_name={short_name!r}, new_version={new_version!r})"
    )
    raise AssertionError(message)


def _never_default_adapter() -> IngestionAdapter:
    """Fail loudly if a Slice 2 test's faked sweep ever builds the default adapter."""
    raise AssertionError("default_adapter must not be called in this test")


def _never_find_catalog_entry(celex: str) -> CatalogEntry | None:
    """Fail loudly if a Slice 2 test's faked sweep ever looks up a catalog entry."""
    message = f"find_catalog_entry must not be called in this test (celex={celex!r})"
    raise AssertionError(message)


@dataclass(frozen=True, slots=True)
class TriggerReingestionCallRecord:
    """One recorded ``trigger_reingestion`` invocation (issue #73 Slice 3, D4).

    Captures the bound argument *values* only -- a plain Python function
    call collapses positional/keyword calling convention by the time the
    callee sees it, so identifier/short_name/new_version/adapter/graph are
    recorded uniformly regardless of how the caller passed them; a test
    asserts on these values (PLAN.md §4 Slice 3's "argument contract").
    """

    identifier: str
    short_name: str
    new_version: str
    adapter: IngestionAdapter
    graph: GraphHandle


@dataclass(frozen=True, slots=True)
class FakeChangeCheckDependencies:
    """The assembled fake dependencies plus handles a test asserts against."""

    dependencies: ChangeCheckDependencies
    single_tenant: FakeGraphHandle
    read_tracked_instruments_graphs: list[GraphHandle]
    poll_for_amendments_graphs: list[GraphHandle]
    find_catalog_entry_calls: list[str]
    open_native_short_names: list[str]
    trigger_reingestion_calls: list[TriggerReingestionCallRecord]


def build_fake_change_check_dependencies(
    *,
    tracked: tuple[TrackedInstrumentNode, ...] = (),
    poll_report: PollReport | None = None,
    catalog_entries: Mapping[str, CatalogEntry] | None = None,
    reingestion_result: ReingestionOutcome | None = None,
    reingestion_results: Sequence[ReingestionOutcome | BaseException] | None = None,
    native_graph: FakeGraphHandle | None = None,
    ingestion_adapter: IngestionAdapter | None = None,
) -> FakeChangeCheckDependencies:
    """Assemble a :class:`FakeChangeCheckDependencies` around scripted tracked/poll results.

    Mirrors :func:`build_fake_pipeline_dependencies`'s exact shape.

    By default (`catalog_entries=None`, `reingestion_result=None`) every
    amendment-reingest dependency (`find_catalog_entry`/`open_native`/
    `default_adapter`/`trigger_reingestion`) is wired to a stand-in that
    raises `AssertionError` if ever called (`_never_*` above) -- correct for
    Slice 2's poll-only tests (no findings ever reach the `finding_ids`
    branch) and for Slice 3's D5 "no curated catalog entry" test taken
    alone (see below).

    Args:
        tracked: The `TrackedInstrumentNode` tuple the fake
            `read_tracked_instruments` returns.
        poll_report: The `PollReport` the fake `poll_for_amendments` returns.
            Defaults to an empty report (`polled_count=len(tracked)`, no
            findings, no failures) when omitted.
        catalog_entries: When given (issue #73 Slice 3), wires a real
            `{celex: CatalogEntry}` lookup for `find_catalog_entry` --
            `.get(celex)` returns `None` for an unmapped celex exactly like
            the real `find_by_celex` (D5), so an empty dict (`{}`) is a
            valid, deliberate way to script "no curated entry for any
            celex" without needing a raising fake. `None` (default) keeps
            the `_never_find_catalog_entry` stand-in.
        reingestion_result: When given (issue #73 Slice 3), wires
            `trigger_reingestion` to a recording fake that always returns
            this `ReingestionOutcome`, and wires `open_native`/
            `default_adapter` to recording fakes returning `native_graph`/
            `ingestion_adapter` (or a fresh sentinel of each, if omitted).
            `None` (default) keeps all three `_never_*` stand-ins -- D5's
            "never called" assertions rely on this default. Ignored when
            `reingestion_results` is also given.
        reingestion_results: When given (issue #73 Slices 4-5), wires
            `trigger_reingestion` to a recording fake that consumes this
            sequence **in call order** -- one entry per call, mirroring
            `FakeGraphHandle.results`'s own "consumed in order" pattern. A
            `ReingestionOutcome` entry is returned; a `BaseException`
            *instance* is raised instead (scripting a per-instrument
            `trigger_reingestion` failure -- D10's national-transposition
            case, D6's generic-failure case, or a mix across multiple
            tracked instruments in one sweep, e.g. Slice 5's isolation
            proof). Calling `trigger_reingestion` more times than entries
            scripted raises `AssertionError` (a test-authoring bug, not
            production behavior). Takes priority over `reingestion_result`
            when both are given. Also wires `open_native`/`default_adapter`
            exactly like `reingestion_result` does.
        native_graph: The `FakeGraphHandle` `open_native` returns; only
            meaningful when `reingestion_result`/`reingestion_results` is
            given. Defaults to a fresh `FakeGraphHandle()` sentinel.
        ingestion_adapter: The `IngestionAdapter` `default_adapter` returns;
            only meaningful when `reingestion_result`/`reingestion_results`
            is given. Defaults to a fresh `FakeIngestionAdapter()` sentinel.

    Returns:
        A :class:`FakeChangeCheckDependencies` whose `dependencies` can be
        passed straight into `run_change_check_sweep`.
    """
    single_tenant = FakeGraphHandle()
    report = (
        poll_report
        if poll_report is not None
        else PollReport(findings=(), polled_count=len(tracked), failed_ids=(), unconfigured_ids=())
    )
    read_tracked_instruments_graphs: list[GraphHandle] = []
    poll_for_amendments_graphs: list[GraphHandle] = []
    find_catalog_entry_calls: list[str] = []
    open_native_short_names: list[str] = []
    trigger_reingestion_calls: list[TriggerReingestionCallRecord] = []

    def _open_single_tenant(config: ServiceConfig) -> GraphHandle:
        _ = config
        return single_tenant

    def _read_tracked_instruments(graph: GraphHandle) -> tuple[TrackedInstrumentNode, ...]:
        read_tracked_instruments_graphs.append(graph)
        return tracked

    def _poll_for_amendments(
        graph: GraphHandle, *, emitter: LogEmitter | None = None
    ) -> PollReport:
        _ = emitter
        poll_for_amendments_graphs.append(graph)
        return report

    find_catalog_entry: Callable[[str], CatalogEntry | None]
    if catalog_entries is None:
        find_catalog_entry = _never_find_catalog_entry
    else:
        entries = catalog_entries

        def _find_catalog_entry(celex: str) -> CatalogEntry | None:
            find_catalog_entry_calls.append(celex)
            return entries.get(celex)

        find_catalog_entry = _find_catalog_entry

    open_native: Callable[[ServiceConfig, str], GraphHandle]
    default_adapter: Callable[[], IngestionAdapter]
    trigger_reingestion: TriggerReingestionCall
    if reingestion_result is None and reingestion_results is None:
        open_native = _never_open_native
        default_adapter = _never_default_adapter
        trigger_reingestion = _never_trigger_reingestion
    else:
        fixed_result = reingestion_result
        scripted: deque[ReingestionOutcome | BaseException] | None = (
            deque(reingestion_results) if reingestion_results is not None else None
        )
        resolved_native_graph = native_graph if native_graph is not None else FakeGraphHandle()
        resolved_ingestion_adapter = (
            ingestion_adapter if ingestion_adapter is not None else FakeIngestionAdapter()
        )

        def _open_native(config: ServiceConfig, short_name: str) -> GraphHandle:
            _ = config
            open_native_short_names.append(short_name)
            return resolved_native_graph

        def _default_adapter() -> IngestionAdapter:
            return resolved_ingestion_adapter

        def _trigger_reingestion(
            identifier: str,
            short_name: str,
            new_version: str,
            *,
            adapter: IngestionAdapter,
            graph: GraphHandle,
            emitter: LogEmitter | None = None,
        ) -> ReingestionOutcome:
            _ = emitter
            trigger_reingestion_calls.append(
                TriggerReingestionCallRecord(identifier, short_name, new_version, adapter, graph)
            )
            if scripted is not None:
                if not scripted:
                    message = (
                        "trigger_reingestion called more times than reingestion_results scripted"
                    )
                    raise AssertionError(message)
                next_result = scripted.popleft()
                if isinstance(next_result, BaseException):
                    raise next_result
                return next_result
            assert fixed_result is not None  # narrows for the type checker: this branch's guard
            return fixed_result

        open_native = _open_native
        default_adapter = _default_adapter
        trigger_reingestion = _trigger_reingestion

    dependencies = ChangeCheckDependencies(
        open_single_tenant=_open_single_tenant,
        open_native=open_native,
        read_tracked_instruments=_read_tracked_instruments,
        poll_for_amendments=_poll_for_amendments,
        trigger_reingestion=trigger_reingestion,
        default_adapter=default_adapter,
        find_catalog_entry=find_catalog_entry,
    )
    return FakeChangeCheckDependencies(
        dependencies=dependencies,
        single_tenant=single_tenant,
        read_tracked_instruments_graphs=read_tracked_instruments_graphs,
        poll_for_amendments_graphs=poll_for_amendments_graphs,
        find_catalog_entry_calls=find_catalog_entry_calls,
        open_native_short_names=open_native_short_names,
        trigger_reingestion_calls=trigger_reingestion_calls,
    )
