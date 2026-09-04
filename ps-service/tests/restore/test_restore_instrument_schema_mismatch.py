"""Tests for `ps_service.restore.restore_instrument.restore_instrument`'s
`schema_version` check (PLAN.md Slice 5.2, D10).

Ordering: checksum verification (D9, Slice 5.1) runs first -- these tests
use a manifest whose checksums are correct so the schema_version check is
the one actually exercised. `restore_instrument` gained a real `db:
FalkorDB` parameter (and others) in Slice 5.5 -- see
`test_restore_instrument_integrity.py`'s module docstring for why every
call below passes `db=_NEVER_TOUCHED_DB` (a plain `object()` cast to
`FalkorDB`) rather than a real connection: verification never reaches it,
so both checks staying before any graph call is proved by construction
(an actually-touched `db` would raise `AttributeError`, not
`ArtifactSchemaVersionMismatchError`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

import ps_service.restore.restore_instrument as restore_instrument_module
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export.models import InstrumentManifest
from ps_service.export.serialize import checksum_bytes
from ps_service.restore.errors import ArtifactSchemaVersionMismatchError
from ps_service.restore.models import RestoreArtifact
from ps_service.restore.restore_instrument import restore_instrument

if TYPE_CHECKING:
    from falkordb import FalkorDB

_REAL_BASELINE_BYTES = b"real-baseline-bytes"
_REAL_NATIVE_BYTES = b"real-native-bytes"
_MISMATCHED_SCHEMA_VERSION = "999"
_NEVER_TOUCHED_DB = cast("FalkorDB", object())


def _manifest(*, schema_version: str) -> InstrumentManifest:
    return InstrumentManifest(
        instrument_id="CRA-1.0",
        celex="32024R2847",
        title="Cyber Resilience Act",
        short_name="CRA",
        version="1.0",
        source_type="external",
        jurisdiction="EU",
        schema_version=schema_version,
        exported_at="2026-09-04T00:00:00Z",
        baseline_sha256=checksum_bytes(_REAL_BASELINE_BYTES),
        native_sha256=checksum_bytes(_REAL_NATIVE_BYTES),
    )


def _artifact(manifest: InstrumentManifest) -> RestoreArtifact:
    return RestoreArtifact(
        manifest=manifest, baseline_blob=_REAL_BASELINE_BYTES, native_blob=_REAL_NATIVE_BYTES
    )


def _restore(artifact: RestoreArtifact) -> None:
    """Call `restore_instrument` with a never-touched `db` (see module docstring)."""
    restore_instrument(
        artifact,
        db=_NEVER_TOUCHED_DB,
        single_tenant_graph_name="__never_touched_single_tenant_graph__",
        similarity_threshold=0.9,
        actor="test",
    )


def test_raises_artifact_schema_version_mismatch_error_when_versions_differ() -> None:
    artifact = _artifact(_manifest(schema_version=_MISMATCHED_SCHEMA_VERSION))

    with pytest.raises(ArtifactSchemaVersionMismatchError):
        _restore(artifact)


def test_schema_version_mismatch_error_names_both_versions() -> None:
    artifact = _artifact(_manifest(schema_version=_MISMATCHED_SCHEMA_VERSION))

    with pytest.raises(ArtifactSchemaVersionMismatchError) as exc_info:
        _restore(artifact)

    message = str(exc_info.value)
    assert _MISMATCHED_SCHEMA_VERSION in message
    assert DOMAIN_SCHEMA_VERSION in message


class _SentinelReachedStartedLogError(Exception):
    """Raised by a monkeypatched `_emit_restore_log` stand-in.

    Proves schema_version verification passed control onward (to
    `restore_instrument`'s `outcome="started"` audit-log emission) without
    raising `ArtifactSchemaVersionMismatchError` -- a stronger, more
    targeted proof than running the whole function to completion (which now
    requires real staging collaborators this file's `_NEVER_TOUCHED_DB`
    deliberately cannot provide; see `test_restore_instrument_integrity.py`'s
    module docstring).
    """


def test_matching_schema_version_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def _sentinel(**_kwargs: object) -> None:
        raise _SentinelReachedStartedLogError

    monkeypatch.setattr(restore_instrument_module, "_emit_restore_log", _sentinel)
    artifact = _artifact(_manifest(schema_version=DOMAIN_SCHEMA_VERSION))

    with pytest.raises(_SentinelReachedStartedLogError):
        _restore(artifact)
