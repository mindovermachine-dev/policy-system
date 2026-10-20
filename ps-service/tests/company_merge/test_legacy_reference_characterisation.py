"""The frozen legacy reference and today's production build the same graph.

`_legacy_reference` is the former writer's Cypher, frozen, run against an interpreter. This file
proves three things about family A (RegulatoryInstrument, create-only Role/Requirement, the
DEFINES/EXPRESSES provenance edges):

* the frozen statements are the original ones (sha256 pins taken from `git show HEAD:`);
* the interpreter applies each Cypher clause the way FalkorDB does (create-only keeps, `SET`
  overwrites, a `MATCH` with a missing endpoint writes nothing, a repeated edge folds), and rejects
  any shape it was not taught;
* CURRENT `merge_baseline_graph`, fed the same baseline graphs, issues the same statements and
  leaves the same graph (digest, node and edge facts, call order) as the reference.

Met modulo edge identity; the live digest and live interpreter agreement are not verifiable here
(no FalkorDB in this sandbox).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from company_merge._gateway_fakes import (
    POLICY_SYSTEM,
    FamilyABaseline,
    MergeRig,
    family_a_baseline_graph,
)
from company_merge._legacy_reference import (
    FROZEN_STATEMENT_HASHES,
    LegacyRecordingGraph,
    SnapshotWriteStore,
    write_legacy_instrument_roles_requirements,
)
from company_merge._seed_merge_graphs import (
    EMBED_MODEL,
    MERGE_THRESHOLD,
    BaselineGraphReader,
    OneHotEmbeddingCaller,
)
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from ps_service.company_merge import graph_reader, graph_writer
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.models import BaselineNode, ProvenanceEdge
from ps_service.graph_gateway.digest import canonical_digest

if TYPE_CHECKING:
    from graph_gateway._fakes import InMemoryGraph

    from company_merge._fakes import MakeEmitter
    from ps_service.logging import LogEmitter

_WRITE_KEYWORD = re.compile(r"\b(MERGE|CREATE|SET|DELETE|REMOVE)\b")


@dataclass
class _CapturingGraph:
    """A `LegacyWriteTarget` that only keeps the statement texts it was given."""

    statements: list[str] = field(default_factory=list)

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        del params
        self.statements.append(q)
        return None


def _instrument(title: str) -> dict[str, object]:
    return {"title": title, "celex": "32024R2847"}


_CRA = FamilyABaseline(
    instrument_id="CRA-1.0",
    instrument_properties=_instrument("Cyber Resilience Act"),
    roles={"role_manufacturer": "Manufacturer", "role_importer": "Importer"},
    requirements={"req_a": "Conduct a risk assessment", "req_b": "Report incidents"},
    defines=(("role_manufacturer", "Art. 3(1)"), ("role_importer", "Art. 3(2)")),
    expresses=(("req_a", "Art. 13(1)"), ("req_b", "Art. 14")),
)
_CRA_DRIFTED = FamilyABaseline(
    instrument_id="CRA-1.0",
    instrument_properties=_instrument("Cyber Resilience Act (amended)"),
    roles={"role_manufacturer": "Manufacturer (renamed)", "role_importer": "Importer"},
    requirements={"req_a": "Conduct a DIFFERENT risk assessment", "req_b": "Report incidents"},
    defines=(("role_manufacturer", "Art. 3(1) as amended"), ("role_importer", "Art. 3(2)")),
    expresses=(("req_a", "Art. 13(1)"), ("req_b", "Art. 14")),
)
_NIS2_SHARING_A_ROLE = FamilyABaseline(
    instrument_id="NIS2-1.0",
    instrument_properties=_instrument("NIS 2 Directive"),
    roles={"role_manufacturer": "Manufacturer under NIS2", "role_operator": "Operator"},
    requirements={"req_a": "Same requirement id, other text", "req_n": "Notify the CSIRT"},
    defines=(("role_manufacturer", "Art. 2"), ("role_operator", "Art. 4")),
    expresses=(("req_a", "Art. 21"), ("req_n", "Art. 23")),
)
_DANGLING = FamilyABaseline(
    instrument_id="DORA-1.0",
    instrument_properties=_instrument("DORA"),
    roles={"role_entity": "Financial entity"},
    requirements={"req_d": "Maintain an ICT register"},
    # Both provenance edges name a node that is not in the graph: MATCH finds no endpoint.
    defines=(("role_entity", "Art. 5"), ("role_missing", "Art. 6")),
    expresses=(("req_d", "Art. 8"), ("req_missing", "Art. 9")),
)
_INSTRUMENT_ONLY = FamilyABaseline(
    instrument_id="EMPTY-1.0", instrument_properties=_instrument("Nothing else")
)

_SCENARIOS: dict[str, tuple[FamilyABaseline, ...]] = {
    "S-fresh": (_CRA,),
    "S-rerun": (_CRA, _CRA),
    "S-rerun-drifted": (_CRA, _CRA_DRIFTED),
    "S-overlap": (_CRA, _NIS2_SHARING_A_ROLE),
    "S-dangling-provenance": (_DANGLING,),
    "S-instrument-only": (_INSTRUMENT_ONLY,),
}


def _run_reference(steps: tuple[FamilyABaseline, ...]) -> LegacyRecordingGraph:
    reference = LegacyRecordingGraph()
    for step in steps:
        read = graph_reader.read_baseline_graph(
            BaselineGraphReader(family_a_baseline_graph(step)), step.instrument_id
        )
        write_legacy_instrument_roles_requirements(
            reference,
            step.instrument_id,
            read.regulatory_instrument_properties,
            read.role_nodes,
            read.requirement_nodes,
            read.provenance_edges,
        )
    return reference


def _run_production(
    steps: tuple[FamilyABaseline, ...], rig: MergeRig, emitter: LogEmitter
) -> InMemoryGraph:
    """The gateway path (slice 2b): the same baselines merged into the rig's `policy_system`."""
    for step in steps:
        merge_baseline_graph(
            step.instrument_id,
            baseline_graph=BaselineGraphReader(family_a_baseline_graph(step)),
            single_tenant_graph=rig.legacy,
            embed_model=EMBED_MODEL,
            merge_threshold=MERGE_THRESHOLD,
            review_threshold=REVIEW_THRESHOLD,
            role_review_threshold=ROLE_REVIEW_THRESHOLD,
            call_embedding=OneHotEmbeddingCaller(),
            emitter=emitter,
            **rig.merge_keywords(),
        )
    return rig.policy_system


@pytest.fixture
def emitter(make_emitter: MakeEmitter) -> LogEmitter:
    return make_emitter()[0]


def test_frozen_statements_match_their_pinned_hashes() -> None:
    capture = _CapturingGraph()

    write_legacy_instrument_roles_requirements(
        capture,
        "X",
        {},
        (BaselineNode("r", {}),),
        (BaselineNode("q", {}),),
        (ProvenanceEdge("DEFINES", "r", "a"), ProvenanceEdge("EXPRESSES", "q", "b")),
    )

    hashes = [hashlib.sha256(statement.encode()).hexdigest() for statement in capture.statements]
    assert hashes == [
        FROZEN_STATEMENT_HASHES[name]
        for name in ("instrument", "role", "requirement", "defines", "expresses")
    ]


def test_the_interpreter_rejects_a_statement_shape_it_was_not_taught() -> None:
    graph = LegacyRecordingGraph()

    with pytest.raises(AssertionError, match="not a frozen legacy statement"):
        graph.query("MATCH (n:Capability {id: $id}) WHERE n.embedding IS NULL SET n.x = 1", {})


def test_the_interpreter_applies_each_clause_the_way_the_database_does() -> None:
    graph = LegacyRecordingGraph()
    create_only = "MERGE (n:Role {id: $id}) ON CREATE SET n += $properties"
    set_always = "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties"
    provenance = (
        "MATCH (r:RegulatoryInstrument {id: $regulatory_instrument_id}), "
        "(n:Role {id: $target_id}) MERGE (r)-[e:DEFINES]->(n) SET e.source_ref = $source_ref"
    )

    graph.query(create_only, {"id": "role_a", "properties": {"name": "first"}})
    graph.query(create_only, {"id": "role_a", "properties": {"name": "second"}})
    graph.query(set_always, {"id": "ri", "properties": {"title": "first", "kept": "yes"}})
    graph.query(set_always, {"id": "ri", "properties": {"title": "second"}})
    ref: dict[str, object] = {
        "regulatory_instrument_id": "ri",
        "target_id": "role_a",
        "source_ref": "one",
    }
    graph.query(provenance, ref)
    graph.query(provenance, {**ref, "source_ref": "two"})
    graph.query(provenance, {**ref, "target_id": "role_missing"})

    assert graph.nodes[("Role", "role_a")] == {"name": "first"}  # ON CREATE SET: first wins
    assert graph.nodes[("RegulatoryInstrument", "ri")] == {"title": "second", "kept": "yes"}
    assert graph.edges == {
        ("DEFINES", "RegulatoryInstrument", "ri", "Role", "role_a"): {"source_ref": "two"}
    }  # a repeated MERGE folds, SET overwrites, a missing endpoint writes nothing


def test_the_interpreter_agrees_with_the_old_fake_on_the_nodes_both_build() -> None:
    """F5(c): the pre-migration fake `SingleTenantStore` and the interpreter build equal nodes."""
    read = graph_reader.read_baseline_graph(
        BaselineGraphReader(family_a_baseline_graph(_CRA)), "CRA-1.0"
    )
    interpreter, old_fake = LegacyRecordingGraph(), SnapshotWriteStore()
    for target in (interpreter, old_fake):
        write_legacy_instrument_roles_requirements(
            target,
            "CRA-1.0",
            read.regulatory_instrument_properties,
            read.role_nodes,
            read.requirement_nodes,
            read.provenance_edges,
        )

    # the old fake keeps the MERGE key `id` as a property too; the interpreter treats it as the key
    old_fake_nodes = {
        key: {name: value for name, value in props.items() if name != "id"}
        for key, props in old_fake.nodes.items()
    }
    assert interpreter.nodes == old_fake_nodes


@pytest.mark.parametrize("scenario", list(_SCENARIOS))
def test_the_frozen_reference_and_the_gateway_path_build_the_same_graph_for_family_a(
    scenario: str, merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    """The migrated writer, through the gateway, against the frozen reference.

    Met modulo edge identity (the reference graph is stamped `TYPE:source->target` by
    `as_gateway_graph`); live digest and live interpreter agreement are not verifiable here.
    """
    steps = _SCENARIOS[scenario]

    reference = _run_reference(steps)
    production = _run_production(steps, merge_rig, emitter)

    assert canonical_digest(production) == canonical_digest(reference.as_gateway_graph())


def test_repeated_ids_and_edges_in_one_input_build_the_graph_the_reference_builds(
    merge_rig: MergeRig,
) -> None:
    """S-repeated: `ON CREATE SET` keeps the first occurrence, a repeated edge keeps the last.

    A baseline graph cannot hold a node id twice, so this drives the writer with the repeated
    input directly, the reference and the gateway path given the same nodes and edges.
    """
    roles = (BaselineNode("role_a", {"name": "first"}), BaselineNode("role_a", {"name": "second"}))
    requirements = (
        BaselineNode("req_a", {"text": "first"}),
        BaselineNode("req_a", {"text": "2nd"}),
    )
    edges = (
        ProvenanceEdge("DEFINES", "role_a", "Art. 1"),
        ProvenanceEdge("DEFINES", "role_a", "Art. 2"),
        ProvenanceEdge("EXPRESSES", "req_a", "Art. 3"),
    )
    reference = LegacyRecordingGraph()
    write_legacy_instrument_roles_requirements(
        reference, "CRA-1.0", {"title": "t"}, roles, requirements, edges
    )

    graph_writer.persist_role_and_requirement_passthrough(
        GraphWriteTarget(merge_rig.gateway, POLICY_SYSTEM, merge_rig.audit_event_id),
        merge_rig.legacy,
        "CRA-1.0",
        {"title": "t"},
        roles,
        requirements,
        edges,
    )

    assert canonical_digest(merge_rig.policy_system) == canonical_digest(
        reference.as_gateway_graph()
    )


def test_the_scenarios_exercise_what_they_claim_to() -> None:
    """Guard of the guard: the comparison would be vacuous if these shapes never occurred."""
    drifted = _run_reference(_SCENARIOS["S-rerun-drifted"])
    overlap = _run_reference(_SCENARIOS["S-overlap"])
    dangling = _run_reference(_SCENARIOS["S-dangling-provenance"])

    # create-only: the second run's drifted Role/Requirement text did not win ...
    assert drifted.nodes[("Role", "role_manufacturer")]["name"] == "Manufacturer"
    assert drifted.nodes[("Requirement", "req_a")]["text"] == "Conduct a risk assessment"
    # ... the instrument `SET` did, and so did the edge's source_ref.
    assert drifted.nodes[("RegulatoryInstrument", "CRA-1.0")]["title"] == (
        "Cyber Resilience Act (amended)"
    )
    assert drifted.edges[
        ("DEFINES", "RegulatoryInstrument", "CRA-1.0", "Role", "role_manufacturer")
    ] == {"source_ref": "Art. 3(1) as amended"}
    # a Role id shared by two regulations keeps the first one's properties and gets both edges
    assert overlap.nodes[("Role", "role_manufacturer")]["name"] == "Manufacturer"
    assert {
        key[2] for key in overlap.edges if key[0] == "DEFINES" and key[4] == "role_manufacturer"
    } == {"CRA-1.0", "NIS2-1.0"}
    # an edge whose endpoint is missing was attempted but left no edge
    assert len(dangling.edge_calls) == 4
    assert len(dangling.edges) == 2
