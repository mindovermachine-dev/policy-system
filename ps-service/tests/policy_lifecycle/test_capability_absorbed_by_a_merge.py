"""Policy Lifecycle treats a Capability a cleanup merge absorbed as nonexistent.

A merge deletes the absorbed Capability and leaves a `MergedCapability` marker, so the draft
claim's read finds nothing under the absorbed id and `PolicyCapabilityNotFoundError` is raised
exactly as for an id that never existed. The claim itself is a gateway group whose write-time
guard re-uses that same read (`test_claim_create_gateway.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from policy_lifecycle._gateway_fakes import POLICY_SYSTEM
from ps_service.policy_lifecycle.errors import PolicyCapabilityNotFoundError
from ps_service.policy_lifecycle.graph_writer import read_capability_governors
from ps_service.policy_lifecycle.service import create_policy_draft

if TYPE_CHECKING:
    from policy_lifecycle._gateway_fakes import LifecycleRig

_OWNER = ("carol", "https://issuer.example")


@dataclass
class _Result:
    result_set: list[object]


class _Graph:
    def __init__(self, rows: list[object]) -> None:
        self.queries: list[str] = []
        self._rows = rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        self.queries.append(q)
        return _Result(self._rows)


def test_governor_read_has_no_status_predicate() -> None:
    graph = _Graph([["cap_live", None]])

    governors = read_capability_governors(graph, ("cap_live", "cap_absorbed"))

    assert governors == {"cap_live": None}
    assert "status" not in graph.queries[0]


def test_claiming_an_absorbed_capability_raises_not_found(lifecycle_rig: LifecycleRig) -> None:
    lifecycle_rig.seed_capability("cap_a")
    lifecycle_rig.graph.seed_node(("MergedCapability",), "cap_b", {"merged_into": "cap_a"})

    with pytest.raises(PolicyCapabilityNotFoundError) as raised:
        create_policy_draft(
            actor=_OWNER,
            title="Claim an absorbed capability",
            capability_ids=("cap_a", "cap_b"),
            graph=lifecycle_rig.read,
            audit_store=lifecycle_rig.audit,
            submitter=lifecycle_rig.gateway,
            graph_name=POLICY_SYSTEM,
        )

    assert raised.value.capability_ids == ("cap_b",)
    assert lifecycle_rig.logged_groups == 0


def test_claiming_a_capability_id_only_a_resolution_marker_holds_raises_not_found(
    lifecycle_rig: LifecycleRig,
) -> None:
    """A resolved id was never minted as a Capability, so a draft cannot claim it."""
    lifecycle_rig.seed_capability("cap_a")
    lifecycle_rig.graph.seed_node(("ResolvedCapability",), "cap_x", {"resolves_to": "cap_a"})

    with pytest.raises(PolicyCapabilityNotFoundError) as raised:
        create_policy_draft(
            actor=_OWNER,
            title="Claim a resolved capability id",
            capability_ids=("cap_a", "cap_x"),
            graph=lifecycle_rig.read,
            audit_store=lifecycle_rig.audit,
            submitter=lifecycle_rig.gateway,
            graph_name=POLICY_SYSTEM,
        )

    assert raised.value.capability_ids == ("cap_x",)
    assert lifecycle_rig.logged_groups == 0
