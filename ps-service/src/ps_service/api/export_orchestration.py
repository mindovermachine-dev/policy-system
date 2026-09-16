"""REST-boundary glue for ``POST /exports`` (issue #71, PLAN.md §0 D2-D4/D6).

Mirrors ``restore_orchestration.py``'s shape exactly: an injection seam
(:class:`ExportDependencies`) and :func:`build_default_export_dependencies`,
which wires the real ``ps_service.export.export_instrument.export_instrument``
delegate via a **function-local** import so that importing ``ps_service.main``
never transitively loads ``ps_service.export.export_instrument`` at module
load (M6 / the Process Harness decoupling guarantee).

:func:`run_export` is the thin wrapper the ``POST /exports`` route calls. It:

1. Derives ``short_name`` from ``instrument_id`` with zero graph access
   (D2) -- a malformed id (no ``-`` separator) raises
   :class:`~ps_service.api.errors.ExportInstrumentNotFoundError` immediately.
2. Requires a configured embedding model (D3) -- raises
   :class:`~ps_service.api.errors.ExportConfigIncompleteError` before any
   FalkorDB call if unset.
3. Safely probes whether the derived ``{short}_baseline`` graph key exists at
   all via ``db.list_graphs()`` -- **before** opening it or issuing any
   ``MATCH`` (issue #71 CHANGES.md Appendix A1's fix: querying a
   not-yet-existing key lazily vivifies a permanent empty graph,
   ``ps_service.restore.staging``'s own documented FalkorDB gotcha). A
   missing key raises ``ExportInstrumentNotFoundError`` with **zero** calls
   to ``open_baseline_graph``/``open_native_graph``.
4. Only once the key is known to exist: opens the baseline graph and looks
   up the live ``RegulatoryInstrument`` node's own properties to build the
   full ``InstrumentDescriptor`` server-side (D2) -- an empty result also
   raises ``ExportInstrumentNotFoundError`` (the key is real, but this exact
   ``instrument_id`` was never written to it). This second query is safe:
   the key's existence is already confirmed, so it cannot vivify anything.
5. Calls the injected ``export`` delegate inside a per-request scratch
   directory (``tempfile.mkdtemp(prefix="ps-export-")`` as ``repo_root``,
   D1) so the delegate's own real file writes (``curated-content/...``,
   a regenerated ``catalog.json``) never touch the real checkout. Reads
   ``baseline.json``/``native.json`` back off the scratch tree, base64
   encodes them into the response, and always ``shutil.rmtree``s the scratch
   directory in a ``finally`` -- success or failure (AC-BI-011).
6. Emits ``started``/``succeeded``/``failed`` audit log entries itself (D4)
   -- ``export_instrument()`` has no audit logging of its own and must not be
   modified (AC-BI-003), so this wrapper owns AC-BI-013 entirely.

No ``ps_service.export.export_instrument``/``InstrumentDescriptor`` type ever
crosses into this module's own runtime import graph except via the
function-local import sites below (mirrors ``restore_orchestration.py``'s
"no falkordb import ever crosses into ps_service.api" convention).
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
)
from ps_service.api.models import ExportAcceptedResponse, ExportManifestPayload, ExportStageOutcome
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Callable

    from falkordb import (
        FalkorDB,  # pyright: ignore[reportMissingTypeStubs] -- falkordb ships no py.typed marker
    )

    from ps_service.api.models import ExportRequest
    from ps_service.config import ServiceConfig
    from ps_service.export.export_instrument import InstrumentDescriptor
    from ps_service.export.falkordb_connection import (
        _GraphQueryHandle,  # pyright: ignore[reportPrivateUsage]
    )
    from ps_service.export.models import InstrumentManifest
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

# The fixed, three-stage summary reported in a successful response. Export's
# own delegate (`export_instrument`) has no per-stage granularity to report
# (its own docstring describes one fixed orchestration order: embed, then
# serialize, then write the manifest/graph files, then regenerate
# catalog.json) -- collapsed here into the three externally-observable
# checkpoints, mirroring `RestoreOutcome`'s own fixed three-entry
# `("verified", "staged", "merged_and_finalized")` shape.
_EXPORT_STAGES: tuple[str, ...] = ("embedded", "serialized", "cataloged")


class ExportStage(Protocol):
    """Call shape of ``ps_service.export.export_instrument.export_instrument``."""

    def __call__(
        self,
        descriptor: InstrumentDescriptor,
        *,
        baseline_graph: _GraphQueryHandle,
        native_graph: _GraphQueryHandle,
        embed_model: str,
        repo_root: Path,
        packaged_copy_path: Path,
        call_embedding: EmbeddingCaller | None = None,
        emitter: LogEmitter | None = None,
    ) -> InstrumentManifest:
        """Curate one instrument end to end: embed, serialize, write, catalog."""
        ...


@dataclass(frozen=True, slots=True)
class ExportDependencies:
    """Everything :func:`run_export` needs that is not per-request.

    ``open_baseline_graph``/``open_native_graph`` take ``(FalkorDB,
    short_name) -> _GraphQueryHandle``, injected separately from ``open_db``
    so orchestration tests can fake graph *content* without a real FalkorDB
    (mirrors ``RestoreDependencies``'s own "small injectable callables, not
    one opaque bundle" shape). ``call_embedding`` is the real-vs-fake
    embedding transport seam, injected at the dependency-bundle level so
    :func:`build_default_export_dependencies` can wire the real
    ``default_embedding_caller``.
    """

    open_db: Callable[[ServiceConfig], FalkorDB]
    open_baseline_graph: Callable[[FalkorDB, str], _GraphQueryHandle]
    open_native_graph: Callable[[FalkorDB, str], _GraphQueryHandle]
    export: ExportStage
    call_embedding: EmbeddingCaller | None


# --- instrument_id -> short_name (D2, zero graph access) -------------------


def _derive_short_name(instrument_id: str) -> str:
    """Return the ``{SHORT}`` prefix of a ``{SHORT}-{VERSION}`` instrument id.

    Mirrors ``ingestion_orchestration._internal_short_name``'s exact
    algorithm (splits on the *last* ``-`` so a short name that itself
    contains a hyphen is not truncated early) -- D2's confirmation that
    every ingestion path always names ``RegulatoryInstrument.id`` this way.

    Raises:
        ExportInstrumentNotFoundError: ``instrument_id`` has no ``-``
            separator at all -- treated the same as "not found" (AC-BI-009),
            with zero graph access.
    """
    short_name, separator, _version = instrument_id.rpartition("-")
    if not separator or not short_name:
        raise ExportInstrumentNotFoundError(
            f"instrument {instrument_id!r} is not a known ingested instrument"
        )
    return short_name


# --- config-completeness guard ----------------------------------------------


def _require_embed_model(config: ServiceConfig) -> str:
    """Return the resolved embedding model, or raise if it is unset.

    Raised before ``open_db`` is ever called -- mirrors
    ``restore_orchestration._require_similarity_threshold``'s own
    fail-fast-before-any-graph-access shape.
    """
    embed_model = config.llm_interface_embed_model
    if embed_model is None:
        raise ExportConfigIncompleteError("PS_LLMINTERFACE_EMBED_MODEL is not set")
    return embed_model


# --- safe existence probe (issue #71 CHANGES.md Appendix A1) ---------------


def _baseline_graph_exists(db: FalkorDB, baseline_name: str) -> bool:
    """Safe existence probe.

    Mirrors ``check_connectivity``'s ``db.list_graphs()``
    round-trip (``ps_service/ingestion/falkordb_client.py``) -- this
    codebase's own established pattern for asking FalkorDB "does this key
    exist" without issuing any command against the key itself. Callers
    MUST call this before ``dependencies.open_baseline_graph``/any ``MATCH``
    against ``baseline_name`` -- querying a not-yet-existing key lazily
    vivifies a permanent empty graph (``ps_service.restore.staging``'s own
    documented FalkorDB gotcha).
    """
    return baseline_name in db.list_graphs()


# --- descriptor building (D2) -----------------------------------------------


def _to_descriptor(
    *, short_name: str, instrument_id: str, properties: dict[str, object]
) -> InstrumentDescriptor:
    """Build the full ``InstrumentDescriptor`` from the live graph's own properties.

    Every field but ``short_name``/``instrument_id`` (both already known
    server-side, D2) is read straight off ``RegulatoryInstrument``'s own
    properties -- never supplied by the client.
    """
    from ps_service.export.export_instrument import (  # noqa: PLC0415 -- M6: function-local
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

# Matched by class name, not imported, so this module never needs a
# module-level dependency on `ps_service.export.errors` -- mirrors
# `restore_orchestration._classify_restore_failure`'s exact reasoning.
_STAGE_BY_EXCEPTION_NAME: dict[str, str] = {
    "ExportSourceGraphError": "serialization",
    "ExportInstrumentIdMismatchError": "serialization",
}


def _classify_export_failure(exc: Exception) -> ExportStageFailedError:
    """Classify a delegate/scratch-directory failure into an ``ExportStageFailedError``.

    Every exception raised inside :func:`run_export`'s scratch-directory
    try block is routed through here -- not just ones the delegate itself
    raises -- so a ``FileNotFoundError``/``OSError`` reading the scratch
    tree's own files never surfaces its (real, absolute) path: an
    unrecognised exception type always gets a generic reason, never
    ``str(exc)`` verbatim (PLAN.md D5's scratch-path leak concern).
    """
    exc_type_name = type(exc).__name__
    stage = _STAGE_BY_EXCEPTION_NAME.get(exc_type_name, _DEFAULT_STAGE)
    return ExportStageFailedError(stage=stage, reason=f"{stage} failed")


# --- audit logging (D4) -----------------------------------------------------


def _emit_export_log(
    *, instrument_id: str, outcome: str, actor: str, schema_version: str, emitter: LogEmitter | None
) -> None:
    """Emit one D4/AC-BI-013 audit log entry, in MA2's corrected call shape.

    ``extra={"caller": actor, "schema_version": ...}`` -- never
    ``extra={"actor": ...}`` -- mirrors
    ``restore_instrument._emit_restore_log`` exactly. This wrapper owns
    every audit-log call site for export: ``export_instrument()`` itself
    emits none (AC-BI-003 forbids modifying it to add any).
    """
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        entity_id=instrument_id,
        outcome=outcome,
        extra={"caller": actor, "schema_version": schema_version},
        emitter=emitter,
    )


# --- response encoding -------------------------------------------------------


def _to_accepted_response(
    *, instrument_id: str, manifest: InstrumentManifest, baseline_blob: bytes, native_blob: bytes
) -> ExportAcceptedResponse:
    """Map a delegate-returned ``InstrumentManifest`` + scratch blobs to the response body."""
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


# --- the wrapper -------------------------------------------------------


def run_export(
    request_body: ExportRequest,
    *,
    config: ServiceConfig,
    actor: str,
    dependencies: ExportDependencies,
    emitter: LogEmitter | None = None,
) -> ExportAcceptedResponse:
    """Export one already-ingested curated instrument via the injected delegate.

    Args:
        request_body: The ``POST /exports`` request body.
        config: The resolved service configuration.
        actor: The requesting client host (mirrors ``run_restoration``'s own
            ``actor`` derivation).
        dependencies: The injected export dependency bundle (the production
            bundle in production; a fake in fast tests).
        emitter: The audit-log emitter (``None`` uses the process-wide
            configured default) -- this wrapper's own seam, since
            ``export_instrument()`` emits no audit log itself (D4).

    Returns:
        An :class:`ExportAcceptedResponse` carrying the manifest and both
        base64-encoded graph blobs.

    Raises:
        ExportInstrumentNotFoundError: ``instrument_id`` is malformed, or
            names no actually-ingested instrument (404).
        ExportConfigIncompleteError: The embedding model is not configured
            (503).
        ExportStageFailedError: The delegate (or the scratch-directory I/O
            around it) failed (502).
    """
    instrument_id = request_body.instrument_id
    short_name = _derive_short_name(instrument_id)
    embed_model = _require_embed_model(config)

    db = dependencies.open_db(config)

    from ps_service.domain_mapper.falkordb_client import (  # noqa: PLC0415 -- M6: function-local
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


# --- default wiring (M6 -- every export import below is function-local) ---


def _default_open_db(config: ServiceConfig) -> FalkorDB:
    """Open the real FalkorDB connection for ``config``."""
    from ps_service.domain_mapper.falkordb_client import (  # noqa: PLC0415 -- M6: function-local
        connect_from_config,
    )

    return connect_from_config(config)


def _default_open_baseline_graph(db: FalkorDB, short_name: str) -> _GraphQueryHandle:
    """Open the real ``{short}_baseline`` graph on ``db``."""
    from ps_service.domain_mapper.falkordb_client import (  # noqa: PLC0415 -- M6: function-local
        baseline_graph_name,
    )
    from ps_service.export.falkordb_connection import (  # noqa: PLC0415 -- M6: function-local
        graph_query_handle,
    )

    return graph_query_handle(db, baseline_graph_name(short_name))


def _default_open_native_graph(db: FalkorDB, short_name: str) -> _GraphQueryHandle:
    """Open the real ``{short}_native`` graph on ``db``."""
    from ps_service.export.falkordb_connection import (  # noqa: PLC0415 -- M6: function-local
        graph_query_handle,
    )
    from ps_service.ingestion.falkordb_client import (  # noqa: PLC0415 -- M6: function-local
        native_graph_name,
    )

    return graph_query_handle(db, native_graph_name(short_name))


def build_default_export_dependencies() -> ExportDependencies:
    """Wire the real ``export_instrument`` orchestration into an ``ExportDependencies``.

    ``export_instrument``/``default_embedding_caller`` are imported
    **function-locally** (here, and in the opener helpers above) so that
    importing ``ps_service.main`` never transitively loads
    ``ps_service.export.export_instrument`` at module load (M6 / the Process
    Harness decoupling guarantee) -- mirrors
    ``restore_orchestration.build_default_restore_dependencies`` exactly.

    Returns:
        An :class:`ExportDependencies` bound to the production export
        orchestration, the real FalkorDB connection/graph-opener helpers,
        and the real LiteLLM embedding transport.
    """
    from ps_service.export.export_instrument import (  # noqa: PLC0415 -- M6: function-local
        export_instrument,
    )
    from ps_service.llm_interface.client import (  # noqa: PLC0415 -- M6: function-local
        default_embedding_caller,
    )

    return ExportDependencies(
        open_db=_default_open_db,
        open_baseline_graph=_default_open_baseline_graph,
        open_native_graph=_default_open_native_graph,
        export=export_instrument,
        call_embedding=default_embedding_caller,
    )
