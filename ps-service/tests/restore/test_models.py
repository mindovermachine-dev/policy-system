"""Tests for ps_service.restore.models."""

from __future__ import annotations

import dataclasses

import pytest

from ps_service.export.models import InstrumentManifest
from ps_service.restore.models import RestoreArtifact, RestoreOutcome


def _manifest() -> InstrumentManifest:
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
        baseline_sha256="a" * 64,
        native_sha256="b" * 64,
    )


def test_restore_artifact_holds_manifest_and_blobs() -> None:
    artifact = RestoreArtifact(
        manifest=_manifest(),
        baseline_blob=b"baseline-bytes",
        native_blob=b"native-bytes",
    )

    assert artifact.manifest.instrument_id == "CRA-1.0"
    assert artifact.baseline_blob == b"baseline-bytes"
    assert artifact.native_blob == b"native-bytes"


def test_restore_artifact_mutation_raises() -> None:
    artifact = RestoreArtifact(
        manifest=_manifest(), baseline_blob=b"baseline-bytes", native_blob=b"native-bytes"
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        artifact.native_blob = b"other"  # pyright: ignore[reportAttributeAccessIssue]  # asserting frozen-dataclass mutation is rejected at runtime


def test_restore_outcome_holds_instrument_id_and_stages() -> None:
    outcome = RestoreOutcome(instrument_id="CRA-1.0", stages=("staged", "merged", "finalized"))

    assert outcome.instrument_id == "CRA-1.0"
    assert outcome.stages == ("staged", "merged", "finalized")


def test_restore_outcome_mutation_raises() -> None:
    outcome = RestoreOutcome(instrument_id="CRA-1.0", stages=("staged",))

    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.instrument_id = "other"  # pyright: ignore[reportAttributeAccessIssue]  # asserting frozen-dataclass mutation is rejected at runtime
