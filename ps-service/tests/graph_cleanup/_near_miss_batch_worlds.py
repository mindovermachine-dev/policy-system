"""Seeded near-miss pairs for the batch tests: any number of disjoint Capability and Role reviews.

Each pair is numbered, so the pairs of one graph share no node. A Capability review's survivor is
the older node; a Role review's survivor is the incoming Role.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig

OLD = "2020-01-01T00:00:00+00:00"
NEW = "2024-01-01T00:00:00+00:00"


class SeededPair(NamedTuple):
    """A seeded review and the survivor and absorbed node the survivor rule picks for it."""

    review_id: str
    survivor_id: str
    absorbed_id: str


def seed_capability_pair_with_review(rig: CleanupRig, n: int) -> SeededPair:
    """Seed two active Capabilities and a review; the older (existing) one survives."""
    survivor, absorbed, review = f"cap_s{n}", f"cap_a{n}", f"review_cap_{n}"
    rig.seed_capability(survivor, name=f"Survivor {n}", status="active")
    rig.seed_capability(absorbed, name=f"Absorbed {n}", status="active")
    obligation, area = f"obl_cap_{n}", f"pa_cap_{n}"
    rig.seed_endpoint("Obligation", obligation)
    rig.seed_endpoint("PracticeArea", area)
    rig.seed_edge(("REQUIRES", "Obligation", obligation, "Capability", absorbed), legacy=False)
    rig.seed_edge(("COVERS", "PracticeArea", area, "Capability", absorbed), legacy=False)
    rig.seed_near_miss_review(
        review,
        incoming_id=absorbed,
        existing_id=survivor,
        incoming_created_at=NEW,
        existing_created_at=OLD,
    )
    return SeededPair(review, survivor, absorbed)


def seed_role_pair_with_review(rig: CleanupRig, n: int) -> SeededPair:
    """Seed two Roles of one instrument and a review; the incoming Role survives."""
    incoming, existing, review = f"role_in{n}", f"role_ex{n}", f"review_role_{n}"
    instrument = f"inst_{n}"
    rig.seed_instrument(instrument)
    for role_id, name, ref in (
        (incoming, f"Operator {n}", "Art. 3(1)"),
        (existing, f"Operators {n}", "Art. 3(2)"),
    ):
        rig.seed_role(role_id, name=name)
        rig.seed_defines(instrument, role_id, source_ref=ref)
        obligation = f"obl_{role_id}"
        rig.seed_endpoint("Obligation", obligation, text=obligation)
        rig.seed_has(role_id, obligation)
    rig.seed_near_miss_review(review, incoming_id=incoming, existing_id=existing, kind="Role")
    return SeededPair(review, incoming, existing)


def seed_policy_governing(rig: CleanupRig, policy_id: str, *capability_ids: str) -> None:
    """Seed an approved Policy that governs each of the given Capabilities."""
    rig.seed_policy(policy_id, title=f"Policy {policy_id}", status="approved")
    for capability_id in capability_ids:
        rig.seed_governs(capability_id, policy_id)
