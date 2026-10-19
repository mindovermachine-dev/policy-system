"""Every Company Merge stage writer maps a gateway failure the same way.

Per writer: a group the gateway rejects, a group committed to the log but not applied
(`committed_apply_pending`) and a log that cannot be reached all surface as
`CompanyMergePersistenceError("FalkorDB write failed: ...")`, with no gateway internals in the
message, and FalkorDB connectivity health is recorded as the gateway records it (transient
exhaustion marks FALKORDB unhealthy; a rejection or an unreachable log does not).

The submitter seam is the gateway rig (in-memory log and FalkorDB fakes, the true boundaries). A
rejection cannot be provoked through the real gateway for every writer (their labels and edges are
fixed and valid), so it is injected at the `GroupSubmitter` boundary with a hand-written wrapper.
The resolve decisions (both) are matrixed in 11b.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from company_merge._gateway_fakes import (
    BASELINE,
    POLICY_SYSTEM,
    MergeRig,
    RejectingSubmitter,
    stepped_values,
)
from company_merge._seed_merge_graphs import SingleTenantStore
from ps_service.company_merge import graph_writer
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.models import (
    BareEdge,
    BaselineNode,
    CanonicalResolution,
    NearMissPair,
)
from ps_service.company_merge.pending_review import persist_pending_reviews
from ps_service.company_merge.reviewable_kinds import CAPABILITY
from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.graph_gateway.errors import (
    GraphLogUnavailableError,
    MissingTargetError,
    StaleGraphStateError,
    UnlistedNameError,
)

if TYPE_CHECKING:
    from collections.abc import Callable


_PREFIX = "FalkorDB write failed: "
_HOST = "Error 111 connecting to 10.0.0.5:6379"
_LEAKS = (
    "10.0.0.5",
    "6379",
    "committed_apply_pending",
    "GraphApply",
    "GraphLog",
    "StaleGraphState",
    "MissingTarget",
    "UnlistedName",
    "sequence",
    "sha256",
    "Traceback",
)

type _Writer = Callable[[MergeRig, GraphWriteTarget], None]


def _role(node_id: str = "role_a") -> BaselineNode:
    return BaselineNode(id=node_id, properties={"name": "A", "confidence": 0.9})


def _capability(node_id: str = "cap_a") -> BaselineNode:
    return BaselineNode(id=node_id, properties={"text": "Do it.", "confidence": 0.9})


def _role_and_requirement(rig: MergeRig, target: GraphWriteTarget) -> None:
    graph_writer.persist_role_and_requirement_passthrough(
        target, rig.legacy, "CRA-1.0", {"title": "CRA"}, (_role(),), (), ()
    )


def _obligation(rig: MergeRig, target: GraphWriteTarget) -> None:
    graph_writer.persist_obligation_passthrough(
        target, rig.legacy, (BaselineNode(id="obl_a", properties={"text": "t"}),)
    )


def _governance(rig: MergeRig, target: GraphWriteTarget) -> None:
    graph_writer.persist_governance_drafts(
        target,
        rig.legacy,
        (BaselineNode(id="pol_a", properties={"title": "P", "status": "draft", "version": "1"}),),
        (),
        (),
        owner=("alice@example.com", "https://idp.example/"),
        policy_embeddings=None,
        values=stepped_values(),
    )


def _classification(rig: MergeRig, target: GraphWriteTarget) -> None:
    graph_writer.persist_practice_area_and_risk_path_passthrough(
        target, rig.legacy, (BaselineNode(id="pa_a", properties={"name": "Privacy"}),), ()
    )


def _canonical(rig: MergeRig, target: GraphWriteTarget) -> None:
    node = _capability()
    graph_writer.persist_canonical_nodes(
        target,
        rig.legacy,
        (node,),
        (
            CanonicalResolution(
                incoming_id=node.id, canonical_id=node.id, match_kind="new", embedding=(0.1,)
            ),
        ),
        kind=CAPABILITY,
        values=stepped_values(),
    )


def _canonical_backfill(rig: MergeRig, target: GraphWriteTarget) -> None:
    rig.policy_system.seed_node(("Capability",), "cap_a", {"name": "a"})
    graph_writer.backfill_canonical_embeddings(
        target, rig.legacy, kind=CAPABILITY, embeddings={"cap_a": (0.1, 0.2)}
    )


def _incoming_backfill(rig: MergeRig, target: GraphWriteTarget) -> None:
    baseline = rig.graphs.open(BASELINE)
    baseline.seed_node(("Capability",), "cap_a", {"name": "a"})
    graph_writer.backfill_incoming_capability_embeddings(
        target, SingleTenantStore(nodes=baseline.nodes), embeddings={"cap_a": (0.1, 0.2)}
    )


def _rewired_edges(rig: MergeRig, target: GraphWriteTarget) -> None:
    rig.policy_system.seed_node(("Obligation",), "obl_a", {"text": "t"})
    rig.policy_system.seed_node(("Capability",), "cap_a", {"name": "a"})
    graph_writer.persist_rewired_edges(
        target, rig.legacy, (BareEdge("REQUIRES", "obl_a", "cap_a"),), {"cap_a": "cap_a"}
    )


def _pending_reviews(rig: MergeRig, target: GraphWriteTarget) -> None:
    del rig
    persist_pending_reviews(
        target,
        (
            NearMissPair(
                incoming_id="cap_in",
                incoming_text="in",
                nearest_existing_id="cap_ex",
                nearest_existing_text="ex",
                similarity=0.7,
            ),
        ),
        kind=CAPABILITY,
        values=stepped_values(),
    )


_WRITERS: list[tuple[str, _Writer, str]] = [
    ("role_and_requirement", _role_and_requirement, POLICY_SYSTEM),
    ("obligation", _obligation, POLICY_SYSTEM),
    ("governance_drafts", _governance, POLICY_SYSTEM),
    ("classification", _classification, POLICY_SYSTEM),
    ("canonical_nodes", _canonical, POLICY_SYSTEM),
    ("canonical_embedding_backfill", _canonical_backfill, POLICY_SYSTEM),
    ("incoming_embedding_backfill", _incoming_backfill, BASELINE),
    ("rewired_edges", _rewired_edges, POLICY_SYSTEM),
    ("pending_reviews", _pending_reviews, POLICY_SYSTEM),
]
_WRITER_PARAMS = [pytest.param(w, g, id=name) for name, w, g in _WRITERS]


def _assert_clean(error: CompanyMergePersistenceError) -> None:
    message = str(error)
    assert message.startswith(_PREFIX)
    for leak in _LEAKS:
        assert leak not in message, f"{leak!r} leaked into {message!r}"


@pytest.mark.parametrize(
    "refusal",
    [
        UnlistedNameError("primitive 0 names a label outside the allow-list"),
        MissingTargetError("primitive 1 names an endpoint that does not exist"),
        StaleGraphStateError("policy_system", 41, 43),
    ],
    ids=["unlisted-name", "missing-target", "stale-state"],
)
@pytest.mark.parametrize(("write", "graph"), _WRITER_PARAMS)
def test_a_rejected_group_is_a_persistence_error_for_every_writer(
    write: _Writer, graph: str, refusal: Exception, merge_rig: MergeRig
) -> None:
    target = GraphWriteTarget(
        RejectingSubmitter(merge_rig, refusal), graph, merge_rig.audit_event_id
    )

    with pytest.raises(CompanyMergePersistenceError) as exc_info:
        write(merge_rig, target)

    _assert_clean(exc_info.value)
    assert exc_info.value.__cause__ is refusal
    assert merge_rig.store.groups == []
    assert is_healthy(FALKORDB) is True  # a refusal is not a connectivity problem


@pytest.mark.parametrize(("write", "graph"), _WRITER_PARAMS)
def test_a_group_committed_but_not_applied_is_a_persistence_error_for_every_writer(
    write: _Writer, graph: str, merge_rig: MergeRig
) -> None:
    """FalkorDB is down when the gateway applies: the group is in the log (the gateway applies it
    on recovery) but the writer must not go on, and FALKORDB health is recorded as unhealthy.
    """
    merge_rig.graphs.open(graph).fail_on_write(redis.exceptions.ConnectionError(_HOST))
    target = GraphWriteTarget(merge_rig.gateway, graph, merge_rig.audit_event_id)

    with pytest.raises(CompanyMergePersistenceError) as exc_info:
        write(merge_rig, target)

    _assert_clean(exc_info.value)
    assert any(group.graph == graph for group in merge_rig.store.groups)
    assert is_healthy(FALKORDB) is False


@pytest.mark.parametrize(("write", "graph"), _WRITER_PARAMS)
def test_an_unreachable_log_is_a_persistence_error_for_every_writer_and_logs_nothing(
    write: _Writer, graph: str, merge_rig: MergeRig
) -> None:
    merge_rig.store.fail_appends(error=GraphLogUnavailableError())
    target = GraphWriteTarget(merge_rig.gateway, graph, merge_rig.audit_event_id)

    with pytest.raises(CompanyMergePersistenceError) as exc_info:
        write(merge_rig, target)

    _assert_clean(exc_info.value)
    assert isinstance(exc_info.value.__cause__, GraphLogUnavailableError)
    assert merge_rig.store.groups == []
