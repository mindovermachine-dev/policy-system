"""`split-capability`: the preview of a split and the passkey approval bound to it.

The MCP tool, the service, the planner, the single-tenant reads and the baseline reads run for real
over the gateway rig's in-memory graphs. Only the graph openers (the approved boundary), the role
store, the audit store and the approval store are fakes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore
from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)

from graph_cleanup._fakes import FakeApprovalStore, RecordingAuditStore, approval_row_count
from graph_cleanup._gateway_fakes import SPLIT_A, SPLIT_POLICY, SPLIT_X, CleanupRig
from graph_cleanup._mcp_calls import call_tool, factory
from ps_service.authz.models import AccessRole
from ps_service.graph_cleanup import dependencies
from ps_service.graph_cleanup.errors import GraphCleanupSplitAcknowledgmentRequiredError
from ps_service.graph_cleanup.service import create_capability_split_approval

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines

    from ps_service.config import ServiceConfig

_ISSUER = "https://issuer.example.com/"
_OWNER = ("owner", _ISSUER)
_OFFICER = ("officer", _ISSUER)
_TOOL = "split-capability"

_X = SPLIT_X
_A = SPLIT_A
_POLICY = SPLIT_POLICY


def _roles() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=_OWNER)
    store.bootstrap_first_owner(_OWNER)
    store.grant(actor=_OWNER, target=_OFFICER, access_role=AccessRole.COMPLIANCE_OFFICER)
    return store


def _seed_world(rig: CleanupRig, *, governed: bool = False) -> None:
    """X resolved onto A, with one edge of every class in every role the split tells apart."""
    rig.seed_capability_split_world(governed=governed)


@dataclass
class _Snapshot:
    log_length: int
    nodes: dict[str, object] = field(default_factory=dict)
    edges: dict[str, object] = field(default_factory=dict)


def _snapshot(rig: CleanupRig) -> _Snapshot:
    return _Snapshot(
        log_length=len(rig.store.groups),
        nodes={repr(key): dict(value) for key, value in rig.graph.nodes.items()},
        edges={repr(key): dict(value) for key, value in rig.graph.edges.items()},
    )


def _install(monkeypatch: pytest.MonkeyPatch, rig: CleanupRig) -> FakeApprovalStore:
    pending = FakeApprovalStore()
    _override(
        monkeypatch,
        role_store_factory=factory(_roles()),
        audit_store_factory=factory(RecordingAuditStore()),
        graph_factory=factory(rig.read),
        pending_approval_store_factory=factory(pending),
    )

    def _opener() -> object:
        def _open(_config: ServiceConfig) -> object:
            return rig.baseline_graphs()

        return _open

    monkeypatch.setattr(dependencies, "build_default_graph_cleanup_baseline_opener", _opener)
    return pending


_EXPECTED_EDGES = {
    "requires": {
        "moves": ["obl_moves"],
        "shared": ["obl_shared"],
        "added": ["obl_added"],
        "stays": ["obl_stays"],
    },
    "covers": {"moves": ["pa_moves"], "shared": [], "added": [], "stays": []},
    "mitigated_by": {"moves": [], "shared": [], "added": [], "stays": ["rp_stays"]},
}


def test_preview_lists_moves_and_stays_and_binds_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = CleanupRig()
    _seed_world(rig)
    pending = _install(monkeypatch, rig)
    before = _snapshot(rig)

    body = json.loads(call_tool(_TOOL, {"x_id": _X}, _OFFICER))

    preview = body["preview"]
    assert (preview["x_id"], preview["x_name"], preview["a_id"], preview["a_name"]) == (
        _X,
        "Vulnerability patching",
        _A,
        "Vulnerability remediation",
    )
    assert preview["marker_resolves_to"] == _A
    assert (preview["source_baseline"], preview["source_instruments"]) == (
        "nis2_baseline",
        ["32022L2555"],
    )
    assert preview["embedding_available"] is True
    assert preview["edges"] == _EXPECTED_EDGES
    assert preview["governance"] is None
    assert preview["obligations_moving"] == 1
    assert preview["skipped_missing_endpoints"] == 1
    row = pending.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert row.tool_name == _TOOL
    assert (row.actor_subject, row.actor_issuer) == _OFFICER
    assert row.normalized_args == {
        "x_id": _X,
        "a_id": _A,
        "acknowledge_governance_change": False,
        "state_digest": preview["state_digest"],
    }
    assert row.display_summary == preview
    assert f"/approvals/{row.id}#" in body["approval_url"]
    assert _snapshot(rig) == before
    assert rig.store.groups == []


def test_a_governed_terminal_without_acknowledgment_returns_the_preview_and_no_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = CleanupRig()
    _seed_world(rig, governed=True)
    pending = _install(monkeypatch, rig)
    before = _snapshot(rig)

    body = json.loads(call_tool(_TOOL, {"x_id": _X}, _OFFICER))

    assert set(body) == {"preview", "acknowledgment_required", "message"}
    assert body["acknowledgment_required"] is True
    governance = body["preview"]["governance"]
    assert governance["policy"] == {
        "id": _POLICY,
        "title": "Patch Management Policy",
        "status": "approved",
    }
    assert governance["acknowledgment_required"] is True
    assert "Patch Management Policy" in body["message"]
    assert "acknowledge_governance_change=true" in body["message"]
    assert approval_row_count(pending) == 0
    assert _snapshot(rig) == before


def test_a_given_acknowledgment_is_bound_into_the_signed_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = CleanupRig()
    _seed_world(rig, governed=True)
    pending = _install(monkeypatch, rig)

    body = json.loads(
        call_tool(_TOOL, {"x_id": _X, "acknowledge_governance_change": True}, _OFFICER)
    )

    row = pending.get_by_id(body["pending_approval_id"])
    assert row is not None
    assert row.normalized_args["acknowledge_governance_change"] is True
    assert row.normalized_args["state_digest"] == body["preview"]["state_digest"]
    assert body["preview"]["governance"]["policy"]["id"] == _POLICY


def _refusal_worlds() -> dict[str, tuple[CleanupRig, str]]:
    no_marker = CleanupRig()
    _seed_world(no_marker)
    del no_marker.graph.nodes[("ResolvedCapability", _X)]
    existing = CleanupRig()
    _seed_world(existing)
    existing.seed_capability(_X, name="Vulnerability patching")
    merged = CleanupRig()
    _seed_world(merged)
    del merged.graph.nodes[("ResolvedCapability", _X)]
    merged.graph.seed_node(("MergedCapability",), _X, {"merged_into": _A})
    unheld = CleanupRig()
    _seed_world(unheld)
    del unheld.baselines["nis2_baseline"]
    cycle = CleanupRig()
    _seed_world(cycle)
    cycle.graph.nodes[("ResolvedCapability", _X)]["resolves_to"] = "cap_d"
    cycle.graph.seed_node(("MergedCapability",), "cap_d", {"merged_into": _X})
    return {
        "no marker": (no_marker, "no automatic resolution"),
        "already a capability": (existing, "already exists as a Capability"),
        "merged": (merged, "merged"),
        "no baseline": (unheld, "no regulation baseline"),
        "cycle": (cycle, "cycle"),
    }


@pytest.mark.parametrize("case", sorted(_refusal_worlds()))
def test_an_id_that_cannot_be_split_is_an_error_and_creates_no_approval(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    rig, fragment = _refusal_worlds()[case]
    pending = _install(monkeypatch, rig)
    before = _snapshot(rig)

    text = call_tool(_TOOL, {"x_id": _X}, _OFFICER)

    assert text.startswith("error: ")
    assert fragment in text
    assert approval_row_count(pending) == 0
    assert _snapshot(rig) == before


def test_the_approval_is_logged_with_ids_and_counts_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    rig = CleanupRig()
    _seed_world(rig, governed=True)
    emitter, log_path = make_emitter()
    store = FakeApprovalStore()

    def _create(*, acknowledge: bool) -> None:
        create_capability_split_approval(
            rig.read,
            rig.baseline_graphs(),
            x_id=_X,
            acknowledge_governance_change=acknowledge,
            actor=_OFFICER,
            base_url="https://ps.example.com",
            store=store,
            emitter=emitter,
        )

    with pytest.raises(GraphCleanupSplitAcknowledgmentRequiredError):
        _create(acknowledge=False)
    _create(acknowledge=True)
    emitter.flush()

    entries = [
        line
        for line in read_lines(log_path)
        if line["action"] == "create_capability_split_approval"
    ]
    assert [entry["outcome"] for entry in entries] == ["acknowledgment_required", "succeeded"]
    required, succeeded = entries
    assert (required["x_id"], required["a_id"], required["policy_id"]) == (_X, _A, _POLICY)
    assert succeeded["approval_id"] == next(iter(store._rows_by_id))  # pyright: ignore[reportPrivateUsage]
    assert (succeeded["moves"], succeeded["shared"], succeeded["added"]) == (2, 1, 1)
    for entry in entries:
        assert entry["component"] == "graph_cleanup"
        text = json.dumps(entry)
        assert "Vulnerability" not in text
        assert "Management" not in text
