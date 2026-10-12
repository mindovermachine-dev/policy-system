"""Tests for `restore_instrument`'s D14/AC-BI-016 audit log entries (PLAN.md
Slice 5.9, MA2's exact `emit_log_entry` call shape).

Since #214 the restore runs FOR REAL against the gateway rig (`restore._gateway_fakes.RestoreRig`:
the in-memory log and FalkorDB boundaries behind the real Graph Write Gateway), so the
`started`/`succeeded`/`failed` entries, and the keys of their `extra`, are proven against the
orchestration that actually writes -- no canned collaborator stands in for any step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export.models import (
    SerializedEdge,
    SerializedGraph,
    SerializedNode,
)
from ps_service.restore.errors import ArtifactContentRejectedError
from restore._fixtures import build_restore_artifact
from restore._gateway_fakes import ACTOR, OWNER, RestoreRig

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines

    from ps_service.restore.models import RestoreArtifact

_INSTRUMENT_ID = "RT59-1.0"
_EMPTY_GRAPH = SerializedGraph(nodes=(), edges=())


def _artifact(
    *, baseline_graph: SerializedGraph = _EMPTY_GRAPH, short_name: str = "RT59"
) -> RestoreArtifact:
    return build_restore_artifact(
        instrument_id=_INSTRUMENT_ID,
        short_name=short_name,
        native_graph=_EMPTY_GRAPH,
        baseline_graph=baseline_graph,
    )


def _restore_log_entries(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        row
        for row in rows
        if row["component"] == "restore" and row["action"] == "restore_instrument"
    ]


def test_succeeded_entry_carries_caller_and_schema_version(
    restore_rig: RestoreRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()

    restore_rig.restore(_artifact(), emitter=emitter)
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
        assert entry["caller"] == ACTOR
        assert entry["schema_version"] == DOMAIN_SCHEMA_VERSION
        assert "actor" not in entry  # MA2's explicit correction: never extra["actor"]
        # Issue #125/D-AUDIT: the upload path (this call passes no `source`)
        # must keep a byte-identical audit log shape -- no "source" key at all.
        assert "source" not in entry


_SOURCE_URL = (
    "https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/curated-content"
)


def test_started_and_succeeded_entries_carry_source_when_given(
    restore_rig: RestoreRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Issue #125/D-AUDIT/AC-BI-011: `run_restoration_from_catalog_source` passes the
    resolved effective source URL, which every emitted audit log entry then carries.
    """
    emitter, log_path = make_emitter()

    restore_rig.restore(_artifact(), emitter=emitter, source=_SOURCE_URL)
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


def _classification_baseline_graph() -> SerializedGraph:
    """A real, non-empty internal-sourced baseline carrying exactly the
    PracticeArea/RiskPath/classification-edge content `_CLASSIFICATION_
    COUNTS` below expects: 2 PracticeArea, 1 RiskPath, 3 COVERS, 1 OWNS, 1
    MITIGATED_BY, 2 VERIFIED_BY -- staged and merged for real via
    `stage_graph`/`_run_baseline_merge` (no canned-return stub), so
    `restore_instrument`'s own audit log wiring is proven against the SAME
    real six-key `graph_writer.classification_write_counts` return value
    `test_restore_instrument_classification_passthrough.py` already proves
    correct for `_run_baseline_merge` in isolation.
    """
    return SerializedGraph(
        nodes=(
            SerializedNode(
                label="RegulatoryInstrument", properties={"id": _INSTRUMENT_ID, "title": "RT59"}
            ),
            SerializedNode(
                label="PracticeArea",
                properties={"id": "pa_1", "name": "PA One", "status": "active"},
            ),
            SerializedNode(
                label="PracticeArea",
                properties={"id": "pa_2", "name": "PA Two", "status": "active"},
            ),
            SerializedNode(
                label="RiskPath", properties={"id": "rp_1", "name": "RP One", "status": "active"}
            ),
            SerializedNode(
                label="Capability", properties={"id": "cap_1", "name": "Cap One", "confidence": 0.9}
            ),
            SerializedNode(
                label="Capability", properties={"id": "cap_2", "name": "Cap Two", "confidence": 0.9}
            ),
            SerializedNode(
                label="Capability",
                properties={"id": "cap_3", "name": "Cap Three", "confidence": 0.9},
            ),
            SerializedNode(
                label="Policy",
                properties={
                    "id": "policy_1",
                    "title": "Policy One",
                    "status": "approved",
                    "confidence": 0.9,
                },
            ),
            SerializedNode(
                label="Control",
                properties={
                    "id": "ctrl_1",
                    "type": "preventive",
                    "title": "Ctrl One",
                    "implementation_status": "implemented",
                    "confidence": 0.9,
                    "status": "approved",
                },
            ),
            SerializedNode(
                label="Control",
                properties={
                    "id": "ctrl_2",
                    "type": "detective",
                    "title": "Ctrl Two",
                    "implementation_status": "implemented",
                    "confidence": 0.9,
                    "status": "approved",
                },
            ),
        ),
        edges=(
            SerializedEdge(
                relationship_type="COVERS",
                source_label="PracticeArea",
                source_id="pa_1",
                target_label="Capability",
                target_id="cap_1",
                properties={},
            ),
            SerializedEdge(
                relationship_type="COVERS",
                source_label="PracticeArea",
                source_id="pa_1",
                target_label="Capability",
                target_id="cap_2",
                properties={},
            ),
            SerializedEdge(
                relationship_type="COVERS",
                source_label="PracticeArea",
                source_id="pa_2",
                target_label="Capability",
                target_id="cap_3",
                properties={},
            ),
            SerializedEdge(
                relationship_type="OWNS",
                source_label="PracticeArea",
                source_id="pa_2",
                target_label="Policy",
                target_id="policy_1",
                properties={},
            ),
            SerializedEdge(
                relationship_type="MITIGATED_BY",
                source_label="RiskPath",
                source_id="rp_1",
                target_label="Capability",
                target_id="cap_1",
                properties={},
            ),
            SerializedEdge(
                relationship_type="VERIFIED_BY",
                source_label="RiskPath",
                source_id="rp_1",
                target_label="Control",
                target_id="ctrl_1",
                properties={},
            ),
            SerializedEdge(
                relationship_type="VERIFIED_BY",
                source_label="RiskPath",
                source_id="rp_1",
                target_label="Control",
                target_id="ctrl_2",
                properties={},
            ),
        ),
    )


def test_succeeded_entry_carries_classification_write_counts(
    restore_rig: RestoreRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-BI-011 (restore path): `restore_instrument`'s own `"succeeded"` audit entry carries
    the same six PracticeArea/RiskPath/classification-edge write counts the live path's
    `merge_baseline_graph` attaches to its own entry (`graph_writer.classification_write_counts`)
    -- computed here by running the REAL restore (read, build, one gateway group) against a real,
    non-empty classification-bearing baseline (no canned-return stub standing in for any step),
    threaded from the built `policy_system` leg into `_emit_restore_log`'s `extra=`
    (PLAN.md §5 point 2).
    """
    emitter, log_path = make_emitter()
    artifact = _artifact(baseline_graph=_classification_baseline_graph())

    restore_rig.restore(artifact, emitter=emitter, owner=OWNER)
    emitter.flush()

    entries = _restore_log_entries(read_lines(log_path))
    succeeded = next(entry for entry in entries if entry["outcome"] == "succeeded")
    for key, expected in _CLASSIFICATION_COUNTS.items():
        assert succeeded[key] == expected
    # issue #183 / AC-BI-014: the draft-governance counts ride on the same entry.
    for key in (
        "governance_policies",
        "governance_standards",
        "governance_controls",
        "governance_status_overridden",
    ):
        assert key in succeeded
    # "started" never carries these -- no classification pass has run yet.
    started = next(entry for entry in entries if entry["outcome"] == "started")
    assert "practice_area_count" not in started


def test_failed_entry_recorded_with_no_succeeded_entry_when_the_build_step_raises(
    restore_rig: RestoreRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """A step after `started` that raises ends in `failed`, never in `succeeded`.

    The manifest names an instrument the baseline does not hold (it holds another), which the
    `policy_system` build refuses -- the same real failure as before #214, with nothing logged to
    the graph log.
    """
    emitter, log_path = make_emitter()
    baseline = SerializedGraph(
        nodes=(SerializedNode("RegulatoryInstrument", {"id": "ANOTHER-1.0", "title": "x"}),),
        edges=(),
    )

    with pytest.raises(ArtifactContentRejectedError, match="RegulatoryInstrument"):
        restore_rig.restore(_artifact(baseline_graph=baseline), emitter=emitter)
    emitter.flush()

    entries = _restore_log_entries(read_lines(log_path))
    outcomes = [entry["outcome"] for entry in entries]
    assert outcomes == ["started", "failed"]
    assert "succeeded" not in outcomes
    failed_entry = entries[-1]
    assert failed_entry["caller"] == ACTOR
    assert failed_entry["schema_version"] == DOMAIN_SCHEMA_VERSION
    # Issue #125/D-AUDIT: no `source` was passed -- the upload path's audit
    # log shape stays byte-identical, including on the failure path.
    assert "source" not in failed_entry
    assert restore_rig.store.groups == []


def test_restore_of_lowercase_curated_short_name_still_targets_lowercase_graphs(
    restore_rig: RestoreRig,
) -> None:
    """CHARACTERIZATION (issue #193, AC-BI-009; passes before and after the change).

    A curated manifest carrying the lowercase ``short_name`` ``cra`` restores into
    ``cra_native``/``cra_baseline`` and records the uppercase instrument id
    ``CRA-1.0``. Ingest-side short_name normalization (#193) must never reach the
    restore path: FalkorDB keys are case-sensitive, so any uppercase graph name here
    would fork from the ``cra_native`` graph the sweep and ingestion open.
    """
    instrument = SerializedNode("RegulatoryInstrument", {"id": "CRA-1.0", "title": "CRA"})
    both = SerializedGraph(nodes=(instrument,), edges=())
    artifact = build_restore_artifact(
        instrument_id="CRA-1.0",
        short_name="cra",
        native_graph=both,
        baseline_graph=both,
    )

    outcome = restore_rig.restore(artifact)

    assert outcome.instrument_id == "CRA-1.0"
    names = set(restore_rig.graphs.graphs)
    assert {"cra_native", "cra_baseline"} <= names
    assert not {"CRA_native", "CRA_baseline"} & names
