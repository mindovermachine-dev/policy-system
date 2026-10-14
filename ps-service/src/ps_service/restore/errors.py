"""Domain-specific exception types for `ps_service.restore`.

One exception type per distinct failure boundary this component owns, never a generic
`Exception` or `ValueError`. The artifact errors (`ArtifactIntegrityError`,
`ArtifactSchemaVersionMismatchError`, `ArtifactContentRejectedError`) and
`RestoreConcurrencyConflictError` are plain, standalone siblings, not subclasses of one another:
each names one distinct boundary. The three gateway-write failures share the base
`RestoreGraphWriteError`.
"""

from __future__ import annotations


class RestoreConcurrencyConflictError(Exception):
    """`policy_system` was modified by another writer on every attempt.

    Raised by `restore_instrument` after `MAX_POLICY_SYSTEM_ATTEMPTS` consecutive attempts whose
    group the gateway refused because `policy_system` had moved since it was read. Chained via
    `raise ... from <the last StaleGraphStateError>`. Nothing was logged, so the live
    `policy_system`, `{short}_native` and `{short}_baseline` graphs are untouched; the caller may
    retry the restore.
    """


class RestoreGraphWriteError(Exception):
    """A restore's graph write did not take effect through the Graph Write Gateway.

    Base of the three failures `restore_instrument` raises for its multi-graph group. The
    message is fixed and sanitized (no host, query or content), so it is safe to carry; the
    gateway's own error, if any, is chained as `__cause__`.
    """


class RestoreGroupRejectedError(RestoreGraphWriteError):
    """The gateway refused the restore's group (off the allow-list, a missing target).

    Nothing was logged on any of the graphs. A retry fails the same way until the artifact or
    the gateway configuration changes.
    """


class RestoreApplyPendingError(RestoreGraphWriteError):
    """The restore is committed to the graph log but not yet applied to every graph.

    The log holds the whole restore and the gateway applies it on recovery (or `catch_up`);
    a re-run is idempotent. Raised, not returned, so a restore never reports success for a
    graph that does not yet hold the instrument.
    """


class RestoreGraphWriteFailedError(RestoreGraphWriteError):
    """The gateway could not reach a store or FalkorDB refused an entry; nothing took effect.

    A group the log did commit but FalkorDB refused is blocked and awaits operator action;
    the message does not say which, only that the restore did not complete.
    """


class RestoreOwnerRequiredError(Exception):
    """An artifact carries Policy/Standard/Control content but no restoring owner was supplied.

    Raised by `restore_instrument.restore_instrument` (and, as a backstop, by
    `merge_primitives.build_policy_system_leg`) when an artifact's baseline holds a Policy and
    the caller's verified `(sub, iss)` identity was not passed. A restored tree is imported as
    `draft`, owned by whoever restored it; an ownerless draft would be unreachable by the normal
    propose/edit flow, so the restore is refused instead. For the restore entry points this is
    raised before the gateway is opened, so no graph is touched. The caller should supply the
    restoring identity and retry.
    """

    def __init__(self) -> None:
        """Carry the one caller-safe message every raise site shares."""
        super().__init__(
            "restoring governance content (Policy/Standard/Control) requires the restoring "
            "caller's verified identity, which becomes the owner of the imported drafts"
        )


class ArtifactContentRejectedError(Exception):
    """A parsed artifact's content is invalid or does not match its own manifest.

    Raised before the gateway is opened, so a rejected artifact writes nothing. Raise sites:

    - `schema_allowlist.validate_serialized_graph`, when a node or edge label or a
      relationship type falls outside its allow-list, a node's `properties["id"]` is missing or
      not a string, or a `Capability` is a merge tombstone.
    - `leg_primitives`, when an edge carries an identity other than `TYPE:source->target` or a
      node or edge holds a value the gateway cannot store.
    - `merge_primitives.build_policy_system_leg`, when the manifest's `instrument_id` matches no
      `RegulatoryInstrument.id` present in the baseline graph; without the check the merge
      would persist every RegulatoryInstrument property blanked out instead of failing.

    The artifact must be corrected; a retry fails the same way.
    """


class ArtifactIntegrityError(Exception):
    """A `RestoreArtifact` blob's SHA-256 doesn't match its manifest digest.

    Raised by `restore_instrument.restore_instrument` when `export.serialize.checksum_bytes` of
    `baseline_blob` or `native_blob` differs from `manifest.baseline_sha256` or `native_sha256`.
    The check runs first, before the `schema_version` comparison and before any graph call, so a
    corrupted artifact is refused before anything is written rather than rolled back.
    """


class ArtifactSchemaVersionMismatchError(Exception):
    """A `RestoreArtifact` manifest's `schema_version` doesn't match this service's own version.

    Raised by `restore_instrument.restore_instrument` immediately after checksum verification
    passes, still before any graph call. There is no migrate or warn path: an exact string
    mismatch always refuses outright. The artifact must be re-exported under this service's
    schema version.
    """
