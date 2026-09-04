"""Tests for `ps_service.restore.restore_instrument.restore_instrument`'s
checksum verification (PLAN.md Slice 5.1, D9).

Slices 5.5-5.9 extended `restore_instrument` with `db`/`single_tenant_graph_
name`/`similarity_threshold`/`actor`/`emitter` -- the FalkorDB-touching
collaborators D9/D10's verification never reaches. Every test below passes
`db=_NEVER_TOUCHED_DB` (a plain `object()` cast to `FalkorDB`, never a real
or fake connection) -- if verification's ordering were ever wrong and `db`
were actually touched, calling any method on it would raise `AttributeError`
instead of the expected `ArtifactIntegrityError`, which is a STRONGER proof
of "zero graph calls before this passes" than a structural fake would be:
the real collaborator is provably present but provably never dereferenced.
`emitter=None` is likewise never reached -- verification fails before the
first `emit_log_entry` call, so no `LoggingLifecycleError` about a missing
default emitter is ever raised either. A call-order spy additionally proves
`_verify_schema_version` -- D10's own check, the very next step inside this
same function -- is never reached once D9's checksum check has already
failed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

import ps_service.restore.restore_instrument as restore_instrument_module
from ps_service.export.models import InstrumentManifest
from ps_service.export.serialize import checksum_bytes
from ps_service.restore.errors import ArtifactIntegrityError
from ps_service.restore.models import RestoreArtifact
from ps_service.restore.restore_instrument import restore_instrument

if TYPE_CHECKING:
    from falkordb import FalkorDB

_REAL_BASELINE_BYTES = b"real-baseline-bytes"
_REAL_NATIVE_BYTES = b"real-native-bytes"
_NEVER_TOUCHED_DB = cast("FalkorDB", object())


def _manifest(
    *, baseline_sha256: str | None = None, native_sha256: str | None = None
) -> InstrumentManifest:
    return InstrumentManifest(
        instrument_id="CRA-1.0",
        celex="32024R2847",
        title="Cyber Resilience Act",
        short_name="CRA",
        version="1.0",
        source_type="external",
        jurisdiction="EU",
        schema_version="1",
        exported_at="2026-09-04T00:00:00Z",
        baseline_sha256=baseline_sha256 or checksum_bytes(_REAL_BASELINE_BYTES),
        native_sha256=native_sha256 or checksum_bytes(_REAL_NATIVE_BYTES),
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


def test_raises_artifact_integrity_error_when_baseline_checksum_mismatches() -> None:
    manifest = _manifest(baseline_sha256="0" * 64)
    artifact = _artifact(manifest)

    with pytest.raises(ArtifactIntegrityError, match="baseline"):
        _restore(artifact)


def test_raises_artifact_integrity_error_when_native_checksum_mismatches() -> None:
    manifest = _manifest(native_sha256="f" * 64)
    artifact = _artifact(manifest)

    with pytest.raises(ArtifactIntegrityError, match="native"):
        _restore(artifact)


def test_integrity_error_names_instrument_id_and_both_digests() -> None:
    expected = "0" * 64
    manifest = _manifest(baseline_sha256=expected)
    artifact = _artifact(manifest)

    with pytest.raises(ArtifactIntegrityError) as exc_info:
        _restore(artifact)

    message = str(exc_info.value)
    assert "CRA-1.0" in message
    assert expected in message
    assert checksum_bytes(_REAL_BASELINE_BYTES) in message


class _SentinelReachedSchemaVersionCheckError(Exception):
    """Raised by a monkeypatched `_verify_schema_version` stand-in.

    Proves checksum verification passed control onward without raising
    `ArtifactIntegrityError` -- a stronger, more targeted proof than running
    the whole function to completion (which now requires real staging
    collaborators this file's `_NEVER_TOUCHED_DB` deliberately cannot
    provide; see module docstring).
    """


def test_valid_checksums_do_not_raise_artifact_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _sentinel(_artifact: RestoreArtifact) -> None:
        raise _SentinelReachedSchemaVersionCheckError

    monkeypatch.setattr(restore_instrument_module, "_verify_schema_version", _sentinel)
    artifact = _artifact(_manifest())

    with pytest.raises(_SentinelReachedSchemaVersionCheckError):
        _restore(artifact)


def test_schema_version_check_is_never_reached_when_checksum_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering proof: `_verify_schema_version` -- D10's own check, the very
    next collaborator `restore_instrument` calls -- is never invoked once
    D9's checksum check has already raised.
    """

    def _fail_if_invoked(_artifact: RestoreArtifact) -> None:
        raise AssertionError("_verify_schema_version must not be invoked when checksum fails")

    monkeypatch.setattr(restore_instrument_module, "_verify_schema_version", _fail_if_invoked)
    manifest = _manifest(baseline_sha256="0" * 64)
    artifact = _artifact(manifest)

    with pytest.raises(ArtifactIntegrityError):
        _restore(artifact)


# `test_restore_instrument_module_never_references_falkordb` (the AST-scan
# proof this file originally carried for Slices 5.1/5.2) is deliberately
# removed here, exactly as its own docstring anticipated: Slice 5.5 gave
# `restore_instrument` a real `db: FalkorDB` parameter and real `falkordb`-
# touching collaborators, so "this module never imports falkordb" is no
# longer a true invariant to prove. The ordering guarantee it stood for --
# "zero graph calls before verification passes" -- now lives in this file's
# `_NEVER_TOUCHED_DB` proof above (a real, dereferenceable collaborator
# provably never touched) plus Slice 5.7's live all-or-nothing proof
# (`test_restore_instrument_all_or_nothing_live.py`).
