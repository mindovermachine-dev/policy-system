"""`merge_baseline_graph` writes the regulatory spine through the Graph Write Gateway.

The RegulatoryInstrument, the create-only Role and Requirement nodes and the
`DEFINES`/`EXPRESSES` provenance edges leave Company Merge as ONE gateway group on
`policy_system`, under the run's audit event. The single-tenant handle Company Merge is given is a
READ handle for this family: it never receives a write statement for these labels.

The gateway rig (in-memory log and graph fakes) is the true boundary; `_SpySubmitter` only records
the order of the calls the gateway receives.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from company_merge._gateway_fakes import (
    POLICY_SYSTEM,
    FamilyABaseline,
    MergeRig,
    family_a_baseline_graph,
)
from company_merge._seed_merge_graphs import (
    EMBED_MODEL,
    MERGE_THRESHOLD,
    BaselineGraphReader,
    OneHotEmbeddingCaller,
)
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from ps_service.company_merge import graph_writer
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.models import BaselineNode, ProvenanceEdge

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from ps_service.company_merge.falkordb_client import GraphQueryResult
    from ps_service.graph_gateway.edge_identity_backfill import EdgeIdentityBackfillOutcome
    from ps_service.graph_gateway.models import GraphLogEntry, GroupOutcome, MutationGroup
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.logging import LogEmitter

_WRITE_KEYWORD = re.compile(r"\b(MERGE|CREATE|SET|DELETE|REMOVE)\b")
_PREFIX = "FalkorDB write failed: "

_CRA = FamilyABaseline(
    instrument_id="CRA-1.0",
    instrument_properties={"title": "Cyber Resilience Act", "celex": "32024R2847"},
    roles={"role_manufacturer": "Manufacturer", "role_importer": "Importer"},
    requirements={"req_a": "Conduct a risk assessment", "req_b": "Report incidents"},
    defines=(("role_manufacturer", "Art. 3(1)"), ("role_importer", "Art. 3(2)")),
    expresses=(("req_a", "Art. 13(1)"), ("req_b", "Art. 14")),
)
_CRA_DRIFTED = FamilyABaseline(
    instrument_id="CRA-1.0",
    instrument_properties={"title": "Cyber Resilience Act (amended)", "celex": "32024R2847"},
    roles={"role_manufacturer": "Manufacturer (renamed)", "role_importer": "Importer"},
    requirements={"req_a": "Conduct a DIFFERENT risk assessment", "req_b": "Report incidents"},
    defines=(("role_manufacturer", "Art. 3(1) as amended"), ("role_importer", "Art. 3(2)")),
    expresses=(("req_a", "Art. 13(1)"), ("req_b", "Art. 14")),
)


class _SpySubmitter:
    """Delegates to the rig's gateway and records which gateway calls were made, in order."""

    def __init__(self, gateway: GroupSubmitter) -> None:
        self._gateway = gateway
        self.calls: list[str] = []

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        self.calls.append("submit")
        return self._gateway.submit_group(group)

    def is_caught_up(self, graph: str) -> bool:
        self.calls.append("caught_up")
        return self._gateway.is_caught_up(graph)

    def stamp_legacy_edge_identity(
        self, graph: str, audit_event_id: str | None
    ) -> EdgeIdentityBackfillOutcome:
        self.calls.append("stamp")
        return self._gateway.stamp_legacy_edge_identity(graph, audit_event_id)


class _DownGraph:
    """A read handle whose FalkorDB is unreachable."""

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        del q, params
        raise redis.exceptions.ConnectionError("Error 111 connecting to 10.0.0.5:6379")


@pytest.fixture
def emitter(make_emitter: MakeEmitter) -> LogEmitter:
    return make_emitter()[0]


def _merge(
    rig: MergeRig, baseline: FamilyABaseline, emitter: LogEmitter, **overrides: object
) -> None:
    keywords: dict[str, object] = {**rig.merge_keywords(), **overrides}
    merge_baseline_graph(
        baseline.instrument_id,
        baseline_graph=BaselineGraphReader(family_a_baseline_graph(baseline)),
        single_tenant_graph=rig.legacy,
        embed_model=EMBED_MODEL,
        merge_threshold=MERGE_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=OneHotEmbeddingCaller(),
        emitter=emitter,
        **keywords,  # pyright: ignore[reportArgumentType] -- the gateway keywords, overridable
    )


def _properties(entry: GraphLogEntry) -> dict[str, object]:
    properties = entry.content["properties"]
    assert isinstance(properties, dict)
    return {str(key): value for key, value in properties.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def test_first_merge_submits_instrument_roles_requirements_and_provenance_edges_as_one_group_under_the_audit_event(  # noqa: E501 -- name fixed by the plan
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    _merge(merge_rig, _CRA, emitter)

    assert len(merge_rig.groups) == 1
    group = merge_rig.groups[0]
    assert group.audit_event_id == merge_rig.audit_event_id
    assert [(entry.name, entry.identity) for entry in group.entries] == [
        ("RegulatoryInstrument", "CRA-1.0"),
        ("Role", "role_manufacturer"),
        ("Role", "role_importer"),
        ("Requirement", "req_a"),
        ("Requirement", "req_b"),
        ("DEFINES", "DEFINES:CRA-1.0->role_manufacturer"),
        ("DEFINES", "DEFINES:CRA-1.0->role_importer"),
        ("EXPRESSES", "EXPRESSES:CRA-1.0->req_a"),
        ("EXPRESSES", "EXPRESSES:CRA-1.0->req_b"),
    ]
    by_identity = {entry.identity: _properties(entry) for entry in group.entries}
    assert by_identity["CRA-1.0"] == {"title": "Cyber Resilience Act", "celex": "32024R2847"}
    assert by_identity["role_manufacturer"]["name"] == "Manufacturer"
    assert by_identity["DEFINES:CRA-1.0->role_manufacturer"] == {"source_ref": "Art. 3(1)"}
    assert by_identity["EXPRESSES:CRA-1.0->req_b"] == {"source_ref": "Art. 14"}
    # the single-tenant handle Company Merge holds received no write for this family
    assert [s for s in merge_rig.legacy.statements if _WRITE_KEYWORD.search(s)] == []


def test_a_role_that_already_exists_keeps_its_properties_and_nothing_is_logged_for_it(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    """Create-only is decided before submission; the group never mentions the node."""
    _merge(merge_rig, _CRA, emitter)

    _merge(merge_rig, _CRA_DRIFTED, emitter)

    second = merge_rig.groups[1]
    assert {entry.name for entry in second.entries} <= {"RegulatoryInstrument", "DEFINES"}
    assert merge_rig.policy_system.nodes[("Role", "role_manufacturer")]["name"] == "Manufacturer"
    assert (
        merge_rig.policy_system.nodes[("Requirement", "req_a")]["text"]
        == "Conduct a risk assessment"
    )
    # the instrument is refreshed (unconditional SET) and so is a changed provenance source_ref
    assert merge_rig.policy_system.nodes[("RegulatoryInstrument", "CRA-1.0")]["title"] == (
        "Cyber Resilience Act (amended)"
    )


def test_an_identical_rerun_logs_nothing(merge_rig: MergeRig, emitter: LogEmitter) -> None:
    _merge(merge_rig, _CRA, emitter)
    logged_before = len(merge_rig.logged())

    _merge(merge_rig, _CRA, emitter)

    assert len(merge_rig.logged()) == logged_before


def test_the_graph_is_stamped_once_before_the_group_and_a_legacy_edge_is_not_duplicated(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    """A `policy_system` edge written before the gateway has no identity: stamp, never duplicate."""
    merge_rig.policy_system.seed_node(("RegulatoryInstrument",), "CRA-1.0", {"title": "old"})
    merge_rig.policy_system.seed_node(("Role",), "role_manufacturer", {"name": "Manufacturer"})
    merge_rig.policy_system.seed_legacy_edge(
        ("DEFINES", "RegulatoryInstrument", "CRA-1.0", "Role", "role_manufacturer"),
        {"source_ref": "Art. 3(1)"},
    )
    spy = _SpySubmitter(merge_rig.gateway)

    _merge(merge_rig, _CRA, emitter, submitter=spy)

    assert spy.calls[:2] == ["stamp", "submit"]
    assert spy.calls.count("stamp") == 1
    defines = [
        key
        for key in merge_rig.policy_system.edges
        if key[0] == "DEFINES" and key[4] == "role_manufacturer"
    ]
    assert len(defines) == 1
    assert defines[0][5] == "DEFINES:CRA-1.0->role_manufacturer"


def test_the_group_is_caught_up_before_the_merge_goes_on(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    spy = _SpySubmitter(merge_rig.gateway)

    _merge(merge_rig, _CRA, emitter, submitter=spy)

    assert spy.calls == ["stamp", "submit", "caught_up"]


def test_a_group_the_log_cannot_take_raises_the_sanitized_persistence_error(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    merge_rig.store.fail_appends()

    with pytest.raises(CompanyMergePersistenceError) as exc_info:
        _merge(merge_rig, _CRA, emitter)

    assert str(exc_info.value).startswith(_PREFIX)
    assert merge_rig.groups == []


def test_a_group_committed_but_not_applied_raises_the_persistence_error(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    merge_rig.policy_system.fail_on_write(
        redis.exceptions.ConnectionError("Error 111 connecting to 10.0.0.5:6379")
    )

    with pytest.raises(CompanyMergePersistenceError) as exc_info:
        _merge(merge_rig, _CRA, emitter)

    message = str(exc_info.value)
    assert message.startswith(_PREFIX)
    assert "10.0.0.5" not in message
    assert merge_rig.policy_system.nodes == {}


def test_a_failing_existing_node_read_raises_query_failed_and_submits_nothing(
    merge_rig: MergeRig,
) -> None:
    target = GraphWriteTarget(merge_rig.gateway, POLICY_SYSTEM, merge_rig.audit_event_id)

    with pytest.raises(CompanyMergePersistenceError, match=r"^FalkorDB query failed: "):
        graph_writer.persist_role_and_requirement_passthrough(
            target,
            _DownGraph(),
            "CRA-1.0",
            {"title": "t"},
            (BaselineNode("role_a", {"name": "A"}),),
            (),
            (ProvenanceEdge("DEFINES", "role_a", "Art. 1"),),
        )

    assert merge_rig.groups == []


def _persist(
    rig: MergeRig,
    *,
    instrument_properties: dict[str, object] | None = None,
    roles: tuple[BaselineNode, ...] = (),
    requirements: tuple[BaselineNode, ...] = (),
    edges: tuple[ProvenanceEdge, ...] = (),
) -> None:
    graph_writer.persist_role_and_requirement_passthrough(
        GraphWriteTarget(rig.gateway, POLICY_SYSTEM, rig.audit_event_id),
        rig.legacy,
        "CRA-1.0",
        {"title": "t"} if instrument_properties is None else instrument_properties,
        roles,
        requirements,
        edges,
    )


# --- slice 2c: create-only, first-wins, dangling-drop and unstorable-property hardening ---


def test_a_repeated_role_or_requirement_id_in_the_input_keeps_the_first_occurrence(
    merge_rig: MergeRig,
) -> None:
    """`ON CREATE SET` semantics: the first occurrence creates the node, later ones change nothing.

    The opposite of the Domain Mapper's last-wins fold (its writer used an unconditional `SET`).
    """
    _persist(
        merge_rig,
        roles=(
            BaselineNode("role_a", {"name": "first"}),
            BaselineNode("role_a", {"name": "second"}),
        ),
        requirements=(
            BaselineNode("req_a", {"text": "first"}),
            BaselineNode("req_a", {"text": "second"}),
        ),
    )

    assert merge_rig.policy_system.nodes[("Role", "role_a")] == {"name": "first"}
    assert merge_rig.policy_system.nodes[("Requirement", "req_a")] == {"text": "first"}
    (group,) = merge_rig.groups
    assert [entry.name for entry in group.entries].count("Role") == 1
    assert [entry.name for entry in group.entries].count("Requirement") == 1


def test_a_repeated_provenance_edge_is_one_primitive_and_the_last_source_ref_wins(
    merge_rig: MergeRig,
) -> None:
    """`MERGE (r)-[e]->(n) SET e.source_ref` twice is one edge holding the later `source_ref`."""
    _persist(
        merge_rig,
        roles=(BaselineNode("role_a", {"name": "A"}),),
        edges=(
            ProvenanceEdge("DEFINES", "role_a", "Art. 1"),
            ProvenanceEdge("DEFINES", "role_a", "Art. 2"),
        ),
    )

    (group,) = merge_rig.groups
    defines = [entry for entry in group.entries if entry.name == "DEFINES"]
    assert [_properties(entry) for entry in defines] == [{"source_ref": "Art. 2"}]


def test_a_provenance_edge_whose_target_exists_nowhere_is_dropped_silently(
    merge_rig: MergeRig,
) -> None:
    """The old `MATCH ... MERGE` wrote nothing when an endpoint was missing; so does the group."""
    _persist(
        merge_rig,
        roles=(BaselineNode("role_a", {"name": "A"}),),
        requirements=(BaselineNode("req_a", {"text": "t"}),),
        edges=(
            ProvenanceEdge("DEFINES", "role_a", "Art. 1"),
            ProvenanceEdge("DEFINES", "role_missing", "Art. 2"),
            ProvenanceEdge("EXPRESSES", "req_a", "Art. 3"),
            ProvenanceEdge("EXPRESSES", "req_missing", "Art. 4"),
        ),
    )

    (group,) = merge_rig.groups
    assert [
        entry.identity for entry in group.entries if entry.name in {"DEFINES", "EXPRESSES"}
    ] == [
        "DEFINES:CRA-1.0->role_a",
        "EXPRESSES:CRA-1.0->req_a",
    ]


def test_a_provenance_edge_to_a_node_an_earlier_run_created_is_kept(merge_rig: MergeRig) -> None:
    """Characterisation: an endpoint outside the group is found by the existing-node read."""
    _persist(merge_rig, roles=(BaselineNode("role_a", {"name": "A"}),))

    _persist(merge_rig, edges=(ProvenanceEdge("DEFINES", "role_a", "Art. 1"),))

    assert (
        "DEFINES",
        "RegulatoryInstrument",
        "CRA-1.0",
        "Role",
        "role_a",
        "DEFINES:CRA-1.0->role_a",
    ) in merge_rig.policy_system.edges


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"instrument_properties": {"title": float("nan")}}, id="instrument-nan"),
        pytest.param({"roles": (BaselineNode("role_a", {"embedding": "x"}),)}, id="role-reserved"),
        pytest.param(
            {"requirements": (BaselineNode("req_a", {"text": float("inf")}),)},
            id="requirement-infinite",
        ),
    ],
)
def test_a_property_the_graph_cannot_store_raises_the_persistence_error_before_any_submit(
    merge_rig: MergeRig, bad: dict[str, object]
) -> None:
    with pytest.raises(CompanyMergePersistenceError, match="cannot store"):
        _persist(merge_rig, **bad)  # pyright: ignore[reportArgumentType] -- one bad collection

    assert merge_rig.groups == []
    assert merge_rig.policy_system.nodes == {}
