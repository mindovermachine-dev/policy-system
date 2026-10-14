"""The ingestion-side seam to the Graph Write Gateway.

`submit_or_raise` is the one place gateway outcomes and errors become the error type of the
ingestion module that submitted the group, so callers keep their existing failure contract
(`"FalkorDB write failed: ..."`) and the gateway's sanitized text is all they ever see.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from ps_service.graph_gateway import (
    GraphApplyBlockedError,
    GraphLogUnavailableError,
    GraphReplayGatedError,
    GraphUnavailableError,
    MissingTargetError,
    MutationGroup,
    NodeRef,
    UpsertEdge,
    UpsertNode,
)
from ps_service.ingestion.graph_submission import (
    GroupSubmitter,
    stamp_legacy_edges_before_first_write,
    submit_or_raise,
)

if TYPE_CHECKING:
    from graph_gateway._fakes import GatewayRig

    from ps_service.graph_gateway import GroupOutcome


class _SubmissionError(Exception):
    """Stands in for the calling module's error type (`IngestionPersistenceError`, ...)."""


def _group(graph: str = "cra_native", node_id: str = "CRA-1.0") -> MutationGroup:
    return MutationGroup(
        graph=graph,
        audit_event_id=str(uuid.uuid4()),
        primitives=(UpsertNode(label="RegulatoryInstrument", id=node_id, properties={"k": "v"}),),
    )


def test_gateway_satisfies_group_submitter_structurally(rig: GatewayRig) -> None:
    submitter: GroupSubmitter = rig.gateway

    assert isinstance(submitter, GroupSubmitter)


def test_submit_or_raise_returns_the_outcome_of_an_applied_group(rig: GatewayRig) -> None:
    outcome: GroupOutcome = submit_or_raise(rig.gateway, _group(), _SubmissionError)

    assert outcome.status == "applied"
    assert rig.store.logged_count("cra_native") == 1


def test_submit_or_raise_treats_an_unchanged_group_as_success(rig: GatewayRig) -> None:
    group = _group()
    submit_or_raise(rig.gateway, group, _SubmissionError)

    outcome = submit_or_raise(rig.gateway, group, _SubmissionError)

    assert outcome.status == "unchanged"
    assert rig.store.logged_count("cra_native") == 1


def test_submit_or_raise_raises_when_the_group_is_committed_but_not_applied(
    rig: GatewayRig,
) -> None:
    rig.graphs.open("cra_native").fail_on_write(redis.exceptions.ConnectionError("down"))

    with pytest.raises(_SubmissionError, match=r"^FalkorDB write failed: .*not.*applied"):
        submit_or_raise(rig.gateway, _group(), _SubmissionError)

    assert rig.store.logged_count("cra_native") == 1


@pytest.mark.parametrize(
    "gateway_error",
    [
        GraphLogUnavailableError(),
        GraphUnavailableError(),
        GraphApplyBlockedError("cra_native"),
        GraphReplayGatedError("cra_native"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_submit_or_raise_maps_gateway_failures_to_the_callers_error_type(
    rig: GatewayRig, gateway_error: Exception
) -> None:
    rig.store.fail_appends(error=gateway_error)

    with pytest.raises(_SubmissionError) as raised:
        submit_or_raise(rig.gateway, _group(), _SubmissionError)

    assert str(raised.value) == f"FalkorDB write failed: {gateway_error}"
    assert raised.value.__cause__ is gateway_error


def test_submit_or_raise_maps_a_rejected_group_to_the_callers_error_type(
    rig: GatewayRig,
) -> None:
    dangling = MutationGroup(
        graph="cra_native",
        audit_event_id=str(uuid.uuid4()),
        primitives=(
            UpsertEdge(
                type="HAS",
                source=NodeRef(label="CHAPTER", id="missing-parent"),
                target=NodeRef(label="ARTICLE", id="missing-child"),
                identity="HAS:missing-parent->missing-child",
            ),
        ),
    )

    with pytest.raises(_SubmissionError, match=r"^FalkorDB write failed: ") as raised:
        submit_or_raise(rig.gateway, dangling, _SubmissionError)

    assert isinstance(raised.value.__cause__, MissingTargetError)
    assert rig.store.logged_count("cra_native") == 0


def test_submit_or_raise_lets_an_unknown_error_propagate(rig: GatewayRig) -> None:
    rig.store.fail_appends(error=ZeroDivisionError("not a gateway error"))

    with pytest.raises(ZeroDivisionError):
        submit_or_raise(rig.gateway, _group(), _SubmissionError)


def test_stamp_legacy_edges_stamps_a_legacy_graph_and_logs_it_under_the_submits_audit_id(
    rig: GatewayRig,
) -> None:
    audit_event_id = str(uuid.uuid4())
    rig.store.audit_rows.add(audit_event_id)
    graph = rig.graphs.open("cra_native")
    graph.seed_node(("CHAPTER",), "c-1", {})
    graph.seed_node(("ARTICLE",), "a-1", {})
    graph.seed_legacy_edge(("HAS", "CHAPTER", "c-1", "ARTICLE", "a-1"), {})

    stamp_legacy_edges_before_first_write(
        rig.gateway, "cra_native", audit_event_id, _SubmissionError
    )

    assert [(g.graph, g.audit_event_id) for g in rig.store.groups] == [
        ("cra_native", audit_event_id)
    ]
    ((*_, identity),) = graph.edges
    assert identity == "HAS:c-1->a-1"


def test_stamp_legacy_edges_on_a_graph_with_nothing_to_stamp_logs_nothing(
    rig: GatewayRig,
) -> None:
    stamp_legacy_edges_before_first_write(
        rig.gateway, "cra_native", str(uuid.uuid4()), _SubmissionError
    )

    assert rig.store.groups == []


def test_stamp_legacy_edges_maps_an_unreadable_graph_to_the_callers_error_type(
    rig: GatewayRig,
) -> None:
    rig.graphs.open("cra_native").fail_on_read(redis.exceptions.ConnectionError("down"))

    with pytest.raises(_SubmissionError, match=r"^FalkorDB write failed: ") as raised:
        stamp_legacy_edges_before_first_write(
            rig.gateway, "cra_native", str(uuid.uuid4()), _SubmissionError
        )

    assert isinstance(raised.value.__cause__, GraphUnavailableError)
    assert rig.store.groups == []
