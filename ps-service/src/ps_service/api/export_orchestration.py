"""REST-boundary glue for ``POST /exports``.

It provides an injection seam (:class:`ExportDependencies`) and
:func:`build_default_export_dependencies`, which wires the real
``ps_service.export.export_instrument.export_instrument`` delegate through a function-local
import, so importing ``ps_service.main`` does not load that component at module load.

:func:`run_export` is the thin wrapper the ``POST /exports`` route calls. It:

1. Derives ``short_name`` from ``instrument_id`` with no graph access. A malformed id (no ``-``
   separator) raises :class:`~ps_service.api.errors.ExportInstrumentNotFoundError` at once.
2. Requires a configured embedding model, raising
   :class:`~ps_service.api.errors.ExportConfigIncompleteError` before any FalkorDB call, then
   resolves the graph write gateway (``GraphWriteUnavailableError`` if none is installed)
   before any graph is opened. The embedding backfill is written through that gateway, never
   directly.
3. Probes with ``db.list_graphs()`` whether the derived ``{short}_baseline`` graph key exists,
   before opening it or issuing any ``MATCH``, because querying a missing key makes FalkorDB
   create a permanent empty graph. A missing key raises ``ExportInstrumentNotFoundError`` with
   no call to ``open_baseline_graph`` or ``open_native_graph``.
4. Once the key is known to exist, opens the baseline graph and reads the live
   ``RegulatoryInstrument`` node's properties to build the full ``InstrumentDescriptor`` on the
   server. An empty result also raises ``ExportInstrumentNotFoundError`` (the key exists but
   this ``instrument_id`` was never written to it).
5. Refuses the export with :class:`~ps_service.api.errors.ExportUnresolvedNearMissesError` when
   an unresolved ``PendingReview`` in ``policy_system`` names a ``Capability`` id of the
   instrument's baseline graph (``ps_service.export.near_miss_gate``). A failure to read either
   graph propagates, so the export fails closed. This runs before step 6, so a refusal writes
   no audit row, no embedding backfill and no file.
6. Records one ``export.instrument`` system audit row, failing closed with
   ``AuditTrailUnavailableError`` before anything is computed or written, then calls the
   injected ``export`` delegate with the gateway, the row's id and the baseline graph name. The
   delegate runs inside a per-request scratch directory used as ``repo_root``, so its file
   writes (``curated-content/...`` and a regenerated ``catalog.json``) never touch a real
   checkout. The wrapper reads ``baseline.json`` and ``native.json`` back from the scratch tree,
   base64-encodes them into the response, and always removes the scratch directory, on success
   or failure.
7. Emits ``started``, ``succeeded`` and ``failed`` audit log entries itself, because the
   delegate emits no audit logging.

No ``ps_service.export.export_instrument`` or ``InstrumentDescriptor`` type enters this module's
runtime import graph except through the function-local imports below.
"""

from __future__ import annotations

import base64
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

from ps_service.api.errors import (
    ExportConfigIncompleteError,
    ExportInstrumentNotFoundError,
    ExportStageFailedError,
    ExportUnresolvedNearMissesError,
)
from ps_service.api.export_audit_actions import (
    EXPORT_INSTRUMENT_ACTION,
    EXPORT_INSTRUMENT_RESOURCE_TYPE,
    ExportInstrumentDetails,
)
from ps_service.api.graph_write_access import GraphWriteAccess, graph_write_access
from ps_service.api.models import ExportAcceptedResponse, ExportManifestPayload, ExportStageOutcome
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.api.models import ExportRequest
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.config import ServiceConfig
    from ps_service.export.export_instrument import InstrumentDescriptor
    from ps_service.export.falkordb_connection import (
        _GraphQueryHandle,  # pyright: ignore[reportPrivateUsage]
    )
    from ps_service.export.models import InstrumentManifest
    from ps_service.graph_gateway.read_only import ReadOnlyDatabase
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.llm_interface.client import EmbeddingCaller
    from ps_service.logging import LogEmitter

_COMPONENT = "export"
_ACTION = "export_instrument"
_DEFAULT_STAGE = "export"
_SCRATCH_DIR_PREFIX = "ps-export-"
_CURATED_CONTENT_DIRNAME = "curated-content"
_BASELINE_FILENAME = "baseline.json"
_NATIVE_FILENAME = "native.json"
_EXISTENCE_QUERY = "MATCH (n:RegulatoryInstrument {id: $id}) RETURN properties(n)"

# The fixed three-stage summary reported in a successful response. The delegate runs one fixed
# order (embed, serialize, write the manifest and graph files, regenerate catalog.json) and
# reports no per-stage detail, so it is collapsed into three externally observable checkpoints.
_EXPORT_STAGES: tuple[str, ...] = ("embedded", "serialized", "cataloged")


class ExportStage(Protocol):
    """Call shape of ``ps_service.export.export_instrument.export_instrument``.

    It lets :class:`ExportDependencies` accept the real delegate or a test fake.
    """

    def __call__(  # noqa: PLR0913 -- mirrors export_instrument(), the delegate this Protocol types
        self,
        descriptor: InstrumentDescriptor,
        *,
        baseline_graph: _GraphQueryHandle,
        native_graph: _GraphQueryHandle,
        embed_model: str,
        repo_root: Path,
        packaged_copy_path: Path,
        submitter: GroupSubmitter,
        audit_event_id: str,
        baseline_graph_name: str,
        call_embedding: EmbeddingCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> InstrumentManifest:
        """Curate one instrument end to end: embed, serialize, write files, regenerate the catalog.

        Args:
            descriptor: Full description of the instrument to export.
            baseline_graph: Query handle on the instrument's ``{short}_baseline`` graph.
            native_graph: Query handle on the instrument's ``{short}_native`` graph.
            embed_model: Name of the embedding model used for the embedding backfill.
            repo_root: Directory under which ``curated-content/<instrument_id>/`` is written.
            packaged_copy_path: Path of the second, packaged copy of ``catalog.json``.
            submitter: Graph write gateway through which the embedding backfill is written.
            audit_event_id: Id of the system audit row that the backfill's write group is
                anchored to.
            baseline_graph_name: Name of the baseline graph the backfill targets.
            call_embedding: Embedding transport; ``None`` selects the default transport.
            emitter: Log emitter; ``None`` selects the process-wide default.

        Returns:
            The :class:`~ps_service.export.models.InstrumentManifest` describing the written
            files.

        Raises:
            Exception: Any failure of the export (for example a source-graph, instrument-id
                mismatch or graph-write error from ``ps_service.export.errors``). The caller
                classifies it into an ``ExportStageFailedError``.

        Side Effects:
            Writes ``baseline.json``, ``native.json`` and a manifest under ``repo_root`` and
            regenerates ``catalog.json`` there and at ``packaged_copy_path``. Calls the
            embedding service, and writes embeddings into the baseline graph through
            ``submitter``.
        """
        ...


@dataclass(frozen=True, slots=True)
class ExportDependencies:
    """Everything :func:`run_export` needs that is not per-request.

    The graph openers take ``(FalkorDB, short_name)`` and return a query handle. They are
    injected separately from ``open_db`` so orchestration tests can fake graph content without
    a real FalkorDB. ``call_embedding`` is the embedding-transport seam, injected at the bundle
    level so :func:`build_default_export_dependencies` can wire the real transport.

    Attributes:
        open_db: Opens the FalkorDB connection for a ``ServiceConfig``.
        open_baseline_graph: Opens the ``{short}_baseline`` graph on a connection for a short
            name.
        open_native_graph: Opens the ``{short}_native`` graph on a connection for a short name.
        export: The delegate that curates the instrument.
        call_embedding: Embedding transport passed to the delegate; ``None`` selects the
            delegate's default.
        graph_write_access: Resolves the graph write gateway and system audit recorder for a
            ``ServiceConfig``; raises ``GraphWriteUnavailableError`` when none is installed.
        open_single_tenant_graph: Opens the single-tenant ``policy_system`` graph on a connection;
            the near-miss gate reads unresolved ``PendingReview`` nodes from it. Required, with no
            default, so a test cannot silently open the real graph.
    """

    open_db: Callable[[ServiceConfig], ReadOnlyDatabase]
    open_baseline_graph: Callable[[ReadOnlyDatabase, str], _GraphQueryHandle]
    open_native_graph: Callable[[ReadOnlyDatabase, str], _GraphQueryHandle]
    export: ExportStage
    call_embedding: EmbeddingCaller | None
    graph_write_access: Callable[[ServiceConfig], GraphWriteAccess]
    open_single_tenant_graph: Callable[[ReadOnlyDatabase], GraphHandle]


# --- instrument_id -> short_name (no graph access) -------------------


def _derive_short_name(instrument_id: str) -> str:
    """Return the ``{SHORT}`` prefix of a ``{SHORT}-{VERSION}`` instrument id.

    It splits on the last ``-``, so a short name that itself contains a hyphen is not truncated.
    Every ingestion path names ``RegulatoryInstrument.id`` this way.

    Args:
        instrument_id: Instrument id of the form ``{SHORT}-{VERSION}``.

    Returns:
        The part before the last ``-``.

    Raises:
        ExportInstrumentNotFoundError: ``instrument_id`` has no ``-`` separator or an empty
            prefix. It is treated as "not found", with no graph access.
    """
    short_name, separator, _version = instrument_id.rpartition("-")
    if not separator or not short_name:
        raise ExportInstrumentNotFoundError(
            f"instrument {instrument_id!r} is not a known ingested instrument"
        )
    return short_name


# --- config-completeness guard ----------------------------------------------


def _require_embed_model(config: ServiceConfig) -> str:
    """Return the configured embedding model name, failing fast when it is unset.

    Called before any graph access so a missing configuration costs nothing.

    Args:
        config: The resolved service configuration.

    Returns:
        The value of ``config.llm_interface_embed_model``.

    Raises:
        ExportConfigIncompleteError: The embedding model is not configured.
    """
    embed_model = config.llm_interface_embed_model
    if embed_model is None:
        raise ExportConfigIncompleteError("PS_LLMINTERFACE_EMBED_MODEL is not set")
    return embed_model


# --- safe existence probe ---------------


def _baseline_graph_exists(db: ReadOnlyDatabase, baseline_name: str) -> bool:
    """Report whether a graph key exists, without issuing any command against that key.

    Uses the ``db.list_graphs()`` round trip. Callers must call this before opening the graph or
    running any ``MATCH`` against ``baseline_name``, because querying a missing key makes
    FalkorDB create a permanent empty graph.

    Args:
        db: The FalkorDB connection.
        baseline_name: Name of the baseline graph key to look for.

    Returns:
        ``True`` if ``baseline_name`` is among the graphs on the server.
    """
    return baseline_name in db.list_graphs()


# --- descriptor building -----------------------------------------------


def _to_descriptor(
    *, short_name: str, instrument_id: str, properties: dict[str, object]
) -> InstrumentDescriptor:
    """Build the full ``InstrumentDescriptor`` from the live graph's own properties.

    Every field except ``short_name`` and ``instrument_id``, which the server already knows, is
    read from the ``RegulatoryInstrument`` node's properties and never supplied by the client.

    Args:
        short_name: Short name derived from the instrument id.
        instrument_id: Id of the instrument.
        properties: Properties of the ``RegulatoryInstrument`` node.

    Returns:
        The populated ``InstrumentDescriptor``. ``celex`` and ``jurisdiction`` are ``None`` when
        absent from ``properties``.

    Raises:
        KeyError: ``properties`` lacks ``version``, ``title`` or ``source_type``.
    """
    from ps_service.export.export_instrument import (  # noqa: PLC0415 -- function-local
        InstrumentDescriptor,
    )

    return InstrumentDescriptor(
        short_name=short_name,
        instrument_id=instrument_id,
        version=cast("str", properties["version"]),
        celex=cast("str | None", properties.get("celex")),
        title=cast("str", properties["title"]),
        source_type=cast("Literal['external', 'internal']", properties["source_type"]),
        jurisdiction=cast("str | None", properties.get("jurisdiction")),
    )


# --- failure classification -------------------------------------------------

# Matched by class name, not imported, so this module needs no module-level dependency on
# `ps_service.export.errors`.
_STAGE_BY_EXCEPTION_NAME: dict[str, str] = {
    "ExportSourceGraphError": "serialization",
    "ExportInstrumentIdMismatchError": "serialization",
    "ExportGraphWriteError": "embedding",
}


def _classify_export_failure(exc: Exception) -> ExportStageFailedError:
    """Turn a delegate or scratch-directory failure into an ``ExportStageFailedError``.

    Every exception raised inside :func:`run_export`'s scratch-directory block is routed through
    here, not only the delegate's own, so an ``OSError`` while reading the scratch files never
    exposes its absolute path: an unrecognised exception type gets a generic reason, never
    ``str(exc)``.

    Args:
        exc: The exception raised inside the scratch-directory block.

    Returns:
        An ``ExportStageFailedError`` naming the stage (``"serialization"`` for a source-graph or
        instrument-id mismatch error, ``"embedding"`` for a graph-write error, otherwise
        ``"export"``) with the reason ``"<stage> failed"``.
    """
    exc_type_name = type(exc).__name__
    stage = _STAGE_BY_EXCEPTION_NAME.get(exc_type_name, _DEFAULT_STAGE)
    return ExportStageFailedError(stage=stage, reason=f"{stage} failed")


# --- audit logging -----------------------------------------------------


def _emit_export_log(
    *,
    instrument_id: str,
    outcome: str,
    actor: str,
    schema_version: str,
    emitter: LogEmitter | None,
    review_count: int | None = None,
) -> None:
    """Emit one export audit log entry.

    The caller identity goes under ``extra["caller"]``, not ``"actor"``. This wrapper owns every
    export audit log call, because the delegate emits none.

    Args:
        instrument_id: Instrument being exported; logged as the entity id.
        outcome: ``"started"``, ``"succeeded"``, ``"failed"`` or ``"refused"``.
        actor: The requesting client host; logged as ``caller``.
        schema_version: Domain schema version logged with the entry.
        emitter: Log emitter; ``None`` selects the process-wide default.
        review_count: Number of unresolved near-miss reviews that refused the export; logged as
            ``review_count`` only when given, so other entries are unchanged.

    Returns:
        None.

    Side Effects:
        Emits one structured log entry.
    """
    extra: dict[str, object] = {"caller": actor, "schema_version": schema_version}
    if review_count is not None:
        extra["review_count"] = review_count
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        entity_id=instrument_id,
        outcome=outcome,
        extra=extra,
        emitter=emitter,
    )


# --- response encoding -------------------------------------------------------


def _to_accepted_response(
    *, instrument_id: str, manifest: InstrumentManifest, baseline_blob: bytes, native_blob: bytes
) -> ExportAcceptedResponse:
    """Map a delegate-returned manifest and the scratch files to the response body.

    Args:
        instrument_id: Id of the exported instrument.
        manifest: The manifest the delegate returned.
        baseline_blob: Raw bytes of ``baseline.json``.
        native_blob: Raw bytes of ``native.json``.

    Returns:
        An ``ExportAcceptedResponse`` with the manifest, both blobs base64-encoded in ASCII, and
        the three fixed stages all marked ``"succeeded"``.
    """
    return ExportAcceptedResponse(
        instrument_id=instrument_id,
        manifest=ExportManifestPayload(
            instrument_id=manifest.instrument_id,
            celex=manifest.celex,
            title=manifest.title,
            short_name=manifest.short_name,
            version=manifest.version,
            source_type=manifest.source_type,
            jurisdiction=manifest.jurisdiction,
            schema_version=manifest.schema_version,
            exported_at=manifest.exported_at,
            baseline_sha256=manifest.baseline_sha256,
            native_sha256=manifest.native_sha256,
        ),
        baseline_blob_base64=base64.b64encode(baseline_blob).decode("ascii"),
        native_blob_base64=base64.b64encode(native_blob).decode("ascii"),
        stages=[ExportStageOutcome(stage=stage, status="succeeded") for stage in _EXPORT_STAGES],
    )


# --- the wrapper ---


def run_export(
    request_body: ExportRequest,
    *,
    config: ServiceConfig,
    actor: str,
    dependencies: ExportDependencies,
    emitter: LogEmitter | None = None,
) -> ExportAcceptedResponse:
    """Export one already-ingested instrument through the injected delegate.

    Args:
        request_body: The ``POST /exports`` request body; ``instrument_id`` has the form
            ``{short}-{version}``.
        config: The resolved service configuration.
        actor: The requesting client host, logged as the caller.
        dependencies: The injected export dependency bundle (production wiring, or a fake in
            tests).
        emitter: Audit-log emitter; ``None`` selects the process-wide default.

    Returns:
        An :class:`ExportAcceptedResponse` carrying the manifest and both base64-encoded graph
        blobs.

    Raises:
        ExportInstrumentNotFoundError: ``instrument_id`` is malformed, or names no ingested
            instrument (HTTP 404).
        ExportConfigIncompleteError: The embedding model is not configured (HTTP 503).
        ExportUnresolvedNearMissesError: An unresolved near-miss review touches a Capability of
            the instrument (HTTP 409); raised before the audit row, so nothing is written.
        GraphWriteUnavailableError: No graph write gateway is installed.
        AuditTrailUnavailableError: The ``export.instrument`` audit row could not be written;
            nothing was computed or written.
        ExportStageFailedError: The delegate, or the scratch-directory I/O around it, failed
            (HTTP 502).
        KeyError: The ``RegulatoryInstrument`` node lacks ``version``, ``title`` or
            ``source_type``.
        Exception: Errors from listing graphs, running the existence query or reading the
            near-miss gate's graphs (for example a FalkorDB connection error) propagate
            unchanged; the export fails closed before the audit row.

    Side Effects:
        Records one ``export.instrument`` system audit row. Creates and always removes a scratch
        directory. The delegate writes embeddings into the baseline graph through the gateway.
        Emits ``started`` and then ``succeeded`` or ``failed`` log entries; a refusal emits
        one ``refused`` entry (with ``review_count``) and no ``started`` entry.
    """
    instrument_id = request_body.instrument_id
    short_name = _derive_short_name(instrument_id)
    embed_model = _require_embed_model(config)
    # Resolved before any graph is opened: the embedding backfill is written through the gateway,
    # so without one nothing may start. There is no direct-write fallback.
    access = dependencies.graph_write_access(config)

    db = dependencies.open_db(config)

    from ps_service.domain_mapper.falkordb_client import (  # noqa: PLC0415 -- function-local
        baseline_graph_name,
    )

    baseline_name = baseline_graph_name(short_name)
    if not _baseline_graph_exists(db, baseline_name):
        raise ExportInstrumentNotFoundError(
            f"instrument {instrument_id!r} was not found (no ingested {baseline_name!r} graph)"
        )

    baseline_graph = dependencies.open_baseline_graph(db, short_name)
    result = baseline_graph.query(_EXISTENCE_QUERY, params={"id": instrument_id})
    rows = cast("list[list[object]]", result.result_set)  # pyright: ignore[reportAttributeAccessIssue]
    if not rows:
        raise ExportInstrumentNotFoundError(
            f"instrument {instrument_id!r} was not found in graph {baseline_name!r}"
        )
    properties = cast("dict[str, object]", rows[0][0])
    descriptor = _to_descriptor(
        short_name=short_name, instrument_id=instrument_id, properties=properties
    )

    # Export serializes the per-instrument graphs only, so a near miss recorded in `policy_system`
    # would be lost from the artifact. Refuse before the audit row, the backfill and any file are
    # written. A read failure propagates: the export fails closed.
    from ps_service.export.near_miss_gate import (  # noqa: PLC0415 -- function-local
        unresolved_near_miss_review_ids,
    )

    blocking_review_ids = unresolved_near_miss_review_ids(
        baseline_graph, dependencies.open_single_tenant_graph(db)
    )
    if blocking_review_ids:
        # An expected user condition, not a failure: `refused`, one entry, no `started` entry.
        _emit_export_log(
            instrument_id=instrument_id,
            outcome="refused",
            actor=actor,
            schema_version=DOMAIN_SCHEMA_VERSION,
            emitter=emitter,
            review_count=len(blocking_review_ids),
        )
        raise ExportUnresolvedNearMissesError(instrument_id, blocking_review_ids)

    # The embedding backfill's write group is anchored to this row. It is recorded outside the try
    # below, before anything is computed: if it cannot be written, AuditTrailUnavailableError
    # propagates and nothing was computed or written.
    audit_event_id = access.system_audit(
        action=EXPORT_INSTRUMENT_ACTION,
        resource_type=EXPORT_INSTRUMENT_RESOURCE_TYPE,
        resource_id=instrument_id,
        details=ExportInstrumentDetails(instrument_id=instrument_id, status="started").model_dump(),
    )

    _emit_export_log(
        instrument_id=instrument_id,
        outcome="started",
        actor=actor,
        schema_version=DOMAIN_SCHEMA_VERSION,
        emitter=emitter,
    )

    scratch_root = Path(tempfile.mkdtemp(prefix=_SCRATCH_DIR_PREFIX))
    try:
        native_graph = dependencies.open_native_graph(db, short_name)
        manifest = dependencies.export(
            descriptor,
            baseline_graph=baseline_graph,
            native_graph=native_graph,
            embed_model=embed_model,
            repo_root=scratch_root,
            packaged_copy_path=scratch_root / "packaged" / "catalog.json",
            submitter=access.gateway,
            audit_event_id=audit_event_id,
            baseline_graph_name=baseline_name,
            call_embedding=dependencies.call_embedding,
            emitter=emitter,
        )
        instrument_dir = scratch_root / _CURATED_CONTENT_DIRNAME / instrument_id
        baseline_blob = (instrument_dir / _BASELINE_FILENAME).read_bytes()
        native_blob = (instrument_dir / _NATIVE_FILENAME).read_bytes()
    except Exception as exc:
        _emit_export_log(
            instrument_id=instrument_id,
            outcome="failed",
            actor=actor,
            schema_version=DOMAIN_SCHEMA_VERSION,
            emitter=emitter,
        )
        raise _classify_export_failure(exc) from exc
    finally:
        shutil.rmtree(scratch_root)

    _emit_export_log(
        instrument_id=instrument_id,
        outcome="succeeded",
        actor=actor,
        schema_version=manifest.schema_version,
        emitter=emitter,
    )
    return _to_accepted_response(
        instrument_id=instrument_id,
        manifest=manifest,
        baseline_blob=baseline_blob,
        native_blob=native_blob,
    )


# --- default wiring (every export import below is function-local) ---


def _default_open_db(config: ServiceConfig) -> ReadOnlyDatabase:
    """Open the real FalkorDB connection for ``config``.

    Args:
        config: Service configuration naming the FalkorDB host and port.

    Returns:
        A read-only FalkorDB database handle; connectivity is not verified here, so connection
        errors surface on the first graph listing or query.

    Side Effects:
        None beyond creating the client handle.
    """
    from ps_service.domain_mapper.falkordb_client import (  # noqa: PLC0415 -- function-local
        connect_from_config,
    )

    return connect_from_config(config)


def _default_open_baseline_graph(db: ReadOnlyDatabase, short_name: str) -> _GraphQueryHandle:
    """Open the real ``{short}_baseline`` graph on ``db``.

    Args:
        db: The FalkorDB connection.
        short_name: Short name of the instrument.

    Returns:
        A read-only query handle on the ``{short}_baseline`` graph.

    Side Effects:
        None beyond selecting the graph. The caller must have confirmed that the graph exists,
        since querying a missing graph makes FalkorDB create an empty one.
    """
    from ps_service.domain_mapper.falkordb_client import (  # noqa: PLC0415 -- function-local
        baseline_graph_name,
    )
    from ps_service.export.falkordb_connection import (  # noqa: PLC0415 -- function-local
        graph_query_handle,
    )

    return graph_query_handle(db, baseline_graph_name(short_name))


def _default_open_native_graph(db: ReadOnlyDatabase, short_name: str) -> _GraphQueryHandle:
    """Open the real ``{short}_native`` graph on ``db``.

    Args:
        db: The FalkorDB connection.
        short_name: Short name of the instrument.

    Returns:
        A query handle on the ``{short}_native`` graph.

    Side Effects:
        None beyond selecting the graph.
    """
    from ps_service.export.falkordb_connection import (  # noqa: PLC0415 -- function-local
        graph_query_handle,
    )
    from ps_service.ingestion.falkordb_client import (  # noqa: PLC0415 -- function-local
        native_graph_name,
    )

    return graph_query_handle(db, native_graph_name(short_name))


def _default_open_single_tenant_graph(db: ReadOnlyDatabase) -> GraphHandle:
    """Open the single-tenant ``policy_system`` graph, read-only, on ``db``.

    The graph name is ``single_tenant_graph_name()`` (``PS_FALKORDB_GRAPH``, default
    ``policy_system``): ``ServiceConfig`` has no graph-name field, a known configuration gap.

    Args:
        db: The read-only FalkorDB connection.

    Returns:
        A read handle on the single-tenant graph.

    Side Effects:
        None beyond selecting the graph.
    """
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- function-local
        select_graph,
        single_tenant_graph_name,
    )

    return select_graph(db, single_tenant_graph_name())


def build_default_export_dependencies() -> ExportDependencies:
    """Wire the real ``export_instrument`` orchestration into an ``ExportDependencies``.

    ``export_instrument`` and ``default_embedding_caller`` are imported inside this function and
    the opener helpers above, so importing ``ps_service.main`` does not load
    ``ps_service.export.export_instrument`` at module load.

    Returns:
        An :class:`ExportDependencies` bound to the production export orchestration, the real
        FalkorDB connection and graph-opener helpers, and the real LiteLLM embedding transport.

    Side Effects:
        None; no connection is opened while building the bundle.
    """
    from ps_service.export.export_instrument import (  # noqa: PLC0415 -- function-local
        export_instrument,
    )
    from ps_service.llm_interface.client import (  # noqa: PLC0415 -- function-local
        default_embedding_caller,
    )

    return ExportDependencies(
        open_db=_default_open_db,
        open_baseline_graph=_default_open_baseline_graph,
        open_native_graph=_default_open_native_graph,
        export=export_instrument,
        call_embedding=default_embedding_caller,
        graph_write_access=graph_write_access,
        open_single_tenant_graph=_default_open_single_tenant_graph,
    )
