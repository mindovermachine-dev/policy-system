"""Domain-specific exception types for `ps_service.restore`.

One exception type per distinct failure boundary this component owns,
never a generic `Exception`/`ValueError` (L1 Error Handling, L2 Error
Handling, `level2-python-instructions.md:141-144`). `ArtifactIntegrityError`/
`ArtifactSchemaVersionMismatchError` (PLAN.md Slices 5.1/5.2, D9/D10) and
`RestoreConcurrencyConflictError`/`ArtifactContentRejectedError` (CHANGES.md
B1, CHANGES2.md §2.4) are plain, standalone siblings, not subclasses of one
another -- each names one distinct boundary this component owns.
`RestoreStageError` (PLAN.md §2's module table) belongs to Batch 5's later
orchestration work (Slices 5.5+) and is not yet built here.
"""

from __future__ import annotations


class RestoreConcurrencyConflictError(Exception):
    """`policy_system` was modified by another writer on every attempt.

    Raised by `restore_instrument` after `MAX_POLICY_SYSTEM_ATTEMPTS` consecutive
    attempts whose group the gateway refused because `policy_system` had moved since it
    was read (issue #214: the expected log position replaces the pre-#214 WATCH guard).
    Chained via `raise ... from <the last StaleGraphStateError>`. Nothing was logged, so
    the live `policy_system`/`{short}_native`/`{short}_baseline` graphs are untouched.
    """


class RestoreGraphWriteError(Exception):
    """A restore's graph write did not take effect through the Graph Write Gateway (issue #214).

    Base of the three failures `restore_instrument` raises for its one multi-graph group. The
    message is fixed and sanitized (no host, query or content), so it is safe to carry; the
    gateway's own error, if any, is chained as `__cause__`.
    """


class RestoreGroupRejectedError(RestoreGraphWriteError):
    """The gateway refused the restore's group (off the allow-list, a missing target).

    Nothing was logged on any of the three graphs.
    """


class RestoreApplyPendingError(RestoreGraphWriteError):
    """The restore is committed to the graph log but not yet applied to every graph.

    The log holds the whole restore and the gateway applies it on recovery (or `catch_up`);
    a re-run is idempotent. Raised, not returned, as the earlier migrated writers do, so a
    restore never reports success for a graph that does not yet hold the instrument.
    """


class RestoreGraphWriteFailedError(RestoreGraphWriteError):
    """The gateway could not reach a store or FalkorDB refused an entry; nothing took effect.

    A group the log did commit but FalkorDB refused is blocked and awaits operator action;
    the message does not say which, only that the restore did not complete.
    """


class RestoreOwnerRequiredError(Exception):
    """An artifact carries Policy/Standard/Control content but no restoring owner was supplied.

    Raised by `restore_instrument.restore_instrument` (and, as a backstop, by
    `merge_primitives.build_policy_system_leg`) when an internal-sourced artifact's
    baseline holds a Policy and the caller's verified `(sub, iss)` identity was
    not passed (issue #183). A restored tree is imported as `draft`, owned by
    whoever restored it; an ownerless draft would be unreachable by the normal
    propose/edit flow, so the restore is refused instead. For the restore
    entry points this is raised before the gateway is opened, so no graph is
    touched.
    """

    def __init__(self) -> None:
        """Carry the one caller-safe message every raise site shares."""
        super().__init__(
            "restoring governance content (Policy/Standard/Control) requires the restoring "
            "caller's verified identity, which becomes the owner of the imported drafts"
        )


class ArtifactContentRejectedError(Exception):
    """A parsed artifact's content is invalid or does not match its own manifest.

    Two distinct raise sites:

    - `schema_allowlist.validate_serialized_graph` (CHANGES2.md §2.4's Query
      Safety guard), when a node/edge label or relationship_type falls
      outside its allow-list, or a node's `properties["id"]` is missing or
      not a string. Raised before the gateway is opened -- a rejected artifact writes nothing.
    - `merge_primitives.build_policy_system_leg`, when the manifest's
      `instrument_id` does not match any `RegulatoryInstrument.id` actually
      present in the baseline graph -- without this check, the merge would
      silently persist every RegulatoryInstrument property (celex, title,
      ...) blanked out instead of failing (confirmed empirically).
    """


class ArtifactIntegrityError(Exception):
    """A `RestoreArtifact` blob's SHA-256 doesn't match its manifest digest.

    Raised by `restore_instrument.restore_instrument` (D9) when
    `export.serialize.checksum_bytes` of `baseline_blob`/`native_blob`
    differs from `manifest.baseline_sha256`/`native_sha256` -- checked
    first, before `schema_version` comparison (D10) and before any
    FalkorDB call of any kind (D8 step 1's "zero graph calls before this
    passes"). AC-BI-010's "before writing" is satisfied by this ordering,
    not a rollback.
    """


class ArtifactSchemaVersionMismatchError(Exception):
    """A `RestoreArtifact` manifest's `schema_version` doesn't match this service's own version.

    Raised by `restore_instrument.restore_instrument` (D10) immediately
    after checksum verification (D9) passes, still before any FalkorDB
    call. No migrate/warn path -- an exact string mismatch always refuses
    outright, matching AC-BI-009.
    """
