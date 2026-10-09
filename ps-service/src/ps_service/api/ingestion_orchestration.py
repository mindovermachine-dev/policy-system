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
* :func:`resolve_via_cellar` -- the pre-pipeline Cellar/ELI existence-and-metadata
  fallback for a CELEX absent from the curated catalog (AC-BI-003/004/005/006/007);
* :func:`run_catalog_ingestion_pipeline` -- the sequencer itself, which also drives
  :mod:`ps_service.api.run_status` (via :func:`_execute_catalog_stages`) so a
  concurrent poller can read the currently-executing stage of an in-flight run
  (AC-BI-008).

No ``falkordb`` import ever crosses into ``ps_service.api``: graphs are opened
through the injected :class:`GraphOpeners` callables and handled via the local
:class:`GraphHandle` Protocol, which the three components' near-duplicate handle
types satisfy structurally.

The internal-document pipeline (request ``source: internal``) is issue #54; this
module covers the catalog path only.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast

from ps_service.api.catalog_entry import CatalogEntry
from ps_service.api.error_handlers import (
    _scrub_text,  # pyright: ignore[reportPrivateUsage]  # shared scrubber; IMPL_4 deviation 1 sanctions reuse
    is_safe_verbatim,
)
from ps_service.api.errors import (
    CatalogIdentifierNotFoundError,
    CelexAlreadyIngestedError,
    IngestionConfigIncompleteError,
    InternalSeedValidationError,
    PipelineStageError,
    ShortNameCollisionError,
)
from ps_service.api.run_status import clear_stage, set_stage
from ps_service.audit.emit import AuditTarget, record_follow_up_row, record_opening_row
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

    from ps_service.audit.emit import AuditContext
    from ps_service.change_monitor.trigger import MetadataFetchingAdapter
    from ps_service.company_merge.models import MergeResult
    from ps_service.config import ServiceConfig
    from ps_service.domain_mapper.adapters.base import DomainMappingAdapter
    from ps_service.domain_mapper.models import DerivationResult, ExtractionResult
    from ps_service.ingestion.adapters.base import IngestionAdapter
    from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed
    from ps_service.ingestion.adapters.internal_seed.persist import InternalIngestResult
    from ps_service.ingestion.models import IngestResult
    from ps_service.llm_interface.client import CompletionCaller, EmbeddingCaller
    from ps_service.logging import LogEmitter

# D-SHORTNAME-PATTERN: must start with a letter, alnum/`_`/`-` body, 1-64 chars --
# mirrors every existing catalog short_name ("cra", "gdpr", "nis2"). Public (not
# module-private) so both this module's own `CatalogIngestionRequest` `Field()` and
# `mcp_interface.mcp_server`'s tool-parameter `Field()` share one canonical pattern
# literal. Per AC-BI-005/CHANGES.md row F4, "one shared validation function" is
# satisfied at this constant/regex level -- one literal, consumed by two independent
# declarative `Field(pattern=...)` annotations -- rather than via a single shared
# callable; the two call sites are pydantic `Field` declarations, not imperative code
# paths a function could usefully wrap.
SHORT_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"

_COMPONENT = "api"
_RUN_ACTION = "ingestion_run"
_STAGE_REASON_MAX_LEN = 300
_INTERNAL_INGESTION_SOURCE_IDENTIFIER = "internal-document"
"""Fixed ``source_identifier`` for every internal-ingestion ``_emit_run`` call
(D7, issue #91) -- there is no filesystem path any more to use as this value,
and the parsed document's own id is not known until after parsing (the
"started" log line fires before then)."""


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


class InternalSeedAdapter(Protocol):
    """Call shape of ``InternalSeedIngestionAdapter.parse_seed``."""

    def parse_seed(self, document: dict[str, object]) -> InternalRegulationSeed:
        """Validate and parse one already-read internal-regulation seed document."""
        ...


class IngestInternalStage(Protocol):
    """Call shape of ``internal_seed.persist.ingest_internal_regulatory_instrument``."""

    def __call__(
        self,
        seed: InternalRegulationSeed,
        *,
        baseline_graph: GraphHandle,
        native_graph: GraphHandle,
        emitter: LogEmitter | None = None,
    ) -> InternalIngestResult:
        """Validate, mint, and persist one internal-regulation seed."""
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
    """The four external-pipeline stage entry points, plus the internal-seed pipeline's own stage.

    In run order. S2 added ``ingest_internal``. GH #76 removed S3 (issue
    #54)'s ``derive_governance`` stage outright -- Policy/Standard/Control
    are now authored directly in the internal-seed document and minted by
    ``ingest_internal`` itself, so there is no separate governance-derivation
    stage any more. S4 reuses the catalog pipeline's own ``merge`` as the
    internal pipeline's second stage -- one shared stage function, two
    callers.
    """

    ingest: IngestStage
    extract: ExtractStage
    derive: DeriveStage
    merge: MergeStage
    ingest_internal: IngestInternalStage


@dataclass(frozen=True, slots=True)
class PipelineAdapters:
    """Zero-arg factories for the source-specific adapters the stages consume."""

    ingestion: Callable[[], IngestionAdapter]
    mapping: Callable[[], DomainMappingAdapter]
    internal_seed: Callable[[], InternalSeedAdapter]


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


def _require_ingestion_config(config: ServiceConfig) -> _ResolvedPipelineConfig:
    """Return the narrowed pipeline config, or raise if any required value is unset.

    Called first by :func:`run_catalog_ingestion_pipeline` and (issue #54, S3)
    :func:`run_internal_ingestion_pipeline`, before any graph or
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
        missing = ", ".join(missing_ingestion_config_fields(config))
        message = f"ingestion configuration incomplete: {missing} not set"
        raise IngestionConfigIncompleteError(message)
    return _ResolvedPipelineConfig(
        chat_model=chat_model, embed_model=embed_model, similarity_threshold=threshold
    )


# --- short_name derivation for a Cellar-resolved (non-curated) entry (AC-BI-004) ---

_SLUG_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_SLUG_MAX_WORDS = 6


def _derive_short_name(title: str, celex: str) -> str:
    """Derive a ``short_name`` for a Cellar-resolved, non-curated regulation.

    A slug of the Cellar-extracted title (its first ``_SLUG_MAX_WORDS`` words,
    lowercased and ``_``-joined) with the lowercase CELEX appended for
    uniqueness -- the concrete slugging function for the rule already decided
    for AC-BI-004 (a curated ``CatalogEntry.short_name`` is never derived this
    way; this is the Cellar-fallback path only). Pure -- no I/O.

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


# --- per-stage failure wrapper (m2 / M5) ---


def _classify_stage_failure(
    name: str, exc: Exception, *, emitter: LogEmitter | None = None
) -> PipelineStageError:
    """Classify one stage failure into a caller-safe ``PipelineStageError``.

    Extracted from :func:`_run_stage` so :func:`resolve_via_cellar` (a
    pre-pipeline step, not itself a ``_run_stage``-wrapped stage) can reuse the
    exact same classification for its own Cellar-fetch/parse failures.

    Args:
        name: The stage name (e.g. ``"extraction"``) for the error and log lines.
        exc: The exception the stage (or stage-equivalent step) raised.
        emitter: Optional log emitter for the server-side failure-detail entry.

    Returns:
        A :class:`PipelineStageError` whose ``reason`` is the scrubbed message
        for a whitelisted domain error, else a generic ``"<name> failed"`` --
        with the full ``repr`` emitted server-side only (AC-BI-009).
    """
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
            ``"<name> failed"`` -- with the full ``repr`` emitted server-side
            only (AC-BI-009).
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
}
"""Failure class name -> the enumerated audit `reason_code` (issue #195, AC-BI-010).

``McpGraphUnavailableError`` is matched by class name so ``api`` does not import
``mcp_interface`` (the idiom of ``change_check_orchestration._NATIONAL_TRANSPOSITION_ERROR_NAME``);
``ChangeMonitorStateError`` / ``SuccessionPersistenceError`` (the amendment sweep's graph-state
and persistence failures) are matched by name for the same reason (``api`` must not import
``change_monitor`` at module load, M6).
"""


def classify_ingestion_failure(exc: BaseException) -> IngestionReasonCode:
    """Map a failed ingestion to its enumerated ``reason_code`` (pure; never reads the message).

    Decided by the exception TYPE only, so no free text, path or host can reach an audit row.
    Anything that is not one of the known domain failures is ``unexpected_error``.
    """
    return _REASON_BY_EXCEPTION_NAME.get(type(exc).__name__, "unexpected_error")


# --- Cellar-fallback existence resolution (D1/D2/D3, AC-BI-003/004/005/006/007) ---


@dataclass(frozen=True, slots=True)
class CellarResolution:
    """A CELEX resolved via Cellar/ELI, plus its fetch-once adapter.

    ``entry`` is a :class:`CatalogEntry` carrying the CELEX, the fetched title and
    version, and the caller's ``short_name``, which downstream code
    (``_execute_catalog_stages``, graph naming, id construction) consumes.
    Public because both ingestion entry points (``routes.create_ingestion`` and the
    ``ingest_regulation`` MCP tool) receive it from
    :func:`resolve_ingestion_entry`. ``adapter`` is a
    :class:`CellarEliAdapter` whose fetch step replays the bytes already
    retrieved during resolution -- the AC-BI-006 fetch-once mechanism (D2).
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
    """Resolve a CELEX against Cellar/ELI.

    Fetches the XHTML document once (``cellar_fetch``) and the RDF/XML
    metadata document once (``cellar_fetch_rdf``), extracts bibliographic
    metadata from both, and resolves a ``short_name`` -- all *before* the
    pipeline ever runs (D1), so the resolved :class:`CatalogEntry` is ready
    for ``run_catalog_ingestion_pipeline``. The returned adapter's fetch steps
    replay both already-fetched byte strings, so Stage 1 never re-fetches
    either (D2, AC-BI-006) -- proven by counting fakes in this function's
    own tests, not just documented here. Without this second cached-bytes
    replay, Stage 1 would issue a third real Cellar/ELI request for the RDF
    document, silently doubling the per-resolution request cost
    (PLAN_REVISED.md §6 item 2).

    Args:
        celex: A well-formed CELEX identifier absent from the curated
            catalog.
        short_name: A caller-supplied ``short_name`` to use verbatim instead
            of deriving one (issue #96, issue #126 CHANGES.md Appendix C1).
            When ``None`` (the default -- REST's ``POST /ingestions`` path,
            which does not collect a ``short_name`` from its caller), the
            title fetched by this same call is slugged via
            :func:`_derive_short_name`, exactly as before. When supplied
            (the MCP ``ingest_regulation`` tool's path), that value is used
            as-is and :func:`_derive_short_name` is never reached -- this is
            the actual #96 fix: two resolutions of the same CELEX with
            differently-worded Cellar titles no longer derive two different
            ``short_name``s, because nothing is derived at all.
        cellar_fetch: The Cellar/ELI XHTML fetch callable; injectable for
            tests. When ``None`` (the default), the module-level
            :func:`fetch_xhtml` name is resolved at call time -- not bound
            as a literal default value -- so a caller-side
            ``monkeypatch.setattr("ps_service.api.
            ingestion_orchestration.fetch_xhtml", ...)`` takes effect even
            for callers (e.g. ``routes.create_ingestion``) that never pass
            ``cellar_fetch`` explicitly. A plain ``= fetch_xhtml`` default
            would bind the real function at module-import time and silently
            ignore such a monkeypatch -- confirmed by a real, unmocked
            outbound call to Cellar/ELI during this increment's own TDD red
            step before this ``None``-sentinel form was adopted.
        cellar_fetch_rdf: The Cellar/ELI RDF/XML fetch callable; injectable
            for tests. Same ``None``-sentinel-resolved-at-call-time pattern
            as ``cellar_fetch``, for the same monkeypatch-safety reason,
            resolving to the module-level :func:`fetch_rdf` name.

    Returns:
        A :class:`CellarResolution` pairing the resolved entry with a
        cached-bytes :class:`CellarEliAdapter`.

    Raises:
        CatalogIdentifierNotFoundError: ``celex`` does not exist on Cellar/ELI
            (a genuine 404, distinguished from an outage -- AC-BI-005).
        PipelineStageError: The Cellar/ELI fetch failed for any other reason,
            or the fetched document could not be turned into valid metadata
            (an unparseable structure, an unsupported CELEX type code, or an
            empty title) -- all classified as stage ``"ingestion"`` (the same
            502 shape a Stage-1 failure would produce, D3).
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
        # Broad on purpose, not narrowed to CellarParseError: an empty
        # extracted title (no `eli-main-title` div, or an empty one) does not
        # make extract_metadata *return* with title="" -- RegulatoryInstrument
        # Metadata.title is `Field(min_length=1)`, so that case raises
        # pydantic.ValidationError instead (verified directly, not assumed).
        # Both failure shapes -- and any other extract_metadata failure --
        # get the same stage="ingestion" classification, so a degenerate
        # `_<celex>`-shaped short_name is never derived from either.
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
    graph names stay lowercase by user decision (issue #193, DECISIONS.md T1).

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

    Called by both ``routes.create_ingestion`` and the ``ingest_regulation`` MCP
    tool (issue #193, AC-BI-014), so both entry points reach identical outcomes.
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
        short_name: The request's caller-supplied ``short_name``.
        single_tenant_graph: The already-opened single-tenant graph, read by the
            collision check.
        emitter: Optional explicit log emitter.
        cellar_fetch: Optional Cellar/ELI XHTML fetch callable (test seam).
        cellar_fetch_rdf: Optional Cellar/ELI RDF/XML fetch callable (test seam).

    Returns:
        The :class:`CellarResolution` for ``celex`` under ``short_name``.

    Raises:
        CelexAlreadyIngestedError: ``celex`` is already in the graph.
        ShortNameCollisionError: ``short_name`` is already claimed by a
            different CELEX in the graph.
        CatalogIdentifierNotFoundError: ``celex`` does not exist on Cellar/ELI.
        PipelineStageError: The CELEX check, the collision check or the Cellar
            resolution failed.
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

    Queries only the passed single-tenant handle (AC-BI-006), inside
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
    """Emit one ``ingestion_identity_check`` log entry (PLAN D7).

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
    """One completed pipeline stage and a small integer summary of what it produced."""

    stage: str
    summary: dict[str, int]


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    """The result of a full ingestion pipeline run.

    ``outcome`` defaults to ``"fresh"`` (a run that executed its stages).
    ``"already_ingested"`` (issue #135, catalog path only) means the
    pre-flight check found an existing fully-merged ``RegulatoryInstrument``
    and skipped every stage -- ``stages`` is empty in that case.
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
        "unmatched_obligations": len(result.unmatched_obligation_ids),
    }


def _merge_summary(result: MergeResult) -> dict[str, int]:
    """Summarise a ``MergeResult`` as small integer counts.

    ``pending_reviews`` (issue #35, Slice 5, AC-BI-010) is a NEW, additive key
    alongside the pre-existing ``near_misses`` -- ``StageOutcome.summary`` is
    ``dict[str, int]``, and ``ps-cli``'s own summary parsing already iterates
    ``summary.items()`` generically, so this is backward-compatible by
    construction (CHANGES.md C2 / PLAN.md §5).

    ``new_obligations``, ``new_capabilities`` and ``matched_capabilities`` (issue #195, Slice 6)
    are the net-new / matched facts ``company_merge`` computed (never approximations): the
    ingestion audit rows carry these three counts. ``obligations`` /
    ``canonical_capabilities`` stay the total processed.
    """
    return {
        "obligations": len(result.obligation_ids),
        "canonical_capabilities": len(result.capability_canonical_ids),
        "near_misses": len(result.near_misses),
        "pending_reviews": result.pending_review_count,
        "new_obligations": result.new_obligation_count,
        "new_capabilities": result.new_capability_count,
        "matched_capabilities": result.matched_capability_count,
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
    """Emit one ``ingestion_run`` log entry (AC-BI-010 / AC-BI-011).

    Args:
        outcome: ``"started"`` / ``"succeeded"`` / ``"failed"``.
        run_id: The request-scoped run id, carried on every line.
        source_identifier: The catalog CELEX, or (issue #91) the fixed
            ``_INTERNAL_INGESTION_SOURCE_IDENTIFIER`` literal for the
            internal-document path -- content-transport carries no path or
            other stable identifier known before parsing.
        caller: The requesting client host (or ``"unknown"``).
        emitter: Optional explicit emitter; otherwise the process default.
        duration_ms: Wall time for the run so far (omitted on ``"started"``).
        failing_stage: The stage that raised, on the ``"failed"`` entry only.
        audit_facts: The ``trigger`` and the three counts the audit rows carry (issue #195),
            on the ``"succeeded"`` entry of an audited run only, so log and audit agree.
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


# --- pre-flight already-merged check (issue #135) ---

_MERGED_INSTRUMENT_EXISTS_QUERY = (
    "MATCH (n:RegulatoryInstrument) WHERE toUpper(n.id) = $id RETURN n.id LIMIT 1"
)


def _is_already_merged(single_tenant_graph: GraphHandle, regulatory_instrument_id: str) -> bool:
    """Return whether a fully-merged ``RegulatoryInstrument`` already exists for this id.

    Company Merge's ``persist_canonical_nodes`` (``company_merge/graph_writer.py``)
    writes a ``RegulatoryInstrument`` node into the single-tenant graph
    unconditionally at the end of every merge run -- its existence there is
    therefore a reliable proxy for "Domain Mapper and Company Merge already
    completed for this exact identifier" (issue #135, AC-BI-001), mirroring
    ``api.export_orchestration``'s own ``_EXISTENCE_QUERY`` shape, pointed at
    the single-tenant graph instead of a ``{short}_baseline`` graph.

    Args:
        single_tenant_graph: The already-opened single-tenant graph.
        regulatory_instrument_id: The ``{short_name}-{version}`` id the
            catalog pipeline would ingest under. Matched case-insensitively
            (``toUpper(n.id) = $id``), so a legacy lowercase celex-less node is
            found for its upper-case successor id (issue #193, M2).

    Returns:
        ``True`` if a matching ``RegulatoryInstrument`` node exists.
    """
    result = single_tenant_graph.query(
        _MERGED_INSTRUMENT_EXISTS_QUERY, params={"id": regulatory_instrument_id.upper()}
    )
    rows = cast("list[list[object]]", result.result_set)
    return len(rows) > 0


# --- cross-instrument short_name collision check (issue #146, AC-BI-006) ---
#
# Two independent `single_tenant` graph opens are accepted for this check plus the
# `_is_already_merged` preflight above: `FalkorDB.__init__` is not lazy -- it calls
# `Is_Cluster(conn)`, which issues a real `conn.info(section="server")` round-trip
# to the FalkorDB/Redis backend. The accepted cost is one extra network round-trip
# (single-digit milliseconds), negligible against this pipeline's own ~613s
# end-to-end SLA (`docs/architecture/ps-service-container-architecture.md:734`).
# Threading a single handle through both call sites to avoid this cost is not
# worth it -- it would require changing `run_catalog_ingestion_pipeline`'s public
# signature for no measurable benefit.

_SHORT_NAME_COLLISION_QUERY = (
    "MATCH (n:RegulatoryInstrument) WHERE toUpper(n.id) STARTS WITH $prefix "
    "AND n.celex IS NOT NULL AND n.celex <> $celex RETURN n.id, n.celex"
)


def check_short_name_collision(
    single_tenant_graph: GraphHandle, *, celex: str, short_name: str
) -> str | None:
    """Return the conflicting CELEX if `short_name` is already claimed by a different one.

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
            ``cra`` collides with a recorded ``CRA-1.0`` (issue #193, AC-BI-004).

    Returns:
        The conflicting CELEX, or ``None`` if no other-CELEX instrument is
        recorded under this short name, ignoring case.
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
module level, M6; a test pins the two equal)."""


@dataclass(frozen=True, slots=True)
class _OpenGraphs:
    """The three graph handles one pipeline run writes through."""

    native: GraphHandle
    baseline: GraphHandle
    single_tenant: GraphHandle


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
) -> tuple[str, tuple[StageReport, ...]]:
    """Run ingest -> extract -> derive -> merge, aborting at the first failure.

    Each stage after the first consumes the ``regulatory_instrument_id`` the
    ingest stage returned (AC-BI-003). The first :func:`_run_stage` to raise a
    ``PipelineStageError`` aborts the sequence -- later stages never run
    (AC-BI-008). :func:`~ps_service.api.run_status.set_stage` is called for each
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
        ingestion_adapter: A pre-built adapter to use for the ingest stage
            (AC-BI-006 -- the fetch-once Cellar-fallback adapter); when
            ``None``, ``dependencies.adapters.ingestion()`` builds the default
            one, exactly as the curated path does today.
        stages_to_run: The stage names to run (the amendment
            re-ingest resumes at the stages a new version is missing); ``None``
            runs all four. Skipped stages are neither run, ``set_stage``d nor
            reported. When ``ingestion`` is skipped the instrument id is
            ``<short_name>-<version>``, the id the ingest stage would return.
        on_stage_complete: Called with the stage name after each run stage
            returns (never for a stage that raised), before the next stage starts.

    Returns:
        The ``regulatory_instrument_id`` and the :class:`StageReport` tuple of
        the stages that ran, in pipeline order.
    """
    selected = frozenset(_STAGE_ORDER if stages_to_run is None else stages_to_run)
    stages = dependencies.stages
    mapping_adapter = dependencies.adapters.mapping()
    reports: list[StageReport] = []

    def complete(stage: str, summary: dict[str, int]) -> None:
        reports.append(StageReport(stage, summary))
        if on_stage_complete is not None:
            on_stage_complete(stage)

    rid = f"{entry.short_name}-{entry.version}"
    if "ingestion" in selected:
        ingest_adapter = (
            dependencies.adapters.ingestion() if ingestion_adapter is None else ingestion_adapter
        )
        set_stage(run_id, "ingestion")
        ingest_result = _run_stage(
            "ingestion",
            lambda: stages.ingest(
                entry.celex,
                entry.short_name,
                version=entry.version,
                adapter=ingest_adapter,
                graph=graphs.native,
                run_id=run_id,
            ),
            emitter=emitter,
        )
        rid = ingest_result.regulatory_instrument_id
        complete("ingestion", _ingestion_summary(ingest_result))
    if "extraction" in selected:
        set_stage(run_id, "extraction")
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
        complete("extraction", _extraction_summary(extract_result))
    if "derivation" in selected:
        set_stage(run_id, "derivation")
        derive_result = _run_stage(
            "derivation",
            lambda: stages.derive(rid, baseline_graph=graphs.baseline, model=resolved.chat_model),
            emitter=emitter,
        )
        complete("derivation", _derivation_summary(derive_result))
    if "merge" in selected:
        set_stage(run_id, "merge")
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

    Sequences Ingestion -> Domain Mapper (extract, derive) -> Company Merge
    in-process (AC-BI-002/003). Before any stage runs, a pre-flight check
    (issue #135, AC-BI-001) looks for an already-fully-merged
    ``RegulatoryInstrument`` for this exact ``{short_name}-{version}`` id in
    the single-tenant graph (:func:`_is_already_merged`); if found, Domain
    Mapper and Company Merge never run and this returns immediately with
    ``outcome="already_ingested"`` and an empty ``stages`` tuple (AC-BI-002/
    003) -- otherwise the pipeline runs exactly as before (AC-BI-004), and
    the check itself failing (e.g. the graph is unreachable) fails closed into
    the same ``PipelineStageError`` path a stage failure would (AC-BI-006),
    never silently treated as already ingested. Re-running for the same
    identifier when no merged instrument exists yet converges on the
    exact-canonical-identity nodes, but LLM-extraction non-determinism
    (issue #34) can still fragment a reworded Capability across re-ingestions
    until #34 is addressed. The whole run shares one ``run_id`` -- it is
    passed explicitly into the ingest stage (the only stage fn that self-binds a
    fresh run context) and carried on the ``ingestion_run`` start/end log entries
    (AC-BI-010/011). A stage failure aborts the sequence and surfaces as a
    ``PipelineStageError`` naming the failing stage (AC-BI-008), with no
    filesystem path / host / URL in its reason (AC-BI-009). The ``run_status``
    entry :func:`_execute_catalog_stages` maintains for this ``run_id`` is
    cleared unconditionally once the run ends -- success or failure -- via a
    ``finally`` block, so the registry never grows unbounded across a long
    server uptime (AC-BI-008; see ``ps_service.api.run_status``'s module
    docstring for the accepted last-writer-wins tradeoff on a colliding
    ``run_id``).

    Args:
        entry: The curated catalog entry (CELEX, title, short name, version).
        config: The resolved service configuration.
        run_id: The request-scoped run id.
        caller: The requesting client host, or ``"unknown"``.
        dependencies: The injected graph openers, stage functions, and adapter
            factories (``build_default_pipeline_dependencies`` in production; a
            fake in fast tests).
        emitter: Optional explicit log emitter; otherwise the process default.
        ingestion_adapter: A pre-built adapter to use for the ingest stage
            (AC-BI-006 -- the fetch-once Cellar-fallback adapter from
            :func:`resolve_via_cellar`); when ``None``, the dependency
            bundle's default factory builds one, exactly as the curated path
            does today.
        trigger: The audited entry point that started this run (issue #195); when given, the
            ``succeeded`` log line also carries it and the merge-stage counts. The audit rows
            themselves are written by the caller, not here.

    Returns:
        An :class:`IngestionOutcome` with ``source="catalog"``, one
        :class:`StageReport` per completed stage, and ``outcome="fresh"`` --
        or, when the pre-flight check finds an existing merged instrument,
        ``outcome="already_ingested"`` with no stage reports at all.

    Raises:
        IngestionConfigIncompleteError: If the configuration is missing an LLM
            model / embed model / similarity threshold (HTTP 503).
        PipelineStageError: If any stage -- or the pre-flight check itself --
            raises (HTTP 502).
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


# --- audited synchronous ingestion (issue #195) ---


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
    """Write the run's one terminal `ingestion_run.complete` row; BEST-EFFORT (AC-BI-015)."""
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
    """Resolve and run one synchronous catalog ingestion between its two audit rows (issue #195).

    The shared orchestration of ``POST /ingestions`` and the ``ingest_regulation`` MCP tool.
    An ``ingestion_run.submit`` opening row (``applied``, ``status=started``,
    ``trigger='sync_ingest'``) is written FIRST, before the identity check (D-A), and is
    FAIL-CLOSED: if it cannot be written nothing runs and ``AuditTrailUnavailableError``
    propagates (AC-BI-011). It is followed by identity resolution
    (:func:`resolve_ingestion_entry`) and the pipeline
    (:func:`run_catalog_ingestion_pipeline`), then one terminal ``ingestion_run.complete`` row
    carrying the instrument id and the merge-stage counts. ``resource_id`` of both rows is
    ``run_id``. The terminal row is BEST-EFFORT (AC-BI-015: a failed write is logged with the run
    id and never changes the outcome or the raised error). Outcomes: a fresh or pre-flight
    ``already_ingested`` run -> ``succeeded``; ``CelexAlreadyIngestedError`` -> ``succeeded`` with
    ``outcome='already_ingested'`` and zero counts, then re-raised; any other exception -> a
    ``failed`` row with an enumerated ``reason_code`` (no message, trace or path; AC-BI-010), then
    re-raised unchanged. The unaudited pipeline function stays public for the async worker, whose
    rows are
    written by the run store.

    Args:
        celex: The request's CELEX identifier.
        short_name: The request's caller-supplied ``short_name`` (any case).
        config: The resolved service configuration.
        run_id: The effective run id (the rows' ``resource_id``).
        caller: The requesting client host or principal label, for the run log.
        dependencies: The injected graph openers, stages and adapters.
        audit: Who is acting and where the rows go.
        emitter: Optional explicit log emitter.

    Returns:
        The pipeline's :class:`IngestionOutcome`, unchanged.

    Raises:
        AuditTrailUnavailableError: The opening row could not be written; nothing ran.
        CelexAlreadyIngestedError: ``celex`` is already in the graph.
        ShortNameCollisionError: ``short_name`` is claimed by a different CELEX.
        CatalogIdentifierNotFoundError: ``celex`` does not exist on Cellar/ELI.
        IngestionConfigIncompleteError: The pipeline configuration is incomplete.
        PipelineStageError: A stage, or the identity/pre-flight reads, failed.
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
            # AC-BI-006: the request was understood and the instrument is already there -- a
            # succeeded run that changed nothing; the caller still gets the existing rejection.
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


# --- internal-seed pipeline (issue #54, S2) ---


def _internal_ingestion_summary(result: InternalIngestResult) -> dict[str, int]:
    """Summarise an ``InternalIngestResult`` as small integer counts."""
    return {
        "roles": result.role_count,
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
    pattern (``internal-regulation-intake-format.md``, ``ps-domain-
    concepts.md``); ``native_graph_name``/``baseline_graph_name`` lowercase
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

    Runs before any pipeline stage and before any graph is opened (AC-BI-006's
    "no I/O until validated" precedent, applied to the internal path): both a
    schema/shape violation (AC-BI-002/003) from ``adapter.parse_seed`` and a
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


def run_internal_ingestion_pipeline(
    document: dict[str, object],
    *,
    config: ServiceConfig,
    run_id: str,
    caller: str,
    dependencies: PipelineDependencies,
    emitter: LogEmitter | None = None,
) -> IngestionOutcome:
    """Run the internal-seed ingestion pipeline for one already-parsed document.

    Two stages in sequence (GH #76 removed issue #54 S3's ``governance_
    derivation`` stage outright -- Policy/Standard/Control are now authored
    directly in the submitted document and minted by ``internal_ingestion``
    itself, so there is no separate governance-derivation stage any more):
    ``internal_ingestion`` (read + validate + mint + persist, including the
    now-authored Policy layer, S2) then ``merge`` (``merge_baseline_graph``,
    S4 -- merges the internal baseline's spine and governance layer into the
    single-tenant ``policy_system`` graph, the same stage function
    :func:`run_catalog_ingestion_pipeline` already uses), each wrapped by
    :func:`_run_stage` so a failure in either of them aborts the sequence and
    names the failing stage (AC-BI-013). The seed is parsed (and translated to
    :class:`InternalSeedValidationError` on a schema/shape violation)
    *before* any graph is opened, since the ``{short}_baseline``/``{short}_
    native`` graph names are derived from the seed's own
    ``RegulatoryInstrument.id`` -- unlike the catalog path, the short name is
    not known until the document has been parsed. This pipeline needs a
    resolved LLM model and similarity threshold (``merge`` needs the Company
    Merge similarity threshold; the LLM model is required for its embedding-
    based dedup pass), so :func:`_require_ingestion_config` runs first,
    before any graph is opened or any stage runs -- the same
    HTTP-503-before-any-I/O guarantee the catalog pipeline already gives.

    Args:
        document: The already-parsed intake document (the request body's
            ``content`` field, issue #91) -- carried directly in the request,
            never resolved against PS Service's own filesystem.
        config: The resolved service configuration.
        run_id: The request-scoped run id.
        caller: The requesting client host, or ``"unknown"``.
        dependencies: The injected graph openers, stage functions, and
            adapter factories (``build_default_pipeline_dependencies`` in
            production; a fake in fast tests).
        emitter: Optional explicit log emitter; otherwise the process default.

    Returns:
        An :class:`IngestionOutcome` with ``source="internal"`` and two
        :class:`StageReport` entries, in pipeline order (GH #76).

    Raises:
        IngestionConfigIncompleteError: If the configuration is missing an
            LLM model / embed model / similarity threshold (HTTP 503).
        InternalSeedValidationError: The seed document fails structural or
            shape validation (422), or its ``RegulatoryInstrument.id`` is
            not in the expected ``{SHORT}-{VERSION}`` shape.
        PipelineStageError: The ``internal_ingestion`` or ``merge`` stage
            raises for any other reason -- e.g. a referential-integrity/
            cardinality violation (AC-BI-011), or a FalkorDB write failure
            (502).
    """
    resolved = _require_ingestion_config(config)
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
            native_graph = dependencies.graphs.native(config, short_name)
            baseline_graph = dependencies.graphs.baseline(config, short_name)
            single_tenant_graph = dependencies.graphs.single_tenant(config)
            set_stage(run_id, "internal_ingestion")
            ingest_result = _run_stage(
                "internal_ingestion",
                lambda: dependencies.stages.ingest_internal(
                    seed,
                    baseline_graph=baseline_graph,
                    native_graph=native_graph,
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
                    embed_model=resolved.embed_model,
                    similarity_threshold=resolved.similarity_threshold,
                ),
                emitter=emitter,
            )
        except PipelineStageError as exc:
            _emit_run(
                outcome="failed",
                run_id=run_id,
                source_identifier=_INTERNAL_INGESTION_SOURCE_IDENTIFIER,
                caller=caller,
                emitter=emitter,
                duration_ms=_elapsed_ms(started),
                failing_stage=exc.stage,
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
    )
    return IngestionOutcome(
        regulatory_instrument_id=rid,
        source="internal",
        stages=(
            StageReport("internal_ingestion", _internal_ingestion_summary(ingest_result)),
            StageReport("merge", _merge_summary(merge_result)),
        ),
    )


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


def _default_ingestion_adapter() -> MetadataFetchingAdapter:
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


def _default_internal_seed_adapter() -> InternalSeedAdapter:
    """Build the default internal-seed Ingestion Adapter (issue #54, S2)."""
    from ps_service.ingestion.adapters.internal_seed.adapter import (  # noqa: PLC0415 -- mirrors _default_ingestion_adapter's own local-import style
        InternalSeedIngestionAdapter,
    )

    return InternalSeedIngestionAdapter()


def build_default_graph_openers() -> GraphOpeners:
    """Wire the real shipped FalkorDB graph openers into a ``GraphOpeners``.

    This is the *only* moving part :func:`build_default_pipeline_dependencies`
    lets a caller substitute (M2/issue #163 Slice C) -- the true infra
    boundary (three FalkorDB client constructions), never the real
    ``PipelineStages``/``PipelineAdapters`` business logic sitting on top of
    it. Extracted to its own top-level function (rather than inlined in
    :func:`build_default_pipeline_dependencies`) specifically so it is its
    own, independently addressable module-level name: a caller-side
    ``monkeypatch.setattr("ps_service.api.ingestion_orchestration.
    build_default_graph_openers", ...)`` substitutes graphs alone, while
    ``build_default_pipeline_dependencies`` itself -- called with no
    arguments -- still resolves this name at call time (ordinary Python
    late-binding for a bare module-level call, the same mechanism
    :func:`resolve_via_cellar`'s own ``None``-sentinel pattern relies on) and
    so picks up the substitution automatically, with zero change to its own
    call sites. ``docs/coding-standards/approved-mock-boundaries.yaml`` lists
    this function itself as the approved boundary -- not
    ``build_default_pipeline_dependencies``, which stays off that list since
    it still bundles real business logic alongside this boundary.

    Returns:
        A :class:`GraphOpeners` bound to the production FalkorDB openers.
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
    module load (M6 / the Process Harness decoupling guarantee).

    issue #163 Slice C narrowed this factory's own DI seam: ``graphs`` is the
    *only* substitutable parameter. There is deliberately no ``stages``/
    ``adapters`` parameter any more -- ``PipelineStages`` (
    ``ingest_regulatory_instrument``/``extract_roles_and_requirements``/
    ``derive_obligations_and_capabilities``/``merge_baseline_graph``) and
    ``PipelineAdapters`` are always the real, shipped implementations,
    unconditionally, with no way for a caller (test or otherwise) to
    substitute fake business logic through this function's own signature.
    Before this change, a caller could -- and 23 ``mcp_interface`` unit tests
    did -- replace this factory's *entire* return value wholesale, faking
    real Domain Mapper/Company Merge/Ingestion engine logic in the name of
    substituting only the FalkorDB boundary beneath it (the "delegate, don't
    reimplement" violation L2's MCP Interface Patterns section warns
    against; see ``.orchestrator/tracker/issue-163/AUDIT_RAW/
    mcp_interface_part1.md``'s DOMINANT FINDING). ``graphs=None`` (the
    default -- every production caller, unchanged) builds the real
    :class:`GraphOpeners` via :func:`build_default_graph_openers`; a caller
    that passes ``graphs`` explicitly substitutes only that true infra
    boundary.

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
