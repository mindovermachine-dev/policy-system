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
    """`policy_system` was modified by another writer on every retry attempt.

    Raised by `staging.stage_and_finalize_policy_system_leg` after
    `_MAX_POLICY_SYSTEM_MERGE_ATTEMPTS` consecutive `redis.exceptions.
    WatchError`s (CHANGES.md B1's optimistic-concurrency retry loop) --
    chained via `raise ... from <the last WatchError>`. By the time this is
    raised, every staged key for this restore attempt has already been
    discarded and the live `policy_system`/`{short}_native`/
    `{short}_baseline` graphs are untouched.
    """


class ArtifactContentRejectedError(Exception):
    """A parsed `SerializedGraph`'s content fails the schema allow-list check.

    Raised by `schema_allowlist.validate_serialized_graph` (CHANGES2.md
    §2.4's Query Safety guard) when a node/edge label or relationship_type
    falls outside its allow-list, or a node's `properties["id"]` is missing
    or not a string. Raised before `staging.stage_graph` creates any staged
    key at all -- a rejected artifact never causes even a staged-key write.
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
