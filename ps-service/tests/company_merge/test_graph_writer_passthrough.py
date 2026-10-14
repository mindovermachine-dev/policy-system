"""Tests for `ps_service.company_merge.graph_writer.
persist_role_and_requirement_passthrough`/`persist_obligation_passthrough`:
the Regulation/Role/Requirement/Obligation writers. RegulatoryInstrument keeps
an unconditional `SET` (never canonically deduped, always refreshed);
Role/Requirement/Obligation are create-only (an existing id is left out of the group), matching
Capability's own load-bearing pattern (`persist_canonical_nodes`) exactly,
so a second `merge_baseline_graph` run against baseline graphs whose content has drifted slightly
cannot silently overwrite the properties of existing Role/Requirement/Obligation nodes.

The RegulatoryInstrument/Role/Requirement writer submits a gateway group (the
`merge_rig` fixture); the Obligation writer submits a group too.

Fakes implement the `GraphHandle`/`GraphQueryResult` Protocols
(`ps_service.company_merge.falkordb_client`) structurally -- no mocking
library, matching L2 Testing Patterns' "mock at component boundaries".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from company_merge._gateway_fakes import POLICY_SYSTEM
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.graph_writer import (
    persist_obligation_passthrough,
    persist_role_and_requirement_passthrough,
)
from ps_service.company_merge.models import BaselineNode, ProvenanceEdge

if TYPE_CHECKING:
    from company_merge._gateway_fakes import MergeRig
    from ps_service.graph_gateway.models import GraphLogEntry


def _role_node() -> BaselineNode:
    return BaselineNode(
        id="role_manufacturer_abc123", properties={"name": "Manufacturer", "confidence": 0.9}
    )


def _requirement_node() -> BaselineNode:
    return BaselineNode(
        id="CRA-1.0_req_art_13.1",
        properties={
            "text": "Conduct a cybersecurity risk assessment.",
            "type": "requirement",
            "confidence": 0.9,
            "role_id": "role_manufacturer_abc123",
        },
    )


def _regulatory_instrument_properties() -> dict[str, object]:
    return {"title": "Cyber Resilience Act", "jurisdiction": "EU"}


def _target(rig: MergeRig) -> GraphWriteTarget:
    return GraphWriteTarget(rig.gateway, POLICY_SYSTEM, rig.audit_event_id)


def _persist(
    rig: MergeRig,
    instrument_id: str,
    instrument_properties: dict[str, object],
    roles: tuple[BaselineNode, ...],
    requirements: tuple[BaselineNode, ...],
    edges: tuple[ProvenanceEdge, ...],
) -> None:
    persist_role_and_requirement_passthrough(
        _target(rig),
        rig.legacy,
        instrument_id,
        instrument_properties,
        roles,
        requirements,
        edges,
    )


def _properties(entry: GraphLogEntry) -> dict[str, object]:
    properties = entry.content["properties"]
    assert isinstance(properties, dict)
    return {str(key): value for key, value in properties.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def test_persist_writes_regulation_role_and_requirement_with_edges(merge_rig: MergeRig) -> None:
    role = _role_node()
    requirement = _requirement_node()
    defines_edge = ProvenanceEdge(
        relationship_type="DEFINES", target_id=role.id, source_ref="Art. 13(1)"
    )
    expresses_edge = ProvenanceEdge(
        relationship_type="EXPRESSES", target_id=requirement.id, source_ref="Art. 13(1)"
    )

    _persist(
        merge_rig,
        "CRA-1.0",
        _regulatory_instrument_properties(),
        (role,),
        (requirement,),
        (defines_edge, expresses_edge),
    )

    (group,) = merge_rig.groups
    assert len(group.entries) == 5
    regulatory_instrument_entry, role_entry, requirement_entry, defines_entry, expresses_entry = (
        group.entries
    )

    assert (regulatory_instrument_entry.name, regulatory_instrument_entry.identity) == (
        "RegulatoryInstrument",
        "CRA-1.0",
    )
    assert _properties(regulatory_instrument_entry) == _regulatory_instrument_properties()

    assert (role_entry.name, role_entry.identity) == ("Role", role.id)
    assert _properties(role_entry) == role.properties

    assert (requirement_entry.name, requirement_entry.identity) == ("Requirement", requirement.id)
    assert _properties(requirement_entry) == requirement.properties

    assert (defines_entry.name, defines_entry.identity) == (
        "DEFINES",
        f"DEFINES:CRA-1.0->{role.id}",
    )
    assert _properties(defines_entry) == {"source_ref": "Art. 13(1)"}

    assert (expresses_entry.name, expresses_entry.identity) == (
        "EXPRESSES",
        f"EXPRESSES:CRA-1.0->{requirement.id}",
    )
    assert _properties(expresses_entry) == {"source_ref": "Art. 13(1)"}


def test_persist_passthrough_writes_instrument_type_verbatim(merge_rig: MergeRig) -> None:
    """Write side: `instrument_type` rides
    through the Regulation upsert verbatim inside its properties —
    `regulatory_instrument_properties` is `dict[str, object]` with no key allow-list,
    so the key propagates by construction with NO src change.
    """
    _persist(
        merge_rig,
        "NIS2-1.0",
        {"title": "NIS2 Directive", "jurisdiction": "EU", "instrument_type": "directive"},
        (),
        (),
        (),
    )

    (group,) = merge_rig.groups
    assert len(group.entries) == 1
    assert group.entries[0].name == "RegulatoryInstrument"
    assert _properties(group.entries[0])["instrument_type"] == "directive"


def test_persist_is_idempotent_across_repeated_calls(merge_rig: MergeRig) -> None:
    """Re-running with identical input twice against the same graph leaves the graph as the
    first run did and logs nothing more (the gateway drops
    the mutations that change nothing).
    """
    role = _role_node()
    requirement = _requirement_node()
    defines_edge = ProvenanceEdge(
        relationship_type="DEFINES", target_id=role.id, source_ref="Art. 13(1)"
    )
    expresses_edge = ProvenanceEdge(
        relationship_type="EXPRESSES", target_id=requirement.id, source_ref="Art. 13(1)"
    )

    for _ in range(2):
        _persist(
            merge_rig,
            "CRA-1.0",
            _regulatory_instrument_properties(),
            (role,),
            (requirement,),
            (defines_edge, expresses_edge),
        )

    assert len(merge_rig.groups) == 1
    assert len(merge_rig.groups[0].entries) == 5
    assert len(merge_rig.policy_system.nodes) == 3
    assert len(merge_rig.policy_system.edges) == 2


def test_persist_with_no_role_or_requirement_nodes_writes_only_regulation(
    merge_rig: MergeRig,
) -> None:
    _persist(merge_rig, "CRA-1.0", _regulatory_instrument_properties(), (), (), ())

    (group,) = merge_rig.groups
    assert [entry.name for entry in group.entries] == ["RegulatoryInstrument"]


def test_persist_role_and_requirement_are_create_only(merge_rig: MergeRig) -> None:
    """Role/Requirement writes are create-only, matching Capability's
    own load-bearing pattern exactly -- NOT the refresh RegulatoryInstrument still gets (it is
    never canonically deduped and is always meant to refresh). An existing node is
    left out of the group (a gateway upsert would merge over it), so the second run's group
    carries the instrument alone.
    """
    for _ in range(2):
        _persist(
            merge_rig,
            "CRA-1.0",
            {**_regulatory_instrument_properties(), "title": f"run {len(merge_rig.groups)}"},
            (_role_node(),),
            (_requirement_node(),),
            (),
        )

    first, second = merge_rig.groups
    assert [entry.name for entry in first.entries] == [
        "RegulatoryInstrument",
        "Role",
        "Requirement",
    ]
    assert [entry.name for entry in second.entries] == ["RegulatoryInstrument"]


def test_persist_role_preserves_pre_existing_properties_on_second_write(
    merge_rig: MergeRig,
) -> None:
    """A second `merge_baseline_graph` run (e.g. re-merging CRA against a
    baseline graph whose content has since drifted) must never overwrite a
    pre-existing Role node's properties -- e.g. `confidence` silently
    drifting from 0.86 to 0.93.
    """
    role_id = "role_the_obligations_laid_down_in_this_regulation_5e6f1d"
    original_properties: dict[str, str | float] = {"name": "Regulated Entity", "confidence": 0.86}
    drifted_properties: dict[str, str | float] = {"name": "Regulated Entity", "confidence": 0.93}

    _persist(
        merge_rig,
        "CRA-1.0",
        _regulatory_instrument_properties(),
        (BaselineNode(role_id, original_properties),),
        (),
        (),
    )
    _persist(
        merge_rig,
        "CRA-1.0",
        _regulatory_instrument_properties(),
        (BaselineNode(role_id, drifted_properties),),
        (),
        (),
    )

    assert merge_rig.policy_system.nodes[("Role", role_id)] == original_properties


def test_persist_requirement_preserves_pre_existing_text_on_second_write(
    merge_rig: MergeRig,
) -> None:
    """A Requirement node's `confidence` (0.97->0.92) AND
    `text` (a "Member States " prefix silently added) must not change on a
    second run against `policy_system`. A second write with different
    property values must leave the pre-existing node untouched.
    """
    requirement_id = "CRA-1.0_req_art_4.3"
    original_properties: dict[str, str | float] = {
        "text": "Ensure conformity of the product.",
        "type": "requirement",
        "confidence": 0.97,
    }
    drifted_properties: dict[str, str | float] = {
        "text": "Member States shall ensure conformity of the product.",
        "type": "requirement",
        "confidence": 0.92,
    }

    _persist(
        merge_rig,
        "CRA-1.0",
        _regulatory_instrument_properties(),
        (),
        (BaselineNode(requirement_id, original_properties),),
        (),
    )
    _persist(
        merge_rig,
        "CRA-1.0",
        _regulatory_instrument_properties(),
        (),
        (BaselineNode(requirement_id, drifted_properties),),
        (),
    )

    assert merge_rig.policy_system.nodes[("Requirement", requirement_id)] == original_properties


def test_persist_obligation_writes_create_only_nodes_as_one_group(merge_rig: MergeRig) -> None:
    """Obligation writes are create-only, matching
    Role/Requirement/Capability's pattern (one gateway group, not Cypher).
    """
    obligation = BaselineNode(
        id="obl_ensure_correct_application_of_the_ce_marking_regime_0579d8",
        properties={
            "text": "Ensure correct application of the CE marking regime.",
            "confidence": 0.88,
        },
    )

    persist_obligation_passthrough(_target(merge_rig), merge_rig.legacy, (obligation,))

    assert len(merge_rig.groups) == 1
    [entry] = merge_rig.groups[0].entries
    assert (entry.name, entry.identity) == ("Obligation", obligation.id)
    assert _properties(entry) == obligation.properties


def test_persist_obligation_preserves_pre_existing_properties_on_second_write(
    merge_rig: MergeRig,
) -> None:
    """Obligation nodes must not have `confidence` drift between
    runs (e.g. `obl_ensure_correct_application_of_the_ce_marking_regime_0579d8`,
    0.88->0.93) after a second `merge_baseline_graph` run. A second write
    with a different `confidence` value must leave the pre-existing node
    untouched.
    """
    obligation_id = "obl_ensure_correct_application_of_the_ce_marking_regime_0579d8"
    original_properties: dict[str, str | float] = {
        "text": "Ensure correct application of the CE marking regime.",
        "confidence": 0.88,
    }
    drifted_properties: dict[str, str | float] = {
        "text": "Ensure correct application of the CE marking regime.",
        "confidence": 0.93,
    }
    target = _target(merge_rig)

    persist_obligation_passthrough(
        target, merge_rig.legacy, (BaselineNode(obligation_id, original_properties),)
    )
    persist_obligation_passthrough(
        target, merge_rig.legacy, (BaselineNode(obligation_id, drifted_properties),)
    )

    assert merge_rig.policy_system.nodes[("Obligation", obligation_id)] == original_properties
    assert len(merge_rig.groups) == 1  # the second write found it and logged nothing
