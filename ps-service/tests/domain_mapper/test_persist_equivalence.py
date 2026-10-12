"""The gateway path builds the same baseline graph the pre-#209 writer did (AC-BI-002).

The reference is `_legacy_reference`, a frozen copy of the old writer's Cypher run against a
recording graph. Both paths are fed the same in-memory inputs (the Role/Requirement/Obligation/
Capability nodes and edges the extraction and derivation steps produce) and compared twice:

* Structural cross-check (independent of the gateway fake): the nodes and edges the legacy calls
  left (label, id, properties; type, endpoints, properties) equal, as multisets, the primitives
  of the groups the new writer submitted. An edge whose endpoint exists nowhere is in neither.
* Digest equivalence modulo identity: `ps-graph-digest-v1` of the graph the legacy calls build,
  with every edge stamped with the gateway's deterministic identity, equals the digest of the
  graph the gateway built. Anything else that differs still fails the digest.

The inputs come from the real pure steps driven by the scripted LLM of `_pipeline` (the
extraction and derivation fixtures), from a `#2` requirement-id collision, from hand-built
collections with repeated nodes and edges and edges whose endpoint exists nowhere, and from
recorded extractions: a trimmed subset of the curated CRA baseline (committed fixture) and the
whole artifact (skipped when absent).

Met modulo identity; the live digest is not verified here (no FalkorDB/Postgres in this sandbox).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from domain_mapper import _pipeline
from domain_mapper._legacy_reference import (
    LegacyRecordingGraph,
    primitive_edge_facts,
    primitive_node_facts,
    write_legacy_obligation_and_capability_graph,
    write_legacy_role_and_requirement_graph,
)
from domain_mapper._recorded_inputs import (
    FULL_ARTIFACT,
    SUBSET_FIXTURE,
    Inputs,
    recorded_inputs,
)
from ps_service.domain_mapper.derivation import (
    _derive_capabilities,  # pyright: ignore[reportPrivateUsage]
    _derive_obligations,  # pyright: ignore[reportPrivateUsage]
)
from ps_service.domain_mapper.extraction import (
    _build_defined_terms_map,  # pyright: ignore[reportPrivateUsage]
    _build_requirement_graph,  # pyright: ignore[reportPrivateUsage]
    _canonicalize_roles,  # pyright: ignore[reportPrivateUsage]
    _extract_all_candidates,  # pyright: ignore[reportPrivateUsage]
)
from ps_service.domain_mapper.graph_writer import (
    persist_obligation_and_capability_graph,
    persist_role_and_requirement_graph,
)
from ps_service.domain_mapper.models import (
    CapabilityNode,
    CapabilityRequiresEdge,
    ObligationHasEdge,
    ObligationNode,
    RequirementCandidate,
    RequirementSatisfiedByEdge,
    RoleDefinesEdge,
    RoleNode,
    RoleRequirements,
)
from ps_service.graph_gateway.digest import canonical_digest

if TYPE_CHECKING:
    from collections.abc import Callable

    from ingestion.adapters.internal_seed._legacy_reference import EdgeFact, NodeFact

    from domain_mapper._fakes import MakeEmitter
    from domain_mapper._gateway_fakes import BaselineRig
    from ps_service.graph_gateway.graph_reader import PersistedElements
    from ps_service.graph_gateway.models import MutationGroup
    from ps_service.ingestion.graph_submission import (
        EdgeIdentityBackfillOutcome,
        GroupOutcome,
    )
    from ps_service.logging import LogEmitter

_INSTRUMENT_ID = _pipeline.INSTRUMENT_ID
# The native node's own id differs in casing: the writer must strip it (BASELINE.md row 2).
_INSTRUMENT_PROPERTIES: dict[str, object] = {
    "id": "cra-1.0",
    "title": "Cyber Resilience Act",
    "version": 2,
}


class _RecordingSubmitter:
    """The rig's gateway, plus a record of every group submitted through it."""

    def __init__(self, rig: BaselineRig) -> None:
        self._gateway = rig.submitter
        self.groups: list[MutationGroup] = []

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        self.groups.append(group)
        return self._gateway.submit_group(group)

    def is_caught_up(self, graph: str) -> bool:
        return self._gateway.is_caught_up(graph)

    def stamp_legacy_edge_identity(
        self, graph: str, audit_event_id: str | None
    ) -> EdgeIdentityBackfillOutcome:
        return self._gateway.stamp_legacy_edge_identity(graph, audit_event_id)

    def read_persisted_elements(self, graph: str) -> PersistedElements:
        return self._gateway.read_persisted_elements(graph)


def _facts(submitter: _RecordingSubmitter) -> tuple[Counter[NodeFact], Counter[EdgeFact]]:
    nodes: Counter[NodeFact] = Counter()
    edges: Counter[EdgeFact] = Counter()
    for group in submitter.groups:
        nodes += primitive_node_facts(group)
        edges += primitive_edge_facts(group)
    return nodes, edges


def _candidate(**overrides: object) -> RequirementCandidate:
    fields: dict[str, object] = {
        "unit_citation_ref": "Art. 13(1)",
        "unit_article_number": "13",
        "unit_paragraph_number": "1",
        "role_name": "Manufacturer",
        "text": "Manufacturers shall conduct a cybersecurity risk assessment.",
        "type": "requirement",
        "letter_suffix": None,
        "confidence": 0.9,
    }
    fields.update(overrides)
    return RequirementCandidate.model_validate(fields)


def _extraction_inputs(candidates: list[RequirementCandidate]) -> Inputs:
    role_nodes, role_edges, role_node_ids, _ = _canonicalize_roles(candidates, _INSTRUMENT_ID, {})
    requirement_nodes, requirement_edges, _ = _build_requirement_graph(
        candidates, _INSTRUMENT_ID, role_node_ids
    )
    return Inputs(
        role_nodes=role_nodes,
        role_edges=role_edges,
        requirement_nodes=requirement_nodes,
        requirement_edges=requirement_edges,
    )


def _scripted_pipeline_inputs(emitter: LogEmitter) -> Inputs:
    """The scripted-LLM extraction and derivation fixtures of `_pipeline`, pure steps only."""
    units = (_pipeline.UNIT_MANUFACTURER, _pipeline.UNIT_IMPORTER)
    candidates, _ = _extract_all_candidates(
        units, model="fake-model", call_completion=_pipeline.scripted_extraction(), emitter=emitter
    )
    defined_terms = _build_defined_terms_map(
        units, model="fake-model", call_completion=_pipeline.scripted_extraction(), emitter=emitter
    )
    role_nodes, role_edges, role_node_ids, _ = _canonicalize_roles(
        candidates, _INSTRUMENT_ID, defined_terms
    )
    requirement_nodes, requirement_edges, _ = _build_requirement_graph(
        candidates, _INSTRUMENT_ID, role_node_ids
    )
    role_names = {node.id: str(node.properties["name"]) for node in role_nodes}
    roles = tuple(
        RoleRequirements(
            role_node_id=role_id,
            role_name=role_names[role_id],
            requirements=tuple(
                (node.id, str(node.properties["text"]))
                for node in requirement_nodes
                if node.properties["role_id"] == role_id
            ),
        )
        for role_id in role_names
    )
    obligation_nodes, has_edges, satisfied_by_edges, _ = _derive_obligations(
        roles, model="fake-model", call_completion=_pipeline.scripted_derivation(), emitter=emitter
    )
    # `_derive_capabilities` shares the one scripted derivation script with `_derive_obligations`
    # in the stages, so rebuild it: two Obligation answers first, then the Capability answers.
    script = _pipeline.scripted_derivation()
    for node_id in range(len(obligation_nodes)):
        script(model="fake-model", messages=[], timeout=1.0)
        del node_id
    capability_nodes, requires_edges, _ = _derive_capabilities(
        obligation_nodes, model="fake-model", call_completion=script, emitter=emitter
    )
    return Inputs(
        role_nodes=role_nodes,
        role_edges=role_edges,
        requirement_nodes=requirement_nodes,
        requirement_edges=requirement_edges,
        obligation_nodes=obligation_nodes,
        has_edges=has_edges,
        satisfied_by_edges=satisfied_by_edges,
        capability_nodes=capability_nodes,
        requires_edges=requires_edges,
    )


def _collision_inputs(_emitter: LogEmitter) -> Inputs:
    """Two different texts at one base id: the second Requirement id carries the `#2` suffix."""
    inputs = _extraction_inputs(
        [
            _candidate(),
            _candidate(text="Manufacturers shall also keep the assessment up to date."),
            _candidate(),
            _candidate(
                role_name="Importer", unit_citation_ref="Art. 14(2)", unit_paragraph_number="2"
            ),
        ]
    )
    assert any(node.id.endswith("#2") for node in inputs.requirement_nodes)
    return inputs


def _hand_built_inputs(_emitter: LogEmitter) -> Inputs:
    """Repeated nodes and edges (folded by `MERGE`) and derivation edges with a missing endpoint."""
    base = _extraction_inputs(
        [
            _candidate(),
            _candidate(
                role_name="Importer",
                unit_citation_ref="Art. 14(2)",
                unit_article_number="14",
                unit_paragraph_number="2",
            ),
        ]
    )
    (role, other_role) = base.role_nodes
    (requirement, _other_requirement) = base.requirement_nodes
    return Inputs(
        role_nodes=(
            *base.role_nodes,
            RoleNode(role.id, {"name": "Manufacturer", "confidence": 0.5}),
        ),
        role_edges=(*base.role_edges, RoleDefinesEdge(role.id, "Art. 99")),
        requirement_nodes=base.requirement_nodes,
        requirement_edges=base.requirement_edges,
        obligation_nodes=(
            ObligationNode("obl_1", {"text": "First", "confidence": 0.9}),
            ObligationNode("obl_2", {"text": "Second", "confidence": 0.8}),
            ObligationNode("obl_1", {"text": "First, revised", "confidence": 0.7}),
        ),
        has_edges=(
            ObligationHasEdge(role.id, "obl_1"),
            ObligationHasEdge(role.id, "obl_1"),
            ObligationHasEdge(other_role.id, "obl_2"),
            ObligationHasEdge("role_ghost", "obl_2"),
        ),
        satisfied_by_edges=(
            RequirementSatisfiedByEdge(requirement.id, "obl_1"),
            RequirementSatisfiedByEdge("req_ghost", "obl_2"),
            RequirementSatisfiedByEdge(requirement.id, "obl_ghost"),
        ),
        capability_nodes=(
            CapabilityNode("cap_1", {"name": "Tool", "confidence": 0.9}),
            CapabilityNode("cap_1", {"name": "Tool", "confidence": 0.9, "description": "d"}),
        ),
        requires_edges=(
            CapabilityRequiresEdge("obl_1", "cap_1"),
            CapabilityRequiresEdge("obl_1", "cap_1"),
            CapabilityRequiresEdge("obl_2", "cap_ghost"),
        ),
    )


def _recorded_subset_inputs(_emitter: LogEmitter) -> Inputs:
    """A trimmed, committed subset of the recorded CRA baseline (see the fixture README)."""
    return recorded_inputs(SUBSET_FIXTURE)


def _recorded_full_inputs(_emitter: LogEmitter) -> Inputs:
    """The whole curated CRA baseline; skipped when the artifact is not in the checkout."""
    if not FULL_ARTIFACT.is_file():
        pytest.skip(f"{FULL_ARTIFACT} is not present in this checkout")
    return recorded_inputs(FULL_ARTIFACT)


_SCENARIOS: list[tuple[str, Callable[[LogEmitter], Inputs]]] = [
    ("scripted-llm-pipeline", _scripted_pipeline_inputs),
    ("requirement-id-collision", _collision_inputs),
    ("repeats-and-missing-endpoints", _hand_built_inputs),
    ("recorded-extraction-cra-subset", _recorded_subset_inputs),
    ("recorded-extraction-cra-full-artifact", _recorded_full_inputs),
]


@pytest.fixture
def emitter(make_emitter: MakeEmitter) -> LogEmitter:
    """An emitter for the pure steps' log entries (not asserted here)."""
    return make_emitter()[0]


_SCENARIO_PARAMS = [pytest.param(make, id=name) for name, make in _SCENARIOS]


def _write_legacy(
    inputs: Inputs, instrument_properties: dict[str, object] | None = None
) -> LegacyRecordingGraph:
    legacy = LegacyRecordingGraph()
    write_legacy_role_and_requirement_graph(
        legacy,
        _INSTRUMENT_ID,
        _INSTRUMENT_PROPERTIES if instrument_properties is None else instrument_properties,
        inputs.role_nodes,
        inputs.role_edges,
        inputs.requirement_nodes,
        inputs.requirement_edges,
    )
    write_legacy_obligation_and_capability_graph(
        legacy,
        inputs.obligation_nodes,
        inputs.has_edges,
        inputs.satisfied_by_edges,
        inputs.capability_nodes,
        inputs.requires_edges,
    )
    return legacy


def _write_new(rig: BaselineRig, inputs: Inputs) -> _RecordingSubmitter:
    submitter = _RecordingSubmitter(rig)
    persist_role_and_requirement_graph(
        submitter,
        rig.baseline_graph_name,
        rig.audit_event_id,
        regulatory_instrument_id=_INSTRUMENT_ID,
        regulatory_instrument_properties=_INSTRUMENT_PROPERTIES,
        role_nodes=inputs.role_nodes,
        role_edges=inputs.role_edges,
        requirement_nodes=inputs.requirement_nodes,
        requirement_edges=inputs.requirement_edges,
    )
    persist_obligation_and_capability_graph(
        submitter,
        rig.baseline_graph_name,
        rig.audit_event_id,
        graph=rig.read_graph,
        obligation_nodes=inputs.obligation_nodes,
        has_edges=inputs.has_edges,
        satisfied_by_edges=inputs.satisfied_by_edges,
        capability_nodes=inputs.capability_nodes,
        requires_edges=inputs.requires_edges,
    )
    return submitter


@pytest.mark.parametrize("make_inputs", _SCENARIO_PARAMS)
def test_node_and_edge_writes_match_the_legacy_calls_one_for_one(
    emitter: LogEmitter, baseline_rig: BaselineRig, make_inputs: Callable[[LogEmitter], Inputs]
) -> None:
    inputs = make_inputs(emitter)
    legacy = _write_legacy(inputs)

    node_facts, edge_facts = _facts(_write_new(baseline_rig, inputs))

    assert node_facts == legacy.final_node_facts()
    assert edge_facts == legacy.final_edge_facts()
    assert sum(node_facts.values()) > 0
    assert sum(edge_facts.values()) > 0


@pytest.mark.parametrize("make_inputs", _SCENARIO_PARAMS)
def test_baseline_digest_after_the_gateway_path_equals_the_pre_migration_digest_modulo_edge_identity(  # noqa: E501 -- name fixed by the plan
    emitter: LogEmitter, baseline_rig: BaselineRig, make_inputs: Callable[[LogEmitter], Inputs]
) -> None:
    inputs = make_inputs(emitter)
    legacy = _write_legacy(inputs)

    _write_new(baseline_rig, inputs)

    assert canonical_digest(baseline_rig.baseline) == canonical_digest(legacy.as_gateway_graph())


def test_the_stages_over_the_scripted_llm_build_the_graph_the_legacy_writer_built(
    emitter: LogEmitter, baseline_rig: BaselineRig
) -> None:
    """The real stages (not just the writer functions) land on the legacy digest."""
    legacy = _write_legacy(
        _scripted_pipeline_inputs(emitter), {"id": _INSTRUMENT_ID, "title": "Cyber Resilience Act"}
    )

    _pipeline.run_extraction(baseline_rig, emitter)
    _pipeline.run_derivation(baseline_rig, emitter)

    assert canonical_digest(baseline_rig.baseline) == canonical_digest(legacy.as_gateway_graph())


# --- the guard of the guard: the comparison must fail on a real difference ---


def test_the_digest_comparison_fails_when_one_property_differs(
    emitter: LogEmitter,
    baseline_rig: BaselineRig,
) -> None:
    inputs = _scripted_pipeline_inputs(emitter)
    legacy = _write_legacy(inputs)
    _write_new(baseline_rig, inputs)
    requirement = inputs.requirement_nodes[0]
    baseline_rig.baseline.nodes[("Requirement", requirement.id)]["text"] = "tampered"

    assert canonical_digest(baseline_rig.baseline) != canonical_digest(legacy.as_gateway_graph())


def test_the_digest_comparison_fails_when_an_edge_is_missing(
    emitter: LogEmitter, baseline_rig: BaselineRig
) -> None:
    inputs = _scripted_pipeline_inputs(emitter)
    legacy = _write_legacy(inputs)
    _write_new(baseline_rig, inputs)
    del baseline_rig.baseline.edges[next(iter(baseline_rig.baseline.edges))]

    assert canonical_digest(baseline_rig.baseline) != canonical_digest(legacy.as_gateway_graph())


def test_the_structural_comparison_fails_when_a_property_differs(
    emitter: LogEmitter,
    baseline_rig: BaselineRig,
) -> None:
    inputs = _scripted_pipeline_inputs(emitter)
    legacy = _write_legacy(inputs)
    tampered = replace(
        inputs,
        role_nodes=(
            RoleNode(inputs.role_nodes[0].id, {"name": "tampered"}),
            *inputs.role_nodes[1:],
        ),
    )

    node_facts, _ = _facts(_write_new(baseline_rig, tampered))

    assert node_facts != legacy.final_node_facts()
