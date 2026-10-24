"""Pydantic request and response payload models for the PS Service REST API.

Every REST request and response body is a Pydantic model, with ``Field()`` constraints on any
value that reaches query construction, a filesystem path, a URL or an LLM call. The models cover
the ``GET /catalog`` listing, ``POST /ingestions`` (a ``source``-discriminated union of a
catalog request and an internal-document request, the accepted response and the ingestion
status shapes), ``POST /restorations``, ``POST /exports``, ``POST /change-checks``, the
near-miss review endpoints, the protected-resource metadata document and the structured error
body.
"""

from __future__ import annotations

import base64
from typing import Annotated, Literal

from fastapi import Path
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ps_service.api.ingestion_orchestration import SHORT_NAME_PATTERN
from ps_service.graph_cleanup.models import (
    CapabilityMergePreview,
    RoleMergePreview,
)

IDENTIFIER_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
"""Letters, digits, underscore and hyphen, 1 to 64 characters: safe as a URL path segment."""

PathIdentifier = Annotated[str, Path(pattern=IDENTIFIER_PATTERN, min_length=1, max_length=64)]
"""A path parameter holding a system-generated identifier; anything else is rejected with 422."""


class CatalogInstrumentEntry(BaseModel):
    """One curated instrument in the ``GET /catalog`` listing.

    The listing covers external and internal instruments alike, so it carries no ``celex``
    field: an internal source has none.

    Attributes:
        instrument_id: Curated instrument identifier; non-empty.
        title: Human-readable instrument title; non-empty.
        source_type: ``"external"`` for an EU regulation, ``"internal"`` for a project-authored
            source.
        jurisdiction: ``"EU"`` for an external regulation; ``None`` for an internal source.
    """

    model_config = ConfigDict(frozen=True)

    instrument_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_type: Literal["external", "internal"]
    jurisdiction: str | None = None


class CuratedCatalogResponse(BaseModel):
    """Response body for ``GET /catalog``.

    Attributes:
        instruments: Every curated instrument, external and internal, unfiltered, in catalog
            order.
    """

    model_config = ConfigDict(frozen=True)

    instruments: list[CatalogInstrumentEntry]


class CatalogIngestionRequest(BaseModel):
    """``POST /ingestions`` body naming a curated EU regulation by its CELEX identifier.

    Unknown fields are rejected.

    Attributes:
        source: Always ``"catalog"``; selects this variant of the request union.
        celex: The 10-character CELEX identifier, in the form of a digit ``3``, four digits, one
            capital letter and four digits, for example ``"32024R2847"``.
        short_name: Short name that drives graph naming; must match the allowed short-name
            pattern.
        run_id: Optional client-supplied correlation id of 1 to 64 letters, digits, underscores
            or hyphens, safe as a URL path segment. It lets the caller poll
            ``GET /ingestions/{run_id}`` for live progress while this request is still running.
            ``None`` makes the server mint a fresh id.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["catalog"]
    celex: str = Field(min_length=10, max_length=10, pattern=r"^3\d{4}[A-Z]\d{4}$")
    short_name: str = Field(pattern=SHORT_NAME_PATTERN)
    run_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)


class InternalIngestionRequest(BaseModel):
    """``POST /ingestions`` body carrying an internal document's content directly.

    The content is nested in the request body rather than named by a filesystem path, so there
    is no path-traversal sink to guard. Its shape is validated by the JSON Schema and model
    parse of the internal-source adapter, not by a ``Field()`` constraint here. Unknown fields
    are rejected.

    Attributes:
        source: Always ``"internal"``; selects this variant of the request union.
        content: The intake document as a JSON object (nodes and edges).
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["internal"]
    content: dict[str, object]


IngestionRequest = Annotated[
    CatalogIngestionRequest | InternalIngestionRequest,
    Field(discriminator="source"),
]
"""The ``POST /ingestions`` request body: a ``source``-discriminated union."""


class StageOutcome(BaseModel):
    """One completed pipeline stage as reported in the accepted response.

    Attributes:
        stage: Stage name; non-empty.
        status: Always ``"succeeded"``; only completed stages are reported.
        summary: Counts reported by the stage, keyed by metric name.
    """

    model_config = ConfigDict(frozen=True)

    stage: str = Field(min_length=1)
    status: Literal["succeeded"]
    summary: dict[str, int]


class IngestionAcceptedResponse(BaseModel):
    """Success body for ``POST /ingestions``: the run id, instrument id and per-stage outcomes.

    Attributes:
        run_id: Identifier of the run; non-empty.
        regulatory_instrument_id: Identifier of the ingested ``RegulatoryInstrument``; non-empty.
        source: ``"catalog"`` or ``"internal"``, matching the request.
        outcome: ``"fresh"`` for a run that executed its stages, or ``"already_ingested"``
            (catalog requests only) when a pre-flight check found an existing, fully merged
            ``RegulatoryInstrument`` and skipped the Domain Mapper and Company Merge stages.
        stages: Completed stages in execution order; empty when ``outcome`` is
            ``"already_ingested"``.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    regulatory_instrument_id: str = Field(min_length=1)
    source: Literal["catalog", "internal"]
    outcome: Literal["fresh", "already_ingested"] = "fresh"
    stages: list[StageOutcome]


class IngestionProgressResponse(BaseModel):
    """Response body for ``GET /ingestions/{run_id}``: a best-effort live-progress read.

    Always returned with HTTP 200, including for an unknown, finished or not-yet-started
    ``run_id``. It observes the in-flight stage registry and is not authoritative resource
    retrieval, so there is no 404 case.

    It differs from :class:`IngestionRunStatusResponse` because the two read different
    sources for different callers. This model reads the in-process stage registry of a
    synchronous run, is open to any authenticated caller, and so carries only ``run_id`` and
    ``stage``. The MCP model reads the durable run row of a background run and is limited to
    ``ComplianceOfficer``. One shared shape would either expose ``result`` and ``error`` to
    every authenticated caller or add a ``status`` that the stage registry cannot know.

    Attributes:
        run_id: The queried run id; non-empty.
        stage: Name of the stage the run is executing now; ``None`` when the run is unknown,
            finished or not yet started.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    stage: str | None = None


class StartIngestionResponse(BaseModel):
    """Success payload of the `start-ingestion` tool.

    Attributes:
        run_id: Identifier of the background ingestion run, to pass to `get-ingestion-status`.
        status: Always ``"running"``.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    status: Literal["running"]


class IngestionRunStatusResponse(BaseModel):
    """Payload of the `get-ingestion-status` tool: one fixed five-key shape for every status.

    Every field is always present, ``None`` included.

    It differs from :class:`IngestionProgressResponse` because it reads the durable run row
    of a background run and is limited to ``ComplianceOfficer``, so it may carry ``result`` and
    ``error``. The REST progress read serves any authenticated caller from the in-process
    stage registry of a synchronous run and cannot know ``status``, ``result`` or ``error``.

    Attributes:
        run_id: The queried run id.
        status: ``"unknown"`` (no such run), ``"running"``, ``"succeeded"`` or ``"failed"``.
        stage: Stage the run is executing now; ``None`` unless it is running.
        result: The success payload that `ingest-regulation` returns; ``None`` unless the run
            succeeded.
        error: Sanitised ``error:`` text of a failed run; ``None`` unless the run failed.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    status: Literal["unknown", "running", "succeeded", "failed"]
    stage: str | None
    result: dict[str, object] | None
    error: str | None


class RestorationManifestPayload(BaseModel):
    """The ``manifest.json`` fields carried inline in a ``POST /restorations`` body.

    Field for field, this is the instrument manifest that an export produces. The REST boundary
    declares it as a Pydantic model, and the restore orchestration converts it to the internal
    manifest type before restoring. Unknown fields are rejected.

    Attributes:
        instrument_id: Curated instrument identifier; non-empty.
        celex: 10-character CELEX identifier; ``None`` for an internal source.
        title: Instrument title; non-empty.
        short_name: Short name that drives graph naming; non-empty.
        version: Catalog version; non-empty.
        source_type: ``"external"`` or ``"internal"``.
        jurisdiction: ``"EU"`` for an external regulation; ``None`` otherwise.
        schema_version: Domain schema version the artifact was exported under; non-empty.
        exported_at: Export timestamp; non-empty.
        baseline_sha256: 64-character SHA-256 hex digest of the baseline blob.
        native_sha256: 64-character SHA-256 hex digest of the native blob.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str = Field(min_length=1)
    celex: str | None = Field(default=None, min_length=10, max_length=10)
    title: str = Field(min_length=1)
    short_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source_type: Literal["external", "internal"]
    jurisdiction: str | None = None
    schema_version: str = Field(min_length=1)
    exported_at: str = Field(min_length=1)
    baseline_sha256: str = Field(min_length=64, max_length=64)
    native_sha256: str = Field(min_length=64, max_length=64)


def _require_strict_base64(value: str) -> str:
    """Return ``value`` unchanged when it is strict base64, otherwise reject it.

    Strict means only alphabet characters with correct padding: whitespace, newlines and other
    non-alphabet characters are not skipped.

    Args:
        value: The submitted text.

    Returns:
        ``value``, unchanged.

    Raises:
        ValueError: ``value`` is not strict base64. The message is fixed and never carries the
            submitted text.
    """
    try:
        base64.b64decode(value, validate=True)
    except ValueError:  # binascii.Error is a ValueError
        raise ValueError("not valid base64") from None
    return value


_StrictBase64 = Annotated[str, Field(min_length=1), AfterValidator(_require_strict_base64)]


class RestorationRequest(BaseModel):
    """``POST /restorations`` body: one curated instrument's artifact.

    The two blobs are the artifact's UTF-8 JSON files, base64-encoded for JSON transport. The
    client reads them from disk and uploads the bytes unchanged; the server decodes them without
    re-encoding. A blob that is not strict base64 (alphabet characters only, correct padding, no
    whitespace) is a validation error, answered as the generic 422, and the submitted text is
    never echoed. Unknown fields are rejected.

    Attributes:
        instrument_id: Curated instrument identifier; non-empty.
        manifest: The artifact's manifest fields.
        baseline_blob_base64: Strict base64 of the baseline graph JSON; non-empty.
        native_blob_base64: Strict base64 of the native graph JSON; non-empty.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    instrument_id: str = Field(min_length=1)
    manifest: RestorationManifestPayload
    baseline_blob_base64: _StrictBase64
    native_blob_base64: _StrictBase64


class CatalogRestorationRequest(BaseModel):
    """``POST /restorations/from-catalog`` body: the instrument to fetch and restore.

    ``instrument_id`` reaches a URL path segment when the curated-content source is fetched, so
    it is restricted to letters, digits, ``_``, ``.`` and ``-`` and must start with a letter or
    digit, as defence in depth at the trust boundary. Unknown fields are rejected.

    Attributes:
        instrument_id: Curated instrument identifier; 1 to 128 characters.
    """

    model_config = ConfigDict(extra="forbid")

    instrument_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
    )


class RestorationStageOutcome(BaseModel):
    """One completed restore stage as reported in the accepted response.

    Attributes:
        stage: Stage name; non-empty.
        status: Always ``"succeeded"``; only completed stages are reported.
    """

    model_config = ConfigDict(frozen=True)

    stage: str = Field(min_length=1)
    status: Literal["succeeded"]


class RestorationAcceptedResponse(BaseModel):
    """Success body for ``POST /restorations``: the instrument id and per-stage outcomes.

    Attributes:
        instrument_id: Identifier of the restored instrument; non-empty.
        stages: Completed restore stages in execution order.
        near_miss_reviews_created: Number of Capability near-miss reviews the restore recorded;
            resolve them with the near-miss review tools.
    """

    model_config = ConfigDict(frozen=True)

    instrument_id: str = Field(min_length=1)
    stages: list[RestorationStageOutcome]
    near_miss_reviews_created: int = Field(ge=0)


class ExportRequest(BaseModel):
    """``POST /exports`` body: the id of one already-ingested instrument.

    Only ``instrument_id`` is accepted. Every other descriptor field (``short_name``, ``title``,
    ``source_type``, ``jurisdiction``, ``version``, ``celex``) is derived on the server from the
    ingested ``{short}_baseline`` graph and is never supplied by the client. The id is
    restricted to letters, digits, ``_``, ``.`` and ``-`` and must start with a letter or digit,
    as defence in depth at the trust boundary. Unknown fields are rejected.

    Attributes:
        instrument_id: Identifier of the ingested instrument, of the form ``{short}-{version}``;
            1 to 128 characters.
    """

    model_config = ConfigDict(extra="forbid")

    instrument_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
    )


class ExportManifestPayload(BaseModel):
    """The ``manifest.json`` fields carried inline in a ``POST /exports`` response.

    It has the same fields as ``RestorationManifestPayload``, here in the response body because
    an export produces a manifest instead of consuming one. Unknown fields are rejected.

    Attributes:
        instrument_id: Curated instrument identifier; non-empty.
        celex: 10-character CELEX identifier; ``None`` for an internal source.
        title: Instrument title; non-empty.
        short_name: Short name that drives graph naming; non-empty.
        version: Catalog version; non-empty.
        source_type: ``"external"`` or ``"internal"``.
        jurisdiction: ``"EU"`` for an external regulation; ``None`` otherwise.
        schema_version: Domain schema version the artifact was exported under; non-empty.
        exported_at: Export timestamp; non-empty.
        baseline_sha256: 64-character SHA-256 hex digest of the baseline blob.
        native_sha256: 64-character SHA-256 hex digest of the native blob.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str = Field(min_length=1)
    celex: str | None = Field(default=None, min_length=10, max_length=10)
    title: str = Field(min_length=1)
    short_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source_type: Literal["external", "internal"]
    jurisdiction: str | None = None
    schema_version: str = Field(min_length=1)
    exported_at: str = Field(min_length=1)
    baseline_sha256: str = Field(min_length=64, max_length=64)
    native_sha256: str = Field(min_length=64, max_length=64)


class ExportStageOutcome(BaseModel):
    """One completed export stage as reported in the accepted response.

    Attributes:
        stage: Stage name; non-empty.
        status: Always ``"succeeded"``; only completed stages are reported.
    """

    model_config = ConfigDict(frozen=True)

    stage: str = Field(min_length=1)
    status: Literal["succeeded"]


class ExportAcceptedResponse(BaseModel):
    """Success body for ``POST /exports``: the manifest plus both base64-encoded blobs.

    The blobs use the same base64 convention as ``RestorationRequest``, here in the response.
    The client decodes them and writes them to the destination directory it chose.

    Attributes:
        instrument_id: Identifier of the exported instrument; non-empty.
        manifest: The exported artifact's manifest fields.
        baseline_blob_base64: Base64 of the baseline graph JSON; non-empty.
        native_blob_base64: Base64 of the native graph JSON; non-empty.
        stages: Export stages in execution order, all ``"succeeded"``.
    """

    model_config = ConfigDict(frozen=True)

    instrument_id: str = Field(min_length=1)
    manifest: ExportManifestPayload
    baseline_blob_base64: str = Field(min_length=1)
    native_blob_base64: str = Field(min_length=1)
    stages: list[ExportStageOutcome]


class InstrumentCheckOutcomeBody(BaseModel):
    """One tracked instrument's classification, as reported by `POST /change-checks`.

    Attributes:
        instrument_id: Identifier of the tracked instrument; non-empty.
        outcome: ``"current"`` (no newer version), ``"poll_failed"`` (the source could not be
            polled), ``"not_configured"`` (the instrument has no poll source),
            ``"amendment_reingested"`` (a newer version was found and is now fully absorbed:
            a fresh, resumed, repaired, finalised or already-processed re-ingest, which
            ``detail`` names), ``"skipped"`` (a national transposition, which is not polled),
            or ``"reingest_failed"`` (any other re-ingest failure).
        detail: Further detail for the outcome; for ``"reingest_failed"`` an enumerated reason
            code, never exception text. ``None`` when there is none.
        reingest_run_id: Run id of the re-ingest this check started; ``None`` when none was
            started.
    """

    model_config = ConfigDict(frozen=True)

    instrument_id: str = Field(min_length=1)
    outcome: Literal[
        "current",
        "amendment_reingested",
        "poll_failed",
        "not_configured",
        "skipped",
        "reingest_failed",
    ]
    detail: str | None = None
    reingest_run_id: str | None = None


class ChangeCheckResponse(BaseModel):
    """Response body for `POST /change-checks`: the run id and each tracked instrument's outcome.

    Attributes:
        run_id: Identifier of the sweep; non-empty.
        instruments: One outcome per tracked instrument; empty when none is tracked.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    instruments: list[InstrumentCheckOutcomeBody]


class PendingReviewEntry(BaseModel):
    """One unresolved near-miss `PendingReview` as returned by `GET /near-misses`.

    It carries the review's own id, the two texts and the similarity score, but not the ids of
    the two graph nodes the review refers to.

    Attributes:
        id: Identifier of the review; non-empty.
        kind: Kind of entity pair under review (for example ``"Capability"``); non-empty.
        incoming_text: Text of the newly derived entity; non-empty.
        nearest_existing_text: Text of the existing entity it resembles; non-empty.
        similarity: Similarity score between the two texts, from 0.0 to 1.0.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    incoming_text: str = Field(min_length=1)
    nearest_existing_text: str = Field(min_length=1)
    similarity: float = Field(ge=0.0, le=1.0)


class PendingReviewListResponse(BaseModel):
    """Response body for `GET /near-misses`.

    Attributes:
        reviews: Every unresolved review; empty when there are none.
    """

    model_config = ConfigDict(frozen=True)

    reviews: list[PendingReviewEntry]


class ResolveReviewRequest(BaseModel):
    """`POST /near-misses/{review_id}/resolve` body.

    Any `decision` other than the two allowed values is rejected with a 422 validation error
    before the route runs. Unknown fields are rejected.

    Attributes:
        decision: ``"keep-separate"`` clears the pending review and changes nothing else.
            ``"merge"`` requests a merge of the two entities of the review, which needs a Compliance
            Officer and a signed passkey approval before it takes effect.
        acknowledge_governance_change: ``True`` to accept the governance change of a merge where
            exactly one of the two Capabilities has a governing Policy. Default ``False``: such a
            merge then returns its preview and ``acknowledgment_required`` and creates no approval.
            It has no effect on ``keep-separate`` or on the other merge cases, and a Role review
            refuses ``True`` because a Role has no governing Policy.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["keep-separate", "merge"]
    acknowledge_governance_change: bool = False


class ResolveReviewResponse(BaseModel):
    """Response body for `POST /near-misses/{review_id}/resolve`.

    A ``"merge"`` decision does not execute synchronously: it previews the merge and, once any
    required acknowledgment is given, carries the pending signed-passkey approval; ``winner_id``
    and ``loser_id`` stay ``None`` (they are reported by `GET /near-misses/approvals/{id}` after
    the approval is signed). A ``"keep-separate"`` decision carries no approval or preview fields.

    Attributes:
        review_id: Identifier of the resolved review; non-empty.
        decision: The decision taken, ``"keep-separate"`` or ``"merge"``.
        winner_id: Id of the surviving entity; ``None`` in the responses this route returns.
        loser_id: Id of the absorbed entity, which is deleted and replaced by a
            ``MergedCapability`` or ``MergedRole`` marker; ``None`` in the responses this route
            returns.
        pending_approval_id: Id of the pending passkey approval for a merge; ``None`` otherwise,
            including a merge that still needs its acknowledgment.
        approval_url: URL where the approver signs the merge; ``None`` unless a merge is
            pending.
        expires_at: Expiry time of the pending approval; ``None`` unless a merge is pending.
        preview: What the merge will do; set for a merge, ``None`` for ``keep-separate``. A
            Capability review carries a ``CapabilityMergePreview`` (survivor, absorbed, Policy case,
            edges that move, state digest); a Role review carries a ``RoleMergePreview``.
        acknowledgment_required: ``True`` when the merge needs ``acknowledge_governance_change``
            before an approval is created; ``None`` otherwise.
        message: The acknowledgment text to show the officer; set with
            ``acknowledgment_required``, ``None`` otherwise.
    """

    model_config = ConfigDict(frozen=True)

    review_id: str = Field(min_length=1)
    decision: str
    winner_id: str | None = None
    loser_id: str | None = None
    pending_approval_id: str | None = None
    approval_url: str | None = None
    expires_at: str | None = None
    preview: CapabilityMergePreview | RoleMergePreview | None = None
    acknowledgment_required: bool | None = None
    message: str | None = None


class PendingApprovalStatusResponse(BaseModel):
    """Response body for `GET /near-misses/approvals/{pending_approval_id}`.

    It has the same fields as the `check-near-miss-approval` tool's response, because both
    surfaces report the same graph-cleanup `merge-capabilities` approval through one mapper.

    Attributes:
        pending_approval_id: Identifier of the approval; non-empty.
        status: ``"pending"`` (awaiting signature), ``"expired"`` or ``"signed"``.
        review_id: Review the approval belongs to; ``None`` when not reported.
        decision: Decision the approval authorises; ``None`` when not reported.
        winner_id: Id of the surviving Capability once the merge ran; otherwise ``None``.
        loser_id: Id of the absorbed Capability (deleted behind a ``MergedCapability`` marker)
            once the merge ran; otherwise ``None``.
        error: Sanitised failure text when the approved merge failed; otherwise ``None``.
    """

    model_config = ConfigDict(frozen=True)

    pending_approval_id: str = Field(min_length=1)
    status: Literal["pending", "expired", "signed"]
    review_id: str | None = None
    decision: str | None = None
    winner_id: str | None = None
    loser_id: str | None = None
    error: str | None = None


class ProtectedResourceMetadata(BaseModel):
    """RFC 9728 (OAuth 2.0 Protected Resource Metadata) document.

    Served unauthenticated at ``GET /.well-known/oauth-protected-resource``; the
    ``WWW-Authenticate: Bearer resource_metadata="..."`` header of an unauthorised response
    points at the same URL. ``ps_cli_client_id`` is a project extension, not part of RFC 9728;
    the route omits it entirely, rather than rendering ``null``, when it is unset.

    Attributes:
        resource: Canonical URL of this protected resource; non-empty.
        authorization_servers: Issuers that can authorise access; empty when authentication is
            not configured.
        scopes_supported: Scopes the resource accepts; empty when authentication is not
            configured.
        ps_cli_client_id: OAuth client id of the command-line client; ``None`` when not
            configured.
    """

    model_config = ConfigDict(frozen=True)

    resource: str = Field(min_length=1)
    authorization_servers: list[str]
    scopes_supported: list[str]
    ps_cli_client_id: str | None = None
