"""`falkordb_live`: the gateway-backed lifecycle writers against the frozen legacy Cypher.

The live twin of `test_policy_lifecycle_equivalence.py`. The frozen pre-gateway statements
(`_legacy_reference`) run VERBATIM on one real FalkorDB graph; the real `graph_writer` builders run
through a real gateway (in-memory log) on another; `canonical_digest` of the two must be equal
once the legacy edges are given the gateway's deterministic `identity` (met
modulo edge identity). It is the only proof that the in-memory interpreter agrees with real
FalkorDB semantics (`SET +=`, `OPTIONAL MATCH` cross products, `FOREACH` ordering).

Written, not run in the implementation sandbox (no FalkorDB module): the fast twin carries the
verified assertions. To run:
`PS_TEST_FALKORDB_HOST=127.0.0.1 PS_TEST_FALKORDB_PORT=16379
uv run pytest ps-service/tests/policy_lifecycle/test_policy_lifecycle_equivalence_live.py
-m falkordb_live`. Uniquely named throwaway graphs, deleted afterwards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from graph_gateway.live_endpoints import falkordb_endpoint
from graph_gateway.live_graphs import AUDIT_EVENT_ID, LiveGraph, LiveGraphs

from policy_lifecycle._legacy_reference import (
    legacy_approve_fork_repoint,
    legacy_cascade_status,
    legacy_create_policy_draft,
    legacy_update_policy_fields,
)
from ps_service.graph_gateway.connection import connect
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.policy_lifecycle.graph_submission import GraphWriteTarget, lifecycle_submitter
from ps_service.policy_lifecycle.graph_writer import (
    ControlDraft,
    StandardDraft,
    approve_fork_repoint,
    cascade_status,
    create_claimed_policy_draft,
    create_policy_draft,
    update_policy_fields,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.falkordb_live

_OWNER = ("live-owner", "https://issuer.example.com/")
_STAMP_IDENTITY = (
    "MATCH (a)-[r]->(b) WHERE r.identity IS NULL "
    "SET r.identity = type(r) + ':' + a.id + '->' + b.id"
)
_CAPS = ("cap_live_a", "cap_live_b")
_TREE = (
    StandardDraft(
        id="std_live",
        title="Std",
        controls=(ControlDraft(id="ctl_live", title="Ctl", control_type="manual"),),
    ),
)


class _Pair:
    """A legacy graph and a gateway-written graph, seeded with the same Capabilities."""

    def __init__(self, legacy: LiveGraph, new: LiveGraph) -> None:
        self.legacy = legacy
        self.new = new
        for graph in (legacy, new):
            graph.store.audit_rows.add(AUDIT_EVENT_ID)
            for cap_id in _CAPS:
                graph.handle.query("MERGE (:Capability {id: $id})", params={"id": cap_id})

    def target(self) -> GraphWriteTarget:
        """Where the new path submits."""
        return GraphWriteTarget(lifecycle_submitter(self.new.gateway), self.new.name)

    def digests(self) -> tuple[str, str]:
        """(legacy modulo identity, new)."""
        self.legacy.handle.query(_STAMP_IDENTITY)
        return canonical_digest(self.legacy.handle), canonical_digest(self.new.handle)


@pytest.fixture
def pair() -> Iterator[_Pair]:
    host, port = falkordb_endpoint()
    graphs = LiveGraphs(connect(host=host, port=port))
    yield _Pair(graphs.new(), graphs.new())
    graphs.close()


def test_create_edit_propose_approve_matches_the_legacy_writers(pair: _Pair) -> None:
    legacy = pair.legacy.handle
    legacy_create_policy_draft(legacy, policy_id="pol_a", title="A", owner=_OWNER, standards=_TREE)
    legacy_update_policy_fields(
        legacy, policy_id="pol_a", properties={"description": "d", "scope_out": None}
    )
    legacy_cascade_status(legacy, policy_id="pol_a", target_status="proposed")
    legacy_cascade_status(legacy, policy_id="pol_a", target_status="approved")

    target = pair.target()
    create_policy_draft(
        target,
        policy_id="pol_a",
        title="A",
        owner=_OWNER,
        standards=_TREE,
        audit_event_id=AUDIT_EVENT_ID,
    )
    update_policy_fields(
        target, policy_id="pol_a", properties={"description": "d", "scope_out": None}
    )
    for status in ("proposed", "approved"):
        cascade_status(
            pair.new.handle,
            target,
            policy_id="pol_a",
            target_status=status,
            audit_event_id=AUDIT_EVENT_ID,
        )

    expected, actual = pair.digests()
    assert actual == expected


def test_a_claim_and_a_fork_approval_match_the_legacy_writers(pair: _Pair) -> None:
    legacy = pair.legacy.handle
    assert legacy_create_policy_draft(
        legacy, policy_id="pol_prior", title="P", owner=_OWNER, capability_ids=_CAPS
    )
    legacy_cascade_status(legacy, policy_id="pol_prior", target_status="approved")
    legacy_create_policy_draft(
        legacy,
        policy_id="pol_fork",
        title="P",
        owner=_OWNER,
        supersedes_policy_id="pol_prior",
        version="2",
        standards=_TREE,
    )
    legacy_cascade_status(legacy, policy_id="pol_fork", target_status="proposed")
    assert legacy_approve_fork_repoint(
        legacy,
        policy_id="pol_fork",
        prior_id="pol_prior",
        capability_ids=_CAPS,
        target_status="approved",
    )
    legacy_cascade_status(legacy, policy_id="pol_prior", target_status="deprecated")

    target = pair.target()
    assert create_claimed_policy_draft(
        pair.new.handle,
        target,
        policy_id="pol_prior",
        title="P",
        owner=_OWNER,
        capability_ids=_CAPS,
        audit_event_id=AUDIT_EVENT_ID,
    )
    cascade_status(
        pair.new.handle,
        target,
        policy_id="pol_prior",
        target_status="approved",
        audit_event_id=AUDIT_EVENT_ID,
    )
    create_policy_draft(
        target,
        policy_id="pol_fork",
        title="P",
        owner=_OWNER,
        supersedes_policy_id="pol_prior",
        version="2",
        standards=_TREE,
        audit_event_id=AUDIT_EVENT_ID,
    )
    cascade_status(
        pair.new.handle,
        target,
        policy_id="pol_fork",
        target_status="proposed",
        audit_event_id=AUDIT_EVENT_ID,
    )
    assert approve_fork_repoint(
        pair.new.handle,
        target,
        policy_id="pol_fork",
        prior_id="pol_prior",
        capability_ids=_CAPS,
        target_status="approved",
        audit_event_id=AUDIT_EVENT_ID,
    )
    cascade_status(
        pair.new.handle,
        target,
        policy_id="pol_prior",
        target_status="deprecated",
        audit_event_id=AUDIT_EVENT_ID,
    )

    expected, actual = pair.digests()
    assert actual == expected


def test_a_stale_claim_and_a_conflicting_repoint_write_nothing_on_both_paths(pair: _Pair) -> None:
    legacy = pair.legacy.handle
    legacy_create_policy_draft(
        legacy, policy_id="pol_owner", title="O", owner=_OWNER, capability_ids=("cap_live_a",)
    )
    target = pair.target()
    create_claimed_policy_draft(
        pair.new.handle,
        target,
        policy_id="pol_owner",
        title="O",
        owner=_OWNER,
        capability_ids=("cap_live_a",),
        audit_event_id=AUDIT_EVENT_ID,
    )

    refused_legacy = legacy_create_policy_draft(
        legacy, policy_id="pol_late", title="L", owner=_OWNER, capability_ids=_CAPS
    )
    refused_new = create_claimed_policy_draft(
        pair.new.handle,
        target,
        policy_id="pol_late",
        title="L",
        owner=_OWNER,
        capability_ids=_CAPS,
        audit_event_id=AUDIT_EVENT_ID,
    )

    assert (refused_legacy, refused_new) == (False, False)
    expected, actual = pair.digests()
    assert actual == expected
