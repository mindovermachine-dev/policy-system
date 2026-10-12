"""A restore equals the pre-#214 restore on every curated artifact (issue #214, slice 9).

AC-BI-007, AC-BI-011, AC-BI-001 (replay) and AC-BI-008 (the #207 harness reports equality). The
REAL `restore_instrument` runs through the gateway rig (one multi-graph group, applied by swap)
and is compared leg by leg, with the #207 harness (`run_equivalence`), against the old path:
`populate_graph` for the native and baseline legs and the frozen `_run_baseline_merge` over the
interim snapshot interpreter for `policy_system` (`restore._old_path`, `restore._spike_builders`).

Digests are compared modulo edge identity (the old path stamps none; the #210 convention) and
`created_at` (the old writer stamps `datetime.now`, which cannot be injected).

Offline dedup scores every pair of a Capability set with the production `cosine_similarity`, a
pure-Python loop over 3,072 doubles: about 25 s per path for CRA. Both paths call the same
function, so these tests swap in a `math.sumprod` version for the run (an equal result to within
rounding, checked below against the production one). That cuts the CRA case from about 80 s to a
few seconds without dropping the artifact from the fast suite. The live twin
(`test_restore_equivalence_live.py`) uses the production function.

What this does not show: agreement with a real FalkorDB or Postgres (none is reachable from the
build host); the live twin is written for the user's environment.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from graph_gateway.equivalence_harness import Artifact, run_equivalence

from ps_service.company_merge import dedup
from ps_service.company_merge.similarity import cosine_similarity
from ps_service.export import catalog_writer
from ps_service.export.models import SerializedGraph, SerializedNode
from ps_service.export.serialize import checksum_bytes, to_json_bytes
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.restore.leg_primitives import embeddings_by_label
from ps_service.restore.models import RestoreArtifact
from restore._curated import CURATED_DIR, CURATED_IDS, load_curated
from restore._equivalence_support import (
    curated_artifact,
    harness_artifact,
    owner_of,
    run_old_path,
)
from restore._spike_builders import masked_copy

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from graph_gateway._fakes import InMemoryGraph

    from restore._gateway_fakes import RestoreRig


_REPEATABLE = "RRF-1.0"
"""A small external curated instrument, used for the over-an-existing-copy cases."""


def _fast_cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return math.sumprod(a, b) / (math.sqrt(math.sumprod(a, a)) * math.sqrt(math.sumprod(b, b)))


@pytest.fixture(autouse=True)
def fast_dedup_cosine(monkeypatch: pytest.MonkeyPatch) -> None:
    # detroit-exception: equivalent but faster cosine; the original is too slow at scale (§1.2)
    monkeypatch.setattr(dedup, "cosine_similarity", _fast_cosine)


def _legs(short: str) -> tuple[str, str, str]:
    return f"{short}_native", f"{short}_baseline", "policy_system"


def _passes(
    artifact: Artifact, old: InMemoryGraph, new: InMemoryGraph, *, masked: bool = False
) -> str:
    """Assert the harness reports equality; return the digest of the new graph (masked or not)."""
    result = run_equivalence(
        artifact,
        lambda _a: old,
        lambda _a: masked_copy(new) if masked else new,
    )
    assert result.passed, result
    assert result.old_digest == result.new_digest
    assert result.node_count > 0
    return result.new_digest


@pytest.mark.parametrize("instrument_id", CURATED_IDS)
def test_a_restore_equals_the_old_path_on_every_leg_and_replays_to_the_same_graphs(
    instrument_id: str, restore_rig: RestoreRig, make_emitter: MakeEmitter
) -> None:
    curated = load_curated(instrument_id)
    emitter, _log_path = make_emitter()
    old = run_old_path(
        instrument_id,
        [(curated.native, curated.baseline)],
        emitter,
        owner=owner_of(instrument_id),
    )

    restore_rig.restore(curated_artifact(instrument_id), owner=owner_of(instrument_id))

    native_name, baseline_name, policy_system_name = _legs(curated.short_name)
    native = harness_artifact(instrument_id, "native")
    baseline = harness_artifact(instrument_id, "baseline")
    digests = {
        native_name: _passes(native, old.native, restore_rig.graph(native_name)),
        baseline_name: _passes(baseline, old.baseline, restore_rig.graph(baseline_name)),
        policy_system_name: _passes(
            baseline, old.policy_system, restore_rig.graph(policy_system_name), masked=True
        ),
    }
    for name in digests:
        restore_rig.gateway.replay_graph(name)
    replayed = {
        name: canonical_digest(
            masked_copy(restore_rig.graph(name))
            if name == policy_system_name
            else restore_rig.graph(name)
        )
        for name in digests
    }
    assert replayed == digests


def _trimmed(graph: SerializedGraph, *, stale: SerializedNode | None) -> SerializedGraph:
    """Every other node (the instrument stays), the edges between them, plus a stale node."""
    kept = tuple(
        node
        for index, node in enumerate(graph.nodes)
        if index % 2 == 0 or node.label == "RegulatoryInstrument"
    )
    present = {(node.label, str(node.properties["id"])) for node in kept}
    edges = tuple(
        edge
        for edge in graph.edges
        if (edge.source_label, edge.source_id) in present
        and (edge.target_label, edge.target_id) in present
    )
    if stale is not None:
        kept = (*kept, stale)
    return SerializedGraph(nodes=kept, edges=edges)


def _artifact_of(
    instrument_id: str, native: SerializedGraph, baseline: SerializedGraph
) -> RestoreArtifact:
    directory = CURATED_DIR / instrument_id
    native_blob, baseline_blob = to_json_bytes(native), to_json_bytes(baseline)
    manifest = replace(
        catalog_writer.read_manifest(directory),
        native_sha256=checksum_bytes(native_blob),
        baseline_sha256=checksum_bytes(baseline_blob),
    )
    return RestoreArtifact(manifest=manifest, baseline_blob=baseline_blob, native_blob=native_blob)


@pytest.mark.parametrize(
    "instrument_id", [_REPEATABLE, "ENGPRAC-1.0"], ids=["external", "internal"]
)
def test_a_restore_over_an_existing_copy_of_itself_equals_the_old_path(
    instrument_id: str, restore_rig: RestoreRig, make_emitter: MakeEmitter
) -> None:
    curated = load_curated(instrument_id)
    emitter, _log_path = make_emitter()
    owner = owner_of(instrument_id)
    old = run_old_path(
        instrument_id,
        [(curated.native, curated.baseline)] * 2,
        emitter,
        owner=owner,
    )

    restore_rig.restore(curated_artifact(instrument_id), owner=owner)
    restore_rig.restore(curated_artifact(instrument_id), owner=owner)

    native_name, baseline_name, policy_system_name = _legs(curated.short_name)
    _passes(harness_artifact(instrument_id, "native"), old.native, restore_rig.graph(native_name))
    _passes(
        harness_artifact(instrument_id, "baseline"),
        old.baseline,
        restore_rig.graph(baseline_name),
    )
    _passes(
        harness_artifact(instrument_id, "baseline"),
        old.policy_system,
        restore_rig.graph(policy_system_name),
        masked=True,
    )


def test_a_restore_over_a_different_existing_version_leaves_nothing_of_it(
    restore_rig: RestoreRig, make_emitter: MakeEmitter
) -> None:
    curated = load_curated(_REPEATABLE)
    emitter, _log_path = make_emitter()
    earlier = (
        _trimmed(
            curated.native,
            stale=SerializedNode("ARTICLE", {"id": "stale-article", "text": "stale"}),
        ),
        _trimmed(
            curated.baseline,
            stale=SerializedNode(
                "Capability", {"id": "stale-capability", "name": "Stale", "confidence": 0.5}
            ),
        ),
    )
    old = run_old_path(
        _REPEATABLE, [earlier, (curated.native, curated.baseline)], emitter, owner=None
    )

    restore_rig.restore(_artifact_of(_REPEATABLE, *earlier))
    restore_rig.restore(curated_artifact(_REPEATABLE))

    native_name, baseline_name, policy_system_name = _legs(curated.short_name)
    native, baseline = restore_rig.graph(native_name), restore_rig.graph(baseline_name)
    assert ("ARTICLE", "stale-article") not in native.nodes
    assert ("Capability", "stale-capability") not in baseline.nodes
    _passes(harness_artifact(_REPEATABLE, "native"), old.native, native)
    _passes(harness_artifact(_REPEATABLE, "baseline"), old.baseline, baseline)
    # `policy_system` is only ever added to (D-4 replaces the instrument's own legs): the stale
    # Capability the earlier restore minted stays there in both paths.
    _passes(
        harness_artifact(_REPEATABLE, "baseline"),
        old.policy_system,
        restore_rig.graph(policy_system_name),
        masked=True,
    )


def test_the_fast_cosine_used_here_agrees_with_the_production_one() -> None:
    vectors = list(embeddings_by_label(load_curated("CRA-1.0").baseline, "Capability").values())[:8]

    for a in vectors:
        for b in vectors:
            assert math.isclose(_fast_cosine(a, b), cosine_similarity(a, b), abs_tol=1e-12)
