"""Tests for the restore's draft-governance import.

Restoring an internal curated instrument imports its Policy/Standard/Control tree as `draft`,
owned by the restoring caller. A draft must never converge onto an existing canonical Policy,
because its draft Standards/Controls would then hang under a possibly-approved parent. The live
ingestion-to-merge path (`merge_baseline_graph`) imports through the same shared function
(`governance_import.import_draft_governance`); that it does is proven in
`tests/restore/test_restore_merge_governance_parity.py` and
`tests/company_merge/test_merge_internal_seed_draft_governance.py`.

The restore's `policy_system` leg is a tuple of gateway primitives built from reads of
the live graph (`merge_primitives.build_policy_system_leg`), so the expectations are asserted on
primitives. The live graph is a hand-written fake satisfying `GraphHandle` structurally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from company_merge._thresholds import REVIEW_THRESHOLD

from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.domain_mapper.identity import policy_id
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.graph_gateway.models import UpsertEdge, UpsertNode
from ps_service.restore.errors import RestoreOwnerRequiredError
from ps_service.restore.merge_primitives import PolicySystemLeg, build_policy_system_leg
from ps_service.restore.restore_instrument import restore_instrument
from restore._fixtures import build_restore_artifact
from restore._gateway_fakes import UNUSED_EVENT_ID, ScriptedLive, never_open_gateway

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB

_THRESHOLD = 0.85
_OWNER = ("alice@example.com", "https://idp.example/")


def _governance_baseline(
    *, instrument_id: str, capability_id_value: str, policy_id_value: str, policy_title: str
) -> SerializedGraph:
    """One Capability, one Policy, one Standard, one Control, fully wired (internal-sourced)."""
    standard_id_value = f"std_{policy_id_value}_v1"
    control_id_value = f"ctrl_{standard_id_value}_manual"

    def edge(kind: str, source: tuple[str, str], target: tuple[str, str]) -> SerializedEdge:
        return SerializedEdge(kind, source[0], source[1], target[0], target[1], {})

    return SerializedGraph(
        nodes=(
            SerializedNode(
                "RegulatoryInstrument", {"id": instrument_id, "title": "Engineering Practices"}
            ),
            SerializedNode(
                "Capability",
                {
                    "id": capability_id_value,
                    "name": "Engineering Review Capability",
                    "confidence": 0.8,
                },
            ),
            SerializedNode(
                "Policy", {"id": policy_id_value, "title": policy_title, "status": "draft"}
            ),
            SerializedNode("Standard", {"id": standard_id_value, "title": "Code Review Standard"}),
            SerializedNode(
                "Control", {"id": control_id_value, "title": "Peer Review", "type": "manual"}
            ),
        ),
        edges=(
            edge("GOVERNED_BY", ("Capability", capability_id_value), ("Policy", policy_id_value)),
            edge("SUPPORTED_BY", ("Policy", policy_id_value), ("Standard", standard_id_value)),
            edge("IMPLEMENTED_BY", ("Standard", standard_id_value), ("Control", control_id_value)),
        ),
    )


def _build(
    baseline: SerializedGraph,
    live: ScriptedLive,
    *,
    instrument_id: str,
    emitter: object,
    policy_embeddings: dict[str, tuple[float, ...]] | None = None,
    owner: tuple[str, str] | None,
) -> PolicySystemLeg:
    return build_policy_system_leg(
        baseline,
        instrument_id=instrument_id,
        live=live,
        governance=GovernanceImport(owner=owner, policy_embeddings=policy_embeddings or {}),
        merge_threshold=_THRESHOLD,
        review_threshold=REVIEW_THRESHOLD,
        emitter=emitter,  # pyright: ignore[reportArgumentType] -- the make_emitter fixture's emitter
    )


def _nodes(leg: PolicySystemLeg, label: str) -> list[UpsertNode]:
    return [p for p in leg.primitives if isinstance(p, UpsertNode) and p.label == label]


def _edges(leg: PolicySystemLeg, edge_type: str) -> list[UpsertEdge]:
    return [p for p in leg.primitives if isinstance(p, UpsertEdge) and p.type == edge_type]


def test_restore_mints_draft_policy_owned_by_the_caller_instead_of_converging(
    make_emitter: MakeEmitter,
) -> None:
    """An incoming Policy that would semantically match an existing
    canonical Policy is still minted as its own `draft` node, owned by the caller.
    """
    emitter, _log_path = make_emitter()
    instrument_id = "ENGPRAC-3.0"
    existing_policy_id = policy_id("Engineering Practices Policy")
    incoming_title = "Engineering Practice Policy"  # reworded -> distinct id
    incoming_policy_id = policy_id(incoming_title)
    assert incoming_policy_id != existing_policy_id

    baseline = _governance_baseline(
        instrument_id=instrument_id,
        capability_id_value="cap_engineering_review_abc",
        policy_id_value=incoming_policy_id,
        policy_title=incoming_title,
    )
    # An existing Policy carrying the SAME vector as the incoming one -- under the old
    # convergence path this would have resolved the incoming Policy onto it.
    live = ScriptedLive(
        existing={"Policy": {existing_policy_id}},
        answer=lambda q, _params: (
            [[existing_policy_id, "Engineering Practices Policy", [1.0, 0.0]]]
            if "(n:Policy) RETURN n.id, n.title, n.embedding" in q
            else None
        ),
    )

    leg = _build(
        baseline,
        live,
        instrument_id=instrument_id,
        emitter=emitter,
        policy_embeddings={incoming_policy_id: (1.0, 0.0)},
        owner=_OWNER,
    )

    (policy,) = _nodes(leg, "Policy")
    assert policy.id == incoming_policy_id
    assert policy.properties["status"] == "draft"
    assert policy.properties["owner_subject"] == _OWNER[0]
    assert policy.properties["owner_issuer"] == _OWNER[1]
    (governed_by,) = _edges(leg, "GOVERNED_BY")
    assert governed_by.target.id == incoming_policy_id, (
        "the governance edge must target the draft Policy, not the existing one it resembles"
    )
    assert not [q for q in live.queries if "(n:Policy) RETURN n.id, n.title, n.embedding" in q], (
        "restore must not read the Policy index at all -- there is no Policy dedup"
    )


def test_restore_writes_standard_and_control_as_draft_and_wires_their_edges(
    make_emitter: MakeEmitter,
) -> None:
    """The whole tree lands as draft with its SUPPORTED_BY/IMPLEMENTED_BY edges."""
    emitter, _log_path = make_emitter()
    instrument_id = "ENGPRAC-3.0"
    baseline = _governance_baseline(
        instrument_id=instrument_id,
        capability_id_value="cap_access",
        policy_id_value=policy_id("Access Policy"),
        policy_title="Access Policy",
    )

    leg = _build(
        baseline, ScriptedLive(), instrument_id=instrument_id, emitter=emitter, owner=_OWNER
    )

    for label in ("Standard", "Control"):
        (node,) = _nodes(leg, label)
        assert node.properties["status"] == "draft"
    assert len(_edges(leg, "SUPPORTED_BY")) == 1
    assert len(_edges(leg, "IMPLEMENTED_BY")) == 1


def test_restore_returns_governance_counts_for_the_audit_entry(make_emitter: MakeEmitter) -> None:
    """The returned counts (folded into the audit log) include the draft counts."""
    emitter, _log_path = make_emitter()
    instrument_id = "ENGPRAC-3.0"
    baseline = _governance_baseline(
        instrument_id=instrument_id,
        capability_id_value="cap_access",
        policy_id_value=policy_id("Access Policy"),
        policy_title="Access Policy",
    )

    leg = _build(
        baseline, ScriptedLive(), instrument_id=instrument_id, emitter=emitter, owner=_OWNER
    )

    assert leg.counts["governance_policies"] == 1
    assert leg.counts["governance_standards"] == 1
    assert leg.counts["governance_controls"] == 1
    assert leg.counts["governance_status_overridden"] == 0


def test_restore_with_governance_content_and_no_owner_is_refused_before_any_read(
    make_emitter: MakeEmitter,
) -> None:
    """Ownerless drafts are never created: the build refuses before reading the live graph."""
    emitter, _log_path = make_emitter()
    instrument_id = "ENGPRAC-3.0"
    baseline = _governance_baseline(
        instrument_id=instrument_id,
        capability_id_value="cap_access",
        policy_id_value=policy_id("Access Policy"),
        policy_title="Access Policy",
    )
    live = ScriptedLive()

    with pytest.raises(RestoreOwnerRequiredError):
        _build(baseline, live, instrument_id=instrument_id, emitter=emitter, owner=None)

    assert live.queries == []


def test_external_baseline_restore_needs_no_owner_and_writes_no_governance(
    make_emitter: MakeEmitter,
) -> None:
    """An external-sourced restore (no Policy nodes) is a no-op for the whole pass."""
    emitter, _log_path = make_emitter()
    instrument_id = "RT60-1.0"
    baseline = SerializedGraph(
        nodes=(
            SerializedNode("RegulatoryInstrument", {"id": instrument_id, "title": "RT60"}),
            SerializedNode(
                "Capability",
                {"id": "cap_no_governance", "name": "Some Capability", "confidence": 0.8},
            ),
        ),
        edges=(),
    )

    leg = _build(baseline, ScriptedLive(), instrument_id=instrument_id, emitter=emitter, owner=None)

    assert not any(
        isinstance(p, UpsertNode) and p.label in {"Policy", "Standard", "Control"}
        for p in leg.primitives
    )
    assert leg.counts["governance_policies"] == 0


def test_restore_instrument_refuses_governance_content_without_an_owner_before_the_graph(
    make_emitter: MakeEmitter,
) -> None:
    """The owner check runs before the gateway is opened: a rejected restore touches no graph."""
    baseline = SerializedGraph(
        nodes=(
            SerializedNode(label="RegulatoryInstrument", properties={"id": "ENGPRAC-1.0"}),
            SerializedNode(label="Policy", properties={"id": "pol_a", "title": "P"}),
        ),
        edges=(),
    )
    artifact = build_restore_artifact(
        instrument_id="ENGPRAC-1.0",
        short_name="ENGPRAC",
        native_graph=SerializedGraph(nodes=(), edges=()),
        baseline_graph=baseline,
    )

    emitter, _log_path = make_emitter()

    class _UntouchableDb:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"the graph must not be touched, but .{name} was accessed")

    with pytest.raises(RestoreOwnerRequiredError):
        restore_instrument(
            artifact,
            db=cast("FalkorDB", _UntouchableDb()),
            single_tenant_graph_name="policy_system",
            merge_threshold=_THRESHOLD,
            review_threshold=REVIEW_THRESHOLD,
            actor="tester",
            open_gateway=never_open_gateway,
            audit_event_id=UNUSED_EVENT_ID,
            emitter=emitter,
            owner=None,
        )
