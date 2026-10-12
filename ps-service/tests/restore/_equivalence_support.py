"""What the equivalence tests share: the old path's result, curated artifacts, the harness input.

Used by `test_restore_equivalence.py` (fakes) and `test_restore_equivalence_live.py` (real stores)
for issue #214, slice 9.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from graph_gateway.equivalence_harness import Artifact, parse_artifact

from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.export import catalog_writer
from ps_service.restore.leg_primitives import embeddings_by_label
from ps_service.restore.models import RestoreArtifact
from restore._curated import CURATED_DIR, INTERNAL_IDS
from restore._gateway_fakes import OWNER
from restore._old_path import populate_old_path
from restore._spike_builders import ArtifactBaseline, ReferenceStore, masked_copy, run_reference

if TYPE_CHECKING:
    from graph_gateway._fakes import InMemoryGraph

    from ps_service.export.models import SerializedGraph
    from ps_service.logging.emitter import LogEmitter


@dataclass
class OldPath:
    """What the pre-#214 restore leaves behind for one instrument (`policy_system` masked)."""

    native: InMemoryGraph
    baseline: InMemoryGraph
    policy_system: InMemoryGraph


def run_old_path(
    instrument_id: str,
    versions: list[tuple[SerializedGraph, SerializedGraph]],
    emitter: LogEmitter,
    *,
    owner: tuple[str, str] | None,
) -> OldPath:
    """Restore `versions` in order on the old path; the legs are the last version's."""
    store = ReferenceStore()
    for _native, baseline in versions:
        staged = populate_old_path(baseline)
        policy_embeddings = embeddings_by_label(baseline, "Policy")
        run_reference(
            store,
            ArtifactBaseline(
                instrument_id=instrument_id,
                graph=staged,
                capability_embeddings=embeddings_by_label(baseline, "Capability"),
                policy_embeddings=policy_embeddings,
            ),
            emitter,
            GovernanceImport(owner=owner, policy_embeddings=policy_embeddings),
        )
    last_native, last_baseline = versions[-1]
    return OldPath(
        native=populate_old_path(last_native),
        baseline=populate_old_path(last_baseline),
        policy_system=masked_copy(store.as_gateway_graph()),
    )


def curated_artifact(instrument_id: str) -> RestoreArtifact:
    """The shipped bytes and manifest of `curated-content/<id>`."""
    directory = CURATED_DIR / instrument_id
    return RestoreArtifact(
        manifest=catalog_writer.read_manifest(directory),
        baseline_blob=(directory / "baseline.json").read_bytes(),
        native_blob=(directory / "native.json").read_bytes(),
    )


def harness_artifact(instrument_id: str, leg: str) -> Artifact:
    """The #207 harness's view of one leg file (`native` or `baseline`)."""
    path = CURATED_DIR / instrument_id / f"{leg}.json"
    return parse_artifact(json.loads(path.read_text(encoding="utf-8")))


def owner_of(instrument_id: str) -> tuple[str, str] | None:
    """The restoring caller: required for the internal instrument, absent for external ones."""
    return OWNER if instrument_id in INTERNAL_IDS else None
