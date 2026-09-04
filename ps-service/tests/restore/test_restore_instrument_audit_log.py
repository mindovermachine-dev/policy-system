"""Tests for `restore_instrument`'s D14/AC-BI-016 audit log entries (PLAN.md
Slice 5.9, MA2's exact `emit_log_entry` call shape).

`restore_instrument`'s real success/failure paths now require real FalkorDB
staging/merge collaborators (Slices 5.5-5.8's own live proofs already cover
graph-content correctness) -- this file stays a fast, non-`falkordb_live`
unit test by monkeypatching `stage_graph`/`stage_and_finalize_policy_system_
leg` directly (module-level names `restore_instrument.py` imports into its
own namespace), exactly mirroring `test_restore_instrument_integrity.py`'s
own established "monkeypatch the real next collaborator" convention. `db`
itself is never touched by either patched collaborator, so a plain
`object()` cast to `FalkorDB` stands in for it, same as the checksum/
schema-mismatch test files.

Real (unfaked) checksum/schema_version verification and blob parsing still
run -- `_EMPTY_GRAPH_BYTES` is real, valid, empty `SerializedGraph` JSON, so
only the FalkorDB-touching staging/merge/finalize step is faked, not
verification itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

import ps_service.restore.restore_instrument as restore_instrument_module
from ps_service.export.models import InstrumentManifest, SerializedGraph
from ps_service.export.serialize import checksum_bytes, to_json_bytes
from ps_service.restore.models import RestoreArtifact
from ps_service.restore.restore_instrument import restore_instrument

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines
    from falkordb import FalkorDB

_NEVER_TOUCHED_DB = cast("FalkorDB", object())
_INSTRUMENT_ID = "RT59-1.0"
_ACTOR = "test-actor-5-9"
_EMPTY_GRAPH_BYTES = to_json_bytes(SerializedGraph(nodes=(), edges=()))


class _ForcedFinalizeFailureError(Exception):
    """The merge/finalize-step failure this test deliberately injects."""


def _manifest() -> InstrumentManifest:
    return InstrumentManifest(
        instrument_id=_INSTRUMENT_ID,
        celex=None,
        title="RT59",
        short_name="RT59",
        version="1.0",
        source_type="external",
        jurisdiction=None,
        schema_version="1",
        exported_at="2026-09-04T00:00:00Z",
        baseline_sha256=checksum_bytes(_EMPTY_GRAPH_BYTES),
        native_sha256=checksum_bytes(_EMPTY_GRAPH_BYTES),
    )


def _artifact() -> RestoreArtifact:
    return RestoreArtifact(
        manifest=_manifest(), baseline_blob=_EMPTY_GRAPH_BYTES, native_blob=_EMPTY_GRAPH_BYTES
    )


def _restore_log_entries(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        row
        for row in rows
        if row["component"] == "restore" and row["action"] == "restore_instrument"
    ]


def _stage_graph_stub(*_args: object, **_kwargs: object) -> str:
    return "staged-key-unused"


def _stage_and_finalize_noop_stub(*_args: object, **_kwargs: object) -> None:
    return None


def _raw_connection_stub(_db: object) -> str:
    return "unused-connection"


def test_succeeded_entry_carries_caller_and_schema_version(
    make_emitter: MakeEmitter, read_lines: ReadLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitter, log_path = make_emitter()
    monkeypatch.setattr(restore_instrument_module, "stage_graph", _stage_graph_stub)
    monkeypatch.setattr(
        restore_instrument_module,
        "stage_and_finalize_policy_system_leg",
        _stage_and_finalize_noop_stub,
    )
    monkeypatch.setattr(restore_instrument_module, "raw_connection", _raw_connection_stub)

    restore_instrument(
        _artifact(),
        db=_NEVER_TOUCHED_DB,
        single_tenant_graph_name="unused-single-tenant",
        similarity_threshold=0.9,
        actor=_ACTOR,
        emitter=emitter,
    )
    emitter.flush()

    entries = _restore_log_entries(read_lines(log_path))
    outcomes = [entry["outcome"] for entry in entries]
    assert outcomes == ["started", "succeeded"]
    for entry in entries:
        # `emit_log_entry`'s `extra` mapping is flattened directly into the
        # JSON payload (logging/models.py::LogEntry.to_json_line), so
        # "caller"/"schema_version" are top-level keys, not nested under an
        # "extra" key.
        assert entry["entity_id"] == _INSTRUMENT_ID
        assert entry["caller"] == _ACTOR
        assert entry["schema_version"] == "1"
        assert "actor" not in entry  # MA2's explicit correction: never extra["actor"]


def test_failed_entry_recorded_with_no_succeeded_entry_when_merge_step_raises(
    make_emitter: MakeEmitter, read_lines: ReadLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitter, log_path = make_emitter()
    monkeypatch.setattr(restore_instrument_module, "stage_graph", _stage_graph_stub)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise _ForcedFinalizeFailureError("forced failure in the merge/finalize step")

    monkeypatch.setattr(restore_instrument_module, "stage_and_finalize_policy_system_leg", _raise)
    monkeypatch.setattr(restore_instrument_module, "raw_connection", _raw_connection_stub)

    with pytest.raises(_ForcedFinalizeFailureError):
        restore_instrument(
            _artifact(),
            db=_NEVER_TOUCHED_DB,
            single_tenant_graph_name="unused-single-tenant",
            similarity_threshold=0.9,
            actor=_ACTOR,
            emitter=emitter,
        )
    emitter.flush()

    entries = _restore_log_entries(read_lines(log_path))
    outcomes = [entry["outcome"] for entry in entries]
    assert outcomes == ["started", "failed"]
    assert "succeeded" not in outcomes
    failed_entry = entries[-1]
    assert failed_entry["caller"] == _ACTOR
    assert failed_entry["schema_version"] == "1"
