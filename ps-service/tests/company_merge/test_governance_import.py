"""Tests for `ps_service.company_merge.governance_import` (issues #227, #210, AC-BI-005).

The draft-governance pass is shared by restore and live internal ingest: a Policy/Standard/Control
tree is imported as `draft`, owned by the importing caller, create-only. The persistence step is
supplied by the caller (`write_drafts` is required): live merge passes the Graph Write Gateway
persister, restore its snapshot copy. These tests drive the real pass with the gateway persister
over the merge rig, so the "never overwrites an existing node" guarantee is proven against
behaviour (an existing node is left out of the group and nothing is logged for it).
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import pytest

from company_merge._gateway_fakes import POLICY_SYSTEM
from ps_service.company_merge.errors import GovernanceOwnerRequiredError
from ps_service.company_merge.governance_import import (
    DraftGovernanceWriter,
    GovernanceImport,
    governance_count_fields,
    import_draft_governance,
)
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.graph_writer import persist_governance_drafts
from ps_service.company_merge.models import BaselineGraph, BaselineNode, GovernanceDraftCounts

if TYPE_CHECKING:
    from collections.abc import Mapping

    from company_merge._gateway_fakes import MergeRig

_OWNER = ("alice@example.com", "https://idp.example/")


def _write_drafts(rig: MergeRig) -> DraftGovernanceWriter:
    """The writer live merge passes: the gateway persister with the rig's target bound."""
    return partial(
        persist_governance_drafts, GraphWriteTarget(rig.gateway, POLICY_SYSTEM, rig.audit_event_id)
    )


def _baseline(
    *,
    policies: tuple[BaselineNode, ...] = (),
    standards: tuple[BaselineNode, ...] = (),
    controls: tuple[BaselineNode, ...] = (),
) -> BaselineGraph:
    return BaselineGraph(
        regulatory_instrument_id="ENGPRAC-3.0",
        regulatory_instrument_properties={"id": "ENGPRAC-3.0"},
        role_nodes=(),
        requirement_nodes=(),
        obligation_nodes=(),
        capability_nodes=(),
        provenance_edges=(),
        bare_edges=(),
        policy_nodes=policies,
        standard_nodes=standards,
        control_nodes=controls,
    )


def _policy(status: str = "draft") -> BaselineNode:
    return BaselineNode(id="pol_a", properties={"title": "Access Policy", "status": status})


def _standard() -> BaselineNode:
    return BaselineNode(id="std_a", properties={"title": "Access Standard", "status": "approved"})


def _control() -> BaselineNode:
    return BaselineNode(id="ctrl_a", properties={"title": "Access Control", "type": "manual"})


def test_baseline_without_governance_writes_nothing_and_returns_none(merge_rig: MergeRig) -> None:
    mapping: dict[str, str] = {}

    counts = import_draft_governance(
        _baseline(),
        single_tenant_graph=merge_rig.legacy,
        governance=GovernanceImport(),
        canonical_id_by_incoming_id=mapping,
        write_drafts=_write_drafts(merge_rig),
    )

    assert counts is None
    assert merge_rig.legacy.statements == []
    assert merge_rig.groups == []
    assert mapping == {}


def test_governance_content_without_an_owner_is_refused_before_any_write(
    merge_rig: MergeRig,
) -> None:
    with pytest.raises(GovernanceOwnerRequiredError):
        import_draft_governance(
            _baseline(policies=(_policy(),)),
            single_tenant_graph=merge_rig.legacy,
            governance=GovernanceImport(owner=None),
            canonical_id_by_incoming_id={},
            write_drafts=_write_drafts(merge_rig),
        )

    assert merge_rig.legacy.statements == []
    assert merge_rig.groups == []


def test_tree_is_written_as_draft_owned_by_the_caller_and_create_only(merge_rig: MergeRig) -> None:
    mapping: dict[str, str] = {}

    counts = import_draft_governance(
        _baseline(policies=(_policy(),), standards=(_standard(),), controls=(_control(),)),
        single_tenant_graph=merge_rig.legacy,
        governance=GovernanceImport(owner=_OWNER),
        canonical_id_by_incoming_id=mapping,
        write_drafts=_write_drafts(merge_rig),
    )

    nodes = merge_rig.policy_system.nodes
    assert counts == GovernanceDraftCounts(policies=1, standards=1, controls=1, status_overridden=1)
    assert nodes[("Policy", "pol_a")]["status"] == "draft"
    assert nodes[("Policy", "pol_a")]["owner_subject"] == _OWNER[0]
    assert nodes[("Policy", "pol_a")]["owner_issuer"] == _OWNER[1]
    assert nodes[("Standard", "std_a")]["status"] == "draft"
    assert nodes[("Control", "ctrl_a")]["status"] == "draft"
    assert mapping == {"pol_a": "pol_a"}
    assert len(merge_rig.groups) == 1


def test_an_existing_approved_node_is_never_overwritten_by_a_reimport(merge_rig: MergeRig) -> None:
    merge_rig.policy_system.seed_node(
        ("Policy",), "pol_a", {"status": "approved", "owner_subject": "someone-else"}
    )

    import_draft_governance(
        _baseline(policies=(_policy(),)),
        single_tenant_graph=merge_rig.legacy,
        governance=GovernanceImport(owner=_OWNER),
        canonical_id_by_incoming_id={},
        write_drafts=_write_drafts(merge_rig),
    )

    assert merge_rig.policy_system.nodes[("Policy", "pol_a")] == {
        "status": "approved",
        "owner_subject": "someone-else",
    }
    assert merge_rig.groups == []


def test_count_fields_are_all_zero_without_governance_content() -> None:
    assert governance_count_fields(None) == {
        "governance_policies": 0,
        "governance_standards": 0,
        "governance_controls": 0,
        "governance_status_overridden": 0,
    }


def test_count_fields_carry_the_written_counts() -> None:
    counts = GovernanceDraftCounts(policies=11, standards=21, controls=39, status_overridden=2)

    assert governance_count_fields(counts) == {
        "governance_policies": 11,
        "governance_standards": 21,
        "governance_controls": 39,
        "governance_status_overridden": 2,
    }


def test_the_draft_writer_is_the_one_the_caller_supplies(merge_rig: MergeRig) -> None:
    """Restore passes its snapshot writer (issue #210, D1); the pass must use it."""
    seen: list[tuple[object, tuple[str, ...]]] = []
    sentinel = GovernanceDraftCounts(policies=7, standards=0, controls=0, status_overridden=0)

    def write_drafts(
        target: object,
        policies: tuple[BaselineNode, ...],
        standards: tuple[BaselineNode, ...],
        controls: tuple[BaselineNode, ...],
        /,
        *,
        owner: tuple[str, str],
        policy_embeddings: Mapping[str, tuple[float, ...]] | None,
    ) -> GovernanceDraftCounts:
        del standards, controls, owner, policy_embeddings
        seen.append((target, tuple(policy.id for policy in policies)))
        return sentinel

    counts = import_draft_governance(
        _baseline(policies=(_policy(),)),
        single_tenant_graph=merge_rig.legacy,
        governance=GovernanceImport(owner=_OWNER),
        canonical_id_by_incoming_id={},
        write_drafts=write_drafts,
    )

    assert counts is sentinel
    assert seen == [(merge_rig.legacy, ("pol_a",))]
    assert merge_rig.groups == []


def test_the_draft_writer_has_no_default(merge_rig: MergeRig) -> None:
    """A caller must choose the gateway or the snapshot writer: omitting it is a type error."""
    with pytest.raises(TypeError, match="write_drafts"):
        import_draft_governance(  # pyright: ignore[reportCallIssue]
            _baseline(policies=(_policy(),)),
            single_tenant_graph=merge_rig.legacy,
            governance=GovernanceImport(owner=_OWNER),
            canonical_id_by_incoming_id={},
        )

    assert merge_rig.groups == []
