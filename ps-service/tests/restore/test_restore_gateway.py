"""A restore is ONE multi-graph group through the Graph Write Gateway.

The real `restore_instrument` runs against the gateway rig (`restore._gateway_fakes.RestoreRig`:
the in-memory log and FalkorDB boundaries behind the real gateway). Covered: one
transaction, one group per leg, shared audit event; a failing restore logs nothing
and leaves the graphs as they were; verification before any gateway call; drafts owned by the
caller, never converged; restore over an existing copy; and the gateway half of the audit
trail (the group carries the restore's event id; the failure outcomes the
audit row records are asserted in `tests/api/test_restore_orchestration_audit.py`).

The restore is applied by swap (`test_restore_swap.py` covers the swap itself).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from graph_gateway._fakes import Fault

from ps_service.api.errors import GraphWriteUnavailableError
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export.models import SerializedGraph, SerializedNode
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import GraphWriteRejectedError
from ps_service.restore.errors import (
    ArtifactContentRejectedError,
    ArtifactIntegrityError,
    RestoreApplyPendingError,
    RestoreGraphWriteFailedError,
    RestoreGroupRejectedError,
    RestoreOwnerRequiredError,
)
from restore._curated import load_curated
from restore._fixtures import build_restore_artifact, small_graphs
from restore._gateway_fakes import OWNER, RestoreRig

if TYPE_CHECKING:
    from collections.abc import Callable

    from graph_gateway._fakes import InMemoryGraph

    from ps_service.graph_gateway.gateway import GraphWriteGateway
    from ps_service.restore.models import RestoreArtifact

_INSTRUMENT_ID = "GW214-1.0"
_SHORT = "gw214"
_NATIVE, _BASELINE, _POLICY_SYSTEM = f"{_SHORT}_native", f"{_SHORT}_baseline", "policy_system"


def _artifact(
    *, instrument_id: str = _INSTRUMENT_ID, capability_name: str = "Keep Logs"
) -> RestoreArtifact:
    native, baseline = small_graphs(instrument_id=instrument_id, capability_name=capability_name)
    return build_restore_artifact(
        instrument_id=instrument_id,
        short_name="GW214",
        native_graph=native,
        baseline_graph=baseline,
    )


def _labels(graph: InMemoryGraph) -> set[tuple[str, str]]:
    return set(graph.nodes)


def test_a_restore_writes_the_instrument_to_all_three_graphs(restore_rig: RestoreRig) -> None:
    outcome = restore_rig.restore(_artifact())

    assert outcome.instrument_id == _INSTRUMENT_ID
    assert outcome.stages == ("verified", "staged", "merged_and_finalized")
    assert ("ARTICLE", "art_1") in _labels(restore_rig.graph(_NATIVE))
    assert ("Capability", "cap_1") in _labels(restore_rig.graph(_BASELINE))
    policy_system = _labels(restore_rig.graph(_POLICY_SYSTEM))
    assert {
        ("RegulatoryInstrument", _INSTRUMENT_ID),
        ("Role", "role_1"),
        ("Requirement", "req_1"),
        ("Obligation", "obl_1"),
        ("Capability", "cap_1"),
    } <= policy_system


def test_the_three_legs_commit_in_one_transaction_under_the_restores_audit_event(
    restore_rig: RestoreRig,
) -> None:
    restore_rig.restore(_artifact())

    assert restore_rig.store.multi_appends == [(_BASELINE, _NATIVE, _POLICY_SYSTEM)]
    groups = restore_rig.store.read_groups_by_audit_event(restore_rig.audit_event_id)
    assert {g.graph for g in groups} == {_NATIVE, _BASELINE, _POLICY_SYSTEM}
    assert len(restore_rig.store.groups) == 3  # nothing else was logged under any other event


def test_the_gateway_is_opened_once_after_verification(restore_rig: RestoreRig) -> None:
    restore_rig.restore(_artifact())

    assert restore_rig.open_gateway_calls == 1
    assert restore_rig.db.selected == [_POLICY_SYSTEM]  # the restore reads only policy_system


def test_a_restore_that_changes_nothing_logs_nothing(restore_rig: RestoreRig) -> None:
    restore_rig.restore(_artifact())
    logged = len(restore_rig.store.groups)

    restore_rig.restore(_artifact())

    assert len(restore_rig.store.groups) == logged


def test_restoring_over_an_existing_copy_leaves_exactly_the_artifacts_content(
    restore_rig: RestoreRig,
) -> None:
    """Nothing from the previous copy survives in the native and baseline graphs."""
    restore_rig.restore(_artifact(capability_name="Keep Logs"))
    changed = _artifact(capability_name="Keep Logs Safely")
    fresh = RestoreRig()
    try:
        fresh.restore(changed)

        restore_rig.restore(changed)

        for name in (_NATIVE, _BASELINE):
            assert canonical_digest(restore_rig.graph(name)) == canonical_digest(fresh.graph(name))
        baseline = restore_rig.graph(_BASELINE)
        assert baseline.nodes[("Capability", "cap_1")]["name"] == "Keep Logs Safely"
    finally:
        fresh.close()


def test_a_node_the_new_copy_lacks_is_gone_from_the_native_and_baseline_graphs(
    restore_rig: RestoreRig,
) -> None:
    restore_rig.restore(_artifact())
    native, baseline = small_graphs()
    trimmed_native = SerializedGraph(nodes=native.nodes[:1], edges=())
    trimmed_baseline = SerializedGraph(nodes=baseline.nodes[:1], edges=())
    smaller = build_restore_artifact(
        instrument_id=_INSTRUMENT_ID,
        short_name="GW214",
        native_graph=trimmed_native,
        baseline_graph=trimmed_baseline,
    )

    restore_rig.restore(smaller)

    assert _labels(restore_rig.graph(_NATIVE)) == {("RegulatoryInstrument", _INSTRUMENT_ID)}
    assert _labels(restore_rig.graph(_BASELINE)) == {("RegulatoryInstrument", _INSTRUMENT_ID)}


def test_a_restore_is_merged_into_policy_system_not_replaced(restore_rig: RestoreRig) -> None:
    """Only the native and baseline legs replace; `policy_system` keeps what other work wrote."""
    restore_rig.restore(_artifact())
    other_native, _unused = small_graphs(instrument_id="OTHER-1.0", capability_name="Other")
    other = build_restore_artifact(
        instrument_id="OTHER-1.0",
        short_name="OTHER",
        native_graph=other_native,
        baseline_graph=SerializedGraph(
            nodes=(
                SerializedNode("RegulatoryInstrument", {"id": "OTHER-1.0", "title": "O"}),
                SerializedNode(
                    "Capability", {"id": "cap_other", "name": "Other", "confidence": 0.8}
                ),
            ),
            edges=(),
        ),
    )

    restore_rig.restore(other)

    policy_system = _labels(restore_rig.graph(_POLICY_SYSTEM))
    assert ("Role", "role_1") in policy_system  # the first instrument is still there
    assert ("RegulatoryInstrument", "OTHER-1.0") in policy_system


# -- failure at every step leaves the log empty and the graphs unchanged ---------------------


def _nothing_logged_nothing_changed(rig: RestoreRig, before: dict[str, str]) -> None:
    assert rig.store.groups == []
    assert rig.store.multi_appends == []
    for name, digest in before.items():
        assert canonical_digest(rig.graph(name)) == digest


def _digests(rig: RestoreRig) -> dict[str, str]:
    return {
        name: canonical_digest(rig.graph(name)) for name in (_NATIVE, _BASELINE, _POLICY_SYSTEM)
    }


def test_a_failing_build_logs_nothing_and_leaves_every_graph_unchanged(
    restore_rig: RestoreRig,
) -> None:
    before = _digests(restore_rig)
    native, baseline = small_graphs()
    mismatched = build_restore_artifact(
        instrument_id="NOT-THE-INSTRUMENT-1.0",
        short_name="GW214",
        native_graph=native,
        baseline_graph=baseline,
    )

    with pytest.raises(ArtifactContentRejectedError, match="RegulatoryInstrument"):
        restore_rig.restore(mismatched)

    _nothing_logged_nothing_changed(restore_rig, before)


def test_a_content_violation_logs_nothing_and_never_opens_the_gateway(
    restore_rig: RestoreRig,
) -> None:
    artifact = build_restore_artifact(
        instrument_id=_INSTRUMENT_ID,
        short_name="GW214",
        native_graph=SerializedGraph(nodes=(), edges=()),
        baseline_graph=SerializedGraph(
            nodes=(SerializedNode("EvilLabel", {"id": "evil"}),), edges=()
        ),
    )

    with pytest.raises(ArtifactContentRejectedError):
        restore_rig.restore(artifact)

    assert restore_rig.open_gateway_calls == 0
    assert restore_rig.store.groups == []


def test_governance_content_without_an_owner_is_refused_before_any_gateway_call(
    restore_rig: RestoreRig,
) -> None:
    native, baseline = small_graphs()
    with_policy = SerializedGraph(
        nodes=(*baseline.nodes, SerializedNode("Policy", {"id": "pol_1", "title": "P"})),
        edges=baseline.edges,
    )
    artifact = build_restore_artifact(
        instrument_id=_INSTRUMENT_ID,
        short_name="GW214",
        native_graph=native,
        baseline_graph=with_policy,
    )

    with pytest.raises(RestoreOwnerRequiredError):
        restore_rig.restore(artifact, owner=None)

    assert restore_rig.open_gateway_calls == 0
    assert restore_rig.store.groups == []


def test_a_bad_checksum_fails_before_any_gateway_or_graph_call(restore_rig: RestoreRig) -> None:
    artifact = _artifact()
    corrupted = type(artifact)(
        manifest=artifact.manifest,
        baseline_blob=artifact.baseline_blob + b" ",
        native_blob=artifact.native_blob,
    )

    with pytest.raises(ArtifactIntegrityError):
        restore_rig.restore(corrupted)

    assert restore_rig.open_gateway_calls == 0
    assert restore_rig.db.selected == []
    assert restore_rig.store.groups == []


def test_a_schema_version_mismatch_fails_before_any_gateway_or_graph_call(
    restore_rig: RestoreRig,
) -> None:
    native, baseline = small_graphs()
    artifact = build_restore_artifact(
        instrument_id=_INSTRUMENT_ID,
        short_name="GW214",
        native_graph=native,
        baseline_graph=baseline,
        schema_version=DOMAIN_SCHEMA_VERSION + "-other",
    )

    with pytest.raises(Exception, match="schema_version"):
        restore_rig.restore(artifact)

    assert restore_rig.open_gateway_calls == 0
    assert restore_rig.store.groups == []


def test_no_gateway_installed_fails_closed_after_verification_with_nothing_written(
    restore_rig: RestoreRig,
) -> None:
    def no_gateway() -> object:
        raise GraphWriteUnavailableError("graph writes are unavailable")

    restore_rig.gateway_opener = no_gateway
    with pytest.raises(GraphWriteUnavailableError):
        restore_rig.restore(_artifact())

    assert restore_rig.store.groups == []


def test_a_gateway_refusal_is_a_group_rejection_and_logs_nothing(restore_rig: RestoreRig) -> None:
    class RefusingGateway:
        """The rig's gateway, except every multi-graph group is refused."""

        def __init__(self, inner: GraphWriteGateway) -> None:
            self.submit_group = inner.submit_group
            self.read_log_position = inner.read_log_position
            self.is_caught_up = inner.is_caught_up
            self.catch_up = inner.catch_up

        def submit_multi_graph_group(self, group: object) -> object:
            del group
            raise GraphWriteRejectedError("the group is refused")

    refusing = RefusingGateway(restore_rig.gateway)
    restore_rig.gateway_opener = lambda: refusing

    with pytest.raises(RestoreGroupRejectedError) as raised:
        restore_rig.restore(_artifact())

    assert isinstance(raised.value.__cause__, GraphWriteRejectedError)
    assert restore_rig.store.groups == []


def test_a_graph_fault_after_the_commit_raises_apply_pending_and_catch_up_completes_it(
    restore_rig: RestoreRig,
) -> None:
    restore_rig.key_port.copy_fault = Fault(redis.exceptions.ConnectionError("down"))

    with pytest.raises(RestoreApplyPendingError):
        restore_rig.restore(_artifact())

    # committed: the log holds all three legs, one transaction, nothing is lost, nothing visible
    assert restore_rig.store.multi_appends == [(_BASELINE, _NATIVE, _POLICY_SYSTEM)]
    assert ("ARTICLE", "art_1") not in _labels(restore_rig.graph(_NATIVE))
    restore_rig.key_port.copy_fault = None
    restore_rig.gateway.catch_up(_NATIVE)
    assert ("ARTICLE", "art_1") in _labels(restore_rig.graph(_NATIVE))


def test_an_apply_refusal_after_the_commit_is_a_graph_write_failure(
    restore_rig: RestoreRig,
) -> None:
    restore_rig.key_port.on_staged_write = _raise_once(redis.exceptions.ResponseError("refused"))

    with pytest.raises(RestoreGraphWriteFailedError):
        restore_rig.restore(_artifact())

    assert ("ARTICLE", "art_1") not in _labels(restore_rig.graph(_NATIVE))  # nothing visible


def _raise_once(error: Exception) -> Callable[..., None]:
    pending = [error]

    def hook(*_: object) -> None:
        if pending:
            raise pending.pop()

    return hook


# -- internal instruments: drafts owned by the caller, never converged ---------------------


def test_the_internal_curated_instrument_lands_as_drafts_owned_by_the_restoring_caller(
    restore_rig: RestoreRig,
) -> None:
    """Draft ownership end to end on the real ENGPRAC artifact."""
    curated = load_curated("ENGPRAC-1.0")
    artifact = build_restore_artifact(
        instrument_id="ENGPRAC-1.0",
        short_name="ENGPRAC",
        native_graph=curated.native,
        baseline_graph=curated.baseline,
    )

    restore_rig.restore(artifact, owner=OWNER)

    policies = [
        properties
        for (label, _id), properties in restore_rig.graph(_POLICY_SYSTEM).nodes.items()
        if label == "Policy"
    ]
    assert policies
    assert {p["status"] for p in policies} == {"draft"}
    assert {(p["owner_subject"], p["owner_issuer"]) for p in policies} == {OWNER}


def test_a_second_restore_never_converges_a_draft_onto_an_existing_policy(
    restore_rig: RestoreRig,
) -> None:
    curated = load_curated("ENGPRAC-1.0")
    artifact = build_restore_artifact(
        instrument_id="ENGPRAC-1.0",
        short_name="ENGPRAC",
        native_graph=curated.native,
        baseline_graph=curated.baseline,
    )
    restore_rig.restore(artifact, owner=OWNER)
    policy_count = sum(
        1 for label, _ in restore_rig.graph(_POLICY_SYSTEM).nodes if label == "Policy"
    )
    other_owner = ("sub-other", "https://idp.example")

    restore_rig.restore(artifact, owner=other_owner)

    policies = [
        properties
        for (label, _id), properties in restore_rig.graph(_POLICY_SYSTEM).nodes.items()
        if label == "Policy"
    ]
    assert len(policies) == policy_count  # create-only: same ids, no second set, no convergence
    assert {(p["owner_subject"], p["owner_issuer"]) for p in policies} == {OWNER}
