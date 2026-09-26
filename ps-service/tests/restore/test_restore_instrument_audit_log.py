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
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export.models import InstrumentManifest, SerializedGraph
from ps_service.export.serialize import checksum_bytes, to_json_bytes
from ps_service.restore.models import RestoreArtifact
from ps_service.restore.restore_instrument import restore_instrument

if TYPE_CHECKING:
    from collections.abc import Callable

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
        schema_version=DOMAIN_SCHEMA_VERSION,
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
        assert entry["schema_version"] == DOMAIN_SCHEMA_VERSION
        assert "actor" not in entry  # MA2's explicit correction: never extra["actor"]
        # Issue #125/D-AUDIT: the upload path (this call passes no `source`)
        # must keep a byte-identical audit log shape -- no "source" key at all.
        assert "source" not in entry


_SOURCE_URL = (
    "https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/curated-content"
)


def test_started_and_succeeded_entries_carry_source_when_given(
    make_emitter: MakeEmitter, read_lines: ReadLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #125/D-AUDIT/AC-BI-011: `run_restoration_from_catalog_source` passes the
    resolved effective source URL, which every emitted audit log entry then carries.
    """
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
        source=_SOURCE_URL,
    )
    emitter.flush()

    entries = _restore_log_entries(read_lines(log_path))
    outcomes = [entry["outcome"] for entry in entries]
    assert outcomes == ["started", "succeeded"]
    for entry in entries:
        assert entry["source"] == _SOURCE_URL


_CLASSIFICATION_COUNTS: dict[str, int] = {
    "practice_area_count": 2,
    "risk_path_count": 1,
    "covers_count": 3,
    "owns_count": 1,
    "mitigated_by_count": 1,
    "verified_by_count": 2,
}


def _run_baseline_merge_stub_with_classification_counts(
    *_args: object, **_kwargs: object
) -> dict[str, int]:
    """Issue #106: stands in for the real `_run_baseline_merge` (already
    covered elsewhere -- `test_restore_instrument_classification_
    passthrough.py` -- for its own graph-writing correctness), returning a
    canned six-key counts dict so this test can prove the WIRING: that
    `restore_instrument`'s `"succeeded"` audit entry carries whatever
    `_run_baseline_merge` returned, via `_run_offline_merge`'s `nonlocal
    classification_counts` capture.
    """
    return dict(_CLASSIFICATION_COUNTS)


def _stage_and_finalize_invokes_offline_merge_stub(
    _db: object,
    _connection: object,
    _single_tenant_graph_name: str,
    _token: str,
    run_offline_merge: object,
    _native: object,
    _baseline: object,
) -> None:
    """A `stage_and_finalize_policy_system_leg` stand-in that, unlike
    `_stage_and_finalize_noop_stub`, actually invokes its own
    `run_offline_merge` callback (against a throwaway snapshot name) --
    needed so `_run_offline_merge`'s `nonlocal classification_counts`
    assignment actually fires in this test.
    """
    cast("Callable[[str], None]", run_offline_merge)("unused-snapshot-name")


def test_succeeded_entry_carries_classification_write_counts(
    make_emitter: MakeEmitter, read_lines: ReadLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-011 (restore path): the restore's own `"succeeded"` audit log
    entry carries the same six PracticeArea/RiskPath/classification-edge
    write counts the live path's `merge_baseline_graph` attaches to its own
    entry (`graph_writer.classification_write_counts`) -- computed here from
    `_run_baseline_merge`'s return value, threaded through `_run_offline_
    merge`'s `nonlocal classification_counts` capture into `_emit_restore_
    log`'s `extra=` (PLAN.md §5 point 2).
    """
    emitter, log_path = make_emitter()
    monkeypatch.setattr(restore_instrument_module, "stage_graph", _stage_graph_stub)
    monkeypatch.setattr(
        restore_instrument_module,
        "_run_baseline_merge",
        _run_baseline_merge_stub_with_classification_counts,
    )
    monkeypatch.setattr(
        restore_instrument_module,
        "stage_and_finalize_policy_system_leg",
        _stage_and_finalize_invokes_offline_merge_stub,
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
    succeeded = next(entry for entry in entries if entry["outcome"] == "succeeded")
    for key, expected in _CLASSIFICATION_COUNTS.items():
        assert succeeded[key] == expected
    # "started" never carries these -- no classification pass has run yet.
    started = next(entry for entry in entries if entry["outcome"] == "started")
    assert "practice_area_count" not in started


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
    assert failed_entry["schema_version"] == DOMAIN_SCHEMA_VERSION
    # Issue #125/D-AUDIT: no `source` was passed -- the upload path's audit
    # log shape stays byte-identical, including on the failure path.
    assert "source" not in failed_entry
