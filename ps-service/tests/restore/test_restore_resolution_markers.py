"""A restore records each semantic Capability resolution and follows the recorded ones.

When a restore's offline dedup resolves an incoming Capability `X` onto an existing `A` by
similarity, the `policy_system` leg carries a `ResolvedCapability {id: X, resolves_to: A}` marker
next to the rewired edges, so the marker and the `REQUIRES` edge onto `A` are written together or
not at all. A later restore that regenerates `X` follows the marker (and any `MergedCapability`
after it) before dedup, so `X` lands where it landed first even when a closer Capability has
arrived since. A broken chain fails the restore before anything is written.

Restore scores from the embeddings the artifact carries, so no embedding call is involved.
The end-to-end tests drive the real `restore_instrument` over the gateway rig and read the graph,
the graph log and the log entries it leaves.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from company_merge._gateway_fakes import RigReadHandle
from company_merge._resolution_fixtures import (
    AMENDED_VERSION,
    FIRST_VERSION,
    MERGE,
    NEWER_ID,
    NEWER_NAME,
    NEWER_VECTOR,
    RESOLVED_ID,
    RESOLVED_NAME,
    RESOLVED_VECTOR,
    TARGET_ID,
    TARGET_NAME,
    TARGET_VECTOR,
    UNRELATED_INSTRUMENT,
    seed_target,
)
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD

from ps_service.company_merge.errors import CompanyMergeValidationError
from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.domain_mapper.identity import capability_id, obligation_id, role_id
from ps_service.export.models import (
    SerializedEdge,
    SerializedGraph,
    SerializedNode,
    SerializedPropertyValue,
)
from ps_service.graph_gateway.models import UpsertEdge, UpsertNode
from ps_service.logging import configure
from ps_service.restore.errors import RestoreConcurrencyConflictError
from ps_service.restore.graph_submission import MAX_POLICY_SYSTEM_ATTEMPTS
from ps_service.restore.merge_primitives import build_policy_system_leg
from restore._fixtures import build_restore_artifact
from restore._gateway_fakes import ScriptedLive, install_racing_gateway

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines

    from ps_service.restore.models import RestoreArtifact
    from restore._gateway_fakes import RestoreRig

_POLICY_SYSTEM = "policy_system"
_MARKER = "ResolvedCapability"
_SURVIVOR_ID = capability_id("Incident Handling")
_SHORT_NAMES = {FIRST_VERSION: "AMEND1", AMENDED_VERSION: "AMEND2", UNRELATED_INSTRUMENT: "OTHER"}


def _role(instrument_id: str) -> str:
    return role_id("Operator", instrument_id)


def _obligation(instrument_id: str) -> str:
    return obligation_id(_role(instrument_id), f"Report incidents under {instrument_id}.")


def _baseline(
    instrument_id: str, capability: str, name: str, vector: tuple[float, ...]
) -> SerializedGraph:
    """One Role of `instrument_id` with one Obligation that `REQUIRES` one embedded Capability."""
    role, obligation = _role(instrument_id), _obligation(instrument_id)

    def edge(kind: str, source: tuple[str, str], target: tuple[str, str]) -> SerializedEdge:
        properties: dict[str, SerializedPropertyValue] = (
            {"source_ref": "Art. 1"} if kind == "DEFINES" else {}
        )
        return SerializedEdge(kind, source[0], source[1], target[0], target[1], properties)

    return SerializedGraph(
        nodes=(
            SerializedNode("RegulatoryInstrument", {"id": instrument_id, "title": instrument_id}),
            SerializedNode("Role", {"id": role, "name": "Operator", "confidence": 0.9}),
            SerializedNode(
                "Obligation",
                {
                    "id": obligation,
                    "text": f"Report incidents under {instrument_id}.",
                    "confidence": 0.9,
                },
            ),
            SerializedNode(
                "Capability",
                {"id": capability, "name": name, "confidence": 0.8, "embedding": list(vector)},
            ),
        ),
        edges=(
            edge("DEFINES", ("RegulatoryInstrument", instrument_id), ("Role", role)),
            edge("HAS", ("Role", role), ("Obligation", obligation)),
            edge("REQUIRES", ("Obligation", obligation), ("Capability", capability)),
        ),
    )


def _artifact(instrument_id: str, baseline: SerializedGraph) -> RestoreArtifact:
    native = SerializedGraph(
        nodes=(
            SerializedNode("RegulatoryInstrument", {"id": instrument_id, "title": instrument_id}),
        ),
        edges=(),
    )
    return build_restore_artifact(
        instrument_id=instrument_id,
        short_name=_SHORT_NAMES[instrument_id],
        native_graph=native,
        baseline_graph=baseline,
    )


def _version(instrument_id: str) -> RestoreArtifact:
    """A version of the regulation that regenerates the resolved Capability `X`."""
    return _artifact(
        instrument_id, _baseline(instrument_id, RESOLVED_ID, RESOLVED_NAME, RESOLVED_VECTOR)
    )


def _unrelated() -> RestoreArtifact:
    """An unrelated regulation that brings `B`, closer to `X` than `A` is."""
    return _artifact(
        UNRELATED_INSTRUMENT,
        _baseline(UNRELATED_INSTRUMENT, NEWER_ID, NEWER_NAME, NEWER_VECTOR),
    )


class _Restorer:
    """Restores artifacts through the rig at the merge threshold and returns each run's log."""

    def __init__(self, rig: RestoreRig, make_emitter: MakeEmitter, read_lines: ReadLines) -> None:
        self._rig = rig
        self._make_emitter = make_emitter
        self._read_lines = read_lines

    def __call__(self, artifact: RestoreArtifact, name: str) -> list[dict[str, object]]:
        """Restore `artifact` and return the log entries the restore emitted."""
        emitter, log_path = self._make_emitter(filename=f"{name}.jsonl")
        self._rig.restore(artifact, emitter=emitter, threshold=MERGE)
        emitter.flush()
        return self._read_lines(log_path)


@pytest.fixture
def restore(restore_rig: RestoreRig, make_emitter: MakeEmitter, read_lines: ReadLines) -> _Restorer:
    return _Restorer(restore_rig, make_emitter, read_lines)


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _graph(rig: RestoreRig) -> RigReadHandle:
    return RigReadHandle(rig.graph(_POLICY_SYSTEM))


def _markers(rig: RestoreRig) -> dict[str, dict[str, object]]:
    return {
        node_id: props
        for (label, node_id), props in rig.graph(_POLICY_SYSTEM).nodes.items()
        if label == _MARKER
    }


def _requires_targets(rig: RestoreRig, instrument_id: str) -> set[str]:
    obligation = _obligation(instrument_id)
    return {
        target
        for kind, source, target in _graph(rig).edges
        if kind == "REQUIRES" and source == obligation
    }


def _entries(log: list[dict[str, object]], action: str) -> list[dict[str, object]]:
    return [entry for entry in log if entry.get("action") == action]


def _seed(rig: RestoreRig, label: str, node_id: str, properties: dict[str, object]) -> None:
    rig.policy_system.seed_node((label,), node_id, properties)


def test_a_semantic_restore_resolution_adds_one_marker_to_the_policy_system_leg() -> None:
    def answer(q: str, params: dict[str, object] | None) -> list[object] | None:
        del params
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return [[TARGET_ID, TARGET_NAME, list(TARGET_VECTOR)]]
        return None

    leg = build_policy_system_leg(
        _baseline(FIRST_VERSION, RESOLVED_ID, RESOLVED_NAME, RESOLVED_VECTOR),
        instrument_id=FIRST_VERSION,
        live=ScriptedLive(existing={"Capability": {TARGET_ID}}, answer=answer),
        governance=GovernanceImport(),
        merge_threshold=MERGE,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=None,
    )

    markers = [p for p in leg.primitives if isinstance(p, UpsertNode) and p.label == _MARKER]
    assert markers == [
        UpsertNode(label=_MARKER, id=RESOLVED_ID, properties={"resolves_to": TARGET_ID})
    ]
    requires = [p for p in leg.primitives if isinstance(p, UpsertEdge) and p.type == "REQUIRES"]
    assert [(e.source.id, e.target.id) for e in requires] == [
        (_obligation(FIRST_VERSION), TARGET_ID)
    ]
    assert leg.primitives.index(markers[0]) > leg.primitives.index(requires[0])
    assert [p for p in leg.primitives if isinstance(p, UpsertNode) and p.id == RESOLVED_ID] == (
        markers
    )
    assert [(r.incoming_id, r.canonical_id) for r in leg.semantic_resolutions] == [
        (RESOLVED_ID, TARGET_ID)
    ]


def test_a_restore_writes_the_marker_in_the_policy_system_group_with_the_requires_edge(
    restore_rig: RestoreRig, restore: _Restorer
) -> None:
    seed_target(restore_rig.policy_system)

    restore(_version(FIRST_VERSION), "first")

    assert _markers(restore_rig) == {RESOLVED_ID: {"resolves_to": TARGET_ID}}
    assert ("Capability", RESOLVED_ID) not in restore_rig.graph(_POLICY_SYSTEM).nodes
    [group] = [g for g in restore_rig.store.groups if g.graph == _POLICY_SYSTEM]
    written = {(e.name, e.identity) for e in group.entries}
    assert (_MARKER, RESOLVED_ID) in written
    assert ("REQUIRES", f"REQUIRES:{_obligation(FIRST_VERSION)}->{TARGET_ID}") in written


def test_a_refused_restore_leg_writes_neither_the_marker_nor_the_edges(
    restore_rig: RestoreRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    seed_target(restore_rig.policy_system)
    install_racing_gateway(restore_rig, races=MAX_POLICY_SYSTEM_ATTEMPTS)
    emitter, log_path = make_emitter()

    with pytest.raises(RestoreConcurrencyConflictError):
        restore_rig.restore(_version(FIRST_VERSION), emitter=emitter, threshold=MERGE)
    emitter.flush()

    assert _markers(restore_rig) == {}
    assert _requires_targets(restore_rig, FIRST_VERSION) == set()
    assert _entries(read_lines(log_path), "record_semantic_resolution") == []


def test_restoring_into_a_graph_with_a_resolution_lands_on_the_recorded_target(
    restore_rig: RestoreRig, restore: _Restorer
) -> None:
    seed_target(restore_rig.policy_system)
    restore(_version(FIRST_VERSION), "first")
    restore(_unrelated(), "unrelated")
    assert ("Capability", NEWER_ID) in restore_rig.graph(_POLICY_SYSTEM).nodes
    first_ids = {_role(FIRST_VERSION), _obligation(FIRST_VERSION), FIRST_VERSION}
    first_edges = {e for e in _graph(restore_rig).edges if first_ids & {e[1], e[2]}}
    groups_before = len(restore_rig.store.groups)

    restore(_version(AMENDED_VERSION), "amended")

    assert _requires_targets(restore_rig, AMENDED_VERSION) == {TARGET_ID}
    assert ("Capability", RESOLVED_ID) not in restore_rig.graph(_POLICY_SYSTEM).nodes
    assert _markers(restore_rig) == {RESOLVED_ID: {"resolves_to": TARGET_ID}}
    amended_groups = restore_rig.store.groups[groups_before:]
    assert [g.graph for g in amended_groups if g.graph == _POLICY_SYSTEM] == [_POLICY_SYSTEM]
    assert [e for g in amended_groups for e in g.entries if e.name == _MARKER] == []
    assert [
        props
        for (label, _), props in restore_rig.graph(_POLICY_SYSTEM).nodes.items()
        if label == "PendingReview" and props["incoming_id"] == RESOLVED_ID
    ] == []
    assert {e for e in _graph(restore_rig).edges if first_ids & {e[1], e[2]}} == first_edges


def test_restore_follows_a_resolution_through_a_later_merge_to_the_survivor(
    restore_rig: RestoreRig, restore: _Restorer
) -> None:
    _seed(
        restore_rig,
        "Capability",
        _SURVIVOR_ID,
        {"name": "Incident Handling", "embedding": [0.0, 1.0]},
    )
    _seed(restore_rig, _MARKER, RESOLVED_ID, {"resolves_to": TARGET_ID})
    _seed(restore_rig, "MergedCapability", TARGET_ID, {"merged_into": _SURVIVOR_ID})

    log = restore(_version(AMENDED_VERSION), "amended")

    assert _requires_targets(restore_rig, AMENDED_VERSION) == {_SURVIVOR_ID}
    assert ("Capability", RESOLVED_ID) not in restore_rig.graph(_POLICY_SYSTEM).nodes
    [redirected] = [
        e for e in _entries(log, "dedupe_canonical_nodes") if e["entity_id"] == RESOLVED_ID
    ]
    assert redirected["outcome"] == "redirected"
    assert redirected["terminal_id"] == _SURVIVOR_ID
    assert redirected["marker"] == _MARKER


def test_restore_fails_closed_on_a_cycle_across_resolved_and_merged_markers(
    restore_rig: RestoreRig,
) -> None:
    _seed(restore_rig, _MARKER, RESOLVED_ID, {"resolves_to": TARGET_ID})
    _seed(restore_rig, "MergedCapability", TARGET_ID, {"merged_into": RESOLVED_ID})

    with pytest.raises(CompanyMergeValidationError) as raised:
        restore_rig.restore(_version(AMENDED_VERSION), threshold=MERGE)

    assert re.fullmatch(
        r"capability marker cycle detected at capability 'cap_[a-z0-9_]+'", str(raised.value)
    )
    assert restore_rig.store.groups == []
    assert restore_rig.store.multi_appends == []


def test_restore_fails_closed_on_a_resolution_ending_nowhere(restore_rig: RestoreRig) -> None:
    _seed(restore_rig, _MARKER, RESOLVED_ID, {"resolves_to": TARGET_ID})

    with pytest.raises(CompanyMergeValidationError) as raised:
        restore_rig.restore(_version(AMENDED_VERSION), threshold=MERGE)

    assert str(raised.value) == (
        f"capability '{RESOLVED_ID}' resolves to '{TARGET_ID}', which does not exist"
    )
    assert restore_rig.store.groups == []


def test_a_restored_marker_write_is_logged_after_the_group_is_applied(
    restore_rig: RestoreRig, restore: _Restorer
) -> None:
    seed_target(restore_rig.policy_system)

    log = restore(_version(FIRST_VERSION), "first")

    [entry] = _entries(log, "record_semantic_resolution")
    assert entry["component"] == "company_merge"
    assert entry["entity_id"] == RESOLVED_ID
    assert entry["outcome"] == "succeeded"
    assert entry["kind"] == "Capability"
    assert entry["resolves_to"] == TARGET_ID
    assert entry["similarity"] == pytest.approx(0.93)
    assert entry["regulatory_instrument_id"] == FIRST_VERSION
    assert RESOLVED_NAME not in str(entry)
    [succeeded] = [e for e in _entries(log, "restore_instrument") if e["outcome"] == "succeeded"]
    assert log.index(entry) < log.index(succeeded)


def test_building_the_leg_logs_neither_the_marker_write_nor_the_redirect(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Building a leg writes nothing, so its resolutions are logged only by the restore."""

    def answer(q: str, params: dict[str, object] | None) -> list[object] | None:
        del params
        if "(m:ResolvedCapability)" in q:
            return [[RESOLVED_ID, TARGET_ID]]
        if "(n:Capability) RETURN n.id, n.name, n.embedding" in q:
            return [[NEWER_ID, NEWER_NAME, list(NEWER_VECTOR)]]
        return None

    emitter, log_path = make_emitter()

    leg = build_policy_system_leg(
        _baseline(AMENDED_VERSION, RESOLVED_ID, RESOLVED_NAME, RESOLVED_VECTOR),
        instrument_id=AMENDED_VERSION,
        live=ScriptedLive(existing={"Capability": {TARGET_ID, NEWER_ID}}, answer=answer),
        governance=GovernanceImport(),
        merge_threshold=MERGE,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        emitter=emitter,
    )
    emitter.flush()

    assert [(r.incoming_id, r.terminal_id) for r in leg.capability_redirects] == [
        (RESOLVED_ID, TARGET_ID)
    ]
    log = read_lines(log_path)
    assert _entries(log, "record_semantic_resolution") == []
    assert [
        e for e in _entries(log, "dedupe_canonical_nodes") if e["outcome"] == "redirected"
    ] == []


def test_a_stale_retry_logs_each_resolution_once(
    restore_rig: RestoreRig, restore: _Restorer
) -> None:
    seed_target(restore_rig.policy_system)
    first_race = install_racing_gateway(restore_rig, races=1)
    first = restore(_version(FIRST_VERSION), "first")
    assert first_race.submits == 2
    restore(_unrelated(), "unrelated")
    amended_race = install_racing_gateway(restore_rig, races=1, rival_prefix="amended")

    amended = restore(_version(AMENDED_VERSION), "amended")

    assert amended_race.submits == 2
    assert [e["entity_id"] for e in _entries(first, "record_semantic_resolution")] == [RESOLVED_ID]
    redirected = [
        e for e in _entries(amended, "dedupe_canonical_nodes") if e["entity_id"] == RESOLVED_ID
    ]
    assert [(e["outcome"], e["terminal_id"], e["marker"]) for e in redirected] == [
        ("redirected", TARGET_ID, _MARKER)
    ]
    assert redirected[0]["regulatory_instrument_id"] == AMENDED_VERSION
    assert _entries(amended, "record_semantic_resolution") == []
