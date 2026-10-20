"""Domain-specific exception types raised at the `ps_service.api` HTTP boundary.

One type per distinct failure boundary, never a bare `Exception` or `ValueError`. Routes and
orchestration code raise one of these; `error_handlers.register_exception_handlers` maps each to
its HTTP status and builds the structured error body. `fastapi.HTTPException` with free text is
never raised by this layer. Every type derives from :class:`ApiError`.
"""

from __future__ import annotations


class ApiError(Exception):
    """Base type for every `ps_service.api` boundary error. Never raised directly.

    Lets `error_handlers` whitelist the whole family and lets callers `except ApiError` without
    enumerating the subclasses.
    """


class CatalogIdentifierNotFoundError(ApiError):
    """A `POST /ingestions` request named a CELEX that does not exist on Cellar/ELI.

    Raised by the Cellar resolver before any pipeline stage runs. The caller should correct the
    CELEX. Handled as HTTP 404; `str(exc)` is domain-level and surfaced verbatim.
    """


class InternalSeedValidationError(ApiError):
    """An internal seed document is malformed, carries an unknown label, or is not internal.

    The API-boundary translation of the internal-seed adapter's own `InternalSeedError` (the
    adapter must not import `ps_service.api`). Raised before any pipeline stage runs. The caller
    should fix the seed document. Handled as HTTP 422; `str(exc)` is surfaced verbatim.
    """


class IngestionConfigIncompleteError(ApiError):
    """The resolved `ServiceConfig` is missing a value the pipeline needs.

    One of `llm_interface_model`, `llm_interface_embed_model`,
    `company_merge_capability_merge_threshold`, `company_merge_capability_review_threshold` or
    `company_merge_role_review_threshold` is `None`. Raised by the orchestration's config guard
    before any graph or stage call. This is an operator problem, not a caller problem. Handled as
    HTTP 503; `str(exc)` is surfaced verbatim.
    """


class GraphWriteUnavailableError(ApiError):
    """No Graph Write Gateway exists, so the request would have to write to a graph directly.

    Raised before any graph is opened when the service has no gateway (the PS state Postgres that
    holds the graph log is not configured, or the gateway could not be built at startup). Fail
    closed by design: a graph write never falls back to a direct FalkorDB write. This is an
    operator problem. Handled as HTTP 503; `str(exc)` is surfaced verbatim.
    """


class RestoreArtifactRejectedError(ApiError):
    """A `POST /restorations` artifact failed integrity or schema-version verification.

    The API-boundary translation of `ps_service.restore.errors.ArtifactIntegrityError` (checksum
    mismatch) and `ps_service.restore.errors.ArtifactSchemaVersionMismatchError`, both raised by
    `ps_service.restore.restore_instrument` before any FalkorDB call, so a rejected artifact never
    causes even a staged-key write. Raised by `api.restore_orchestration.run_restoration` before
    any pipeline stage runs. The caller should supply an intact artifact of the supported schema
    version. Handled as HTTP 422; `str(exc)` is domain-level and surfaced verbatim.
    """


class RestoreStageFailedError(ApiError):
    """A restore stage raised; the restore did not complete.

    Carries the failing stage name and an already sanitised reason, in the same shape as
    `PipelineStageError`. Handled as HTTP 502.

    Attributes:
        stage: The restore stage that failed.
        reason: A caller-safe, scrubbed reason string.
    """

    def __init__(self, *, stage: str, reason: str) -> None:
        """Record the failing stage and its sanitised reason.

        Args:
            stage: The restore stage that raised (for example `"staging"`).
            reason: A caller-safe reason string, already scrubbed of paths and hosts.
        """
        super().__init__(f"{stage} stage failed: {reason}")
        self.stage: str = stage
        self.reason: str = reason


class RequestBodyTooLargeError(ApiError):
    """A request body's `Content-Length` exceeds `ServiceConfig.max_request_body_bytes`.

    Raised by `main._MaxBodySizeMiddleware`, a pure ASGI middleware added directly on the app
    (not `BaseHTTPMiddleware`, which buffers the whole body), before Starlette reads any request
    body bytes. The caller should send a smaller body. Handled as HTTP 413; `str(exc)` is
    domain-level and surfaced verbatim.
    """


class PendingReviewNotFoundError(ApiError):
    """A `POST /near-misses/{review_id}/resolve` review id does not exist or was already resolved.

    A resolved review's `PendingReview` node is deleted outright, never soft-status-changed, so a
    nonexistent id and an already resolved one are the same not-found condition. Also raised for a
    merge whose incoming or existing node no longer exists (a stale review), with its own message.
    Raised by `api.near_miss_review_orchestration.run_resolve_near_miss` as the API-boundary
    translation of `ps_service.company_merge.pending_review.resolve_review` returning `None` or
    raising `StalePendingReviewError`; `ps_service.company_merge` never raises or imports this
    type. Handled as HTTP 404; `str(exc)` is domain-level and surfaced verbatim.
    """


class NearMissMergeRefusedError(ApiError):
    """A `POST /near-misses/{review_id}/resolve` merge is refused by the graph-cleanup merge rules.

    Raised for a review of a kind that has no merge (only a Capability near miss can be merged;
    any other kind can only be kept separate), and for two Capabilities governed by different
    Policies (release one with `release-capability-governance` first). Nothing was written and no
    approval exists. The message is written for the Compliance Officer; it is the translation of
    `ps_service.graph_cleanup.errors.GraphCleanupValidationError`. Handled as HTTP 409;
    `str(exc)` is domain-level and surfaced verbatim.
    """


class ReviewKindUnsupportedError(ApiError):
    """A stored near-miss review has a kind with no registered spec.

    Raised by the near-miss review and export orchestration as the API-boundary translation of
    `ps_service.company_merge.errors.UnknownReviewKindError`, for list, resolve, merge and export.
    The operation fails closed: one stray review refuses the whole operation, because hiding it
    could unblock an export. `str(exc)` is domain-level, names the kind so the row can be found,
    and is surfaced verbatim. Handled as HTTP 409 (`review_kind_unsupported`).
    """


class InternalIngestOwnerRequiredError(ApiError):
    """A `POST /ingestions` internal seed carries a Policy but the caller is not verified.

    An imported Policy tree is created `draft` and owned by the ingesting caller; an ownerless
    draft would be unreachable by the propose and edit flow, so the pipeline refuses a
    Policy-bearing seed given no owner before any stage, audit row or graph write. Over
    `POST /ingestions` the `ComplianceOfficer` gate rejects an unverified caller first, so this is
    a defence-in-depth guard. Handled as HTTP 401 (`owner_required`) if it ever surfaces;
    `str(exc)` is a fixed, domain-level message.
    """


class MergeApprovalRequiresAuthenticatedCallerError(ApiError):
    """A `POST /near-misses/{review_id}/resolve` call with `decision="merge"` has no real caller.

    A merge creates a signed-passkey pending approval rather than executing synchronously, and
    that approval must be bound to a real, verified actor identity. An unauthenticated request, or
    one running under the local-test bypass (which never establishes a real `Principal`), is
    refused before any Postgres or FalkorDB write, matching the MCP tool's own fail-closed actor
    check. The caller should authenticate. Handled as HTTP 401, the same shape as
    `RestAuthMiddleware`'s 401; `str(exc)` is a fixed, domain-level message, safe to surface
    verbatim.
    """


class PendingApprovalNotFoundError(ApiError):
    """A `GET /near-misses/approvals/{pending_approval_id}` id is unknown or not this caller's.

    Raised for both "no such id" and "this id belongs to a different actor", and the two are never
    distinguished in the response, so the endpoint leaks nothing about other actors' approvals. It
    mirrors `ps_service.api.near_miss_review_orchestration.check_near_miss_merge_approval`, which
    returns `None` for either case and for any approval that is not a near-miss merge. Handled as
    HTTP 404; `str(exc)` is domain-level and safe to surface verbatim.
    """


class PendingApprovalInvalidOrExpiredError(ApiError):
    """A companion-browser `/approvals/{id}/*` call's code or state failed verification.

    Raised for an unknown `pending_approval_id`, a `code` whose `sha256` digest does not match the
    row's `code_hash`, and a row that is no longer `'pending'` or has passed its `expires_at`.
    The cases are deliberately never distinguished in the response, so a caller never learns which
    condition applies. Raised by the `ps_service.passkey_signing.router` handlers and by
    `ps_service.passkey_signing.service._require_pending_and_unexpired`, before any WebAuthn
    library call or Postgres write. The message is fixed in `__init__` and never caller-supplied,
    so every raise site produces byte-identical text. Handled as HTTP 404; `str(exc)` is safe to
    surface verbatim.
    """

    def __init__(self) -> None:
        """Fix the message so every raise site produces byte-identical text."""
        super().__init__("This approval link is no longer valid.")


class ExportInstrumentNotFoundError(ApiError):
    """A `POST /exports` `instrument_id` names no actually ingested instrument.

    Raised by `api.export_orchestration.run_export` before any file I/O when `instrument_id` is
    malformed (no `{short}-{version}` shape; the graph is not touched), when the derived
    `{short}_baseline` graph key does not exist (checked with `db.list_graphs()` before any
    `MATCH` is issued against that key), or when the graph exists but holds no
    `RegulatoryInstrument` node under this id. The caller should name an ingested instrument.
    Handled as HTTP 404; `str(exc)` is domain-level and surfaced verbatim.
    """


class ExportConfigIncompleteError(ApiError):
    """The resolved `ServiceConfig` is missing the embedding model that export needs.

    `llm_interface_embed_model` is `None`. Raised by the orchestration's config guard before any
    FalkorDB call. This is an operator problem. Handled as HTTP 503; `str(exc)` is surfaced
    verbatim.
    """


class ExportUnresolvedNearMissesError(ApiError):
    """A `POST /exports` instrument still has unresolved near-miss reviews.

    Raised by `api.export_orchestration.run_export` after the instrument is found and before the
    `export.instrument` audit row is recorded, when at least one resolvable `PendingReview` in
    `policy_system` names an id of a node of a registered reviewable kind (Capability, Role) in the
    instrument's `{short}_baseline` graph. Review decisions live outside the exported graphs, so
    exporting now would publish nodes that were never reviewed. The message names no single kind,
    because a review of any registered kind can refuse the export. The caller resolves the reviews
    (`near_misses_list`, `near_misses_resolve`) and exports again. Handled as HTTP 409; `str(exc)`
    is the full message, safe to surface verbatim, and is the one text the maintainer script
    also prints.

    Attributes:
        instrument_id: The instrument whose export was refused.
        review_ids: Ids of the blocking reviews, in the order given.
    """

    def __init__(self, instrument_id: str, review_ids: tuple[str, ...]) -> None:
        """Build the message from the instrument id, the count and the review ids.

        Args:
            instrument_id: The instrument whose export was refused.
            review_ids: Ids of the blocking reviews; at least one.
        """
        self.instrument_id = instrument_id
        self.review_ids = review_ids
        noun, verb = ("review", "involves") if len(review_ids) == 1 else ("reviews", "involve")
        super().__init__(
            f"cannot export {instrument_id!r}: {len(review_ids)} unresolved near-miss {noun} "
            f"{verb} entities of this instrument ({', '.join(review_ids)}). "
            "Resolve them (near_misses_list, near_misses_resolve) and export again."
        )

    @property
    def count(self) -> int:
        """The number of blocking reviews."""
        return len(self.review_ids)


class ExportStageFailedError(ApiError):
    """An export stage raised; the export did not complete.

    Carries the failing stage name and an already sanitised reason, in the same shape as
    `RestoreStageFailedError`. Handled as HTTP 502.

    Attributes:
        stage: The export stage that failed.
        reason: A caller-safe, scrubbed reason string.
    """

    def __init__(self, *, stage: str, reason: str) -> None:
        """Record the failing stage and its sanitised reason.

        Args:
            stage: The export stage that raised (for example `"serialization"`).
            reason: A caller-safe reason string, already scrubbed of paths and hosts.
        """
        super().__init__(f"{stage} stage failed: {reason}")
        self.stage: str = stage
        self.reason: str = reason


class CuratedSourceUnavailableError(ApiError):
    """The configured curated-content source could not be fetched.

    The API-boundary translation of `ps_service.curated_source.errors.CuratedSourceFetchError`,
    raised by the `GET /catalog` route handler when the injected fetch dependency raises (the
    source is unreachable, or its response is missing or malformed). Never a silent fallback to
    stale data. Handled as HTTP 502; `str(exc)` is domain-level (names the source URL and the
    failure) and surfaced verbatim.
    """


class RestoreInstrumentIdAmbiguousError(ApiError):
    """A requested `instrument_id` matches more than one catalog entry case-insensitively.

    The API-boundary translation of
    `ps_service.curated_source.errors.CuratedSourceAmbiguousInstrumentIdError`, raised by
    `api.restore_orchestration.run_restoration_from_catalog_source` before any artifact fetch is
    attempted. Like `ShortNameCollisionError`, it reports that more than one thing claims one
    identity. The caller should use the exact canonical id. Handled as HTTP 409; `str(exc)` is
    domain-level (names the requested id and every colliding canonical id) and surfaced verbatim.
    """


class RestoreInstrumentIdNotFoundError(ApiError):
    """A requested `instrument_id` matches no catalog entry in any letter case.

    The API-boundary translation of
    `ps_service.curated_source.errors.CuratedSourceUnknownInstrumentIdError`, raised by
    `api.restore_orchestration.run_restoration_from_catalog_source` before any artifact fetch is
    attempted. The caller should pick an id from the catalog. Handled as HTTP 404; `str(exc)` is
    domain-level (names the requested id and the closest candidate ids) and surfaced verbatim.
    """


class CatalogSourceOverrideUnavailableError(ApiError):
    """The catalog-source override could not be read.

    The API-boundary translation of `ps_service.runtime_config`'s read failures, raised by
    `GET /catalog` and `POST /restorations/from-catalog` when resolving the effective
    curated-content source fails (the PS state Postgres is unreachable, or the stored value no
    longer validates). Fail closed: the environment-variable or default source is never served in
    its place, because the operator's override may point somewhere else on purpose. This is an
    operator problem. The message is fixed and carries no host, port or driver detail. Handled as
    HTTP 503; `str(exc)` is safe to surface verbatim.
    """

    def __init__(self) -> None:
        """Set the one fixed, detail-free message."""
        super().__init__(
            "The catalog source configuration could not be read; "
            "the request was not served from any source."
        )


class AccessDeniedError(ApiError):
    """A caller's active `AccessRole` set does not satisfy a role-gated action's minimum.

    Raised by `ps_service.authz.service.RoleDecision.raise_if_denied` (via `check_role`), by the
    request gate `ps_service.authz.request_gate.enforce_role_gate` and, transitively, by
    `grant_role`, `revoke_role` and `list_assignments`. The message is fixed at the raise site,
    never a caller-supplied value, so it leaks no internal detail. Handled as HTTP 403;
    `str(exc)` is safe to surface verbatim.
    """


class InvalidAccessRoleError(ApiError):
    """A grant or revoke call named an `AccessRole` outside the closed set this flow manages.

    Raised by `ps_service.authz.service.grant_role` and `revoke_role` for a string that either
    fails to construct as an `AccessRole` (`AccessRole(access_role)` raising `ValueError`) or
    constructs but names a role this flow does not manage (for example `AuthenticatedUser`, which
    is never grantable or revocable through these tools). The message is fixed at the raise site,
    never a caller-supplied value, so it leaks no internal detail. The caller should name a
    manageable role. Handled as HTTP 400; `str(exc)` is safe to surface verbatim.
    """


class SelfGrantOrRevokeBlockedError(ApiError):
    """A caller attempted to grant or revoke one of their own `AccessRole`s.

    Raised by `ps_service.authz.service.grant_role` and `revoke_role` when
    `ps_service.authz.rules.block_self_target` rejects the call (actor and target are the same
    `(sub, iss)` pair). The message is fixed at the raise site. Handled as HTTP 403; `str(exc)` is
    safe to surface verbatim.
    """


class SystemOwnerFloorViolationError(ApiError):
    """A revoke would leave zero active `SystemOwner`s.

    Raised by `ps_service.authz.service.revoke_role` when
    `ps_service.authz.rules.enforce_system_owner_floor` rejects a `SYSTEM_OWNER` revoke (the
    target is the last remaining active `SystemOwner`), and also when the store's own
    advisory-locked recount
    (`ps_service.authz.store.PsycopgAccessRoleStore._revoke_system_owner`) detects the same
    condition through a concurrent-revoke race (`AccessRoleSystemOwnerFloorRaceError`, translated
    here). The message is fixed at the raise site. Handled as HTTP 403; `str(exc)` is safe to
    surface verbatim.
    """


class InvalidAuditQueryFilterError(ApiError):
    """A `list-audit-events` filter or pagination argument is invalid.

    Raised by `ps_service.authz.service.list_audit_events` before `AuditStore.query` is called,
    naming the invalid input in its message: an unknown `action` (not in the typed-model
    registry), an unknown `resource_type` (not in the resource-type registry), an `occurred_from`
    later than `occurred_to`, or a `page_size` above the configured maximum. The message is
    caller-safe and domain-level, never internal detail. The caller should correct the filter.
    Handled as HTTP 400; `str(exc)` is safe to surface verbatim.
    """


class AuthorizationStoreUnavailableError(ApiError):
    """The PS state Postgres store could not be reached for a role-gated action.

    Raised by `ps_service.authz.service`'s functions when the underlying `ps_service.authz.store`
    raises `StatePostgresConnectionError` or `AccessRoleAssignmentPersistenceError`: the store is
    unconfigured or unreachable, so the action fails closed rather than falling open. This is an
    operator problem. The message is fixed at the raise site. Handled as HTTP 503; `str(exc)` is
    safe to surface verbatim.
    """


class CelexAlreadyIngestedError(ApiError):
    """A CELEX is already present in the live graph.

    Raised by `ingestion_orchestration.resolve_ingestion_entry` before any pipeline stage runs.
    It is shared by `POST /ingestions` and the `ingest_regulation` MCP tool, so both entry points
    reject a re-ingest of an already ingested CELEX (under any `short_name`, including the same
    one) with byte-identical semantics. The message names only the CELEX and the `short_name` it
    is already ingested under. Handled as HTTP 409; `str(exc)` is domain-level and surfaced
    verbatim.
    """


class ShortNameCollisionError(ApiError):
    """A `short_name` is already claimed by a different instrument (a different CELEX).

    Raised by `ingestion_orchestration.resolve_ingestion_entry` before any pipeline stage runs.
    The check reads only the live graph (`ingestion_orchestration.check_short_name_collision`): a
    different CELEX already recorded under this `short_name`, compared case-insensitively. The
    curated catalog is never consulted. Shared by `POST /ingestions` and the `ingest_regulation`
    MCP tool, so both entry points reject a cross-instrument collision with byte-identical
    semantics. Handled as HTTP 409; `str(exc)` is domain-level and surfaced verbatim.
    """


class PipelineStageError(ApiError):
    """A pipeline stage raised; later stages were skipped.

    Carries the failing stage name and an already sanitised reason so the handler can name the
    stage in the body without re-deriving it. Handled as HTTP 502.

    Attributes:
        stage: The pipeline stage that failed.
        reason: A caller-safe, scrubbed reason string.
    """

    def __init__(self, *, stage: str, reason: str) -> None:
        """Record the failing stage and its sanitised reason.

        Args:
            stage: The pipeline stage that raised (for example `"extraction"`).
            reason: A caller-safe reason string, already scrubbed of paths and hosts.
        """
        super().__init__(f"{stage} stage failed: {reason}")
        self.stage: str = stage
        self.reason: str = reason


class SigningHostNotAllowedError(ApiError):
    """A companion-browser `/approvals/{id}/*` POST arrived on a `Host` that is not configured.

    Raised before the approval id or code is read, so the response is no oracle for approval
    ids and no challenge is issued. The message is fixed in `__init__` and names neither the
    requested host nor any configured one. Handled as HTTP 421 (Misdirected Request);
    `str(exc)` is safe to surface verbatim.
    """

    def __init__(self) -> None:
        """Fix the message so every raise site produces byte-identical text."""
        super().__init__("This address is not permitted for approvals.")
