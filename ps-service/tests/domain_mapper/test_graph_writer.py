"""Tests for `ps_service.domain_mapper.graph_writer` (PLAN_REVIEWED.md §11
Increment 9).

Fakes implement the `GraphHandle`/`GraphQueryResult` Protocols
(`ps_service.domain_mapper.falkordb_client`) structurally — no mocking
library, matching L2 Testing Patterns' "mock at component boundaries" and
this issue's binding testing convention (§0.3/§0.5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from graph_gateway._fakes import Fault
from graph_gateway.checkpoint_checks import (
    assert_head_checkpoints,
    assert_no_checkpoint,
)

from domain_mapper._gateway_fakes import entry_properties, seed_extraction_output
from ps_service.dependency_health import FALKORDB, is_healthy, mark_unhealthy
from ps_service.domain_mapper.errors import DomainMapperPersistenceError
from ps_service.domain_mapper.graph_writer import (
    persist_obligation_and_capability_graph,
    persist_role_and_requirement_graph,
)
from ps_service.domain_mapper.models import (
    CapabilityNode,
    CapabilityRequiresEdge,
    ObligationHasEdge,
    ObligationNode,
    RequirementExpressesEdge,
    RequirementNode,
    RequirementSatisfiedByEdge,
    RoleDefinesEdge,
    RoleNode,
)

if TYPE_CHECKING:
    from domain_mapper._gateway_fakes import BaselineRig
    from ps_service.graph_gateway.models import GraphLogEntry


def _role_node(role_id: str = "role_manufacturer_abc123", confidence: float = 0.9) -> RoleNode:
    return RoleNode(id=role_id, properties={"name": "Manufacturer", "confidence": confidence})


def _requirement_node(
    requirement_id: str = "CRA-1.0_req_art_13.1",
    role_id: str = "role_manufacturer_abc123",
    text: str = "Conduct a cybersecurity risk assessment.",
    confidence: float = 0.9,
) -> RequirementNode:
    return RequirementNode(
        id=requirement_id,
        properties={
            "text": text,
            "type": "requirement",
            "confidence": confidence,
            "role_id": role_id,
        },
    )


def _regulatory_instrument_properties() -> dict[str, object]:
    return {"title": "Cyber Resilience Act", "jurisdiction": "EU"}


# --- The extraction group: what is submitted, in what order ------------------


def _persist(
    rig: BaselineRig,
    *,
    regulatory_instrument_id: str = "CRA-1.0",
    regulatory_instrument_properties: dict[str, object] | None = None,
    role_nodes: tuple[RoleNode, ...] = (),
    role_edges: tuple[RoleDefinesEdge, ...] = (),
    requirement_nodes: tuple[RequirementNode, ...] = (),
    requirement_edges: tuple[RequirementExpressesEdge, ...] = (),
) -> None:
    persist_role_and_requirement_graph(
        rig.submitter,
        rig.baseline_graph_name,
        rig.audit_event_id,
        regulatory_instrument_id=regulatory_instrument_id,
        regulatory_instrument_properties=(
            _regulatory_instrument_properties()
            if regulatory_instrument_properties is None
            else regulatory_instrument_properties
        ),
        role_nodes=role_nodes,
        role_edges=role_edges,
        requirement_nodes=requirement_nodes,
        requirement_edges=requirement_edges,
    )


def _endpoint_id(entry: GraphLogEntry, side: str) -> object:
    endpoint = entry.content[side]
    assert isinstance(endpoint, dict)
    return endpoint["id"]  # pyright: ignore[reportUnknownVariableType]


def test_persist_writes_regulation_role_and_requirement_with_edges(
    baseline_rig: BaselineRig,
) -> None:
    role = _role_node()
    requirement = _requirement_node()
    role_edge = RoleDefinesEdge(role_node_id=role.id, source_ref="Art. 13(1)")
    requirement_edge = RequirementExpressesEdge(
        requirement_node_id=requirement.id, source_ref="Art. 13(1)"
    )

    _persist(
        baseline_rig,
        role_nodes=(role,),
        role_edges=(role_edge,),
        requirement_nodes=(requirement,),
        requirement_edges=(requirement_edge,),
    )

    assert len(baseline_rig.logged()) == 5
    regulatory_instrument, role_entry, defines, requirement_entry, expresses = baseline_rig.logged()

    assert regulatory_instrument.name == "RegulatoryInstrument"
    assert regulatory_instrument.identity == "CRA-1.0"
    assert entry_properties(regulatory_instrument) == _regulatory_instrument_properties()

    assert role_entry.name == "Role"
    assert role_entry.identity == role.id
    assert entry_properties(role_entry) == role.properties

    assert defines.name == "DEFINES"
    assert _endpoint_id(defines, "source") == "CRA-1.0"
    assert _endpoint_id(defines, "target") == role.id
    assert entry_properties(defines) == {"source_ref": "Art. 13(1)"}

    assert requirement_entry.name == "Requirement"
    assert requirement_entry.identity == requirement.id
    assert entry_properties(requirement_entry) == requirement.properties

    assert expresses.name == "EXPRESSES"
    assert _endpoint_id(expresses, "source") == "CRA-1.0"
    assert _endpoint_id(expresses, "target") == requirement.id
    assert entry_properties(expresses) == {"source_ref": "Art. 13(1)"}


def test_persist_writes_requirement_status_from_real_extraction_output(
    baseline_rig: BaselineRig,
) -> None:
    """AC-BI-001 end-to-end (mint -> persist): a RequirementNode built by
    extraction.py's own _build_requirement_graph (not this file's
    _requirement_node() fixture) carries status="active" all the way into
    the logged Requirement entry's properties, proving the property bag passes
    through the writer unchanged (no property allow-list exists here,
    unlike ps_service.ingestion.graph_writer's element_type allow-list).
    """
    from ps_service.domain_mapper.extraction import (
        _build_requirement_graph,  # pyright: ignore[reportPrivateUsage]
    )
    from ps_service.domain_mapper.models import RequirementCandidate

    candidate = RequirementCandidate.model_validate(
        {
            "unit_citation_ref": "Art. 13(1)",
            "unit_article_number": "13",
            "unit_paragraph_number": "1",
            "role_name": "Manufacturer",
            "text": "Manufacturers shall conduct a cybersecurity risk assessment.",
            "type": "requirement",
            "letter_suffix": None,
            "confidence": 0.9,
        }
    )
    requirement_nodes, _edges, _collided = _build_requirement_graph(
        [candidate], "CRA-1.0", {"Manufacturer": "role_manufacturer_abc123"}
    )

    _persist(
        baseline_rig,
        role_nodes=(_role_node(),),
        role_edges=(
            RoleDefinesEdge(role_node_id="role_manufacturer_abc123", source_ref="Art. 13(1)"),
        ),
        requirement_nodes=requirement_nodes,
        requirement_edges=(
            RequirementExpressesEdge(
                requirement_node_id=requirement_nodes[0].id, source_ref="Art. 13(1)"
            ),
        ),
    )

    [requirement_entry] = baseline_rig.entries_named("Requirement")
    assert entry_properties(requirement_entry)["status"] == "active"


def test_persist_writes_instrument_type_verbatim_when_present_in_properties(
    baseline_rig: BaselineRig,
) -> None:
    """AC-BI-010 (Domain Mapper, write side): `instrument_type` rides
    through into the RegulatoryInstrument entry verbatim --
    `regulatory_instrument_properties` is `dict[str, object]` with no key allow-list,
    so the key propagates by construction with NO src change.
    """
    _persist(
        baseline_rig,
        regulatory_instrument_id="NIS2-1.0",
        regulatory_instrument_properties={
            "title": "NIS2 Directive",
            "jurisdiction": "EU",
            "instrument_type": "directive",
        },
    )

    [regulatory_instrument] = baseline_rig.logged()
    assert regulatory_instrument.name == "RegulatoryInstrument"
    assert entry_properties(regulatory_instrument)["instrument_type"] == "directive"


def test_persist_carries_source_ref_only_as_an_edge_property(
    baseline_rig: BaselineRig,
) -> None:
    """`source_ref` is present in the DEFINES/EXPRESSES edge properties, and its
    VALUE appears nowhere else in the group -- not in a label, a relationship
    type or an identity, and not on a node.
    """
    role = _role_node()
    requirement = _requirement_node()
    distinctive_source_ref = "Art. 13(1)(zzz-distinctive-value)"
    role_edge = RoleDefinesEdge(role_node_id=role.id, source_ref=distinctive_source_ref)
    requirement_edge = RequirementExpressesEdge(
        requirement_node_id=requirement.id, source_ref=distinctive_source_ref
    )

    _persist(
        baseline_rig,
        role_nodes=(role,),
        role_edges=(role_edge,),
        requirement_nodes=(requirement,),
        requirement_edges=(requirement_edge,),
    )

    for entry in baseline_rig.logged():
        assert distinctive_source_ref not in entry.name
        assert distinctive_source_ref not in entry.identity
        if entry.name in {"DEFINES", "EXPRESSES"}:
            assert entry_properties(entry)["source_ref"] == distinctive_source_ref
        else:
            assert distinctive_source_ref not in str(entry.content)


def test_persist_writes_zero_role_and_requirement_elements_when_collections_empty(
    baseline_rig: BaselineRig,
) -> None:
    """Zero-unit extraction: the group holds only the RegulatoryInstrument, no
    exception -- mirrors Increment 10's zero-unit-extraction case.
    """
    _persist(baseline_rig)

    [group] = baseline_rig.groups
    assert [entry.name for entry in group.entries] == ["RegulatoryInstrument"]


# --- B3 fix (a): validate-then-write, nothing logged before the raise ---------


def test_persist_raises_before_any_write_when_requirement_role_id_is_dangling(
    baseline_rig: BaselineRig,
) -> None:
    """THE B3 fix (a) direct test. A Requirement node whose `role_id`
    references a Role NOT present in this same call's `role_nodes`
    collection -> DomainMapperPersistenceError raised, AND the rig's log is
    asserted EMPTY at raise time -- proving nothing was submitted before the
    raise, not just that a raise happened.
    """
    role = _role_node(role_id="role_manufacturer_abc123")
    dangling_requirement = _requirement_node(role_id="role_importer_does_not_exist")
    role_edge = RoleDefinesEdge(role_node_id=role.id, source_ref="Art. 13(1)")
    requirement_edge = RequirementExpressesEdge(
        requirement_node_id=dangling_requirement.id, source_ref="Art. 13(1)"
    )

    with pytest.raises(DomainMapperPersistenceError) as exc_info:
        _persist(
            baseline_rig,
            role_nodes=(role,),
            role_edges=(role_edge,),
            requirement_nodes=(dangling_requirement,),
            requirement_edges=(requirement_edge,),
        )

    assert dangling_requirement.id in str(exc_info.value)
    assert "role_importer_does_not_exist" in str(exc_info.value)
    assert baseline_rig.groups == []
    assert baseline_rig.logged() == []


def test_persist_raises_when_role_nodes_collection_is_empty_but_requirement_references_role(
    baseline_rig: BaselineRig,
) -> None:
    """A second shape of the same dangling-reference bug: zero Role nodes
    at all in this call, but a Requirement still carries a role_id.
    """
    requirement = _requirement_node(role_id="role_manufacturer_abc123")
    requirement_edge = RequirementExpressesEdge(
        requirement_node_id=requirement.id, source_ref="Art. 13(1)"
    )

    with pytest.raises(DomainMapperPersistenceError):
        _persist(
            baseline_rig,
            requirement_nodes=(requirement,),
            requirement_edges=(requirement_edge,),
        )

    assert baseline_rig.groups == []
    assert baseline_rig.logged() == []


# --- BASELINE.md row #2 regression: RegulatoryInstrument merge idempotency ---


def test_persist_regulatory_instrument_merge_is_idempotent_across_repeated_calls(
    baseline_rig: BaselineRig,
) -> None:
    """BASELINE.md row #2 regression. Live capstone reconnaissance: the
    native graph's own RegulatoryInstrument node's full property bag
    (`extraction.py`'s `_read_regulatory_instrument_properties`, `dict(node.
    properties)`) carries its OWN `id` ("cra-1.0", ingestion's own casing) --
    different from the caller's `regulatory_instrument_id` ("CRA-1.0",
    extraction's casing) -- and that whole bag, `id` key included, used to
    flow unchanged into `regulatory_instrument_properties` here.

    Writing that bag over the matched node blindly overwrote its `id` and broke
    the match key for every SUBSEQUENT call: confirmed live, `cra_baseline`
    ended up with THREE RegulatoryInstrument nodes all at `id: 'cra-1.0'` and
    ZERO at `id: 'CRA-1.0'` after ~3 calls.

    Persisting TWICE in a row with the same `regulatory_instrument_id`, given
    `regulatory_instrument_properties` whose OWN `id` differs from it, must
    leave exactly ONE RegulatoryInstrument node in the baseline graph -- with
    `id == regulatory_instrument_id` -- not two, and not stuck at the native
    graph's own casing.
    """
    # The native graph's own RegulatoryInstrument node property bag -- its
    # own `id` ("cra-1.0") differs in casing from the caller's
    # regulatory_instrument_id ("CRA-1.0") below, exactly like the live bug.
    native_properties: dict[str, object] = {"id": "cra-1.0", "title": "Cyber Resilience Act"}

    _persist(baseline_rig, regulatory_instrument_properties=native_properties)
    _persist(baseline_rig, regulatory_instrument_properties=native_properties)

    instruments = {
        node_id: node
        for (label, node_id), node in baseline_rig.baseline.nodes.items()
        if label == "RegulatoryInstrument"
    }
    assert list(instruments) == ["CRA-1.0"]
    assert instruments["CRA-1.0"]["title"] == "Cyber Resilience Act"
    assert ("RegulatoryInstrument", "cra-1.0") not in baseline_rig.baseline.nodes


# --- Gateway failures reach the caller as the persistence error --------------
# Write-side dependency health is the gateway's (`graph_gateway.retry`, `gateway`); the
# writer only turns a refused or unapplied group into `DomainMapperPersistenceError`.


def test_persist_raises_the_persistence_error_when_the_group_cannot_be_logged(
    baseline_rig: BaselineRig,
) -> None:
    baseline_rig.store.fail_appends()

    with pytest.raises(DomainMapperPersistenceError) as exc_info:
        _persist(baseline_rig)

    assert str(exc_info.value).startswith("FalkorDB write failed: ")
    assert exc_info.value.__cause__ is not None
    assert baseline_rig.logged() == []


def test_persist_marks_falkordb_unhealthy_on_connection_error(
    baseline_rig: BaselineRig,
) -> None:
    """FalkorDB down: the group is committed (safe in the log) but not applied, which the
    writer reports as the persistence error, and the gateway records the outage.
    """
    baseline_rig.baseline.fail_on_write(redis.exceptions.ConnectionError("Error 111 connecting"))

    with pytest.raises(DomainMapperPersistenceError):
        _persist(baseline_rig)

    assert is_healthy(FALKORDB) is False
    assert [entry.name for entry in baseline_rig.logged()] == ["RegulatoryInstrument"]


def test_persist_marks_falkordb_healthy_on_success(baseline_rig: BaselineRig) -> None:
    mark_unhealthy(FALKORDB, error=RuntimeError("earlier outage"))

    _persist(baseline_rig)

    assert is_healthy(FALKORDB) is True


def test_validation_error_does_not_mark_falkordb_unhealthy(baseline_rig: BaselineRig) -> None:
    """A B3-style validation failure (dangling role_id) never reaches the gateway
    at all -- it must not be mistaken for a FalkorDB outage.
    """
    dangling_requirement = _requirement_node(role_id="role_does_not_exist")
    requirement_edge = RequirementExpressesEdge(
        requirement_node_id=dangling_requirement.id, source_ref="Art. 13(1)"
    )

    with pytest.raises(DomainMapperPersistenceError):
        _persist(
            baseline_rig,
            requirement_nodes=(dangling_requirement,),
            requirement_edges=(requirement_edge,),
        )

    assert is_healthy(FALKORDB) is True


# --- persist_obligation_and_capability_graph (PLAN_REVIEWED.md §11 Increment 15) --

_ROLE_ID = "role_manufacturer_abc123"
_REQUIREMENT_ID = "CRA-1.0_req_art_13.1"


def _obligation_node(
    obligation_id: str = "obl_cooperate_with_market_surveillance_requests_abc123",
    text: str = "Cooperate with market surveillance requests.",
    confidence: float = 0.85,
) -> ObligationNode:
    return ObligationNode(id=obligation_id, properties={"text": text, "confidence": confidence})


def _capability_node(
    capability_id: str = "cap_incident_reporting_process_def456",
    name: str = "Incident Reporting Process",
    confidence: float = 0.8,
    description: str | None = None,
) -> CapabilityNode:
    properties: dict[str, str | float] = {"name": name, "confidence": confidence}
    if description is not None:
        properties["description"] = description
    return CapabilityNode(id=capability_id, properties=properties)


def _seed_endpoints(rig: BaselineRig) -> None:
    """The Role and Requirement a prior extraction left in the graph for the edges to join."""
    seed_extraction_output(
        rig,
        "CRA-1.0",
        {_ROLE_ID: "Manufacturer"},
        [(_REQUIREMENT_ID, "Conduct a cybersecurity risk assessment.", _ROLE_ID)],
    )


def _persist_derivation(
    rig: BaselineRig,
    *,
    obligation_nodes: tuple[ObligationNode, ...] = (),
    has_edges: tuple[ObligationHasEdge, ...] = (),
    satisfied_by_edges: tuple[RequirementSatisfiedByEdge, ...] = (),
    capability_nodes: tuple[CapabilityNode, ...] = (),
    requires_edges: tuple[CapabilityRequiresEdge, ...] = (),
) -> None:
    persist_obligation_and_capability_graph(
        rig.submitter,
        rig.baseline_graph_name,
        rig.audit_event_id,
        graph=rig.read_graph,
        obligation_nodes=obligation_nodes,
        has_edges=has_edges,
        satisfied_by_edges=satisfied_by_edges,
        capability_nodes=capability_nodes,
        requires_edges=requires_edges,
    )


def _derived(rig: BaselineRig) -> list[GraphLogEntry]:
    """What the derivation group logged: everything after the seeded extraction group."""
    [_seed_group, group] = rig.groups
    return list(group.entries)


def test_persist_writes_obligation_capability_nodes_and_edges_exact_shape(
    baseline_rig: BaselineRig,
) -> None:
    _seed_endpoints(baseline_rig)
    obligation = _obligation_node()
    capability = _capability_node(description="Process for reporting security incidents.")
    has_edge = ObligationHasEdge(role_node_id=_ROLE_ID, obligation_node_id=obligation.id)
    satisfied_by_edge = RequirementSatisfiedByEdge(
        requirement_id=_REQUIREMENT_ID, obligation_node_id=obligation.id
    )
    requires_edge = CapabilityRequiresEdge(
        obligation_node_id=obligation.id, capability_node_id=capability.id
    )

    _persist_derivation(
        baseline_rig,
        obligation_nodes=(obligation,),
        has_edges=(has_edge,),
        satisfied_by_edges=(satisfied_by_edge,),
        capability_nodes=(capability,),
        requires_edges=(requires_edge,),
    )

    entries = _derived(baseline_rig)
    assert len(entries) == 5
    obligation_entry, capability_entry, has_entry, satisfied_by_entry, requires_entry = entries

    assert obligation_entry.name == "Obligation"
    assert obligation_entry.identity == obligation.id
    assert entry_properties(obligation_entry) == obligation.properties

    assert capability_entry.name == "Capability"
    assert capability_entry.identity == capability.id
    assert entry_properties(capability_entry) == capability.properties

    assert has_entry.name == "HAS"
    assert _endpoint_id(has_entry, "source") == has_edge.role_node_id
    assert _endpoint_id(has_entry, "target") == has_edge.obligation_node_id

    assert satisfied_by_entry.name == "SATISFIED_BY"
    assert _endpoint_id(satisfied_by_entry, "source") == satisfied_by_edge.requirement_id
    assert _endpoint_id(satisfied_by_entry, "target") == satisfied_by_edge.obligation_node_id

    assert requires_entry.name == "REQUIRES"
    assert _endpoint_id(requires_entry, "source") == requires_edge.obligation_node_id
    assert _endpoint_id(requires_entry, "target") == requires_edge.capability_node_id


def test_persist_writes_capability_status_from_real_derivation_output(
    baseline_rig: BaselineRig,
) -> None:
    """AC-BI-002 end-to-end (mint -> persist): a CapabilityNode built by
    derivation.py's own _to_capability_node (not this file's
    _capability_node() fixture) carries status="active" all the way into
    the written node's properties — same pass-through proof as Slice 1's
    requirement-side equivalent; graph_writer.py needs no change.
    """
    from ps_service.domain_mapper.derivation import (
        _to_capability_node,  # pyright: ignore[reportPrivateUsage]
    )
    from ps_service.domain_mapper.models import CapabilityDecision

    decision = CapabilityDecision(
        obligation_node_id="obl_risk_management_abc123",
        capability_node_id="cap_security_logging_abc123",
        name="Security Logging",
        description=None,
        confidence=0.85,
    )
    capability = _to_capability_node(decision)

    _persist_derivation(baseline_rig, capability_nodes=(capability,))

    [entry] = baseline_rig.logged()
    assert entry.name == "Capability"
    assert entry_properties(entry)["status"] == "active"


def test_persist_obligation_and_capability_submits_nothing_when_collections_empty(
    baseline_rig: BaselineRig,
) -> None:
    """No group at all (a group needs at least one primitive), no stamp and no endpoint read."""
    _persist_derivation(baseline_rig)

    assert baseline_rig.groups == []
    assert baseline_rig.logged() == []
    assert baseline_rig.read_graph.queries == []


def test_persist_obligation_and_capability_writes_nodes_before_edges(
    baseline_rig: BaselineRig,
) -> None:
    """A group edge must follow the nodes it joins, so the write order is load-bearing: both
    node entries come before either edge entry.
    """
    _seed_endpoints(baseline_rig)
    obligation = _obligation_node()
    capability = _capability_node()
    has_edge = ObligationHasEdge(role_node_id=_ROLE_ID, obligation_node_id=obligation.id)
    requires_edge = CapabilityRequiresEdge(
        obligation_node_id=obligation.id, capability_node_id=capability.id
    )

    _persist_derivation(
        baseline_rig,
        obligation_nodes=(obligation,),
        has_edges=(has_edge,),
        capability_nodes=(capability,),
        requires_edges=(requires_edge,),
    )

    names = [entry.name for entry in _derived(baseline_rig)]
    node_indexes = [i for i, name in enumerate(names) if name in {"Obligation", "Capability"}]
    edge_indexes = [i for i, name in enumerate(names) if name in {"HAS", "REQUIRES"}]
    assert len(node_indexes) == 2
    assert len(edge_indexes) == 2
    assert max(node_indexes) < min(edge_indexes)


# --- Edge Catalog: HAS/SATISFIED_BY/REQUIRES carry NO properties, ever -----


def test_has_satisfied_by_requires_edges_never_carry_source_ref_or_any_property(
    baseline_rig: BaselineRig,
) -> None:
    """Direct test of the Edge Catalog's "no properties" column for these three edge types: an
    explicit negative assertion that `"source_ref"` never appears in any written
    HAS/SATISFIED_BY/REQUIRES entry, and that the entries hold no property at all.
    """
    _seed_endpoints(baseline_rig)
    obligation = _obligation_node()
    capability = _capability_node()
    has_edge = ObligationHasEdge(role_node_id=_ROLE_ID, obligation_node_id=obligation.id)
    satisfied_by_edge = RequirementSatisfiedByEdge(
        requirement_id=_REQUIREMENT_ID, obligation_node_id=obligation.id
    )
    requires_edge = CapabilityRequiresEdge(
        obligation_node_id=obligation.id, capability_node_id=capability.id
    )

    _persist_derivation(
        baseline_rig,
        obligation_nodes=(obligation,),
        has_edges=(has_edge,),
        satisfied_by_edges=(satisfied_by_edge,),
        capability_nodes=(capability,),
        requires_edges=(requires_edge,),
    )

    edge_entries = [
        entry
        for entry in _derived(baseline_rig)
        if entry.name in {"HAS", "SATISFIED_BY", "REQUIRES"}
    ]
    assert len(edge_entries) == 3
    for entry in edge_entries:
        assert "source_ref" not in repr(entry.content)
        assert entry_properties(entry) == {}
        # The content carries the operation, the two endpoints and the (empty) properties.
        assert set(entry.content) == {"op", "source", "target", "properties"}


# --- Obligation node properties: text, confidence --------------------------


def test_obligation_node_properties_text_and_confidence_are_node_properties_only(
    baseline_rig: BaselineRig,
) -> None:
    obligation = _obligation_node(
        obligation_id="obl_report_security_incidents_xyz789",
        text="Report security incidents to the competent authority.",
        confidence=0.73,
    )

    _persist_derivation(baseline_rig, obligation_nodes=(obligation,))

    [entry] = baseline_rig.logged()
    obligation_text = obligation.properties["text"]
    assert isinstance(obligation_text, str)
    assert obligation_text not in entry.name
    assert obligation_text not in entry.identity
    assert entry_properties(entry) == {
        "text": "Report security incidents to the competent authority.",
        "confidence": 0.73,
    }


# --- Capability node properties: name, description?, confidence ------------


def test_capability_node_properties_include_description_when_present(
    baseline_rig: BaselineRig,
) -> None:
    capability = _capability_node(
        capability_id="cap_incident_reporting_process_def456",
        name="Incident Reporting Process",
        confidence=0.9,
        description="A documented process for reporting security incidents.",
    )

    _persist_derivation(baseline_rig, capability_nodes=(capability,))

    [entry] = baseline_rig.logged()
    assert entry_properties(entry) == {
        "name": "Incident Reporting Process",
        "confidence": 0.9,
        "description": "A documented process for reporting security incidents.",
    }


def test_capability_node_omits_description_key_when_not_provided(
    baseline_rig: BaselineRig,
) -> None:
    capability = _capability_node(
        capability_id="cap_incident_reporting_process_def456",
        name="Incident Reporting Process",
        confidence=0.9,
        description=None,
    )

    _persist_derivation(baseline_rig, capability_nodes=(capability,))

    [entry] = baseline_rig.logged()
    properties = entry_properties(entry)
    assert "description" not in properties
    assert properties == {"name": "Incident Reporting Process", "confidence": 0.9}


# --- Dependency health ---------------------------------------------------------
# Write-side health is the gateway's (see the extraction tests above); the endpoint read made
# here through the baseline handle records FalkorDB reachability as the old writes did.


def test_persist_obligation_and_capability_marks_falkordb_unhealthy_on_connection_error(
    baseline_rig: BaselineRig,
) -> None:
    """FalkorDB down when the group is applied: committed but unapplied is the persistence
    error, and the gateway records the outage.
    """
    baseline_rig.baseline.fail_on_write(redis.exceptions.ConnectionError("Error 111 connecting"))

    with pytest.raises(DomainMapperPersistenceError):
        _persist_derivation(baseline_rig, obligation_nodes=(_obligation_node(),))

    assert is_healthy(FALKORDB) is False
    assert [entry.name for entry in baseline_rig.logged()] == ["Obligation"]


def test_persist_obligation_and_capability_marks_falkordb_healthy_on_success(
    baseline_rig: BaselineRig,
) -> None:
    mark_unhealthy(FALKORDB, error=RuntimeError("earlier outage"))

    _persist_derivation(baseline_rig, obligation_nodes=(_obligation_node(),))

    assert is_healthy(FALKORDB) is True


def test_a_failed_endpoint_read_marks_falkordb_unhealthy_and_submits_nothing(
    baseline_rig: BaselineRig,
) -> None:
    """The existing-node read hits FalkorDB: its connection error is the persistence error."""
    obligation = _obligation_node()
    baseline_rig.read_graph.error = redis.exceptions.ConnectionError("Error 111 connecting")

    with pytest.raises(DomainMapperPersistenceError) as exc_info:
        _persist_derivation(
            baseline_rig,
            obligation_nodes=(obligation,),
            has_edges=(ObligationHasEdge(role_node_id=_ROLE_ID, obligation_node_id=obligation.id),),
        )

    assert str(exc_info.value).startswith("FalkorDB write failed: ")
    assert isinstance(exc_info.value.__cause__, redis.exceptions.ConnectionError)
    assert is_healthy(FALKORDB) is False
    assert baseline_rig.groups == []


def test_a_successful_endpoint_read_marks_falkordb_healthy(baseline_rig: BaselineRig) -> None:
    """The read itself records recovery: the group is then refused for a log (Postgres) fault, so
    nothing after the read can have marked FalkorDB healthy.
    """
    _seed_endpoints(baseline_rig)
    obligation = _obligation_node()
    mark_unhealthy(FALKORDB, error=RuntimeError("earlier outage"))
    baseline_rig.store.fail_appends()

    with pytest.raises(DomainMapperPersistenceError):
        _persist_derivation(
            baseline_rig,
            obligation_nodes=(obligation,),
            has_edges=(ObligationHasEdge(role_node_id=_ROLE_ID, obligation_node_id=obligation.id),),
        )

    assert baseline_rig.read_graph.queries != []
    assert is_healthy(FALKORDB) is True


# --- Checkpoints (#215, S4c; AC-BI-009) --------------------------------------


def _one_derivation(rig: BaselineRig) -> None:
    obligation = _obligation_node()
    _persist_derivation(
        rig,
        obligation_nodes=(obligation,),
        has_edges=(ObligationHasEdge(role_node_id=_ROLE_ID, obligation_node_id=obligation.id),),
    )


def test_the_derivation_group_requests_a_checkpoint_so_the_baseline_has_one_at_its_head(
    baseline_rig: BaselineRig,
) -> None:
    _seed_endpoints(baseline_rig)

    _one_derivation(baseline_rig)

    assert_head_checkpoints(baseline_rig, [baseline_rig.baseline_graph_name])


def test_the_extraction_group_is_not_terminal_and_requests_no_checkpoint(
    baseline_rig: BaselineRig,
) -> None:
    _persist(baseline_rig, role_nodes=(_role_node(),))

    assert_no_checkpoint(baseline_rig)


def test_a_checkpoint_that_cannot_be_recorded_does_not_fail_the_derivation(
    baseline_rig: BaselineRig,
) -> None:
    _seed_endpoints(baseline_rig)
    baseline_rig.store.checkpoint_fault = Fault(redis.exceptions.ConnectionError("down"))

    _one_derivation(baseline_rig)

    assert ("Obligation", _obligation_node().id) in baseline_rig.baseline.nodes
    assert_no_checkpoint(baseline_rig)
