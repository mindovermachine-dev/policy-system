"""An external ingest, Domain Mapper and Company Merge run leaves head checkpoints.

The real writers of the three stages run, one after the other, on one gateway rig: Ingestion's
`{short}_native` group, the Domain Mapper's two `{short}_baseline` groups and Company Merge's
`policy_system` and baseline groups. Whatever order they ran in, every graph the run touched ends
with a checkpoint at its head equal to the live canonical digest.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
from domain_mapper._gateway_fakes import BaselineReadHandle
from graph_gateway.checkpoint_checks import assert_head_checkpoints
from ingestion.test_pipeline_gateway import _Adapter  # pyright: ignore[reportPrivateUsage]

from company_merge._gateway_fakes import POLICY_SYSTEM, MergeRig
from company_merge._seed_merge_graphs import EMBED_MODEL, SIMILARITY_THRESHOLD, BaselineGraphReader
from ps_service.company_merge.merge import merge_baseline_graph
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
from ps_service.ingestion.models import (
    FetchedRegulatoryInstrumentStructure,
    RegulatoryInstrumentMetadata,
    StructuralEdge,
    StructuralNode,
)
from ps_service.ingestion.pipeline import ingest_regulatory_instrument

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from ps_service.logging import LogEmitter

_NATIVE = "cra_native"
_BASELINE = "cra_baseline"
_ROLE = "role_manufacturer"
_REQUIREMENT = "CRA-1.0_req_1"
_OBLIGATION = "obl_notify"
_CAPABILITY = "cap_report"


@pytest.fixture
def emitter(make_emitter: MakeEmitter) -> LogEmitter:
    return make_emitter()[0]


class _Rows:
    def __init__(self) -> None:
        self.result_set: list[object] = [[0]]


class _CountReader:
    """The native read handle of the ingest: every count is zero."""

    def query(self, q: str, params: dict[str, object] | None = None) -> _Rows:
        del q, params
        return _Rows()


def _structure() -> FetchedRegulatoryInstrumentStructure:
    metadata = RegulatoryInstrumentMetadata(
        title="Cyber Resilience Act",
        jurisdiction="EU",
        effective_date=date(2027, 12, 11),
        version="1.0",
        status="active",
        source_type="external",
        instrument_type="regulation",
        celex="32024R2847",
    )
    props: dict[str, str | int] = {"text": "t", "citation_ref": "Art. 1", "order": 1}
    return FetchedRegulatoryInstrumentStructure(
        metadata=metadata,
        nodes=(StructuralNode("ARTICLE", "32024R2847#art_1", props),),
        edges=(
            StructuralEdge("RegulatoryInstrument", "32024R2847", "ARTICLE", "32024R2847#art_1"),
        ),
    )


def _extract_and_derive(rig: MergeRig) -> None:
    submitter, audit = rig.gateway, rig.audit_event_id
    persist_role_and_requirement_graph(
        submitter,
        _BASELINE,
        audit,
        regulatory_instrument_id="CRA-1.0",
        regulatory_instrument_properties={"title": "Cyber Resilience Act"},
        role_nodes=(RoleNode(id=_ROLE, properties={"name": "Manufacturer", "confidence": 0.9}),),
        role_edges=(RoleDefinesEdge(role_node_id=_ROLE, source_ref="Art. 1"),),
        requirement_nodes=(
            RequirementNode(
                id=_REQUIREMENT,
                properties={
                    "text": "t",
                    "type": "requirement",
                    "confidence": 0.9,
                    "role_id": _ROLE,
                },
            ),
        ),
        requirement_edges=(
            RequirementExpressesEdge(requirement_node_id=_REQUIREMENT, source_ref="Art. 1"),
        ),
    )
    persist_obligation_and_capability_graph(
        submitter,
        _BASELINE,
        audit,
        graph=BaselineReadHandle(rig.graphs.open(_BASELINE)),
        obligation_nodes=(
            ObligationNode(id=_OBLIGATION, properties={"text": "Notify.", "confidence": 0.8}),
        ),
        has_edges=(ObligationHasEdge(role_node_id=_ROLE, obligation_node_id=_OBLIGATION),),
        satisfied_by_edges=(
            RequirementSatisfiedByEdge(requirement_id=_REQUIREMENT, obligation_node_id=_OBLIGATION),
        ),
        capability_nodes=(
            CapabilityNode(
                id=_CAPABILITY, properties={"name": "Report incidents", "confidence": 0.8}
            ),
        ),
        requires_edges=(
            CapabilityRequiresEdge(obligation_node_id=_OBLIGATION, capability_node_id=_CAPABILITY),
        ),
    )


def test_a_full_external_run_leaves_a_head_checkpoint_on_every_graph_it_touched(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    merge_rig.store.audit_rows.add(merge_rig.audit_event_id)
    ingest_regulatory_instrument(
        "32024R2847",
        "CRA",
        version="1.0",
        adapter=_Adapter(_structure()),
        graph=_CountReader(),
        submitter=merge_rig.gateway,
        audit_event_id=merge_rig.audit_event_id,
        emitter=emitter,
    )
    _extract_and_derive(merge_rig)
    merge_baseline_graph(
        "CRA-1.0",
        baseline_graph=BaselineGraphReader(merge_rig.graphs.open(_BASELINE)),
        single_tenant_graph=merge_rig.legacy,
        embed_model=EMBED_MODEL,
        similarity_threshold=SIMILARITY_THRESHOLD,
        emitter=emitter,
        **merge_rig.merge_keywords(),
    )

    assert set(merge_rig.store.logged_graphs()) == {_NATIVE, _BASELINE, POLICY_SYSTEM}
    assert_head_checkpoints(merge_rig)
