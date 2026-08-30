"""In-process orchestration of the external ingestion pipeline (AC-BI-002/003/008).

``ps_service.api`` owns the sequential Ingestion -> Domain Mapper -> Company Merge
pipeline for a catalog (CELEX) regulation. This module holds:

* the injection seam -- :class:`PipelineDependencies` and its nested
  :class:`GraphOpeners` / :class:`PipelineStages` / :class:`PipelineAdapters`
  bundles, plus :func:`build_default_pipeline_dependencies`, which wires the real
  shipped entry points via **function-local** imports so that importing
  ``ps_service.main`` never transitively loads Domain Mapper / Company Merge at
  module load (M6 / the Process Harness decoupling guarantee);
* :func:`_require_ingestion_config` -- the config-completeness guard, returning a
  narrowed :class:`_ResolvedPipelineConfig` (HTTP 503 before any I/O if a
  required value is unset);
* :func:`_run_stage` -- the per-stage ``try``/``except`` wrapper that converts any
  stage failure into a :class:`~ps_service.api.errors.PipelineStageError` with a
  caller-safe, path/host-scrubbed reason (AC-BI-008/009);
* :func:`run_catalog_ingestion_pipeline` -- the sequencer itself.

No ``falkordb`` import ever crosses into ``ps_service.api``: graphs are opened
through the injected :class:`GraphOpeners` callables and handled via the local
:class:`GraphHandle` Protocol, which the three components' near-duplicate handle
types satisfy structurally.

The internal-document pipeline (request ``source: internal``) is issue #54; this
module covers the catalog path only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from ps_service.api.error_handlers import (
    _scrub_text,  # pyright: ignore[reportPrivateUsage]  # shared scrubber; IMPL_4 deviation 1 sanctions reuse
    is_safe_verbatim,
)
from ps_service.api.errors import IngestionConfigIncompleteError, PipelineStageError
from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.api.catalog import CatalogEntry
    from ps_service.company_merge.models import MergeResult
    from ps_service.config import ServiceConfig
    from ps_service.domain_mapper.adapters.base import DomainMappingAdapter
    from ps_service.domain_mapper.models import DerivationResult, ExtractionResult
    from ps_service.ingestion.adapters.base import IngestionAdapter
    from ps_service.ingestion.models import IngestResult
    from ps_service.llm_interface.client import CompletionCaller, EmbeddingCaller
    from ps_service.logging import LogEmitter

_COMPONENT = "api"
_RUN_ACTION = "ingestion_run"
_STAGE_REASON_MAX_LEN = 300


# --- graph seam (m9 -- no `falkordb` type ever crosses into `ps_service.api`) ---


class _QueryResult(Protocol):
    """The one property the orchestration reads off a Cypher result."""

    @property
    def result_set(self) -> list[object]:
        """The rows returned by the query."""
        ...


class GraphHandle(Protocol):
    """Structural stand-in for a FalkorDB graph handle.

    The ``GraphHandle`` types of ``ps_service.ingestion`` / ``domain_mapper`` /
    ``company_merge`` satisfy this structurally, so ``ps_service.api`` never
    imports ``falkordb`` or any component's concrete handle type.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> _QueryResult:
        """Run Cypher ``q`` (optionally parameterized via ``params``) and return the result."""
        ...


# --- stage seams (each Protocol mirrors the real shipped stage fn signature) ---


class IngestStage(Protocol):
    """Call shape of ``ps_service.ingestion.ingest_regulatory_instrument``."""

    def __call__(
        self,
        identifier: str,
        short_name: str,
        *,
        version: str,
        adapter: IngestionAdapter,
        graph: GraphHandle,
        run_id: str | None = None,
        emitter: LogEmitter | None = None,
    ) -> IngestResult:
        """Ingest one regulatory instrument's native structural graph."""
        ...


class ExtractStage(Protocol):
    """Call shape of ``ps_service.domain_mapper.extract_roles_and_requirements``."""

    def __call__(
        self,
        regulatory_instrument_id: str,
        *,
        adapter: DomainMappingAdapter,
        native_graph: GraphHandle,
        baseline_graph: GraphHandle,
        model: str,
        call_completion: CompletionCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> ExtractionResult:
        """Extract the Role / Requirement spine into the baseline graph."""
        ...


class DeriveStage(Protocol):
    """Call shape of ``ps_service.domain_mapper.derive_obligations_and_capabilities``."""

    def __call__(
        self,
        regulatory_instrument_id: str,
        *,
        baseline_graph: GraphHandle,
        model: str,
        call_completion: CompletionCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> DerivationResult:
        """Derive Obligation / Capability nodes on the baseline graph."""
        ...


class MergeStage(Protocol):
    """Call shape of ``ps_service.company_merge.merge_baseline_graph``."""

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
        """Merge one regulation's baseline graph into the single-tenant graph."""
        ...


# --- injection seam (M2) ---


@dataclass(frozen=True, slots=True)
class GraphOpeners:
    """Callables that open the three graphs a pipeline run needs, by ``short_name``."""

    native: Callable[[ServiceConfig, str], GraphHandle]
    baseline: Callable[[ServiceConfig, str], GraphHandle]
    single_tenant: Callable[[ServiceConfig], GraphHandle]


@dataclass(frozen=True, slots=True)
class PipelineStages:
    """The four external-pipeline stage entry points, in run order."""

    ingest: IngestStage
    extract: ExtractStage
    derive: DeriveStage
    merge: MergeStage


@dataclass(frozen=True, slots=True)
class PipelineAdapters:
    """Zero-arg factories for the source-specific adapters the stages consume."""

    ingestion: Callable[[], IngestionAdapter]
    mapping: Callable[[], DomainMappingAdapter]


@dataclass(frozen=True, slots=True)
class PipelineDependencies:
    """Everything ``run_catalog_ingestion_pipeline`` needs that is not per-request."""

    graphs: GraphOpeners
    stages: PipelineStages
    adapters: PipelineAdapters


# --- config-completeness guard (M1) ---


@dataclass(frozen=True, slots=True)
class _ResolvedPipelineConfig:
    """The pipeline-relevant ``ServiceConfig`` values, narrowed to non-``None``."""

    chat_model: str
    embed_model: str
    similarity_threshold: float


def _missing_config_names(config: ServiceConfig) -> list[str]:
    """Return the names of the pipeline-required config values that are ``None``."""
    return [
        name
        for name, value in (
            ("llm_interface_model", config.llm_interface_model),
            ("llm_interface_embed_model", config.llm_interface_embed_model),
            ("company_merge_similarity_threshold", config.company_merge_similarity_threshold),
        )
        if value is None
    ]


def _require_ingestion_config(config: ServiceConfig) -> _ResolvedPipelineConfig:
    """Return the narrowed pipeline config, or raise if any required value is unset.

    Called first by :func:`run_catalog_ingestion_pipeline`, before any graph or
    stage call, so an incomplete configuration fails as HTTP 503 with no I/O.

    Args:
        config: The resolved service configuration.

    Returns:
        A :class:`_ResolvedPipelineConfig` whose three fields are all non-``None``.

    Raises:
        IngestionConfigIncompleteError: If ``llm_interface_model``,
            ``llm_interface_embed_model``, or
            ``company_merge_similarity_threshold`` is ``None``.
    """
    chat_model = config.llm_interface_model
    embed_model = config.llm_interface_embed_model
    threshold = config.company_merge_similarity_threshold
    if chat_model is None or embed_model is None or threshold is None:
        missing = ", ".join(_missing_config_names(config))
        message = f"ingestion configuration incomplete: {missing} not set"
        raise IngestionConfigIncompleteError(message)
    return _ResolvedPipelineConfig(
        chat_model=chat_model, embed_model=embed_model, similarity_threshold=threshold
    )


# --- per-stage failure wrapper (m2 / M5) ---


def _run_stage[T](name: str, thunk: Callable[[], T], *, emitter: LogEmitter | None = None) -> T:
    """Run one pipeline stage, converting any failure into a ``PipelineStageError``.

    Args:
        name: The stage name (e.g. ``"extraction"``) for the error and log lines.
        thunk: A zero-arg callable that runs the stage and returns its result.
        emitter: Optional log emitter for the server-side failure-detail entry.

    Returns:
        The stage's result, unchanged, on success.

    Raises:
        PipelineStageError: If ``thunk`` raises. ``reason`` is the scrubbed
            message for a whitelisted domain error, else a generic
            ``"<name> failed"`` -- with the full ``repr`` emitted server-side
            only (AC-BI-009).
    """
    try:
        return thunk()
    except Exception as exc:
        if is_safe_verbatim(exc):
            reason = _scrub_text(f"{type(exc).__name__}: {exc}")[:_STAGE_REASON_MAX_LEN]
        else:
            reason = f"{name} failed"
            emit_log_entry(
                component=_COMPONENT,
                action=_RUN_ACTION,
                outcome="failed",
                extra={"failing_stage": name, "detail": repr(exc)},
                emitter=emitter,
            )
        raise PipelineStageError(stage=name, reason=reason) from exc


# --- run outcome ---


@dataclass(frozen=True, slots=True)
class StageReport:
    """One completed pipeline stage and a small integer summary of what it produced."""

    stage: str
    summary: dict[str, int]


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    """The result of a full ingestion pipeline run."""

    regulatory_instrument_id: str
    source: Literal["catalog", "internal"]
    stages: tuple[StageReport, ...]


def _ingestion_summary(result: IngestResult) -> dict[str, int]:
    """Summarise an ``IngestResult`` as small integer counts."""
    return {"verified_labels": len(result.counts)}


def _extraction_summary(result: ExtractionResult) -> dict[str, int]:
    """Summarise an ``ExtractionResult`` as small integer counts."""
    return {
        "roles": len(result.role_node_ids),
        "requirements": len(result.requirement_ids),
        "candidates": result.candidate_count,
        "skipped_units": result.skipped_unit_count,
    }


def _derivation_summary(result: DerivationResult) -> dict[str, int]:
    """Summarise a ``DerivationResult`` as small integer counts."""
    return {
        "obligations": len(result.obligation_node_ids),
        "capabilities": len(result.capability_node_ids),
        "unmatched_requirements": len(result.unmatched_requirement_ids),
    }


def _merge_summary(result: MergeResult) -> dict[str, int]:
    """Summarise a ``MergeResult`` as small integer counts."""
    return {
        "obligations": len(result.obligation_ids),
        "canonical_capabilities": len(result.capability_canonical_ids),
        "near_misses": len(result.near_misses),
    }


# --- logging + timing helpers ---


def _elapsed_ms(started: float) -> float:
    """Milliseconds elapsed since ``started`` (a ``time.perf_counter()`` reading)."""
    return (time.perf_counter() - started) * 1000


def _emit_run(
    *,
    outcome: str,
    run_id: str,
    source_identifier: str,
    caller: str,
    emitter: LogEmitter | None,
    duration_ms: float | None = None,
    failing_stage: str | None = None,
) -> None:
    """Emit one ``ingestion_run`` log entry (AC-BI-010 / AC-BI-011).

    Args:
        outcome: ``"started"`` / ``"succeeded"`` / ``"failed"``.
        run_id: The request-scoped run id, carried on every line.
        source_identifier: The catalog CELEX (or, in #54, the internal seed id).
        caller: The requesting client host (or ``"unknown"``).
        emitter: Optional explicit emitter; otherwise the process default.
        duration_ms: Wall time for the run so far (omitted on ``"started"``).
        failing_stage: The stage that raised, on the ``"failed"`` entry only.
    """
    extra: dict[str, object] = {"source_identifier": source_identifier, "caller": caller}
    if failing_stage is not None:
        extra["failing_stage"] = failing_stage
    emit_log_entry(
        component=_COMPONENT,
        action=_RUN_ACTION,
        outcome=outcome,
        run_id=run_id,
        duration_ms=duration_ms,
        extra=extra,
        emitter=emitter,
    )


# --- the sequencer ---


@dataclass(frozen=True, slots=True)
class _OpenGraphs:
    """The three graph handles one pipeline run writes through."""

    native: GraphHandle
    baseline: GraphHandle
    single_tenant: GraphHandle


def _execute_catalog_stages(
    entry: CatalogEntry,
    *,
    run_id: str,
    resolved: _ResolvedPipelineConfig,
    graphs: _OpenGraphs,
    dependencies: PipelineDependencies,
    emitter: LogEmitter | None,
) -> tuple[str, tuple[StageReport, ...]]:
    """Run ingest -> extract -> derive -> merge, aborting at the first failure.

    Each stage after the first consumes the ``regulatory_instrument_id`` the
    ingest stage returned (AC-BI-003). The first :func:`_run_stage` to raise a
    ``PipelineStageError`` aborts the sequence -- later stages never run
    (AC-BI-008).

    Returns:
        The ``regulatory_instrument_id`` and the per-stage :class:`StageReport`
        tuple, in pipeline order.
    """
    stages = dependencies.stages
    ingestion_adapter = dependencies.adapters.ingestion()
    mapping_adapter = dependencies.adapters.mapping()

    ingest_result = _run_stage(
        "ingestion",
        lambda: stages.ingest(
            entry.celex,
            entry.short_name,
            version=entry.version,
            adapter=ingestion_adapter,
            graph=graphs.native,
            run_id=run_id,
        ),
        emitter=emitter,
    )
    rid = ingest_result.regulatory_instrument_id
    extract_result = _run_stage(
        "extraction",
        lambda: stages.extract(
            rid,
            adapter=mapping_adapter,
            native_graph=graphs.native,
            baseline_graph=graphs.baseline,
            model=resolved.chat_model,
        ),
        emitter=emitter,
    )
    derive_result = _run_stage(
        "derivation",
        lambda: stages.derive(rid, baseline_graph=graphs.baseline, model=resolved.chat_model),
        emitter=emitter,
    )
    merge_result = _run_stage(
        "merge",
        lambda: stages.merge(
            rid,
            baseline_graph=graphs.baseline,
            single_tenant_graph=graphs.single_tenant,
            embed_model=resolved.embed_model,
            similarity_threshold=resolved.similarity_threshold,
        ),
        emitter=emitter,
    )
    reports = (
        StageReport("ingestion", _ingestion_summary(ingest_result)),
        StageReport("extraction", _extraction_summary(extract_result)),
        StageReport("derivation", _derivation_summary(derive_result)),
        StageReport("merge", _merge_summary(merge_result)),
    )
    return rid, reports


def run_catalog_ingestion_pipeline(
    entry: CatalogEntry,
    *,
    config: ServiceConfig,
    run_id: str,
    caller: str,
    dependencies: PipelineDependencies,
    emitter: LogEmitter | None = None,
) -> IngestionOutcome:
    """Run the external ingestion pipeline for one catalog regulation.

    Sequences Ingestion -> Domain Mapper (extract, derive) -> Company Merge
    in-process (AC-BI-002/003). Re-running for the same identifier converges on
    the exact-canonical-identity nodes, but LLM-extraction non-determinism
    (issue #34) can still fragment a reworded Capability across re-ingestions
    until #34 is addressed. The whole run shares one ``run_id`` -- it is
    passed explicitly into the ingest stage (the only stage fn that self-binds a
    fresh run context) and carried on the ``ingestion_run`` start/end log entries
    (AC-BI-010/011). A stage failure aborts the sequence and surfaces as a
    ``PipelineStageError`` naming the failing stage (AC-BI-008), with no
    filesystem path / host / URL in its reason (AC-BI-009).

    Args:
        entry: The curated catalog entry (CELEX, title, short name, version).
        config: The resolved service configuration.
        run_id: The request-scoped run id.
        caller: The requesting client host, or ``"unknown"``.
        dependencies: The injected graph openers, stage functions, and adapter
            factories (``build_default_pipeline_dependencies`` in production; a
            fake in fast tests).
        emitter: Optional explicit log emitter; otherwise the process default.

    Returns:
        An :class:`IngestionOutcome` with ``source="catalog"`` and one
        :class:`StageReport` per completed stage.

    Raises:
        IngestionConfigIncompleteError: If the configuration is missing an LLM
            model / embed model / similarity threshold (HTTP 503).
        PipelineStageError: If any stage raises (HTTP 502).
    """
    resolved = _require_ingestion_config(config)
    graphs = _OpenGraphs(
        native=dependencies.graphs.native(config, entry.short_name),
        baseline=dependencies.graphs.baseline(config, entry.short_name),
        single_tenant=dependencies.graphs.single_tenant(config),
    )
    started = time.perf_counter()
    _emit_run(
        outcome="started",
        run_id=run_id,
        source_identifier=entry.celex,
        caller=caller,
        emitter=emitter,
    )
    try:
        rid, reports = _execute_catalog_stages(
            entry,
            run_id=run_id,
            resolved=resolved,
            graphs=graphs,
            dependencies=dependencies,
            emitter=emitter,
        )
    except PipelineStageError as exc:
        _emit_run(
            outcome="failed",
            run_id=run_id,
            source_identifier=entry.celex,
            caller=caller,
            emitter=emitter,
            duration_ms=_elapsed_ms(started),
            failing_stage=exc.stage,
        )
        raise
    _emit_run(
        outcome="succeeded",
        run_id=run_id,
        source_identifier=entry.celex,
        caller=caller,
        emitter=emitter,
        duration_ms=_elapsed_ms(started),
    )
    return IngestionOutcome(regulatory_instrument_id=rid, source="catalog", stages=reports)


# --- default wiring (M6 -- every pipeline import below is function-local) ---


def _open_native_graph(config: ServiceConfig, short_name: str) -> GraphHandle:
    """Open the ``{short}_native`` graph for ``short_name``."""
    from ps_service.ingestion.falkordb_client import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off the pipeline at import
        connect_from_config,
        native_graph_name,
        select_graph,
    )

    return select_graph(connect_from_config(config), native_graph_name(short_name))


def _open_baseline_graph(config: ServiceConfig, short_name: str) -> GraphHandle:
    """Open the ``{short}_baseline`` graph for ``short_name``."""
    from ps_service.domain_mapper.falkordb_client import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Domain Mapper at import
        baseline_graph_name,
        connect_from_config,
        select_graph,
    )

    return select_graph(connect_from_config(config), baseline_graph_name(short_name))


def _open_single_tenant_graph(config: ServiceConfig) -> GraphHandle:
    """Open the single-tenant (``policy_system``) graph."""
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Company Merge at import
        connect_from_config,
        select_graph,
        single_tenant_graph_name,
    )

    return select_graph(connect_from_config(config), single_tenant_graph_name())


def _default_ingestion_adapter() -> IngestionAdapter:
    """Build the default (Cellar/ELI) Ingestion Adapter."""
    from ps_service.ingestion.adapters.cellar_eli.adapter import (  # noqa: PLC0415 -- M6: function-local
        CellarEliAdapter,
    )

    return CellarEliAdapter()


def _default_mapping_adapter() -> DomainMappingAdapter:
    """Build the default (Cellar/ELI) Domain Mapping Adapter."""
    from ps_service.domain_mapper.adapters.cellar_eli import (  # noqa: PLC0415 -- M6: function-local
        CellarEliDomainMappingAdapter,
    )

    return CellarEliDomainMappingAdapter()


def build_default_pipeline_dependencies() -> PipelineDependencies:
    """Wire the real shipped pipeline entry points into a ``PipelineDependencies``.

    Every stage entry point, adapter class, and FalkorDB client is imported
    **function-locally** (here and in the opener helpers) so that importing
    ``ps_service.main`` never transitively loads Domain Mapper or Company Merge at
    module load (M6 / the Process Harness decoupling guarantee).

    Returns:
        A :class:`PipelineDependencies` bound to the production stage functions,
        graph openers, and adapter factories.
    """
    from ps_service.company_merge import merge_baseline_graph  # noqa: PLC0415 -- M6: function-local
    from ps_service.domain_mapper import (  # noqa: PLC0415 -- M6: function-local
        derive_obligations_and_capabilities,
        extract_roles_and_requirements,
    )
    from ps_service.ingestion import (  # noqa: PLC0415 -- M6: function-local
        ingest_regulatory_instrument,
    )

    return PipelineDependencies(
        graphs=GraphOpeners(
            native=_open_native_graph,
            baseline=_open_baseline_graph,
            single_tenant=_open_single_tenant_graph,
        ),
        stages=PipelineStages(
            ingest=ingest_regulatory_instrument,
            extract=extract_roles_and_requirements,
            derive=derive_obligations_and_capabilities,
            merge=merge_baseline_graph,
        ),
        adapters=PipelineAdapters(
            ingestion=_default_ingestion_adapter,
            mapping=_default_mapping_adapter,
        ),
    )
