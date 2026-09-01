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

from ps_service.api.ingestion_orchestration import (
    GraphOpeners,
    PipelineAdapters,
    PipelineDependencies,
    PipelineStages,
)
from ps_service.company_merge.models import MergeResult
from ps_service.domain_mapper.models import DerivationResult, ExtractionResult
from ps_service.ingestion.models import IngestResult

if TYPE_CHECKING:
    from pathlib import Path

    from ps_service.api.ingestion_orchestration import GraphHandle
    from ps_service.config import ServiceConfig
    from ps_service.domain_mapper.models import ExtractionUnit
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
) -> FakePipeline:
    """Assemble a :class:`FakePipeline` around one shared :class:`StageRecorder`.

    Args:
        rid: The ``regulatory_instrument_id`` the fake ingest stage returns.
        ingest_error: If set, the ingest stage raises this instead of returning.
        extract_error: If set, the extract stage raises this.
        derive_error: If set, the derive stage raises this.
        merge_error: If set, the merge stage raises this.

    Returns:
        A :class:`FakePipeline` whose ``dependencies`` can be passed straight into
        ``run_catalog_ingestion_pipeline``.
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
            derive=FakeDeriveStage(recorder, error=derive_error),
            merge=FakeMergeStage(recorder, error=merge_error),
        ),
        adapters=PipelineAdapters(ingestion=FakeIngestionAdapter, mapping=FakeDomainMappingAdapter),
    )
    return FakePipeline(
        dependencies=dependencies,
        recorder=recorder,
        native=native,
        baseline=baseline,
        single_tenant=single_tenant,
    )
