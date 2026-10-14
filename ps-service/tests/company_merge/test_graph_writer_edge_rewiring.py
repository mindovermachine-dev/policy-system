"""Tests for `ps_service.company_merge.graph_writer.persist_rewired_edges`, the edge writer.

Only a `REQUIRES` edge's Capability target (and the Policy and Capability endpoints of the
governance and classification edges) is rewritten through the caller-supplied canonical-id mapping;
every other endpoint is a passthrough node written as its baseline-local id.

Each edge is an `UpsertEdge` primitive in ONE gateway group on `policy_system`
(identity `{type}:{source}->{target}`, no properties). A missing endpoint writes nothing; that is
decided before submission from one batched existing-id read per label, and an edge with a missing
endpoint is left out of the group (a group edge with a missing endpoint would fail the whole
group). The gateway rig is the true boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from company_merge._gateway_fakes import POLICY_SYSTEM, MergeRig
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.graph_writer import persist_rewired_edges
from ps_service.company_merge.models import BareEdge
from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.domain_mapper.graph_writer import (
    _edge_identity,  # pyright: ignore[reportPrivateUsage]
)
from ps_service.graph_gateway.label_allow_list import (
    ALLOWED_NODE_LABELS,
    ALLOWED_RELATIONSHIP_TYPES,
)

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphQueryResult

# relationship type -> (source label, target label): the Edge Catalog rows this writer handles.
_ENDPOINTS: dict[str, tuple[str, str]] = {
    "HAS": ("Role", "Obligation"),
    "SATISFIED_BY": ("Requirement", "Obligation"),
    "REQUIRES": ("Obligation", "Capability"),
    "GOVERNED_BY": ("Capability", "Policy"),
    "SUPPORTED_BY": ("Policy", "Standard"),
    "IMPLEMENTED_BY": ("Standard", "Control"),
    "COVERS": ("PracticeArea", "Capability"),
    "OWNS": ("PracticeArea", "Policy"),
    "MITIGATED_BY": ("RiskPath", "Capability"),
    "VERIFIED_BY": ("RiskPath", "Control"),
}


class _DownGraph:
    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        del q, params
        raise redis.exceptions.ConnectionError("Error 111 connecting to 10.0.0.5:6379")


def _target(rig: MergeRig) -> GraphWriteTarget:
    return GraphWriteTarget(rig.gateway, POLICY_SYSTEM, rig.audit_event_id)


def _seed(rig: MergeRig, label: str, node_id: str) -> None:
    rig.policy_system.seed_node((label,), node_id, {})


def _seed_endpoints(rig: MergeRig, edge_type: str, source_id: str, target_id: str) -> None:
    source_label, target_label = _ENDPOINTS[edge_type]
    _seed(rig, source_label, source_id)
    _seed(rig, target_label, target_id)


def _written(rig: MergeRig) -> set[tuple[str, str, str, str, str]]:
    """The edges `policy_system` holds, as (type, source label, source id, target label, id)."""
    return {key[:5] for key in rig.policy_system.edges}


def _edge_entries(rig: MergeRig) -> list[tuple[str, str]]:
    return [(e.name, e.identity) for e in rig.logged() if e.name in _ENDPOINTS]


def test_every_edge_type_is_submitted_with_its_type_source_arrow_target_identity(
    merge_rig: MergeRig,
) -> None:
    """All ten Edge Catalog types, one group; the identity scheme is the Domain Mapper's own."""
    edges: list[BareEdge] = []
    for edge_type in _ENDPOINTS:
        _seed_endpoints(merge_rig, edge_type, f"{edge_type}_s", f"{edge_type}_t")
        edges.append(BareEdge(edge_type, f"{edge_type}_s", f"{edge_type}_t"))  # pyright: ignore[reportArgumentType]
    mapping = {e.source_id: e.source_id for e in edges} | {e.target_id: e.target_id for e in edges}

    persist_rewired_edges(_target(merge_rig), merge_rig.legacy, tuple(edges), mapping)

    assert len(merge_rig.groups) == 1
    assert merge_rig.groups[0].audit_event_id == merge_rig.audit_event_id
    assert [(e.name, e.identity) for e in merge_rig.groups[0].entries] == [
        (edge_type, _edge_identity(edge_type, f"{edge_type}_s", f"{edge_type}_t"))
        for edge_type in _ENDPOINTS
    ]
    assert _written(merge_rig) == {
        (
            edge_type,
            *_ENDPOINTS[edge_type][:1],
            f"{edge_type}_s",
            _ENDPOINTS[edge_type][1],
            f"{edge_type}_t",
        )
        for edge_type in _ENDPOINTS
    }
    assert [e.content["properties"] for e in merge_rig.groups[0].entries] == [{}] * 10  # pyright: ignore[reportIndexIssue]


def test_all_ten_edge_types_and_their_endpoint_labels_are_gateway_allow_listed() -> None:
    """A type or label outside the allow-list would fail the whole group at submission."""
    assert set(_ENDPOINTS) <= ALLOWED_RELATIONSHIP_TYPES
    assert {label for pair in _ENDPOINTS.values() for label in pair} <= ALLOWED_NODE_LABELS


def test_has_edge_target_is_rewritten_to_canonical_id(merge_rig: MergeRig) -> None:
    """A HAS edge whose baseline-local Obligation id maps onto a DIFFERENT existing node's
    canonical id is written with the canonical id as its target, never the baseline-local one.
    """
    _seed_endpoints(merge_rig, "HAS", "role_manufacturer_abc123", "obl_canonical_existing")
    edge = BareEdge("HAS", "role_manufacturer_abc123", "obl_baseline_local")

    persist_rewired_edges(
        _target(merge_rig),
        merge_rig.legacy,
        (edge,),
        {"obl_baseline_local": "obl_canonical_existing"},
    )

    assert _written(merge_rig) == {
        ("HAS", "Role", "role_manufacturer_abc123", "Obligation", "obl_canonical_existing")
    }
    assert _edge_entries(merge_rig) == [
        ("HAS", "HAS:role_manufacturer_abc123->obl_canonical_existing")
    ]


def test_satisfied_by_edge_target_is_rewritten_to_canonical_id(merge_rig: MergeRig) -> None:
    _seed_endpoints(merge_rig, "SATISFIED_BY", "CRA-1.0_req_art_13.1", "obl_canonical_existing")
    edge = BareEdge("SATISFIED_BY", "CRA-1.0_req_art_13.1", "obl_baseline_local")

    persist_rewired_edges(
        _target(merge_rig),
        merge_rig.legacy,
        (edge,),
        {"obl_baseline_local": "obl_canonical_existing"},
    )

    assert _written(merge_rig) == {
        (
            "SATISFIED_BY",
            "Requirement",
            "CRA-1.0_req_art_13.1",
            "Obligation",
            "obl_canonical_existing",
        )
    }


def test_requires_edge_source_is_rewritten_to_canonical_id(merge_rig: MergeRig) -> None:
    """A REQUIRES edge's Obligation SOURCE is rewritten too (the rule is endpoint-agnostic); the
    Capability target maps onto itself (an exact match), since validation requires an entry for it.
    """
    _seed_endpoints(merge_rig, "REQUIRES", "obl_canonical_existing", "cap_encrypt_data")
    edge = BareEdge("REQUIRES", "obl_baseline_local", "cap_encrypt_data")

    persist_rewired_edges(
        _target(merge_rig),
        merge_rig.legacy,
        (edge,),
        {"obl_baseline_local": "obl_canonical_existing", "cap_encrypt_data": "cap_encrypt_data"},
    )

    assert _written(merge_rig) == {
        ("REQUIRES", "Obligation", "obl_canonical_existing", "Capability", "cap_encrypt_data")
    }


def test_a_requires_target_is_rewritten_to_the_canonical_id_before_the_primitive_is_built(
    merge_rig: MergeRig,
) -> None:
    """The bug this guards: a matched Capability is never minted under its baseline-local id, so
    leaving that id in place made the old `MATCH` find no row and the edge was silently never
    written. The edge is written, to the CANONICAL Capability.
    """
    _seed_endpoints(merge_rig, "REQUIRES", "obl_canonical_existing", "cap_canonical_existing")
    edge = BareEdge("REQUIRES", "obl_baseline_local", "cap_baseline_local")

    persist_rewired_edges(
        _target(merge_rig),
        merge_rig.legacy,
        (edge,),
        {
            "obl_baseline_local": "obl_canonical_existing",
            "cap_baseline_local": "cap_canonical_existing",
        },
    )

    assert _edge_entries(merge_rig) == [
        ("REQUIRES", "REQUIRES:obl_canonical_existing->cap_canonical_existing")
    ]


def test_role_and_requirement_endpoints_pass_through_unchanged(merge_rig: MergeRig) -> None:
    """Role (HAS's source) and Requirement (SATISFIED_BY's source) are never in a mapping."""
    _seed_endpoints(merge_rig, "HAS", "role_manufacturer_abc123", "obl_x")
    _seed(merge_rig, "Requirement", "CRA-1.0_req_art_13.1")
    edges = (
        BareEdge("HAS", "role_manufacturer_abc123", "obl_x"),
        BareEdge("SATISFIED_BY", "CRA-1.0_req_art_13.1", "obl_x"),
    )

    persist_rewired_edges(_target(merge_rig), merge_rig.legacy, edges, {"obl_x": "obl_x"})

    assert _written(merge_rig) == {
        ("HAS", "Role", "role_manufacturer_abc123", "Obligation", "obl_x"),
        ("SATISFIED_BY", "Requirement", "CRA-1.0_req_art_13.1", "Obligation", "obl_x"),
    }


def test_rerunning_the_merge_adds_no_parallel_edge(merge_rig: MergeRig) -> None:
    """Idempotent: the same edge set again leaves one edge each and logs nothing new."""
    _seed_endpoints(merge_rig, "HAS", "role_manufacturer_abc123", "obl_canonical_x")
    _seed(merge_rig, "Capability", "cap_encrypt_data")
    edges = (
        BareEdge("HAS", "role_manufacturer_abc123", "obl_x"),
        BareEdge("REQUIRES", "obl_x", "cap_encrypt_data"),
    )
    _seed(merge_rig, "Obligation", "obl_canonical_x")
    mapping = {"obl_x": "obl_canonical_x", "cap_encrypt_data": "cap_encrypt_data"}
    persist_rewired_edges(_target(merge_rig), merge_rig.legacy, edges, mapping)
    logged = len(merge_rig.logged())

    persist_rewired_edges(_target(merge_rig), merge_rig.legacy, edges, mapping)

    assert len(merge_rig.logged()) == logged
    assert len(merge_rig.policy_system.edges) == 2
    assert _written(merge_rig) == {
        ("HAS", "Role", "role_manufacturer_abc123", "Obligation", "obl_canonical_x"),
        ("REQUIRES", "Obligation", "obl_canonical_x", "Capability", "cap_encrypt_data"),
    }


def test_has_edge_obligation_target_absent_from_mapping_passes_through(
    merge_rig: MergeRig,
) -> None:
    """An Obligation is a passthrough node: no mapping entry is required, and an absent
    one is written verbatim, not a `CompanyMergePersistenceError`.
    """
    _seed_endpoints(merge_rig, "HAS", "role_manufacturer_abc123", "obl_x")
    _seed(merge_rig, "Role", "role_importer_def456")
    _seed(merge_rig, "Obligation", "obl_never_mapped")
    edges = (
        BareEdge("HAS", "role_manufacturer_abc123", "obl_x"),
        BareEdge("HAS", "role_importer_def456", "obl_never_mapped"),
    )

    persist_rewired_edges(_target(merge_rig), merge_rig.legacy, edges, {})

    assert _edge_entries(merge_rig) == [
        ("HAS", "HAS:role_manufacturer_abc123->obl_x"),
        ("HAS", "HAS:role_importer_def456->obl_never_mapped"),
    ]


def test_covers_edge_target_is_rewritten_to_canonical_capability_id(merge_rig: MergeRig) -> None:
    """`COVERS` (PracticeArea, Capability) rewrites its Capability."""
    _seed_endpoints(merge_rig, "COVERS", "pa_secure_sdlc_4a7c1d", "cap_canonical_existing")
    edge = BareEdge("COVERS", "pa_secure_sdlc_4a7c1d", "cap_baseline_local")

    persist_rewired_edges(
        _target(merge_rig),
        merge_rig.legacy,
        (edge,),
        {"cap_baseline_local": "cap_canonical_existing"},
    )

    assert _written(merge_rig) == {
        ("COVERS", "PracticeArea", "pa_secure_sdlc_4a7c1d", "Capability", "cap_canonical_existing")
    }


def test_owns_edge_target_is_rewritten_to_canonical_policy_id(merge_rig: MergeRig) -> None:
    """`OWNS` (PracticeArea, Policy) rewrites its Policy target."""
    _seed_endpoints(merge_rig, "OWNS", "pa_secure_sdlc_4a7c1d", "pol_canonical_existing")
    edge = BareEdge("OWNS", "pa_secure_sdlc_4a7c1d", "pol_baseline_local")

    persist_rewired_edges(
        _target(merge_rig),
        merge_rig.legacy,
        (edge,),
        {"pol_baseline_local": "pol_canonical_existing"},
    )

    assert _written(merge_rig) == {
        ("OWNS", "PracticeArea", "pa_secure_sdlc_4a7c1d", "Policy", "pol_canonical_existing")
    }


def test_mitigated_by_edge_target_is_rewritten_to_canonical_capability_id(
    merge_rig: MergeRig,
) -> None:
    """`MITIGATED_BY` (RiskPath, Capability) rewrites its Capability."""
    _seed_endpoints(
        merge_rig, "MITIGATED_BY", "rp_secure_build_release_d93f8a", "cap_canonical_existing"
    )
    edge = BareEdge("MITIGATED_BY", "rp_secure_build_release_d93f8a", "cap_baseline_local")

    persist_rewired_edges(
        _target(merge_rig),
        merge_rig.legacy,
        (edge,),
        {"cap_baseline_local": "cap_canonical_existing"},
    )

    assert _written(merge_rig) == {
        (
            "MITIGATED_BY",
            "RiskPath",
            "rp_secure_build_release_d93f8a",
            "Capability",
            "cap_canonical_existing",
        )
    }


def test_verified_by_edge_control_target_passes_through_unchanged(merge_rig: MergeRig) -> None:
    """`VERIFIED_BY` (RiskPath, Control): neither endpoint is canonically deduped."""
    _seed_endpoints(
        merge_rig, "VERIFIED_BY", "rp_secure_build_release_d93f8a", "ctrl_baseline_local"
    )
    edge = BareEdge("VERIFIED_BY", "rp_secure_build_release_d93f8a", "ctrl_baseline_local")

    persist_rewired_edges(_target(merge_rig), merge_rig.legacy, (edge,), {})

    assert _written(merge_rig) == {
        (
            "VERIFIED_BY",
            "RiskPath",
            "rp_secure_build_release_d93f8a",
            "Control",
            "ctrl_baseline_local",
        )
    }


def test_an_edge_whose_endpoint_exists_in_neither_graph_nor_group_is_dropped_silently(
    merge_rig: MergeRig,
) -> None:
    """The old `MATCH` found no row for a missing endpoint and wrote nothing: no error, no log.

    Also the label counts: an Obligation id held under another label is not an endpoint.
    """
    _seed(merge_rig, "Role", "role_a")
    _seed(merge_rig, "Capability", "obl_wrong_label")
    edges = (
        BareEdge("HAS", "role_a", "obl_missing"),
        BareEdge("HAS", "role_missing", "obl_missing"),
        BareEdge("HAS", "role_a", "obl_wrong_label"),
    )

    persist_rewired_edges(_target(merge_rig), merge_rig.legacy, edges, {})

    assert merge_rig.groups == []
    assert merge_rig.policy_system.edges == {}


def test_a_dangling_edge_among_valid_ones_is_dropped_and_the_rest_are_written(
    merge_rig: MergeRig,
) -> None:
    _seed_endpoints(merge_rig, "HAS", "role_a", "obl_a")
    edges = (BareEdge("HAS", "role_a", "obl_missing"), BareEdge("HAS", "role_a", "obl_a"))

    persist_rewired_edges(_target(merge_rig), merge_rig.legacy, edges, {})

    assert _edge_entries(merge_rig) == [("HAS", "HAS:role_a->obl_a")]


def test_a_duplicate_edge_collapses_to_one_primitive(merge_rig: MergeRig) -> None:
    """A repeated edge is one primitive; so are two edges that rewrite to the same ends."""
    _seed_endpoints(merge_rig, "HAS", "role_a", "obl_canonical")
    edges = (
        BareEdge("HAS", "role_a", "obl_canonical"),
        BareEdge("HAS", "role_a", "obl_canonical"),
        BareEdge("HAS", "role_a", "obl_local"),
    )

    persist_rewired_edges(
        _target(merge_rig), merge_rig.legacy, edges, {"obl_local": "obl_canonical"}
    )

    assert [(e.name, e.identity) for e in merge_rig.groups[0].entries] == [
        ("HAS", "HAS:role_a->obl_canonical")
    ]


def test_endpoints_are_read_once_per_label_with_the_ids_as_parameters(merge_rig: MergeRig) -> None:
    _seed_endpoints(merge_rig, "HAS", "role_a", "obl_a")
    _seed_endpoints(merge_rig, "REQUIRES", "obl_b", "cap_a")
    edges = (
        BareEdge("HAS", "role_a", "obl_a"),
        BareEdge("REQUIRES", "obl_b", "cap_a"),
        BareEdge("HAS", "role_a", "obl_b"),
    )

    persist_rewired_edges(_target(merge_rig), merge_rig.legacy, edges, {"cap_a": "cap_a"})

    reads = sorted(merge_rig.legacy.statements)
    assert reads == [
        "MATCH (n:Capability) WHERE n.id IN $ids RETURN n.id",
        "MATCH (n:Obligation) WHERE n.id IN $ids RETURN n.id",
        "MATCH (n:Role) WHERE n.id IN $ids RETURN n.id",
    ]
    assert sorted(
        (str(statement), params["ids"])
        for statement, params in zip(
            merge_rig.legacy.statements, merge_rig.legacy.params_seen, strict=True
        )
    ) == [
        ("MATCH (n:Capability) WHERE n.id IN $ids RETURN n.id", ["cap_a"]),
        ("MATCH (n:Obligation) WHERE n.id IN $ids RETURN n.id", ["obl_a", "obl_b"]),
        ("MATCH (n:Role) WHERE n.id IN $ids RETURN n.id", ["role_a"]),
    ]


def test_no_edges_reads_and_submits_nothing(merge_rig: MergeRig) -> None:
    persist_rewired_edges(_target(merge_rig), merge_rig.legacy, (), {})

    assert merge_rig.groups == []
    assert merge_rig.legacy.statements == []


def test_a_failing_endpoint_read_is_query_failed_and_submits_nothing(merge_rig: MergeRig) -> None:
    with pytest.raises(CompanyMergePersistenceError, match=r"^FalkorDB query failed: "):
        persist_rewired_edges(
            _target(merge_rig), _DownGraph(), (BareEdge("HAS", "role_a", "obl_a"),), {}
        )

    assert merge_rig.groups == []
    assert is_healthy(FALKORDB) is False


def test_covers_edge_target_missing_from_mapping_raises_before_any_write(
    merge_rig: MergeRig,
) -> None:
    """The flip side: a `COVERS` Capability target with no mapping entry raises, with no
    read made and nothing submitted.
    """
    edge = BareEdge("COVERS", "pa_secure_sdlc_4a7c1d", "cap_unmapped")

    with pytest.raises(CompanyMergePersistenceError, match="cap_unmapped"):
        persist_rewired_edges(_target(merge_rig), merge_rig.legacy, (edge,), {})

    assert merge_rig.groups == []
    assert merge_rig.legacy.statements == []


def test_requires_edge_target_missing_from_mapping_raises_before_any_write(
    merge_rig: MergeRig,
) -> None:
    """An incomplete caller-supplied mapping is a hard pre-write error: absent this check the
    baseline-local Capability id would be written verbatim, reproducing the dangling-edge bug.
    """
    edge = BareEdge("REQUIRES", "obl_baseline_local", "cap_unmapped")

    with pytest.raises(CompanyMergePersistenceError, match="cap_unmapped"):
        persist_rewired_edges(
            _target(merge_rig),
            merge_rig.legacy,
            (edge,),
            {"obl_baseline_local": "obl_canonical_existing"},
        )

    assert merge_rig.groups == []
    assert merge_rig.legacy.statements == []
