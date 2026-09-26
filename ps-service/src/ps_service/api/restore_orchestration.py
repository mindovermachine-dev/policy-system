"""REST-boundary glue for ``POST /restorations`` (D5, PLAN.md §0.7).

Mirrors ``ingestion_orchestration.py``'s shape: an injection seam
(:class:`RestoreDependencies`, mirroring :class:`PipelineDependencies`) and
:func:`build_default_restore_dependencies`, which wires the real
``ps_service.restore.restore_instrument`` orchestration via a **function-local**
import so that importing ``ps_service.main`` never transitively loads
``ps_service.restore``/``ps_service.company_merge`` at module load (M6 / the
Process Harness decoupling guarantee).

:func:`run_restoration` is the thin wrapper the ``POST /restorations`` route
calls: it decodes the request body into a ``RestoreArtifact``, calls the
injected restore stage, and translates ``ps_service.restore``'s domain
exceptions into the two API-boundary error types ``error_handlers`` knows how
to shape --

* ``ArtifactIntegrityError`` / ``ArtifactSchemaVersionMismatchError`` (D9/D10,
  checksum/schema_version verification failures) -> :class:`~ps_service.api.
  errors.RestoreArtifactRejectedError` (422);
* anything else the delegate raises (``ArtifactContentRejectedError``,
  ``RestoreConcurrencyConflictError``, or an unexpected failure) ->
  :class:`~ps_service.api.errors.RestoreStageFailedError` (502), naming the
  failing stage.

No ``ps_service.restore``/``ps_service.company_merge`` type ever crosses into
this module's own runtime import graph except via the one function-local
import site (mirrors ``ingestion_orchestration.py``'s "no falkordb import
ever crosses into ps_service.api" convention for the pipeline components).
``ps_service.restore.errors``/``ps_service.restore.models`` are safe to import
at module level here -- unlike ``ps_service.restore.restore_instrument``,
neither imports ``ps_service.company_merge`` (verified: both are plain
dataclass/exception modules with no heavy dependency of their own).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ps_service.api.error_handlers import (
    _scrub_text,  # pyright: ignore[reportPrivateUsage]  # shared scrubber; mirrors ingestion_orchestration.py's own reuse
    is_safe_verbatim,
)
from ps_service.api.errors import (
    CuratedSourceUnavailableError,
    RestoreArtifactRejectedError,
    RestoreStageFailedError,
)
from ps_service.api.models import RestorationAcceptedResponse, RestorationStageOutcome
from ps_service.curated_source.artifact_client import FetchArtifactCall, fetch_artifact
from ps_service.curated_source.errors import CuratedSourceFetchError
from ps_service.curated_source.resolve import EffectiveCatalogSource, resolve_effective_source
from ps_service.export.models import InstrumentManifest
from ps_service.restore.errors import ArtifactIntegrityError, ArtifactSchemaVersionMismatchError
from ps_service.restore.models import RestoreArtifact

if TYPE_CHECKING:
    from collections.abc import Callable

    from falkordb import (
        FalkorDB,  # pyright: ignore[reportMissingTypeStubs] -- falkordb ships no py.typed marker
    )

    from ps_service.api.models import (
        CatalogRestorationRequest,
        RestorationManifestPayload,
        RestorationRequest,
    )
    from ps_service.config import ServiceConfig
    from ps_service.curated_source.store import GraphHandle
    from ps_service.logging import LogEmitter
    from ps_service.restore.models import RestoreOutcome

_STAGE_REASON_MAX_LEN = 300
_CONFIGURATION_STAGE = "configuration"
_DEFAULT_STAGE = "restore"


class RestoreStage(Protocol):
    """Call shape of ``ps_service.restore.restore_instrument.restore_instrument``."""

    def __call__(
        self,
        artifact: RestoreArtifact,
        *,
        db: FalkorDB,
        single_tenant_graph_name: str,
        similarity_threshold: float,
        actor: str,
        emitter: LogEmitter | None = None,
    ) -> RestoreOutcome:
        """Restore one curated instrument's artifact end to end."""
        ...


@dataclass(frozen=True, slots=True)
class RestoreDependencies:
    """Everything :func:`run_restoration` needs that is not per-request."""

    open_db: Callable[[ServiceConfig], FalkorDB]
    single_tenant_graph_name: Callable[[ServiceConfig], str]
    restore: RestoreStage


class CatalogRestoreStage(Protocol):
    """Call shape of ``restore_instrument`` when invoked with a resolved ``source`` (D-AUDIT).

    A strict superset of :class:`RestoreStage`'s call shape (the same
    callable, ``ps_service.restore.restore_instrument.restore_instrument``,
    satisfies both Protocols structurally) -- kept separate from
    ``RestoreStage`` rather than widening it in place, so
    ``RestoreDependencies``'s existing shape (and every test constructing
    one) is untouched by issue #125 (AC-BI-005: the upload path is
    unaffected).
    """

    def __call__(
        self,
        artifact: RestoreArtifact,
        *,
        db: FalkorDB,
        single_tenant_graph_name: str,
        similarity_threshold: float,
        actor: str,
        emitter: LogEmitter | None = None,
        source: str | None = None,
    ) -> RestoreOutcome:
        """Restore one curated instrument's artifact end to end, recording ``source``."""
        ...


@dataclass(frozen=True, slots=True)
class CatalogRestoreDependencies:
    """Everything :func:`run_restoration_from_catalog_source` needs that is not per-request.

    Composes the curated-content fetch step (``fetch_artifact``) alongside
    the same restore-step shape :class:`RestoreDependencies` uses
    (``open_db``/``single_tenant_graph_name``/``restore``) -- declared as its
    own fields rather than nesting a :class:`RestoreDependencies` instance,
    so a test can override just the fetch step or just the restore step
    independently, mirroring every other ``*Dependencies`` bundle in this
    package.
    """

    fetch_artifact: FetchArtifactCall
    resolve_effective_source: Callable[[ServiceConfig], EffectiveCatalogSource]
    open_db: Callable[[ServiceConfig], FalkorDB]
    single_tenant_graph_name: Callable[[ServiceConfig], str]
    restore: CatalogRestoreStage


# --- request decoding ---------------------------------------------------


def _to_instrument_manifest(payload: RestorationManifestPayload) -> InstrumentManifest:
    """Convert the request body's nested manifest payload to an ``InstrumentManifest``.

    Field-for-field, no renaming -- :class:`~ps_service.api.models.
    RestorationManifestPayload` is a Pydantic mirror of this dataclass's own
    fields (D1/D12).
    """
    return InstrumentManifest(
        instrument_id=payload.instrument_id,
        celex=payload.celex,
        title=payload.title,
        short_name=payload.short_name,
        version=payload.version,
        source_type=payload.source_type,
        jurisdiction=payload.jurisdiction,
        schema_version=payload.schema_version,
        exported_at=payload.exported_at,
        baseline_sha256=payload.baseline_sha256,
        native_sha256=payload.native_sha256,
    )


def _to_restore_artifact(request_body: RestorationRequest) -> RestoreArtifact:
    """Decode the request body's base64 blobs into a ``RestoreArtifact``.

    Raises:
        RestoreArtifactRejectedError: Either blob is not valid base64 -- a
            malformed artifact is rejected the same way a checksum/
            schema_version failure is (422), before any FalkorDB call.
    """
    try:
        baseline_blob = base64.b64decode(request_body.baseline_blob_base64, validate=True)
        native_blob = base64.b64decode(request_body.native_blob_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        message = f"malformed base64 artifact blob for instrument {request_body.instrument_id!r}"
        raise RestoreArtifactRejectedError(message) from exc
    return RestoreArtifact(
        manifest=_to_instrument_manifest(request_body.manifest),
        baseline_blob=baseline_blob,
        native_blob=native_blob,
    )


# --- config-completeness guard -------------------------------------------


def _require_similarity_threshold(config: ServiceConfig) -> float:
    """Return the resolved similarity threshold, or raise if it is unset.

    Restore's offline dedup replay (D6) needs the same
    ``PS_COMPANYMERGE_SIMILARITY_THRESHOLD`` value live ingestion's merge
    stage requires. Raised before the delegate is ever called -- named stage
    ``"configuration"`` since this is not a delegate failure, but the same
    502/failing-stage shape a delegate failure would produce.
    """
    threshold = config.company_merge_similarity_threshold
    if threshold is None:
        raise RestoreStageFailedError(
            stage=_CONFIGURATION_STAGE,
            reason="PS_COMPANYMERGE_SIMILARITY_THRESHOLD is not set",
        )
    return threshold


# --- failure classification -----------------------------------------------


def _classify_restore_failure(exc: Exception) -> RestoreStageFailedError:
    """Classify a non-integrity/schema-version delegate failure into a ``RestoreStageFailedError``.

    Unlike ``ingestion_orchestration._classify_stage_failure``, the delegate
    (``restore_instrument``) has no per-stage granularity to report -- D8's
    whole staged-write sequence is one function, not four named stages -- so
    the stage name is derived from the exception's own class name (matched,
    not imported, so this module never needs a module-level dependency on
    ``ps_service.restore.errors``'s less-common types beyond the two already
    imported for the 422 path).
    """
    exc_type_name = type(exc).__name__
    stage = {
        "ArtifactContentRejectedError": "content_validation",
        "RestoreConcurrencyConflictError": "concurrency",
    }.get(exc_type_name, _DEFAULT_STAGE)
    if is_safe_verbatim(exc):
        reason = _scrub_text(f"{exc_type_name}: {exc}")[:_STAGE_REASON_MAX_LEN]
    else:
        reason = f"{stage} failed"
    return RestoreStageFailedError(stage=stage, reason=reason)


# --- response encoding -----------------------------------------------------


def _to_accepted_response(outcome: RestoreOutcome) -> RestorationAcceptedResponse:
    """Map a ``RestoreOutcome`` to the ``POST /restorations`` success body."""
    return RestorationAcceptedResponse(
        instrument_id=outcome.instrument_id,
        stages=[
            RestorationStageOutcome(stage=stage, status="succeeded") for stage in outcome.stages
        ],
    )


# --- the wrapper -------------------------------------------------------


def run_restoration(
    request_body: RestorationRequest,
    *,
    config: ServiceConfig,
    actor: str,
    dependencies: RestoreDependencies,
) -> RestorationAcceptedResponse:
    """Restore one curated instrument's artifact via the injected delegate.

    Args:
        request_body: The ``POST /restorations`` request body.
        config: The resolved service configuration.
        actor: The requesting client host (mirrors ``ingestion_orchestration
            .run_catalog_ingestion_pipeline``'s ``caller`` derivation).
        dependencies: The injected restore dependency bundle (the production
            bundle in production; a fake in fast tests).

    Returns:
        A :class:`RestorationAcceptedResponse` naming the completed stages.

    Raises:
        RestoreArtifactRejectedError: The artifact is malformed, or fails
            checksum (D9) / schema_version (D10) verification (422).
        RestoreStageFailedError: Any other failure, including a missing
            similarity-threshold configuration value (502).
    """
    artifact = _to_restore_artifact(request_body)
    threshold = _require_similarity_threshold(config)
    db = dependencies.open_db(config)
    single_tenant_graph_name = dependencies.single_tenant_graph_name(config)
    try:
        outcome = dependencies.restore(
            artifact,
            db=db,
            single_tenant_graph_name=single_tenant_graph_name,
            similarity_threshold=threshold,
            actor=actor,
        )
    except (ArtifactIntegrityError, ArtifactSchemaVersionMismatchError) as exc:
        raise RestoreArtifactRejectedError(str(exc)) from exc
    except Exception as exc:
        raise _classify_restore_failure(exc) from exc
    return _to_accepted_response(outcome)


def run_restoration_from_catalog_source(
    request_body: CatalogRestorationRequest,
    *,
    config: ServiceConfig,
    actor: str,
    dependencies: CatalogRestoreDependencies,
) -> RestorationAcceptedResponse:
    """Fetch and restore one curated instrument's artifact from the curated-content source.

    Issue #125, ``POST /restorations/from-catalog`` (D-NEW-ROUTE) --
    restores via the same delegate :func:`run_restoration` (the upload
    path) uses. Since Slice 3, the source fetched from is the *effective*
    curated-content source: a persisted FalkorDB override when one exists,
    else ``config.curated_source_base_url``, resolved on every call via
    ``dependencies.resolve_effective_source`` (AC-BI-013) before
    ``dependencies.fetch_artifact`` is called -- a FalkorDB outage during
    that check falls open to ``config.curated_source_base_url`` rather than
    failing the request (D-FAILOPEN). The fetched artifact is passed to the
    injected ``restore`` delegate unmodified -- the exact same D9 checksum /
    D10 schema_version verification :func:`run_restoration` relies on runs
    first and unconditionally inside that one shared delegate (never
    duplicated here), so a fetched-but-corrupted or version-mismatched
    artifact is rejected exactly like an uploaded one (AC-BI-007/009, mirrors
    #66). The resolved effective source URL is passed through as ``source``
    so the restore's own audit log entries carry it (AC-BI-011, mirrors #66
    AC-BI-016) -- distinct from :func:`run_restoration`, whose call site
    never passes ``source`` at all, keeping the upload path's audit log shape
    byte-identical (D-AUDIT).

    Args:
        request_body: The ``POST /restorations/from-catalog`` request body
            (just the instrument id to fetch).
        config: The resolved service configuration -- names the effective
            curated-content source URL to fetch from.
        actor: The requesting client host (mirrors ``run_restoration``'s own
            ``actor`` derivation).
        dependencies: The injected fetch-and-restore dependency bundle (the
            production bundle in production; a fake in fast tests).

    Returns:
        A :class:`RestorationAcceptedResponse` naming the completed stages.

    Raises:
        CuratedSourceUnavailableError: The configured source is unreachable,
            or the fetched artifact is missing/malformed (AC-BI-004/006) --
            502, naming the source and instrument.
        RestoreArtifactRejectedError: The fetched artifact fails checksum
            (D9) / schema_version (D10) verification (422, AC-BI-007/009).
        RestoreStageFailedError: Any other restore failure, including a
            missing similarity-threshold configuration value (502).
    """
    effective_source = dependencies.resolve_effective_source(config)
    try:
        fetched = dependencies.fetch_artifact(effective_source.url, request_body.instrument_id)
    except CuratedSourceFetchError as exc:
        raise CuratedSourceUnavailableError(str(exc)) from exc
    artifact = RestoreArtifact(
        manifest=fetched.manifest,
        baseline_blob=fetched.baseline_blob,
        native_blob=fetched.native_blob,
    )
    threshold = _require_similarity_threshold(config)
    db = dependencies.open_db(config)
    single_tenant_graph_name = dependencies.single_tenant_graph_name(config)
    try:
        outcome = dependencies.restore(
            artifact,
            db=db,
            single_tenant_graph_name=single_tenant_graph_name,
            similarity_threshold=threshold,
            actor=actor,
            source=effective_source.url,
        )
    except (ArtifactIntegrityError, ArtifactSchemaVersionMismatchError) as exc:
        raise RestoreArtifactRejectedError(str(exc)) from exc
    except Exception as exc:
        raise _classify_restore_failure(exc) from exc
    return _to_accepted_response(outcome)


# --- default wiring (M6 -- every restore/company_merge import below is function-local) ---


def _default_open_db(config: ServiceConfig) -> FalkorDB:
    """Open the real FalkorDB connection for ``config``."""
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Company Merge at import
        connect_from_config,
    )

    return connect_from_config(config)


def _default_single_tenant_graph_name(config: ServiceConfig) -> str:
    """Return the single-tenant (``policy_system``) graph name."""
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Company Merge at import
        single_tenant_graph_name,
    )

    _ = config
    return single_tenant_graph_name()


def build_default_restore_dependencies() -> RestoreDependencies:
    """Wire the real ``restore_instrument`` orchestration into a ``RestoreDependencies``.

    ``restore_instrument`` is imported **function-locally** (here, and in the
    opener helpers above) so that importing ``ps_service.main`` never
    transitively loads ``ps_service.restore``/``ps_service.company_merge`` at
    module load (M6 / the Process Harness decoupling guarantee) -- mirrors
    ``ingestion_orchestration.build_default_pipeline_dependencies`` exactly.

    Returns:
        A :class:`RestoreDependencies` bound to the production restore
        orchestration and the real FalkorDB connection/graph-name helpers.
    """
    from ps_service.restore.restore_instrument import (  # noqa: PLC0415 -- M6: function-local
        restore_instrument,
    )

    return RestoreDependencies(
        open_db=_default_open_db,
        single_tenant_graph_name=_default_single_tenant_graph_name,
        restore=restore_instrument,
    )


def _default_resolve_effective_source(config: ServiceConfig) -> EffectiveCatalogSource:
    """Resolve the effective curated-content source (issue #125, Slice 3, AC-BI-013).

    ``ps_service.company_merge.falkordb_client`` is imported
    **function-locally**, exactly like :func:`_default_open_db` (M6 --
    ``ps_service.main`` never transitively loads ``ps_service.company_merge``
    at module load). The graph opened is the same single-tenant
    ``policy_system`` graph :func:`_default_open_db`/
    :func:`_default_single_tenant_graph_name` already resolve against.
    """
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Company Merge at import
        connect_from_config,
        select_graph,
        single_tenant_graph_name,
    )

    def _open_graph() -> GraphHandle:
        return select_graph(connect_from_config(config), single_tenant_graph_name())

    return resolve_effective_source(config, open_graph=_open_graph)


def build_default_restore_from_catalog_dependencies() -> CatalogRestoreDependencies:
    """Wire the real fetch-and-restore path into a ``CatalogRestoreDependencies``.

    ``restore_instrument`` is imported **function-locally**, exactly like
    :func:`build_default_restore_dependencies` (M6 -- ``ps_service.main``
    never transitively loads ``ps_service.restore``/``ps_service.
    company_merge`` at module load). ``curated_source.artifact_client.
    fetch_artifact`` carries no such restriction (it has no
    ``ps_service.restore``/``ps_service.company_merge`` dependency of its
    own -- confirmed by its imports) and is wired via this module's ordinary
    module-level import.

    Returns:
        A :class:`CatalogRestoreDependencies` bound to the production
        curated-content fetch step, the production `resolve_effective_source`
        (issue #125, Slice 3), and the same restore entry point / FalkorDB
        connection/graph-name helpers :func:`build_default_
        restore_dependencies` uses.
    """
    from ps_service.restore.restore_instrument import (  # noqa: PLC0415 -- M6: function-local
        restore_instrument,
    )

    return CatalogRestoreDependencies(
        fetch_artifact=fetch_artifact,
        resolve_effective_source=_default_resolve_effective_source,
        open_db=_default_open_db,
        single_tenant_graph_name=_default_single_tenant_graph_name,
        restore=restore_instrument,
    )
