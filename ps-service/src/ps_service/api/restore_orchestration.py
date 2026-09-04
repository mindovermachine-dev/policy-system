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
from ps_service.api.errors import RestoreArtifactRejectedError, RestoreStageFailedError
from ps_service.api.models import RestorationAcceptedResponse, RestorationStageOutcome
from ps_service.export.models import InstrumentManifest
from ps_service.restore.errors import ArtifactIntegrityError, ArtifactSchemaVersionMismatchError
from ps_service.restore.models import RestoreArtifact

if TYPE_CHECKING:
    from collections.abc import Callable

    from falkordb import (
        FalkorDB,  # pyright: ignore[reportMissingTypeStubs] -- falkordb ships no py.typed marker
    )

    from ps_service.api.models import RestorationManifestPayload, RestorationRequest
    from ps_service.config import ServiceConfig
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
