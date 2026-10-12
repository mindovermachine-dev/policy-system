"""The gateway-backed succession builds the graph the frozen legacy writer built (issue #213).

Each scenario seeds a graph as a pre-gateway writer left it (edges without an identity), runs the
FROZEN legacy statement (`_legacy_reference`, against its interpreter) on a copy, runs the real
writer over the gateway rig on the other, and compares the two graphs with the equivalence
harness of #207, by `canonical_digest` (AC-BI-002, AC-BI-005). "Modulo edge identity": the
legacy graph's edges are stamped by the same `{type}:{source}->{target}` formula before the
comparison, as the gateway stamps its own.

The `cra_native` graph is the 505-edge curated CRA native artifact (the prior version `CRA-1.0`
among its nodes) plus the new version, so the five shapes are proven to touch nothing else.
`policy_system` is a small graph holding both versions, compared by its own digest. Each scenario
also runs twice: the second run is idempotent on both paths and adds no entry to the rig's log.

Documented non-equivalence, tested as a difference rather than an equivalence: a missing node was
a silent no-op for the old `MATCH`; it is now `ChangeMonitorStateError` (the graph is the same on
both sides, the new side also raises). Met modulo edge identity; the live digest and live
interpreter agreement are not verifiable here (no FalkorDB in this sandbox): the `falkordb_live`
tests stay as the live anchor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from graph_gateway._fakes import GatewayRig, InMemoryGraph
from graph_gateway.equivalence_harness import Artifact, load_artifact, run_equivalence

from change_monitor._gateway_fakes import SuccessionRig
from change_monitor._legacy_reference import (
    LegacyRecordingGraph,
    legacy_clear_marker,
    legacy_link_and_supersede,
    legacy_mark_stage,
    legacy_set_version,
    legacy_supersede_in_single_tenant,
)
from ps_service.change_monitor.errors import ChangeMonitorStateError
from ps_service.change_monitor.graph_submission import SuccessionTarget
from ps_service.change_monitor.succession import (
    clear_marker,
    link_and_supersede,
    mark_stage_complete,
    set_new_version_property,
    supersede_in_single_tenant,
)
from ps_service.graph_gateway.digest import canonical_digest

if TYPE_CHECKING:
    from collections.abc import Callable

    from change_monitor._fakes import MakeEmitter

_ARTIFACT = load_artifact(
    Path(__file__).resolve().parents[3] / "curated-content" / "CRA-1.0" / "native.json"
)
_PRIOR = "CRA-1.0"
_NEW = "CRA-2.0"
_STAGES = ("ingestion", "extraction", "derivation", "merge")

type GraphSeed = Callable[[InMemoryGraph], None]
type LegacyRun = Callable[[LegacyRecordingGraph, LegacyRecordingGraph], None]
type NewRun = Callable[[SuccessionTarget], None]


@dataclass(frozen=True)
class _Scenario:
    native: GraphSeed
    policy: GraphSeed
    legacy: LegacyRun
    new: NewRun


def _seed_new_version(graph: InMemoryGraph, *, marker: str | None = None) -> None:
    SuccessionRig.seed_instrument(graph, _NEW)  # as Ingestion leaves it: version 1.0
    if marker is not None:
        SuccessionRig.seed_marker(graph, _NEW, marker)


def _seed_repair(graph: InMemoryGraph) -> None:
    """An earlier ingestion-only link: prior superseded, edge without `absorbed` or identity."""
    _seed_new_version(graph, marker="merge")
    graph.nodes[("RegulatoryInstrument", _PRIOR)]["status"] = "superseded"
    SuccessionRig.seed_edge(graph, _PRIOR, _NEW, absorbed=None)


def _seed_finalize(graph: InMemoryGraph) -> None:
    """The native link landed (pre-gateway code: no identity); `policy_system` did not."""
    _seed_new_version(graph, marker="linked")
    graph.nodes[("RegulatoryInstrument", _PRIOR)]["status"] = "superseded"
    SuccessionRig.seed_edge(graph, _PRIOR, _NEW, absorbed=True)


def _seed_policy(graph: InMemoryGraph, *, legacy_edge: bool = False) -> None:
    SuccessionRig.seed_instrument(graph, _PRIOR)
    SuccessionRig.seed_instrument(graph, _NEW)
    if legacy_edge:
        SuccessionRig.seed_edge(graph, _PRIOR, _NEW, absorbed=None)


def _nothing(_graph: InMemoryGraph) -> None:
    return None


def _all_steps_legacy(native: LegacyRecordingGraph, policy: LegacyRecordingGraph) -> None:
    legacy_set_version(native, _NEW, "2.0")
    for stage in _STAGES:
        legacy_mark_stage(native, _NEW, stage)
    legacy_link_and_supersede(native, _PRIOR, _NEW)
    legacy_supersede_in_single_tenant(policy, _PRIOR, _NEW)
    legacy_clear_marker(native, _NEW)


def _all_steps_new(target: SuccessionTarget) -> None:
    set_new_version_property(target, _NEW, "2.0")
    for stage in _STAGES:
        mark_stage_complete(target, _NEW, stage)
    link_and_supersede(target, _PRIOR, _NEW)
    supersede_in_single_tenant(target, _PRIOR, _NEW)
    clear_marker(target, _NEW)


def _mark_every_stage_legacy(native: LegacyRecordingGraph, _policy: LegacyRecordingGraph) -> None:
    for stage in _STAGES:
        legacy_mark_stage(native, _NEW, stage)


def _mark_every_stage_new(target: SuccessionTarget) -> None:
    for stage in _STAGES:
        mark_stage_complete(target, _NEW, stage)


def _supersede_legacy(_native: LegacyRecordingGraph, policy: LegacyRecordingGraph) -> None:
    legacy_supersede_in_single_tenant(policy, _PRIOR, _NEW)


_SCENARIOS: dict[str, _Scenario] = {
    "set_version": _Scenario(
        native=_seed_new_version,
        policy=_nothing,
        legacy=lambda native, _policy: legacy_set_version(native, _NEW, "2.0"),
        new=lambda target: set_new_version_property(target, _NEW, "2.0"),
    ),
    "mark_every_stage": _Scenario(
        native=_seed_new_version,
        policy=_nothing,
        legacy=_mark_every_stage_legacy,
        new=_mark_every_stage_new,
    ),
    "link_fresh": _Scenario(
        native=lambda graph: _seed_new_version(graph, marker="merge"),
        policy=_nothing,
        legacy=lambda native, _policy: legacy_link_and_supersede(native, _PRIOR, _NEW),
        new=lambda target: link_and_supersede(target, _PRIOR, _NEW),
    ),
    "link_over_a_legacy_edge": _Scenario(
        native=_seed_repair,
        policy=_nothing,
        legacy=lambda native, _policy: legacy_link_and_supersede(native, _PRIOR, _NEW),
        new=lambda target: link_and_supersede(target, _PRIOR, _NEW),
    ),
    "link_finalize_again": _Scenario(
        native=_seed_finalize,
        policy=_nothing,
        legacy=lambda native, _policy: legacy_link_and_supersede(native, _PRIOR, _NEW),
        new=lambda target: link_and_supersede(target, _PRIOR, _NEW),
    ),
    "supersede_in_single_tenant": _Scenario(
        native=_nothing,
        policy=_seed_policy,
        legacy=_supersede_legacy,
        new=lambda target: supersede_in_single_tenant(target, _PRIOR, _NEW),
    ),
    "supersede_in_single_tenant_over_a_legacy_edge": _Scenario(
        native=_nothing,
        policy=lambda graph: _seed_policy(graph, legacy_edge=True),
        legacy=_supersede_legacy,
        new=lambda target: supersede_in_single_tenant(target, _PRIOR, _NEW),
    ),
    "clear_marker": _Scenario(
        native=lambda graph: _seed_new_version(graph, marker="linked"),
        policy=_nothing,
        legacy=lambda native, _policy: legacy_clear_marker(native, _NEW),
        new=lambda target: clear_marker(target, _NEW),
    ),
    "clear_marker_without_one": _Scenario(
        native=_seed_new_version,
        policy=_nothing,
        legacy=lambda native, _policy: legacy_clear_marker(native, _NEW),
        new=lambda target: clear_marker(target, _NEW),
    ),
    "the_whole_succession": _Scenario(
        native=_seed_new_version,
        policy=_seed_policy,
        legacy=_all_steps_legacy,
        new=_all_steps_new,
    ),
    "the_whole_succession_over_legacy_edges": _Scenario(
        native=_seed_repair,
        policy=lambda graph: _seed_policy(graph, legacy_edge=True),
        legacy=_all_steps_legacy,
        new=_all_steps_new,
    ),
}


# Sequences of several steps: a rerun walks the stage marker back and forth, as the legacy writer
# did (digest-equal on both paths), so only the single-shape scenarios assert "adds no log entry".
_SEQUENCES = frozenset(
    {"mark_every_stage", "the_whole_succession", "the_whole_succession_over_legacy_edges"}
)


def _background(graph: InMemoryGraph, artifact: Artifact) -> InMemoryGraph:
    """The curated native artifact as pre-existing, identified content."""
    for node in artifact.nodes:
        graph.seed_node((node.label,), node.id, {**node.properties})
    for edge in artifact.edges:
        key = (
            edge.relationship_type,
            edge.source_label,
            edge.source_id,
            edge.target_label,
            edge.target_id,
            edge.identity,
        )
        graph.edges[key] = {"identity": edge.identity, **edge.properties}
        graph.edge_internal_id(key)
    return graph


@dataclass
class _Outcome:
    """What each path leaves besides the native graph the harness compares."""

    old_policy: InMemoryGraph | None = None
    new_policy: InMemoryGraph | None = None
    groups_after_each_run: list[int] | None = None


def _compare(scenario: _Scenario, emitter: object, *, runs: int) -> tuple[bool, str, _Outcome]:
    outcome = _Outcome(groups_after_each_run=[])

    def old_path(artifact: Artifact) -> InMemoryGraph:
        native = _background(InMemoryGraph(), artifact)
        scenario.native(native)
        policy = InMemoryGraph()
        scenario.policy(policy)
        legacy_native = LegacyRecordingGraph.copy_of(native)
        legacy_policy = LegacyRecordingGraph.copy_of(policy)
        for _ in range(runs):
            scenario.legacy(legacy_native, legacy_policy)
        outcome.old_policy = legacy_policy.as_gateway_graph()
        return legacy_native.as_gateway_graph()

    def new_path(artifact: Artifact) -> InMemoryGraph:
        rig = GatewayRig()
        native = _background(rig.graphs.open("cra_native"), artifact)
        scenario.native(native)
        policy = rig.graphs.open("policy_system")
        scenario.policy(policy)
        audit_event_id = str(uuid.uuid4())
        rig.store.audit_rows.add(audit_event_id)
        target = SuccessionTarget(
            rig.gateway,
            "cra_native",
            "policy_system",
            lambda: audit_event_id,
            emitter,  # pyright: ignore[reportArgumentType]
        )
        assert outcome.groups_after_each_run is not None
        for _ in range(runs):
            scenario.new(target)
            outcome.groups_after_each_run.append(len(rig.store.groups))
        outcome.new_policy = policy
        rig.close()
        return native

    result = run_equivalence(_ARTIFACT, old_path, new_path)
    return result.passed, f"{result.old_digest} != {result.new_digest}", outcome


@pytest.mark.parametrize("runs", [1, 2], ids=["once", "twice"])
@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_the_gateway_succession_equals_the_frozen_legacy_writer_modulo_edge_identity(
    name: str, runs: int, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()

    passed, detail, outcome = _compare(_SCENARIOS[name], emitter, runs=runs)

    assert passed, detail
    assert outcome.old_policy is not None
    assert outcome.new_policy is not None
    assert canonical_digest(outcome.old_policy) == canonical_digest(outcome.new_policy)
    if runs == 2 and name not in _SEQUENCES:  # one shape run again is idempotent: nothing is logged
        first, second = outcome.groups_after_each_run or [0, 1]
        assert second == first


def test_a_missing_node_is_a_state_error_where_the_old_match_was_a_silent_no_op(
    make_emitter: MakeEmitter,
) -> None:
    """Documented difference (AC-BI-006): same graph on both sides, the new side also raises."""
    emitter, _ = make_emitter()
    missing_prior = _Scenario(
        native=_seed_new_version,
        policy=_nothing,
        legacy=lambda native, _policy: legacy_link_and_supersede(native, "CRA-0.9", _NEW),
        new=lambda target: link_and_supersede(target, "CRA-0.9", _NEW),
    )

    with pytest.raises(ChangeMonitorStateError):
        _compare(missing_prior, emitter, runs=1)

    old_untouched = _Scenario(
        native=_seed_new_version,
        policy=_nothing,
        legacy=lambda native, _policy: legacy_link_and_supersede(native, "CRA-0.9", _NEW),
        new=lambda _target: None,
    )
    passed, detail, _ = _compare(old_untouched, emitter, runs=1)
    assert passed, detail
