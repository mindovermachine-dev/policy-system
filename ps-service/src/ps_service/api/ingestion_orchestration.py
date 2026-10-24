"""In-process orchestration of the Ingestion -> Domain Mapper -> Company Merge pipelines.

``ps_service.api`` owns the sequential pipeline for a catalog (CELEX) regulation and for an
internal-document seed. This module holds:

* the injection seam: ``PipelineDependencies`` and its nested ``GraphOpeners``,
  ``PipelineStages`` and ``PipelineAdapters`` bundles, plus
  ``build_default_pipeline_dependencies``, which wires the real stage entry points through
  function-local imports so that importing ``ps_service.main`` never loads Domain Mapper or
  Company Merge at module load;
* the configuration guard that turns an incomplete model/threshold configuration into
  ``IngestionConfigIncompleteError`` before any I/O;
* the per-stage wrapper that converts any stage failure into a ``PipelineStageError`` carrying
  a caller-safe, path/host-scrubbed reason, and ``classify_ingestion_failure``, which maps a
  failure to the enumerated audit reason code;
* identity resolution before a run: ``resolve_via_cellar`` (existence and metadata for a CELEX
  absent from the curated catalog), the CELEX and ``short_name`` collision checks and
  ``resolve_ingestion_entry``, the single function both ingestion entry points call;
* the sequencers ``run_catalog_ingestion_pipeline``, ``run_audited_catalog_ingestion`` and
  ``run_internal_ingestion_pipeline``, which drive ``ps_service.api.run_status`` so a
  concurrent poller can read the stage an in-flight run is executing.

No ``falkordb`` import crosses into ``ps_service.api``: graphs are opened through the injected
``GraphOpeners`` callables and handled through the local ``GraphHandle`` Protocol, which the
three components' near-duplicate handle types satisfy structurally.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast

from ps_service.api.catalog_entry import CatalogEntry
from ps_service.api.error_handlers import (
    _scrub_text,  # pyright: ignore[reportPrivateUsage]  # shared scrubber, one redaction rule for every surface
    is_safe_verbatim,
)
from ps_service.api.errors import (
    CatalogIdentifierNotFoundError,
    CelexAlreadyIngestedError,
    GraphWriteUnavailableError,
    IngestionConfigIncompleteError,
    InternalIngestOwnerRequiredError,
    InternalSeedValidationError,
    PipelineStageError,
    ShortNameCollisionError,
)
from ps_service.api.graph_write_access import GRAPH_WRITE_UNAVAILABLE_MESSAGE, graph_write_access
from ps_service.api.run_status import clear_stage, set_stage
from ps_service.api.stage_failure_log import stage_failure_detail
from ps_service.audit.emit import AuditTarget, record_follow_up_row, record_opening_row
from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.config import missing_ingestion_config_fields
from ps_service.ingestion.adapters.cellar_eli.adapter import CellarEliAdapter
from ps_service.ingestion.adapters.cellar_eli.fetch import fetch_rdf, fetch_xhtml
from ps_service.ingestion.adapters.cellar_eli.metadata import extract_metadata
from ps_service.ingestion.adapters.errors import CellarNotFoundError
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.ingestion.adapters.internal_seed.persist import find_regulatory_instrument
from ps_service.ingestion_runs.audit_actions import (
    INGESTION_RUN_COMPLETE_ACTION,
    INGESTION_RUN_RESOURCE_TYPE,
    INGESTION_RUN_SUBMIT_ACTION,
    IngestionCounts,
    IngestionReasonCode,
    IngestionTrigger,
    completion_audit_entry,
    submission_audit_entry,
)
from ps_service.logging.facade import emit_log_entry
from ps_service.logging.run_context import bind_run_context

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from ps_service.api.graph_write_access import GraphWriteAccess
    from ps_service.audit.emit import AuditContext
    from ps_service.change_monitor.trigger import MetadataFetchingAdapter
    from ps_service.company_merge.models import MergeResult
    from ps_service.config import ServiceConfig
    from ps_service.domain_mapper.adapters.base import DomainMappingAdapter
    from ps_service.domain_mapper.models import DerivationResult, ExtractionResult
    from ps_service.graph_gateway.system_audit import SystemAuditRecorder
    from ps_service.ingestion.adapters.base import IngestionAdapter
    from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed
    from ps_service.ingestion.adapters.internal_seed.persist import InternalIngestResult
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.ingestion.models import IngestResult
    from ps_service.llm_interface.client import CompletionCaller, EmbeddingCaller
    from ps_service.logging import LogEmitter

# A short_name starts with a letter, continues with letters, digits, `_` or `-`, and is 1-64
# characters long, like every existing catalog short_name ("cra", "gdpr", "nis2"). Public so the
# REST request model and the MCP tool parameter share one pattern literal; both are declarative
# pydantic `Field(pattern=...)` annotations, which a shared callable could not usefully wrap.
SHORT_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"

_COMPONENT = "api"
_RUN_ACTION = "ingestion_run"
_STAGE_REASON_MAX_LEN = 300
# Fixed `source_identifier` for every internal-ingestion `_emit_run` call: there is no file path
# to use, and the parsed document's own id is unknown when the "started" log line fires.
_INTERNAL_INGESTION_SOURCE_IDENTIFIER = "internal-document"
_INTERNAL_RUN_AUDIT_TRIGGER = "internal_seed"
_OWNER_REQUIRED_MESSAGE = (
    "Ingesting a document that carries a Policy requires a signed-in user; the imported "
    "policy tree is created as a draft owned by the ingesting caller."
)
Owner = tuple[str, str]
"""A verified caller's ``(sub, iss)`` identity pair."""


# --- graph seam: no `falkordb` type ever crosses into `ps_service.api` ---


class _QueryResult(Protocol):
    """The one property the orchestration reads off a Cypher result."""

    @property
    def result_set(self) -> list[object]:
        """Return the rows the query produced.

        Returns:
            One entry per result row; empty when the query matched nothing.

        Side Effects:
            None; the rows are already materialised, so reading never fails or queries again.
        """
        ...


class GraphHandle(Protocol):
    """Structural stand-in for a FalkorDB graph handle.

    The ``GraphHandle`` types of ``ps_service.ingestion`` / ``domain_mapper`` /
    ``company_merge`` satisfy this structurally, so ``ps_service.api`` never
    imports ``falkordb`` or any component's concrete handle type.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> _QueryResult:
        """Run a Cypher query against the graph.

        Args:
            q: The Cypher text; use ``$name`` placeholders for values.
            params: Values bound to the placeholders in ``q``. ``None`` means no parameters.

        Returns:
            The query result; its ``result_set`` is empty when nothing matched.

        Raises:
            Exception: The graph database is unreachable or rejects the query; the concrete
                error type belongs to the database client and is not narrowed here.

        Side Effects:
            Whatever ``q`` does. The orchestration only passes read queries.
        """
        ...


# --- stage seams: each Protocol mirrors the real stage function's signature ---


class IngestStage(Protocol):
    """Call shape of ``ps_service.ingestion.ingest_regulatory_instrument``."""

    def __call__(  # noqa: PLR0913 -- mirrors ingest_regulatory_instrument's call shape exactly
        self,
        identifier: str,
        short_name: str,
        *,
        version: str,
        adapter: IngestionAdapter,
        graph: GraphHandle,
        submitter: GroupSubmitter,
        audit_event_id: str,
        run_id: str | None = None,
        emitter: LogEmitter | None = None,
    ) -> IngestResult:
        """Ingest one regulatory instrument's native structural graph.

        Args:
            identifier: Source key understood by ``adapter`` (a CELEX number for the Cellar
                adapter).
            short_name: Caller-supplied short name of the regulation; selects the native graph.
            version: Version label; combined with ``short_name`` into the instrument id.
            adapter: Fetches and parses the instrument's structure.
            graph: Read handle on the native graph.
            submitter: Graph Write Gateway seam through which every write is made.
            audit_event_id: Audit event the submitted group is logged under.
            run_id: Request-scoped run id to bind; ``None`` mints a fresh one.
            emitter: Log emitter for the stage entries; ``None`` uses the default.

        Returns:
            The ``IngestResult`` with the instrument id, the run id and the per-label
            reachability counts.

        Raises:
            Exception: Any fetch, parse, validation or persistence failure of the ingestion
                stage; the sequencer wraps it into a ``PipelineStageError``.

        Side Effects:
            Fetches the instrument from the source, reads the native graph, submits mutation
            groups through ``submitter`` and emits one log entry per completed step.
        """
        ...


class ExtractStage(Protocol):
    """Call shape of ``ps_service.domain_mapper.extract_roles_and_requirements``."""

    def __call__(  # noqa: PLR0913 -- mirrors extract_roles_and_requirements's call shape exactly
        self,
        regulatory_instrument_id: str,
        *,
        adapter: DomainMappingAdapter,
        native_graph: GraphHandle,
        submitter: GroupSubmitter,
        baseline_graph_name: str,
        audit_event_id: str,
        model: str,
        call_completion: CompletionCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> ExtractionResult:
        """Extract the Role / Requirement spine into the baseline graph.

        Args:
            regulatory_instrument_id: Id of the instrument node in ``native_graph``.
            adapter: Domain Mapping Adapter paired with the instrument's source.
            native_graph: Read handle on the native graph.
            submitter: Graph Write Gateway seam for the baseline group.
            baseline_graph_name: Name of the baseline graph the group targets.
            audit_event_id: Audit event the group is logged under.
            model: Chat model identifier passed to the LLM interface.
            call_completion: LLM completion caller; ``None`` uses the default.
            emitter: Log emitter; ``None`` uses the default.

        Returns:
            The ``ExtractionResult`` with the Role and Requirement ids written and the counts;
            all counts are zero when the instrument has no extraction units.

        Raises:
            Exception: Extraction or persistence failure, or an LLM provider failure; the
                sequencer wraps it into a ``PipelineStageError``.

        Side Effects:
            Calls the LLM per unit, submits one baseline group through ``submitter`` and
            emits log entries.
        """
        ...


class DeriveStage(Protocol):
    """Call shape of ``ps_service.domain_mapper.derive_obligations_and_capabilities``."""

    def __call__(
        self,
        regulatory_instrument_id: str,
        *,
        baseline_graph: GraphHandle,
        submitter: GroupSubmitter,
        baseline_graph_name: str,
        audit_event_id: str,
        model: str,
        call_completion: CompletionCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> DerivationResult:
        """Derive Obligation / Capability nodes on the baseline graph.

        Args:
            regulatory_instrument_id: Id of the instrument whose Requirements are derived from.
            baseline_graph: Read handle on the baseline graph.
            submitter: Graph Write Gateway seam for the derivation group.
            baseline_graph_name: Name of the baseline graph the group targets.
            audit_event_id: Audit event the group is logged under.
            model: Chat model identifier passed to the LLM interface.
            call_completion: LLM completion caller; ``None`` uses the default.
            emitter: Log emitter; ``None`` uses the default.

        Returns:
            The ``DerivationResult`` with the Obligation and Capability ids written; empty
            tuples when there is nothing to derive.

        Raises:
            Exception: Derivation or persistence failure (including a baseline graph behind its
                log) or an LLM provider failure; the sequencer wraps it into a
                ``PipelineStageError``.

        Side Effects:
            Reads the baseline graph, calls the LLM, submits one group through ``submitter``
            and emits log entries.
        """
        ...


class InternalSeedAdapter(Protocol):
    """Call shape of ``InternalSeedIngestionAdapter.parse_seed``."""

    def parse_seed(self, document: dict[str, object]) -> InternalRegulationSeed:
        """Validate and parse one already-read internal-regulation seed document.

        Args:
            document: The intake document as a JSON object with ``nodes`` and ``edges``
                arrays.

        Returns:
            The parsed ``InternalRegulationSeed``; never ``None``.

        Raises:
            InternalSeedError: The document fails the packaged schema or does not parse into
                the seed model.

        Side Effects:
            None; the adapter performs no I/O.
        """
        ...


class IngestInternalStage(Protocol):
    """Call shape of ``internal_seed.persist.ingest_internal_regulatory_instrument``."""

    def __call__(
        self,
        seed: InternalRegulationSeed,
        *,
        submitter: GroupSubmitter,
        native_graph_name: str,
        baseline_graph_name: str,
        audit_event_id: str,
        emitter: LogEmitter | None = None,
    ) -> InternalIngestResult:
        """Validate, mint, and persist one internal-regulation seed.

        Args:
            seed: The parsed submission; declares exactly one regulatory instrument.
            submitter: Graph Write Gateway seam used to read and submit both graphs.
            native_graph_name: Target of the raw submission group.
            baseline_graph_name: Target of the minted spine group.
            audit_event_id: Audit event each group is logged under.
            emitter: Accepted for call-shape parity; ignored.

        Returns:
            The ``InternalIngestResult`` with the instrument id and the per-label node counts.

        Raises:
            InternalSeedError: The document violates a referential or cardinality rule, a graph
                is behind its log or unreadable, or a group submission does not take effect.

        Side Effects:
            Reads both graphs and submits one mutation group to each (native first) through
            ``submitter``.
        """
        ...


class MergeStage(Protocol):
    """Call shape of ``ps_service.company_merge.merge_baseline_graph``."""

    def __call__(  # noqa: PLR0913 -- the two read handles, the gateway seam and the merge's own options; no natural grouping
        self,
        regulatory_instrument_id: str,
        *,
        baseline_graph: GraphHandle,
        single_tenant_graph: GraphHandle,
        submitter: GroupSubmitter,
        single_tenant_graph_name: str,
        baseline_graph_name: str,
        audit_event_id: str,
        embed_model: str,
        merge_threshold: float | None,
        review_threshold: float | None,
        role_review_threshold: float | None,
        call_embedding: EmbeddingCaller | None = None,
        emitter: LogEmitter | None = None,
        governance: GovernanceImport | None = None,
    ) -> MergeResult:
        """Merge one regulation's baseline graph into the single-tenant graph.

        Args:
            regulatory_instrument_id: Id of the instrument whose baseline is merged.
            baseline_graph: Read handle on the baseline graph.
            single_tenant_graph: Read handle on the single-tenant graph.
            submitter: Graph Write Gateway seam; every write is submitted through it as a group
                on a named graph.
            single_tenant_graph_name: Graph the merged groups are submitted to.
            baseline_graph_name: Graph the embedding backfills are submitted to.
            audit_event_id: Audit event the groups are logged under.
            embed_model: Embedding model used for the semantic duplicate comparison.
            merge_threshold: Cosine similarity at or above which a semantic match is
                accepted; ``None`` is a configuration error.
            review_threshold: Cosine similarity at or above which a Capability near miss is
                raised for review; ``None`` is a configuration error.
            role_review_threshold: Cosine similarity at or above which a Role near miss is
                raised for review; ``None`` is a configuration error.
            call_embedding: Embedding caller; ``None`` uses the default.
            emitter: Log emitter; ``None`` uses the default.
            governance: The ingesting caller's owner for an internal baseline's Policy tree;
                ``None`` means ownerless, so a baseline with a Policy tree fails closed.

        Returns:
            The ``MergeResult`` with the merged ids, near-miss pairs and counts.

        Raises:
            Exception: Missing threshold, missing governance owner, embedding failure, read or
                persistence failure; the sequencer wraps it into a ``PipelineStageError``.

        Side Effects:
            Calls the embedding model, submits gateway groups to both graphs (including
            ``PendingReview`` nodes for near misses) and emits log entries.
        """
        ...


# --- injection seam ---


@dataclass(frozen=True, slots=True)
class GraphOpeners:
    """Callables that open the three graphs a pipeline run needs, by ``short_name``.

    Attributes:
        native: Opens the instrument's native graph from the configuration and ``short_name``.
        baseline: Opens the instrument's baseline graph from the configuration and
            ``short_name``.
        single_tenant: Opens the shared single-tenant graph from the configuration.
    """

    native: Callable[[ServiceConfig, str], GraphHandle]
    baseline: Callable[[ServiceConfig, str], GraphHandle]
    single_tenant: Callable[[ServiceConfig], GraphHandle]


@dataclass(frozen=True, slots=True)
class PipelineStages:
    """The four external-pipeline stage entry points, plus the internal-seed pipeline's own stage.

    Policy, Standard and Control nodes are authored directly in an internal-seed document and
    minted by ``ingest_internal`` itself, so there is no separate governance stage. The internal
    pipeline reuses ``merge`` as its second stage: one stage function, two callers.

    Attributes:
        ingest: Fetches and persists the native structural graph (external pipeline stage 1).
        extract: Extracts Roles and Requirements into the baseline graph (stage 2).
        derive: Derives Obligations and Capabilities on the baseline graph (stage 3).
        merge: Merges a baseline graph into the single-tenant graph (stage 4 of both pipelines).
        ingest_internal: Validates and persists an internal-regulation seed (stage 1 of the
            internal pipeline).
    """

    ingest: IngestStage
    extract: ExtractStage
    derive: DeriveStage
    merge: MergeStage
    ingest_internal: IngestInternalStage


@dataclass(frozen=True, slots=True)
class PipelineAdapters:
    """Zero-arg factories for the source-specific adapters the stages consume.

    Attributes:
        ingestion: Builds the adapter that fetches an external instrument's structure.
        mapping: Builds the Domain Mapping Adapter the extraction stage uses.
        internal_seed: Builds the adapter that parses an internal-regulation seed document.
    """

    ingestion: Callable[[], IngestionAdapter]
    mapping: Callable[[], DomainMappingAdapter]
    internal_seed: Callable[[], InternalSeedAdapter]


@dataclass(frozen=True, slots=True)
class PipelineDependencies:
    """Everything the pipeline sequencers need that is not per-request.

    Attributes:
        graphs: Openers for the native, baseline and single-tenant graphs.
        stages: The stage entry points to run.
        adapters: Factories for the source-specific adapters.
    """

    graphs: GraphOpeners
    stages: PipelineStages
    adapters: PipelineAdapters


# --- config-completeness guard ---


@dataclass(frozen=True, slots=True)
class _ResolvedPipelineConfig:
    """The pipeline-relevant ``ServiceConfig`` values, narrowed to non-``None``.

    Attributes:
        chat_model: Chat model identifier for extraction and derivation.
        embed_model: Embedding model identifier for the merge's duplicate comparison.
        merge_threshold: Cosine similarity at or above which two Capabilities merge.
        review_threshold: Cosine similarity at or above which a Capability near miss is raised.
        role_review_threshold: Cosine similarity at or above which a Role near miss is raised.
        single_tenant_graph_name: Graph the merge stage submits its groups to.
    """

    chat_model: str
    embed_model: str
    merge_threshold: float
    review_threshold: float
    role_review_threshold: float
    single_tenant_graph_name: str


def _require_ingestion_config(config: ServiceConfig) -> _ResolvedPipelineConfig:
    """Return the narrowed pipeline config, or raise if any required value is unset.

    Called first by both pipeline sequencers, before any graph or stage call, so an incomplete
    configuration fails as HTTP 503 with no I/O.

    Args:
        config: The resolved service configuration.

    Returns:
        A ``_ResolvedPipelineConfig`` whose fields are all non-``None``.

    Raises:
        IngestionConfigIncompleteError: If ``llm_interface_model``,
            ``llm_interface_embed_model``, or
            ``company_merge_capability_merge_threshold``,
            ``company_merge_capability_review_threshold`` or
            ``company_merge_role_review_threshold`` is ``None``.
    """
    chat_model = config.llm_interface_model
    embed_model = config.llm_interface_embed_model
    threshold = config.company_merge_capability_merge_threshold
    review_threshold = config.company_merge_capability_review_threshold
    role_review_threshold = config.company_merge_role_review_threshold
    if (
        chat_model is None
        or embed_model is None
        or threshold is None
        or review_threshold is None
        or role_review_threshold is None
    ):
        missing = ", ".join(missing_ingestion_config_fields(config))
        message = f"ingestion configuration incomplete: {missing} not set"
        raise IngestionConfigIncompleteError(message)
    return _ResolvedPipelineConfig(
        chat_model=chat_model,
        embed_model=embed_model,
        merge_threshold=threshold,
        review_threshold=review_threshold,
        role_review_threshold=role_review_threshold,
        single_tenant_graph_name=config.falkordb_graph,
    )


# --- short_name derivation for a Cellar-resolved (non-curated) entry ---

_SLUG_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_SLUG_MAX_WORDS = 6


def _derive_short_name(title: str, celex: str) -> str:
    """Derive a ``short_name`` for a Cellar-resolved, non-curated regulation.

    A slug of the Cellar-extracted title (its first ``_SLUG_MAX_WORDS`` words,
    lowercased and ``_``-joined) with the lowercase CELEX appended for
    uniqueness. Only the Cellar fallback derives a name this way; a curated
    ``CatalogEntry.short_name`` is never derived. Pure, no I/O.

    Args:
        title: The regulation's title, as extracted from Cellar/ELI.
        celex: The regulation's CELEX identifier.

    Returns:
        A deterministic, always-valid ``short_name`` component, unique per
        CELEX even if two titles share their first ``_SLUG_MAX_WORDS`` words.
    """
    words = _SLUG_WORD_RE.findall(title)[:_SLUG_MAX_WORDS]
    slug = "_".join(word.lower() for word in words)
    return f"{slug}_{celex.lower()}"


# --- per-stage failure wrapper ---


def _classify_stage_failure(
    name: str, exc: Exception, *, emitter: LogEmitter | None = None
) -> PipelineStageError:
    """Classify one stage failure into a caller-safe ``PipelineStageError``.

    Shared by :func:`_run_stage` and :func:`resolve_via_cellar` (a pre-pipeline step, not itself a
    ``_run_stage``-wrapped stage), so both classify failures identically.

    Args:
        name: The stage name (e.g. ``"extraction"``) for the error and log lines.
        exc: The exception the stage (or stage-equivalent step) raised.
        emitter: Optional log emitter for the server-side failure-detail entry.

    Returns:
        A :class:`PipelineStageError` whose ``reason`` is the scrubbed message
        for a whitelisted domain error, else a generic ``"<name> failed"`` --
        with the scrubbed, truncated message and exception type emitted server-side only.
    """
    if is_safe_verbatim(exc):
        reason = _scrub_text(f"{type(exc).__name__}: {exc}")[:_STAGE_REASON_MAX_LEN]
    else:
        reason = f"{name} failed"
        emit_log_entry(
            component=_COMPONENT,
            action=_RUN_ACTION,
            outcome="failed",
            extra={
                "failing_stage": name,
                "exception_type": type(exc).__name__,
                "detail": stage_failure_detail(exc),
            },
            emitter=emitter,
        )
    return PipelineStageError(stage=name, reason=reason)


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
            ``"<name> failed"`` -- with the scrubbed failure detail emitted server-side
            only.
    """
    try:
        return thunk()
    except Exception as exc:
        raise _classify_stage_failure(name, exc, emitter=emitter) from exc


_REASON_BY_EXCEPTION_NAME: dict[str, IngestionReasonCode] = {
    "IngestionConfigIncompleteError": "config_incomplete",
    "McpGraphUnavailableError": "graph_unavailable",
    "CatalogIdentifierNotFoundError": "celex_not_found",
    "ShortNameCollisionError": "short_name_collision",
    "PipelineStageError": "pipeline_stage_failed",
    "ChangeMonitorStateError": "inconsistent_graph_state",
    "SuccessionPersistenceError": "graph_unavailable",
    "GraphWriteUnavailableError": "graph_unavailable",
}
"""Failure class name -> the enumerated audit `reason_code`.

``McpGraphUnavailableError`` is matched by class name so ``api`` does not import
``mcp_interface`` (the idiom of ``change_check_orchestration._NATIONAL_TRANSPOSITION_ERROR_NAME``);
``ChangeMonitorStateError`` / ``SuccessionPersistenceError`` (the amendment sweep's graph-state
and persistence failures) are matched by name for the same reason (``api`` must not import
``change_monitor`` at module load).
"""


def classify_ingestion_failure(exc: BaseException) -> IngestionReasonCode:
    """Map a failed ingestion to its enumerated audit ``reason_code``.

    Decided by the exception TYPE name only, so no free text, path or host can reach an audit
    row.

    Args:
        exc: The exception that ended the ingestion run.

    Returns:
        The reason code for a known domain failure (configuration incomplete, graph
        unavailable, identifier not found, short-name collision, stage failure, inconsistent
        graph state), else ``"unexpected_error"``. Pure; never raises and never reads the
        message.
    """
    return _REASON_BY_EXCEPTION_NAME.get(type(exc).__name__, "unexpected_error")


# --- Cellar-fallback existence resolution ---


@dataclass(frozen=True, slots=True)
class CellarResolution:
    """A CELEX resolved via Cellar/ELI, plus its fetch-once adapter.

    Public because both ingestion entry points (``routes.create_ingestion`` and the
    ``ingest-regulation`` MCP tool) receive it from :func:`resolve_ingestion_entry`.

    Attributes:
        entry: The ``CatalogEntry`` carrying the CELEX, the fetched title and version, and the
            caller's ``short_name``; graph naming and id construction consume it.
        adapter: A ``CellarEliAdapter`` whose fetch steps replay the bytes already retrieved
            during resolution, so the ingestion stage never fetches the same document again.
    """

    entry: CatalogEntry
    adapter: IngestionAdapter


def resolve_via_cellar(
    celex: str,
    *,
    short_name: str | None = None,
    cellar_fetch: Callable[[str], bytes] | None = None,
    cellar_fetch_rdf: Callable[[str], bytes] | None = None,
) -> CellarResolution:
    """Resolve a CELEX against Cellar/ELI before any pipeline stage runs.

    Fetches the XHTML document once and the RDF/XML metadata document once, extracts the
    bibliographic metadata from both and settles a ``short_name``. The returned adapter replays
    both byte strings, so the ingestion stage issues no further Cellar/ELI request (a third,
    RDF request would silently double the per-resolution cost).

    Args:
        celex: A well-formed CELEX identifier absent from the curated catalog.
        short_name: A caller-supplied name used verbatim. ``None`` (the REST path, which does
            not collect one) derives it from the fetched title, so two resolutions of one CELEX
            with differently worded titles can yield different names; when supplied, nothing is
            derived.
        cellar_fetch: Callable fetching the XHTML document for a CELEX (test seam). ``None``
            resolves the module-level ``fetch_xhtml`` at call time, so a monkeypatch of that
            name takes effect for callers that never pass this argument.
        cellar_fetch_rdf: Callable fetching the RDF/XML document for a CELEX (test seam).
            ``None`` resolves the module-level ``fetch_rdf`` at call time, as above.

    Returns:
        A ``CellarResolution`` pairing the resolved ``CatalogEntry`` with a cached-bytes
        ``CellarEliAdapter``.

    Raises:
        CatalogIdentifierNotFoundError: ``celex`` does not exist on Cellar/ELI (a genuine 404,
            distinguished from an outage).
        PipelineStageError: The Cellar/ELI fetch failed for any other reason, or the fetched
            document could not be turned into valid metadata (an unparseable structure, an
            unsupported CELEX type code, or an empty title); stage ``"ingestion"``, the same
            502 shape an ingestion-stage failure produces.

    Side Effects:
        Two outbound HTTP requests to Cellar/ELI (through the injected callables); on a
        non-whitelisted failure, one failure-detail log entry.
    """
    fetch = cellar_fetch if cellar_fetch is not None else fetch_xhtml
    fetch_rdf_ = cellar_fetch_rdf if cellar_fetch_rdf is not None else fetch_rdf
    try:
        xhtml = fetch(celex)
        rdf = fetch_rdf_(celex)
    except CellarNotFoundError as exc:
        message = f"CELEX {celex!r} does not exist on Cellar/ELI."
        raise CatalogIdentifierNotFoundError(message) from exc
    except Exception as exc:
        raise _classify_stage_failure("ingestion", exc) from exc

    try:
        metadata = extract_metadata(xhtml, rdf, celex)
    except Exception as exc:
        # Broad on purpose, not narrowed to CellarParseError: an empty extracted title does not
        # make extract_metadata return title="" (RegulatoryInstrumentMetadata.title has
        # min_length=1), it raises pydantic.ValidationError. Every extract_metadata failure gets
        # the same stage="ingestion" classification, so a degenerate `_<celex>`-shaped
        # short_name is never derived.
        raise _classify_stage_failure("ingestion", exc) from exc

    resolved_short_name = (
        short_name if short_name is not None else _derive_short_name(metadata.title, celex)
    )
    entry = CatalogEntry(celex, metadata.title, resolved_short_name, metadata.version)
    adapter = CellarEliAdapter(fetch=lambda _identifier: xhtml, fetch_rdf=lambda _identifier: rdf)
    return CellarResolution(entry=entry, adapter=adapter)


def normalize_short_name(short_name: str) -> str:
    """Return the canonical (upper-case) form of a caller-supplied ``short_name``.

    Applied once, at the top of :func:`resolve_ingestion_entry`, so every
    downstream consumer of the ingest path (collision check, id construction,
    the ingest stage, the graph openers) sees one spelling. Deliberately NOT
    applied in the request validators, in ``ingestion/pipeline.py`` or in the
    graph-name builders: restore and the change-check sweep share those, and the
    graph names stay lowercase.

    Args:
        short_name: The caller-supplied short name, any case.

    Returns:
        ``short_name`` upper-cased.
    """
    return short_name.upper()


def resolve_ingestion_entry(
    celex: str,
    short_name: str,
    *,
    single_tenant_graph: GraphHandle,
    emitter: LogEmitter | None = None,
    cellar_fetch: Callable[[str], bytes] | None = None,
    cellar_fetch_rdf: Callable[[str], bytes] | None = None,
) -> CellarResolution:
    """Resolve one ingestion request's identity -- the single shared entry-point function.

    Called by both ``routes.create_ingestion`` and the ``ingest-regulation`` MCP
    tool, so both entry points reach identical outcomes.
    Identity is decided by the live graph and Cellar -- never by the curated
    catalog. Checks run in order, graph first so a request that is doomed by the
    graph never reaches the network:

    1. The CELEX-existence check (:func:`_reject_if_celex_already_ingested`): a
       CELEX already in the graph is rejected under any ``short_name``.
    2. The graph-side ``short_name`` collision check
       (:func:`check_short_name_collision`), wrapped in :func:`_run_stage` so a
       genuine I/O failure fails closed as a :class:`PipelineStageError` rather
       than being read as "no collision". The wrapped thunk only *reads*;
       :class:`ShortNameCollisionError` is raised here, in the caller, because
       ``_run_stage`` would reclassify an exception raised inside the thunk.
    3. :func:`resolve_via_cellar` under the normalized ``short_name``.

    Args:
        celex: The request's CELEX identifier.
        short_name: The request's caller-supplied ``short_name``, any case; upper-cased before
            use.
        single_tenant_graph: The already-opened single-tenant graph, read by the
            collision checks.
        emitter: Log emitter for the identity-check and failure entries; ``None`` uses the
            default.
        cellar_fetch: Cellar/ELI XHTML fetch callable (test seam); ``None`` uses the real fetch.
        cellar_fetch_rdf: Cellar/ELI RDF/XML fetch callable (test seam); ``None`` uses the real
            fetch.

    Returns:
        The ``CellarResolution`` for ``celex`` under the normalized ``short_name``.

    Raises:
        CelexAlreadyIngestedError: ``celex`` is already in the graph.
        ShortNameCollisionError: ``short_name`` is already claimed by a
            different CELEX in the graph.
        CatalogIdentifierNotFoundError: ``celex`` does not exist on Cellar/ELI.
        PipelineStageError: The CELEX check, the collision check or the Cellar
            resolution failed.

    Side Effects:
        Reads the single-tenant graph, makes the Cellar/ELI requests of
        :func:`resolve_via_cellar` and emits one identity-check log entry per outcome.
    """
    short_name = normalize_short_name(short_name)
    _reject_if_celex_already_ingested(
        celex, short_name, single_tenant_graph=single_tenant_graph, emitter=emitter
    )
    _reject_if_short_name_claimed(
        celex, short_name, single_tenant_graph=single_tenant_graph, emitter=emitter
    )
    resolution = resolve_via_cellar(
        celex, short_name=short_name, cellar_fetch=cellar_fetch, cellar_fetch_rdf=cellar_fetch_rdf
    )
    _emit_identity_check("passed", celex, short_name, emitter=emitter)
    return resolution


_CELEX_EXISTS_QUERY = "MATCH (n:RegulatoryInstrument {celex: $celex}) RETURN n.id LIMIT 1"


def _reject_if_celex_already_ingested(
    celex: str,
    short_name: str,
    *,
    single_tenant_graph: GraphHandle,
    emitter: LogEmitter | None,
) -> None:
    """Raise :class:`CelexAlreadyIngestedError` if the graph already holds ``celex``.

    Queries only the passed single-tenant handle, inside
    :func:`_run_stage` so a genuine I/O failure fails closed as a
    ``celex_check`` :class:`PipelineStageError` rather than being read as "not
    ingested". The thunk only reads; the rejection is raised here, in the
    caller, because ``_run_stage`` would reclassify an exception raised inside it.
    """
    try:
        existing_id = _run_stage(
            "celex_check",
            lambda: _find_instrument_id_by_celex(single_tenant_graph, celex),
            emitter=emitter,
        )
    except PipelineStageError:
        _emit_identity_check("failed", celex, short_name, emitter=emitter)
        raise
    if existing_id is not None:
        existing_short_name = existing_id.rpartition("-")[0]
        _emit_identity_check(
            "rejected_celex_exists",
            celex,
            short_name,
            emitter=emitter,
            extra={"existing_short_name": existing_short_name},
        )
        raise CelexAlreadyIngestedError(
            f"CELEX {celex} is already ingested as short_name '{existing_short_name}'"
        )


def _find_instrument_id_by_celex(single_tenant_graph: GraphHandle, celex: str) -> str | None:
    """Return the id of the ``RegulatoryInstrument`` recorded for ``celex``, or ``None``."""
    result = single_tenant_graph.query(_CELEX_EXISTS_QUERY, params={"celex": celex})
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return None
    return cast("str", rows[0][0])


def _reject_if_short_name_claimed(
    celex: str,
    short_name: str,
    *,
    single_tenant_graph: GraphHandle,
    emitter: LogEmitter | None,
) -> None:
    """Raise :class:`ShortNameCollisionError` if a different CELEX already claims ``short_name``."""
    try:
        colliding_celex = _run_stage(
            "collision_check",
            lambda: check_short_name_collision(
                single_tenant_graph, celex=celex, short_name=short_name
            ),
            emitter=emitter,
        )
    except PipelineStageError:
        _emit_identity_check("failed", celex, short_name, emitter=emitter)
        raise
    if colliding_celex is not None:
        _emit_identity_check(
            "rejected_short_name_claimed",
            celex,
            short_name,
            emitter=emitter,
            extra={"conflicting_celex": colliding_celex},
        )
        raise ShortNameCollisionError(
            f"short_name '{short_name}' is already claimed by CELEX {colliding_celex}"
        )


def _emit_identity_check(
    outcome: str,
    celex: str,
    short_name: str,
    *,
    emitter: LogEmitter | None,
    extra: dict[str, object] | None = None,
) -> None:
    """Emit one ``ingestion_identity_check`` log entry.

    Carries only the CELEX, the ``short_name`` and the conflicting CELEX -- never
    raw Cypher, exception text, or another tenant's data. The run id comes from
    the bound request context.

    Args:
        outcome: ``"passed"``, ``"rejected_celex_exists"``,
            ``"rejected_short_name_claimed"`` or ``"failed"``.
        celex: The request's CELEX.
        short_name: The request's ``short_name``.
        emitter: Optional explicit emitter; otherwise the process default.
        extra: Further safe fields, merged over ``celex``/``short_name``.
    """
    emit_log_entry(
        component=_COMPONENT,
        action="ingestion_identity_check",
        outcome=outcome,
        extra={"celex": celex, "short_name": short_name, **(extra or {})},
        emitter=emitter,
    )


# --- run outcome ---


@dataclass(frozen=True, slots=True)
class StageReport:
    """One completed pipeline stage and a small integer summary of what it produced.

    Attributes:
        stage: Stage name: ``"ingestion"``, ``"extraction"``, ``"derivation"`` or ``"merge"``
            (``"ingestion"`` and ``"merge"`` for the internal pipeline).
        summary: Counter name to integer count, as produced by the stage's summary function.
    """

    stage: str
    summary: dict[str, int]


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    """The result of a full ingestion pipeline run.

    Attributes:
        regulatory_instrument_id: The ``{short_name}-{version}`` id the run ingested under.
        source: ``"catalog"`` for a CELEX run, ``"internal"`` for an internal-document run.
        stages: The completed stages in run order; empty for ``"already_ingested"``.
        outcome: ``"fresh"`` (the default) for a run that executed its stages;
            ``"already_ingested"`` (catalog path only) when the pre-flight check found an
            existing fully-merged ``RegulatoryInstrument`` and skipped every stage.
    """

    regulatory_instrument_id: str
    source: Literal["catalog", "internal"]
    stages: tuple[StageReport, ...]
    outcome: Literal["fresh", "already_ingested"] = "fresh"


def _ingestion_summary(result: IngestResult) -> dict[str, int]:
    """Summarise an ``IngestResult`` as small integer counts."""
    return {"verified_labels": len(result.counts)}


def _extraction_summary(result: ExtractionResult) -> dict[str, int]:
    """Summarise an ``ExtractionResult`` as small integer counts."""
    return {
        "roles": len(set(result.role_node_ids.values())),
        "requirements": len(result.requirement_ids),
        "candidates": result.candidate_count,
        "skipped_units": result.skipped_unit_count,
        "roles_minted": result.roles_minted,
        "non_actor_roles_dropped": result.non_actor_roles_dropped,
    }


def _derivation_summary(result: DerivationResult) -> dict[str, int]:
    """Summarise a ``DerivationResult`` as small integer counts."""
    return {
        "obligations": len(result.obligation_node_ids),
        "capabilities": len(result.capability_node_ids),
        "unmatched_requirements": len(result.unmatched_requirement_ids),
        "unmatched_obligations": len(result.unmatched_obligation_ids),
    }


def _merge_summary(result: MergeResult) -> dict[str, int]:
    """Summarise a ``MergeResult`` as small integer counts.

    ``new_obligations``, ``new_capabilities`` and ``matched_capabilities`` are the net-new and
    matched facts ``company_merge`` computed (never approximations); the ingestion audit rows
    carry these three counts. ``obligations`` and ``canonical_capabilities`` are the totals
    processed, ``pending_reviews`` counts the near-miss reviews opened (Capability and Role),
    ``role_near_misses`` is the Role share of those. Consumers iterate the mapping generically,
    so a new key is additive.
    """
    return {
        "obligations": len(result.obligation_ids),
        "canonical_capabilities": len(result.capability_canonical_ids),
        "near_misses": len(result.near_misses),
        "pending_reviews": result.pending_review_count,
        "role_near_misses": result.role_near_miss_count,
        "new_obligations": result.new_obligation_count,
        "new_capabilities": result.new_capability_count,
        "matched_capabilities": result.matched_capability_count,
        "governance_policies": result.governance_policies,
        "governance_standards": result.governance_standards,
        "governance_controls": result.governance_controls,
        "governance_status_overridden": result.governance_status_overridden,
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
    audit_facts: dict[str, object] | None = None,
) -> None:
    """Emit one ``ingestion_run`` log entry.

    Args:
        outcome: ``"started"`` / ``"succeeded"`` / ``"failed"``.
        run_id: The request-scoped run id, carried on every line.
        source_identifier: The catalog CELEX, or the fixed
            ``_INTERNAL_INGESTION_SOURCE_IDENTIFIER`` literal for the
            internal-document path, whose content carries no stable identifier known before
            parsing.
        caller: The requesting client host on REST, or the fixed MCP caller label `"mcp"`
            (``"unknown"`` when neither is known).
        emitter: Optional explicit emitter; otherwise the process default.
        duration_ms: Wall time for the run so far (omitted on ``"started"``).
        failing_stage: The stage that raised, on the ``"failed"`` entry only.
        audit_facts: The ``trigger`` and the three counts the audit rows carry, on the
            ``"succeeded"`` entry of an audited run only, so log and audit agree.
    """
    extra: dict[str, object] = {"source_identifier": source_identifier, "caller": caller}
    if audit_facts is not None:
        extra.update(audit_facts)
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


# --- pre-flight already-merged check ---

_MERGED_INSTRUMENT_EXISTS_QUERY = (
    "MATCH (n:RegulatoryInstrument) WHERE toUpper(n.id) = $id RETURN n.id LIMIT 1"
)


def _is_already_merged(single_tenant_graph: GraphHandle, regulatory_instrument_id: str) -> bool:
    """Return whether a fully-merged ``RegulatoryInstrument`` already exists for this id.

    Company Merge's ``persist_canonical_nodes`` (``company_merge/graph_writer.py``)
    writes a ``RegulatoryInstrument`` node into the single-tenant graph
    unconditionally at the end of every merge run, so its existence there is
    a reliable proxy for "Domain Mapper and Company Merge already
    completed for this exact identifier".

    Args:
        single_tenant_graph: The already-opened single-tenant graph.
        regulatory_instrument_id: The ``{short_name}-{version}`` id the
            catalog pipeline would ingest under. Matched case-insensitively
            (``toUpper(n.id) = $id``), so a legacy lowercase celex-less node is
            found for its upper-case successor id.

    Returns:
        ``True`` if a matching ``RegulatoryInstrument`` node exists.
    """
    result = single_tenant_graph.query(
        _MERGED_INSTRUMENT_EXISTS_QUERY, params={"id": regulatory_instrument_id.upper()}
    )
    rows = cast("list[list[object]]", result.result_set)
    return len(rows) > 0


# --- cross-instrument short_name collision check ---
#
# Two independent `single_tenant` graph opens are accepted for this check plus the
# `_is_already_merged` preflight above: `FalkorDB.__init__` is not lazy -- it calls
# `Is_Cluster(conn)`, which issues a real `conn.info(section="server")` round-trip
# to the FalkorDB/Redis backend. The cost is one extra network round-trip
# (single-digit milliseconds), negligible against the pipeline's end-to-end run time.
# Threading a single handle through both call sites would change
# `run_catalog_ingestion_pipeline`'s public signature for no measurable benefit.

_SHORT_NAME_COLLISION_QUERY = (
    "MATCH (n:RegulatoryInstrument) WHERE toUpper(n.id) STARTS WITH $prefix "
    "AND n.celex IS NOT NULL AND n.celex <> $celex RETURN n.id, n.celex"
)


def check_short_name_collision(
    single_tenant_graph: GraphHandle, *, celex: str, short_name: str
) -> str | None:
    """Return the conflicting CELEX if `short_name` is already claimed by a different one.

    Used before ingestion so two regulations never share one graph naming prefix.

    `STARTS WITH $prefix` is a cheap DB-side candidate filter, not the final answer: a
    differently-named short_name that happens to be a hyphenated extension of this one
    (e.g. stored id "cra-legacy-1.0" when checking short_name "cra") would otherwise
    false-positive. Each candidate's own short-name segment is recovered via the same
    rpartition("-") convention `_internal_short_name` already uses and compared for exact
    equality before being treated as a real collision.

    Args:
        single_tenant_graph: The already-opened single-tenant graph.
        celex: The CELEX the caller supplied -- a candidate sharing this same
            CELEX is not a collision (excluded server-side via `n.celex <> $celex`).
        short_name: The caller-supplied short name to check. Compared
            case-insensitively: it is upper-cased here (idempotent for an
            already-normalized value), as is each stored id (``toUpper``), so
            ``cra`` collides with a recorded ``CRA-1.0``.

    Returns:
        The conflicting CELEX, or ``None`` if no other-CELEX instrument is
        recorded under this short name, ignoring case.

    Raises:
        Exception: The graph query failed (database unreachable); the error type belongs to the
            database client and propagates unchanged.

    Side Effects:
        One read query on the single-tenant graph.
    """
    normalized = normalize_short_name(short_name)
    result = single_tenant_graph.query(
        _SHORT_NAME_COLLISION_QUERY, params={"prefix": f"{normalized}-", "celex": celex}
    )
    rows = cast("list[list[object]]", result.result_set)
    for row in rows:
        candidate_id = cast("str", row[0])
        conflicting_celex = cast("str", row[1])
        recorded_short_name, _separator, _version = candidate_id.rpartition("-")
        if normalize_short_name(recorded_short_name) == normalized:
            return conflicting_celex
    return None


# --- the sequencer ---

_STAGE_ORDER = ("ingestion", "extraction", "derivation", "merge")
"""The stage names, in run order. Mirrors ``change_monitor.models.PIPELINE_STAGES``
(duplicated, not imported: ``api`` must not import ``ps_service.change_monitor`` at
module level; a test pins the two equal)."""


@dataclass(frozen=True, slots=True)
class _OpenGraphs:
    """The three graph handles one pipeline run writes through."""

    native: GraphHandle
    baseline: GraphHandle
    single_tenant: GraphHandle


def _require_graph_writes(graph_writes: GraphWriteAccess | None) -> GraphWriteAccess:
    """Return ``graph_writes``, or raise: an ingest stage never writes a graph without a gateway."""
    if graph_writes is None:
        raise GraphWriteUnavailableError(GRAPH_WRITE_UNAVAILABLE_MESSAGE)
    return graph_writes


def _record_catalog_run_audit(
    system_audit: SystemAuditRecorder,
    *,
    run_id: str,
    entry: CatalogEntry,
    trigger: IngestionTrigger | None,
) -> str:
    """Record the system audit row of one catalog run's graph write and return its id.

    The id anchors the native group in the graph log. Every external path uses this one helper;
    ``resource_id`` is the run id, which joins this row to the rows the entry points write (the
    synchronous opening row, the run store's rows).

    Raises:
        AuditTrailUnavailableError: The row could not be written (HTTP 503); the ingest stage
            has not run and nothing was written to any graph.
    """
    details: dict[str, object] = {
        "status": "started",
        "celex": entry.celex,
        "short_name": entry.short_name,
    }
    if trigger is not None:
        details["trigger"] = trigger
    return system_audit(
        action=INGESTION_RUN_SUBMIT_ACTION,
        resource_type=INGESTION_RUN_RESOURCE_TYPE,
        resource_id=run_id,
        details=details,
    )


class _LazyWriteContext:
    """The gateway and the one system audit event id of a run, resolved on first use.

    Ingestion, extraction, derivation and merge all write a graph under the same audit event; the
    first of them to run resolves the gateway and records the run's one row.
    """

    def __init__(
        self,
        graph_writes: GraphWriteAccess | None,
        *,
        run_id: str,
        entry: CatalogEntry,
        trigger: IngestionTrigger | None,
    ) -> None:
        """Hold what the first call needs; nothing is resolved or recorded yet."""
        self._graph_writes = graph_writes
        self._run_id = run_id
        self._entry = entry
        self._trigger: IngestionTrigger | None = trigger
        self._resolved: tuple[GraphWriteAccess, str] | None = None

    def __call__(self) -> tuple[GraphWriteAccess, str]:
        """Return the gateway access and the audit event id, recording the run's row once.

        Raises:
            GraphWriteUnavailableError: The process has no gateway.
            AuditTrailUnavailableError: The row could not be written.
        """
        if self._resolved is None:
            writes = _require_graph_writes(self._graph_writes)
            audit_event_id = _record_catalog_run_audit(
                writes.system_audit, run_id=self._run_id, entry=self._entry, trigger=self._trigger
            )
            self._resolved = (writes, audit_event_id)
        return self._resolved


def _execute_catalog_stages(  # noqa: PLR0913 -- one run's collaborators plus the stage subset/callback; no natural grouping
    entry: CatalogEntry,
    *,
    run_id: str,
    resolved: _ResolvedPipelineConfig,
    graphs: _OpenGraphs,
    dependencies: PipelineDependencies,
    emitter: LogEmitter | None,
    ingestion_adapter: IngestionAdapter | None = None,
    stages_to_run: Collection[str] | None = None,
    on_stage_complete: Callable[[str], None] | None = None,
    graph_writes: GraphWriteAccess | None = None,
    trigger: IngestionTrigger | None = None,
) -> tuple[str, tuple[StageReport, ...]]:
    """Run ingest -> extract -> derive -> merge, aborting at the first failure.

    Each stage after the first consumes the ``regulatory_instrument_id`` the
    ingest stage returned. The first :func:`_run_stage` to raise a
    ``PipelineStageError`` aborts the sequence -- later stages never run.
    :func:`~ps_service.api.run_status.set_stage` is called for each
    stage name immediately *before* that stage runs, so a concurrent poller of
    ``run_status`` sees "currently executing", never "just completed" -- the
    caller (:func:`run_catalog_ingestion_pipeline`) clears the entry once the
    whole run ends, success or failure.

    Args:
        entry: The catalog entry (curated or Cellar-resolved) being ingested.
        run_id: The request-scoped run id, passed to the ingest stage.
        resolved: The narrowed, non-``None`` pipeline config.
        graphs: The three already-opened graph handles for this run.
        dependencies: The injected stage functions and adapter factories.
        emitter: Optional explicit log emitter; otherwise the process default.
        ingestion_adapter: A pre-built adapter to use for the ingest stage (the fetch-once
            Cellar-fallback adapter); when ``None``, ``dependencies.adapters.ingestion()``
            builds the default one, as the curated path does.
        stages_to_run: The stage names to run (the amendment
            re-ingest resumes at the stages a new version is missing); ``None``
            runs all four. Skipped stages are neither run, ``set_stage``d nor
            reported. When ``ingestion`` is skipped the instrument id is
            ``<short_name>-<version>``, the id the ingest stage would return.
        on_stage_complete: Called with the stage name after each run stage
            returns (never for a stage that raised), before the next stage starts.
        graph_writes: The Graph Write Gateway and system audit recorder the ingest, extract and
            derive stages write their graphs through; required when any of
            ``ingestion``, ``extraction`` or ``derivation`` runs. One system audit row is recorded
            per run, before the first of those stages, and its id is shared by all of them.
        trigger: The audited entry point that started the run, recorded on the system audit row
            the ingest stage's group is logged under; ``None`` leaves it off the row.

    Returns:
        The ``regulatory_instrument_id`` and the :class:`StageReport` tuple of
        the stages that ran, in pipeline order.

    Raises:
        GraphWriteUnavailableError: A stage that writes a graph runs and ``graph_writes`` is
            ``None``.
        AuditTrailUnavailableError: The run's system audit row could not be written; nothing was
            written to any graph.
    """
    selected = frozenset(_STAGE_ORDER if stages_to_run is None else stages_to_run)
    stages = dependencies.stages
    mapping_adapter = dependencies.adapters.mapping()
    reports: list[StageReport] = []
    baseline_graph_name = _baseline_graph_name(entry.short_name)

    def complete(stage: str, summary: dict[str, int]) -> None:
        reports.append(StageReport(stage, summary))
        if on_stage_complete is not None:
            on_stage_complete(stage)

    write_context = _LazyWriteContext(graph_writes, run_id=run_id, entry=entry, trigger=trigger)

    rid = f"{entry.short_name}-{entry.version}"
    if "ingestion" in selected:
        ingest_adapter = (
            dependencies.adapters.ingestion() if ingestion_adapter is None else ingestion_adapter
        )
        set_stage(run_id, "ingestion")
        writes, audit_event_id = write_context()
        ingest_result = _run_stage(
            "ingestion",
            lambda: stages.ingest(
                entry.celex,
                entry.short_name,
                version=entry.version,
                adapter=ingest_adapter,
                graph=graphs.native,
                submitter=writes.gateway,
                audit_event_id=audit_event_id,
                run_id=run_id,
            ),
            emitter=emitter,
        )
        rid = ingest_result.regulatory_instrument_id
        complete("ingestion", _ingestion_summary(ingest_result))
    if "extraction" in selected:
        set_stage(run_id, "extraction")
        writes, audit_event_id = write_context()
        extract_result = _run_stage(
            "extraction",
            lambda: stages.extract(
                rid,
                adapter=mapping_adapter,
                native_graph=graphs.native,
                submitter=writes.gateway,
                baseline_graph_name=baseline_graph_name,
                audit_event_id=audit_event_id,
                model=resolved.chat_model,
            ),
            emitter=emitter,
        )
        complete("extraction", _extraction_summary(extract_result))
    if "derivation" in selected:
        set_stage(run_id, "derivation")
        writes, audit_event_id = write_context()
        derive_result = _run_stage(
            "derivation",
            lambda: stages.derive(
                rid,
                baseline_graph=graphs.baseline,
                submitter=writes.gateway,
                baseline_graph_name=baseline_graph_name,
                audit_event_id=audit_event_id,
                model=resolved.chat_model,
            ),
            emitter=emitter,
        )
        complete("derivation", _derivation_summary(derive_result))
    if "merge" in selected:
        set_stage(run_id, "merge")
        writes, audit_event_id = write_context()
        merge_result = _run_stage(
            "merge",
            lambda: stages.merge(
                rid,
                baseline_graph=graphs.baseline,
                single_tenant_graph=graphs.single_tenant,
                submitter=writes.gateway,
                single_tenant_graph_name=resolved.single_tenant_graph_name,
                baseline_graph_name=baseline_graph_name,
                audit_event_id=audit_event_id,
                embed_model=resolved.embed_model,
                merge_threshold=resolved.merge_threshold,
                review_threshold=resolved.review_threshold,
                role_review_threshold=resolved.role_review_threshold,
            ),
            emitter=emitter,
        )
        complete("merge", _merge_summary(merge_result))
    return rid, tuple(reports)


def _audit_facts(trigger: IngestionTrigger, reports: tuple[StageReport, ...]) -> dict[str, object]:
    """The ``trigger`` and merge-stage counts for the succeeded log line (zero without a merge)."""
    merge: dict[str, int] = {}
    for report in reports:
        if report.stage == "merge":
            merge = report.summary
    counts = IngestionCounts.from_merge_summary(merge)
    return {
        "trigger": trigger,
        "new_obligations": counts.new_obligations,
        "new_capabilities": counts.new_capabilities,
        "matched_capabilities": counts.matched_capabilities,
    }


def run_catalog_ingestion_pipeline(
    entry: CatalogEntry,
    *,
    config: ServiceConfig,
    run_id: str,
    caller: str,
    dependencies: PipelineDependencies,
    emitter: LogEmitter | None = None,
    ingestion_adapter: IngestionAdapter | None = None,
    trigger: IngestionTrigger | None = None,
) -> IngestionOutcome:
    """Run the external ingestion pipeline for one catalog regulation.

    Sequences Ingestion -> Domain Mapper (extract, derive) -> Company Merge in-process. Before
    any stage runs, a pre-flight check looks in the single-tenant graph for an
    already-fully-merged ``RegulatoryInstrument`` with this exact ``{short_name}-{version}`` id;
    if found, no stage runs and the call returns at once with ``outcome="already_ingested"`` and
    an empty ``stages`` tuple. If the check itself fails (for example the graph is unreachable)
    it fails closed as a ``PipelineStageError``, never as "already ingested".

    Business Rules:
        Re-running for the same identifier when no merged instrument exists converges on the
        exact-canonical-identity nodes, but LLM extraction is not deterministic, so a reworded
        Capability can still fragment across re-ingestions. The whole run shares one ``run_id``:
        it is passed into the ingest stage (the only stage that binds its own run context) and
        carried on the ``ingestion_run`` start/end log entries. A stage failure aborts the
        sequence as a ``PipelineStageError`` naming the failing stage, with no path, host or URL
        in its reason. The ``run_status`` entry for this ``run_id`` is cleared unconditionally
        when the run ends, so the registry never grows across a long server uptime (a colliding
        ``run_id`` is last-writer-wins).

    Args:
        entry: The curated catalog entry (CELEX, title, short name, version).
        config: The resolved service configuration.
        run_id: The request-scoped run id.
        caller: The requesting client host on REST, or the fixed MCP caller label `"mcp"`
            (``"unknown"`` when neither is known).
        dependencies: The injected graph openers, stage functions, and adapter
            factories (``build_default_pipeline_dependencies`` in production; a
            fake in fast tests).
        emitter: Optional explicit log emitter; otherwise the process default.
        ingestion_adapter: A pre-built adapter for the ingest stage (the fetch-once
            Cellar-fallback adapter from :func:`resolve_via_cellar`); when ``None``, the
            dependency bundle's default factory builds one, as the curated path does.
        trigger: The audited entry point that started this run; when given, the ``succeeded``
            log line also carries it and the merge-stage counts. The audit rows themselves are
            written by the caller, not here.

    Returns:
        An :class:`IngestionOutcome` with ``source="catalog"``, one
        :class:`StageReport` per completed stage, and ``outcome="fresh"`` --
        or, when the pre-flight check finds an existing merged instrument,
        ``outcome="already_ingested"`` with no stage reports at all.

    Raises:
        IngestionConfigIncompleteError: The configuration is missing an LLM model, embed model
            or Capability merge threshold (HTTP 503); nothing was opened.
        GraphWriteUnavailableError: The process has no graph write gateway installed (HTTP 503).
        AuditTrailUnavailableError: The run's system audit row could not be written (HTTP 503);
            nothing was written to any graph.
        PipelineStageError: Any stage, or the pre-flight check itself, raised (HTTP 502).

    Side Effects:
        Opens the native, baseline and single-tenant graphs; reads and writes graphs through the
        gateway by way of the stages; records one system audit row (before the first writing
        stage); emits ``ingestion_run`` start/end log entries; sets and finally clears the
        ``run_status`` stage for ``run_id``.
    """
    resolved = _require_ingestion_config(config)
    regulatory_instrument_id = f"{entry.short_name}-{entry.version}"
    single_tenant_graph = dependencies.graphs.single_tenant(config)
    already_merged = _run_stage(
        "preflight",
        lambda: _is_already_merged(single_tenant_graph, regulatory_instrument_id),
        emitter=emitter,
    )
    if already_merged:
        _emit_run(
            outcome="already_ingested",
            run_id=run_id,
            source_identifier=entry.celex,
            caller=caller,
            emitter=emitter,
        )
        return IngestionOutcome(
            regulatory_instrument_id=regulatory_instrument_id,
            source="catalog",
            stages=(),
            outcome="already_ingested",
        )
    graph_writes = graph_write_access(config)
    graphs = _OpenGraphs(
        native=dependencies.graphs.native(config, entry.short_name),
        baseline=dependencies.graphs.baseline(config, entry.short_name),
        single_tenant=single_tenant_graph,
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
        try:
            rid, reports = _execute_catalog_stages(
                entry,
                run_id=run_id,
                resolved=resolved,
                graphs=graphs,
                dependencies=dependencies,
                emitter=emitter,
                ingestion_adapter=ingestion_adapter,
                graph_writes=graph_writes,
                trigger=trigger,
            )
        except (PipelineStageError, AuditTrailUnavailableError) as exc:
            _emit_run(
                outcome="failed",
                run_id=run_id,
                source_identifier=entry.celex,
                caller=caller,
                emitter=emitter,
                duration_ms=_elapsed_ms(started),
                failing_stage=exc.stage if isinstance(exc, PipelineStageError) else "system_audit",
            )
            raise
    finally:
        clear_stage(run_id)
    _emit_run(
        outcome="succeeded",
        run_id=run_id,
        source_identifier=entry.celex,
        caller=caller,
        emitter=emitter,
        duration_ms=_elapsed_ms(started),
        audit_facts=None if trigger is None else _audit_facts(trigger, reports),
    )
    return IngestionOutcome(regulatory_instrument_id=rid, source="catalog", stages=reports)


# --- audited synchronous ingestion ---


def _outcome_as_result(outcome: IngestionOutcome) -> dict[str, object]:
    """The accepted-response-shaped dict `IngestionCounts` and the audit builder read."""
    return {
        "regulatory_instrument_id": outcome.regulatory_instrument_id,
        "outcome": outcome.outcome,
        "stages": [{"stage": r.stage, "summary": r.summary} for r in outcome.stages],
    }


def _record_terminal_row(
    audit: AuditContext,
    run_id: str,
    celex: str,
    *,
    result: dict[str, object] | None,
    reason_code: IngestionReasonCode | None,
    emitter: LogEmitter | None,
) -> None:
    """Write the run's one terminal `ingestion_run.complete` row; best-effort."""
    entry = completion_audit_entry(
        status="failed" if reason_code is not None else "succeeded",
        celex=celex,
        trigger="sync_ingest",
        result=result,
        reason_code=reason_code,
    )
    record_follow_up_row(
        audit,
        AuditTarget(INGESTION_RUN_COMPLETE_ACTION, INGESTION_RUN_RESOURCE_TYPE, run_id),
        component=_COMPONENT,
        outcome=entry.outcome,
        details=entry.details,
        emitter=emitter,
    )


def run_audited_catalog_ingestion(
    celex: str,
    short_name: str,
    *,
    config: ServiceConfig,
    run_id: str,
    caller: str,
    dependencies: PipelineDependencies,
    audit: AuditContext,
    emitter: LogEmitter | None = None,
) -> IngestionOutcome:
    """Resolve and run one synchronous catalog ingestion between its two audit rows.

    The shared orchestration of ``POST /ingestions`` and the ``ingest-regulation`` MCP tool.
    An ``ingestion_run.submit`` opening row (``applied``, ``status=started``,
    ``trigger='sync_ingest'``) is written FIRST, before the identity check, and is
    fail-closed: if it cannot be written nothing runs and ``AuditTrailUnavailableError``
    propagates. It is followed by identity resolution (:func:`resolve_ingestion_entry`) and the
    pipeline (:func:`run_catalog_ingestion_pipeline`), then one terminal
    ``ingestion_run.complete`` row carrying the instrument id and the merge-stage counts.
    ``resource_id`` of both rows is ``run_id``.

    Business Rules:
        The terminal row is best-effort: a failed write is logged with the run id and never
        changes the outcome or the raised error. A fresh or pre-flight ``already_ingested`` run
        records ``succeeded``; ``CelexAlreadyIngestedError`` records ``succeeded`` with
        ``outcome='already_ingested'`` and zero counts, then is re-raised; any other exception
        records ``failed`` with an enumerated ``reason_code`` (no message, trace or path) and is
        re-raised unchanged. The unaudited pipeline function stays public for the async worker,
        whose rows are written by the run store.

    Args:
        celex: The request's CELEX identifier.
        short_name: The request's caller-supplied ``short_name`` (any case).
        config: The resolved service configuration.
        run_id: The effective run id (the rows' ``resource_id``).
        caller: The requesting client host (REST) or the fixed MCP caller label, for the run log.
        dependencies: The injected graph openers, stages and adapters.
        audit: Who is acting and where the rows go.
        emitter: Optional explicit log emitter.

    Returns:
        The pipeline's :class:`IngestionOutcome`, unchanged.

    Raises:
        AuditTrailUnavailableError: The opening row could not be written, or the pipeline's
            system audit row could not; nothing ran or nothing was written to a graph.
        CelexAlreadyIngestedError: ``celex`` is already in the graph.
        ShortNameCollisionError: ``short_name`` is claimed by a different CELEX.
        CatalogIdentifierNotFoundError: ``celex`` does not exist on Cellar/ELI.
        IngestionConfigIncompleteError: The pipeline configuration is incomplete.
        GraphWriteUnavailableError: The process has no graph write gateway.
        PipelineStageError: A stage, or the identity/pre-flight reads, failed.

    Side Effects:
        Writes the opening and terminal audit rows; everything
        :func:`run_catalog_ingestion_pipeline` and :func:`resolve_ingestion_entry` do (graph
        writes, Cellar/ELI requests, log entries).
    """
    submission = submission_audit_entry(
        celex=celex, short_name=normalize_short_name(short_name), trigger="sync_ingest"
    )
    with bind_run_context(run_id):
        record_opening_row(
            audit,
            AuditTarget(INGESTION_RUN_SUBMIT_ACTION, INGESTION_RUN_RESOURCE_TYPE, run_id),
            component=_COMPONENT,
            details=submission.details,
            emitter=emitter,
        )
        try:
            outcome = _resolve_and_run(
                celex,
                short_name,
                config=config,
                run_id=run_id,
                caller=caller,
                dependencies=dependencies,
                emitter=emitter,
            )
        except CelexAlreadyIngestedError:
            # The request was understood and the instrument is already there: a succeeded run
            # that changed nothing; the caller still gets the existing rejection.
            _record_terminal_row(
                audit,
                run_id,
                celex,
                result={"outcome": "already_ingested"},
                reason_code=None,
                emitter=emitter,
            )
            raise
        except Exception as exc:
            _record_terminal_row(
                audit,
                run_id,
                celex,
                result=None,
                reason_code=classify_ingestion_failure(exc),
                emitter=emitter,
            )
            raise
        _record_terminal_row(
            audit,
            run_id,
            celex,
            result=_outcome_as_result(outcome),
            reason_code=None,
            emitter=emitter,
        )
        return outcome


def _resolve_and_run(
    celex: str,
    short_name: str,
    *,
    config: ServiceConfig,
    run_id: str,
    caller: str,
    dependencies: PipelineDependencies,
    emitter: LogEmitter | None,
) -> IngestionOutcome:
    """Resolve the request's identity, then run the unaudited catalog pipeline."""
    resolution = resolve_ingestion_entry(
        celex,
        short_name,
        single_tenant_graph=dependencies.graphs.single_tenant(config),
        emitter=emitter,
    )
    return run_catalog_ingestion_pipeline(
        resolution.entry,
        config=config,
        run_id=run_id,
        caller=caller,
        dependencies=dependencies,
        emitter=emitter,
        ingestion_adapter=resolution.adapter,
        trigger="sync_ingest",
    )


# --- internal-seed pipeline ---


def _internal_ingestion_summary(result: InternalIngestResult) -> dict[str, int]:
    """Summarise an ``InternalIngestResult`` as small integer counts.

    An internal seed skips extraction, so no non-actor candidate can be dropped:
    ``non_actor_roles_dropped`` is 0 by construction.
    """
    return {
        "roles": result.role_count,
        "roles_minted": result.role_count,
        "non_actor_roles_dropped": 0,
        "requirements": result.requirement_count,
        "obligations": result.obligation_count,
        "capabilities": result.capability_count,
        "policies": result.policy_count,
        "standards": result.standard_count,
        "controls": result.control_count,
        "practice_areas": result.practice_area_count,
        "risk_paths": result.risk_path_count,
    }


def _internal_short_name(regulatory_instrument_id: str) -> str:
    """``{SHORT}`` from a ``{SHORT}-{VERSION}`` internal RegulatoryInstrument id.

    Mirrors the intake format's own ``{SHORT}-{VERSION}`` natural-key
    pattern; ``native_graph_name``/``baseline_graph_name`` lowercase
    it themselves, so no case handling is needed here. Splits on the
    *last* ``-`` so a short name that itself contains a hyphen (unusual but
    not forbidden) is not truncated early -- only the version segment is
    discarded.

    Args:
        regulatory_instrument_id: The seed's own ``RegulatoryInstrument.id``.

    Returns:
        The ``{SHORT}`` prefix.

    Raises:
        InternalSeedValidationError: ``regulatory_instrument_id`` has no
            ``-`` separator at all.
    """
    short_name, separator, _version = regulatory_instrument_id.rpartition("-")
    if not separator or not short_name:
        raise InternalSeedValidationError(
            f"RegulatoryInstrument id {regulatory_instrument_id!r} is not in "
            "the expected '{SHORT}-{VERSION}' shape"
        )
    return short_name


def _read_internal_seed_and_short_name(
    adapter: InternalSeedAdapter, document: dict[str, object]
) -> tuple[InternalRegulationSeed, str]:
    """Parse ``document`` and derive the graph ``short_name`` for it.

    Runs before any pipeline stage and before any graph is opened (no I/O until validated):
    both a schema/shape violation from ``adapter.parse_seed`` and a
    missing/duplicate ``RegulatoryInstrument`` node from ``find_regulatory_instrument``
    are translated to ``InternalSeedValidationError`` (422) here, distinct from a
    later ``PipelineStageError`` (502) a genuine stage failure would raise.
    """
    try:
        seed = adapter.parse_seed(document)
        short_name = _internal_short_name(find_regulatory_instrument(seed).id)
    except InternalSeedError as exc:
        raise InternalSeedValidationError(str(exc)) from exc
    return seed, short_name


def _require_owner_for_policies(seed: InternalRegulationSeed, owner: Owner | None) -> None:
    """Refuse a seed that carries a Policy when the caller has no verified identity.

    An imported Policy tree is created ``draft`` and owned by the ingesting caller;
    without an owner the draft would be unreachable by the propose/edit flow. Runs before any
    audit row, stage, or graph write. A seed with no Policy is not gated.

    Raises:
        InternalIngestOwnerRequiredError: ``owner`` is ``None`` and the seed holds a Policy.
    """
    if owner is None and any(node.label == "Policy" for node in seed.nodes):
        raise InternalIngestOwnerRequiredError(_OWNER_REQUIRED_MESSAGE)


def _require_graph_write_gateway(gateway: GroupSubmitter | None) -> GroupSubmitter:
    """Return ``gateway``, or raise if the service has none (fail closed).

    Called before any graph is opened. There is deliberately no fallback to a direct FalkorDB
    write: a deployment without a gateway (no PS state Postgres) does not ingest.

    Raises:
        GraphWriteUnavailableError: ``gateway`` is ``None``.
    """
    if gateway is None:
        raise GraphWriteUnavailableError(GRAPH_WRITE_UNAVAILABLE_MESSAGE)
    return gateway


def _record_internal_run_audit(
    system_audit: SystemAuditRecorder, *, run_id: str, short_name: str
) -> str:
    """Record the system audit row of one internal run and return its id (the groups' anchor).

    Raises:
        AuditTrailUnavailableError: The row could not be written; nothing has been written to
            any graph (the caller does not run the ingest stage).
    """
    return system_audit(
        action=INGESTION_RUN_SUBMIT_ACTION,
        resource_type=INGESTION_RUN_RESOURCE_TYPE,
        resource_id=run_id,
        details={
            "status": "started",
            "trigger": _INTERNAL_RUN_AUDIT_TRIGGER,
            "short_name": short_name,
        },
    )


def _native_graph_name(short_name: str) -> str:
    """The ``{short}_native`` graph name, the target of the seed's native graph group."""
    from ps_service.ingestion.falkordb_client import (  # noqa: PLC0415 -- function-local keeps ps_service.main off the pipeline at import
        native_graph_name,
    )

    return native_graph_name(short_name)


def _baseline_graph_name(short_name: str) -> str:
    """The ``{short}_baseline`` graph name, the target of the seed's baseline graph group."""
    from ps_service.domain_mapper.falkordb_client import (  # noqa: PLC0415 -- function-local keeps ps_service.main off Domain Mapper at import
        baseline_graph_name,
    )

    return baseline_graph_name(short_name)


def run_internal_ingestion_pipeline(  # noqa: PLR0913 -- one run's collaborators plus the caller identity; no natural grouping
    document: dict[str, object],
    *,
    config: ServiceConfig,
    run_id: str,
    caller: str,
    dependencies: PipelineDependencies,
    gateway: GroupSubmitter | None,
    system_audit: SystemAuditRecorder,
    owner: Owner | None = None,
    emitter: LogEmitter | None = None,
) -> IngestionOutcome:
    """Run the internal-seed ingestion pipeline for one already-parsed document.

    Two stages in sequence: ``internal_ingestion`` (read, validate, mint and persist, including
    the Policy/Standard/Control layer authored in the document) then ``merge``
    (``merge_baseline_graph``, which merges the internal baseline's spine and governance layer
    into the configured single-tenant graph, the stage function the catalog pipeline also
    uses). Each stage is wrapped by :func:`_run_stage`, so a failure in either aborts the
    sequence and names the failing stage.

    Business Rules:
        The seed is parsed (an invalid document raises :class:`InternalSeedValidationError`)
        before any graph is opened, because the ``{short}_baseline`` and ``{short}_native`` graph
        names derive from the seed's own ``RegulatoryInstrument.id``; unlike the catalog path,
        the short name is unknown until the document is parsed. The merge needs the Company
        Capability merge threshold and an embedding model, so :func:`_require_ingestion_config`
        runs first, before any graph is opened or stage runs, giving the same
        HTTP-503-before-any-I/O guarantee as the catalog pipeline. The gateway is required before
        anything else is opened.

    Args:
        document: The already-parsed intake document (the request body's ``content`` field),
            carried in the request and never resolved against the service's filesystem.
        config: The resolved service configuration.
        run_id: The request-scoped run id.
        caller: The requesting client host, or ``"unknown"``.
        dependencies: The injected graph openers, stage functions, and
            adapter factories (``build_default_pipeline_dependencies`` in
            production; a fake in fast tests).
        gateway: The Graph Write Gateway the seed's graph writes are submitted through, or
            ``None`` if the service has none -- then the run fails closed, never writing a
            graph directly.
        system_audit: Records the system audit row (actor ``system:ingestion``) the run's graph
            groups are logged under; the internal path has no principal of its own.
        owner: The ingesting caller's verified ``(sub, iss)``, or ``None`` with no verified
            identity; a seed carrying a Policy is refused without it.
        emitter: Optional explicit log emitter; otherwise the process default.

    Returns:
        An :class:`IngestionOutcome` with ``source="internal"`` and two
        :class:`StageReport` entries (``internal_ingestion`` then ``merge``).

    Raises:
        IngestionConfigIncompleteError: If the configuration is missing an
            LLM model / embed model / Capability merge threshold (HTTP 503).
        InternalIngestOwnerRequiredError: The seed carries a Policy and ``owner`` is ``None``
            (HTTP 401); nothing ran.
        GraphWriteUnavailableError: ``gateway`` is ``None`` (HTTP 503); nothing ran.
        AuditTrailUnavailableError: The system audit row could not be written (HTTP 503);
            nothing was written to any graph.
        InternalSeedValidationError: The seed document fails structural or
            shape validation (422), or its ``RegulatoryInstrument.id`` is
            not in the expected ``{SHORT}-{VERSION}`` shape.
        PipelineStageError: The ``internal_ingestion`` or ``merge`` stage
            raises for any other reason, for example a referential-integrity or
            cardinality violation, or a FalkorDB write failure (502).

    Side Effects:
        Opens the baseline and single-tenant graphs, records one system audit row, writes the
        native and baseline graphs and the single-tenant graph through ``gateway``, emits
        ``ingestion_run`` start/end log entries, and sets and finally clears the ``run_status``
        stage for ``run_id``.
    """
    resolved = _require_ingestion_config(config)
    submitter = _require_graph_write_gateway(gateway)
    adapter = dependencies.adapters.internal_seed()
    started = time.perf_counter()
    _emit_run(
        outcome="started",
        run_id=run_id,
        source_identifier=_INTERNAL_INGESTION_SOURCE_IDENTIFIER,
        caller=caller,
        emitter=emitter,
    )
    try:
        try:
            seed, short_name = _read_internal_seed_and_short_name(adapter, document)
            _require_owner_for_policies(seed, owner)
            baseline_graph = dependencies.graphs.baseline(config, short_name)
            single_tenant_graph = dependencies.graphs.single_tenant(config)
            audit_event_id = _record_internal_run_audit(
                system_audit, run_id=run_id, short_name=short_name
            )
            set_stage(run_id, "internal_ingestion")
            ingest_result = _run_stage(
                "internal_ingestion",
                lambda: dependencies.stages.ingest_internal(
                    seed,
                    submitter=submitter,
                    native_graph_name=_native_graph_name(short_name),
                    baseline_graph_name=_baseline_graph_name(short_name),
                    audit_event_id=audit_event_id,
                    emitter=emitter,
                ),
                emitter=emitter,
            )
            rid = ingest_result.regulatory_instrument_id
            set_stage(run_id, "merge")
            merge_result = _run_stage(
                "merge",
                lambda: dependencies.stages.merge(
                    rid,
                    baseline_graph=baseline_graph,
                    single_tenant_graph=single_tenant_graph,
                    submitter=submitter,
                    single_tenant_graph_name=resolved.single_tenant_graph_name,
                    baseline_graph_name=_baseline_graph_name(short_name),
                    audit_event_id=audit_event_id,
                    embed_model=resolved.embed_model,
                    merge_threshold=resolved.merge_threshold,
                    review_threshold=resolved.review_threshold,
                    role_review_threshold=resolved.role_review_threshold,
                    emitter=emitter,
                    governance=GovernanceImport(owner=owner),
                ),
                emitter=emitter,
            )
        except (PipelineStageError, AuditTrailUnavailableError) as exc:
            _emit_run(
                outcome="failed",
                run_id=run_id,
                source_identifier=_INTERNAL_INGESTION_SOURCE_IDENTIFIER,
                caller=caller,
                emitter=emitter,
                duration_ms=_elapsed_ms(started),
                failing_stage=exc.stage if isinstance(exc, PipelineStageError) else "system_audit",
            )
            raise
    finally:
        clear_stage(run_id)
    _emit_run(
        outcome="succeeded",
        run_id=run_id,
        source_identifier=_INTERNAL_INGESTION_SOURCE_IDENTIFIER,
        caller=caller,
        emitter=emitter,
        duration_ms=_elapsed_ms(started),
        audit_facts=dict(_merge_summary(merge_result)),
    )
    return IngestionOutcome(
        regulatory_instrument_id=rid,
        source="internal",
        stages=(
            StageReport("internal_ingestion", _internal_ingestion_summary(ingest_result)),
            StageReport("merge", _merge_summary(merge_result)),
        ),
    )


# --- default wiring: every pipeline import below is function-local ---


def _open_native_graph(config: ServiceConfig, short_name: str) -> GraphHandle:
    """Open the ``{short}_native`` graph for ``short_name``."""
    from ps_service.ingestion.falkordb_client import (  # noqa: PLC0415 -- function-local keeps ps_service.main off the pipeline at import
        connect_from_config,
        native_graph_name,
        select_graph,
    )

    return select_graph(connect_from_config(config), native_graph_name(short_name))


def _open_baseline_graph(config: ServiceConfig, short_name: str) -> GraphHandle:
    """Open the ``{short}_baseline`` graph for ``short_name``."""
    from ps_service.domain_mapper.falkordb_client import (  # noqa: PLC0415 -- function-local keeps ps_service.main off Domain Mapper at import
        baseline_graph_name,
        connect_from_config,
        select_graph,
    )

    return select_graph(connect_from_config(config), baseline_graph_name(short_name))


def _open_single_tenant_graph(config: ServiceConfig) -> GraphHandle:
    """Open the single-tenant graph named by ``config.falkordb_graph``."""
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- function-local keeps ps_service.main off Company Merge at import
        connect_from_config,
        select_graph,
    )

    return select_graph(connect_from_config(config), config.falkordb_graph)


def _default_ingestion_adapter() -> MetadataFetchingAdapter:
    """Build the default (Cellar/ELI) Ingestion Adapter."""
    from ps_service.ingestion.adapters.cellar_eli.adapter import (  # noqa: PLC0415 -- function-local
        CellarEliAdapter,
    )

    return CellarEliAdapter()


def _default_mapping_adapter() -> DomainMappingAdapter:
    """Build the default (Cellar/ELI) Domain Mapping Adapter."""
    from ps_service.domain_mapper.adapters.cellar_eli import (  # noqa: PLC0415 -- function-local
        CellarEliDomainMappingAdapter,
    )

    return CellarEliDomainMappingAdapter()


def _default_internal_seed_adapter() -> InternalSeedAdapter:
    """Build the default internal-seed Ingestion Adapter."""
    from ps_service.ingestion.adapters.internal_seed.adapter import (  # noqa: PLC0415 -- mirrors _default_ingestion_adapter's own local-import style
        InternalSeedIngestionAdapter,
    )

    return InternalSeedIngestionAdapter()


def build_default_graph_openers() -> GraphOpeners:
    """Wire the real shipped FalkorDB graph openers into a ``GraphOpeners``.

    This is the only part of :func:`build_default_pipeline_dependencies` a caller may
    substitute: the true infrastructure boundary (three FalkorDB client constructions), never
    the stage and adapter business logic above it. It is its own module-level name so that a
    caller-side ``monkeypatch.setattr("ps_service.api.ingestion_orchestration.
    build_default_graph_openers", ...)`` swaps the graphs alone; ``build_default_pipeline_
    dependencies`` resolves this name at call time (ordinary late binding of a module-level
    call) and picks the substitute up. ``docs/coding-standards/approved-mock-boundaries.yaml``
    lists this function as the approved boundary.

    Returns:
        A :class:`GraphOpeners` bound to the production FalkorDB openers. The openers connect
        lazily: no connection is made until one is called.

    Side Effects:
        None; building the bundle opens no connection and cannot fail.
    """
    return GraphOpeners(
        native=_open_native_graph,
        baseline=_open_baseline_graph,
        single_tenant=_open_single_tenant_graph,
    )


def build_default_pipeline_dependencies(
    *, graphs: GraphOpeners | None = None
) -> PipelineDependencies:
    """Wire the real shipped pipeline entry points into a ``PipelineDependencies``.

    Every stage entry point, adapter class, and FalkorDB client is imported
    **function-locally** (here and in the opener helpers) so that importing
    ``ps_service.main`` never transitively loads Domain Mapper or Company Merge at
    module load.

    Business Rules:
        ``graphs`` is the only substitutable part. ``PipelineStages`` and ``PipelineAdapters`` are
        always the real implementations, so a caller cannot replace Domain Mapper, Company Merge
        or Ingestion engine logic through this signature; only the infrastructure boundary
        beneath them is replaceable.

    Args:
        graphs: Graph openers to use instead of the real FalkorDB openers. ``None`` (the default,
            used by every production caller) builds them with
            :func:`build_default_graph_openers`.

    Returns:
        A :class:`PipelineDependencies` bound to the production stage functions,
        the given or default graph openers, and adapter factories.

    Raises:
        ImportError: A pipeline component cannot be imported.

    Side Effects:
        Imports the Ingestion, Domain Mapper and Company Merge components on first call.
    """
    from ps_service.company_merge import merge_baseline_graph  # noqa: PLC0415 -- function-local
    from ps_service.domain_mapper import (  # noqa: PLC0415 -- function-local
        derive_obligations_and_capabilities,
        extract_roles_and_requirements,
    )
    from ps_service.ingestion import (  # noqa: PLC0415 -- function-local
        ingest_regulatory_instrument,
    )
    from ps_service.ingestion.adapters.internal_seed.persist import (  # noqa: PLC0415 -- mirrors the other stage imports' local-import style
        ingest_internal_regulatory_instrument,
    )

    return PipelineDependencies(
        graphs=graphs if graphs is not None else build_default_graph_openers(),
        stages=PipelineStages(
            ingest=ingest_regulatory_instrument,
            extract=extract_roles_and_requirements,
            derive=derive_obligations_and_capabilities,
            merge=merge_baseline_graph,
            ingest_internal=ingest_internal_regulatory_instrument,
        ),
        adapters=PipelineAdapters(
            ingestion=_default_ingestion_adapter,
            mapping=_default_mapping_adapter,
            internal_seed=_default_internal_seed_adapter,
        ),
    )
