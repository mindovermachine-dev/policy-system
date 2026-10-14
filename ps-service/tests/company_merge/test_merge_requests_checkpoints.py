"""`merge_baseline_graph` requests a digest checkpoint on its terminal groups.

On `policy_system` the LAST group a merge writes asks for the
checkpoint (the canonical-embedding backfill when it has something to write, otherwise the
rewired edges); on the regulation's `{short}_baseline` the incoming-embedding backfill does. A
checkpoint that cannot be recorded never fails the merge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import redis.exceptions
from graph_gateway._fakes import Fault
from graph_gateway.checkpoint_checks import (
    assert_head_checkpoint,
    assert_head_checkpoints,
    assert_no_checkpoint,
)

from company_merge._gateway_fakes import POLICY_SYSTEM, FamilyABaseline, MergeRig
from company_merge._merge_equivalence import production_run

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter
    from ps_service.logging import LogEmitter

import pytest

_CAP = "capability_encrypt_data"
_ALIAS = "capability_encrypt_data_alias"

_FIRST = FamilyABaseline(
    instrument_id="CRA-1.0",
    instrument_properties={"title": "Cyber Resilience Act"},
    roles={"role_manufacturer": "Manufacturer"},
    obligations={"obl_notify": ("Notify the CSIRT.", 0.7)},
    capabilities={_CAP: ("Encrypt data", 0.8)},
    edges=(
        ("HAS", "Role", "role_manufacturer", "Obligation", "obl_notify"),
        ("REQUIRES", "Obligation", "obl_notify", "Capability", _CAP),
    ),
)
# The same Capability under another id: dedup matches it onto the first regulation's node, which
# was minted without an embedding, so this merge ends with a canonical-embedding backfill.
_SECOND = FamilyABaseline(
    instrument_id="NIS2-1.0",
    instrument_properties={"title": "NIS 2"},
    roles={"role_operator": "Operator"},
    obligations={"obl_records": ("Keep records.", 0.6)},
    capabilities={_ALIAS: ("Encrypt data", 0.8)},
    edges=(
        ("HAS", "Role", "role_operator", "Obligation", "obl_records"),
        ("REQUIRES", "Obligation", "obl_records", "Capability", _ALIAS),
    ),
)


@pytest.fixture
def emitter(make_emitter: MakeEmitter) -> LogEmitter:
    return make_emitter()[0]


def _policy_system_checkpoints(rig: MergeRig) -> list[int]:
    return sorted(position for graph, position in rig.store.checkpoints if graph == POLICY_SYSTEM)


def test_a_merge_with_no_backfill_leaves_a_head_checkpoint_from_the_rewired_edges(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    production_run((_FIRST,), merge_rig, emitter)

    assert_head_checkpoint(merge_rig, POLICY_SYSTEM)
    assert len(_policy_system_checkpoints(merge_rig)) == 1


def test_a_merge_that_backfills_requests_the_checkpoint_on_the_backfill_only(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    production_run((_FIRST,), merge_rig, emitter)
    head_after_first = merge_rig.store.last_position(POLICY_SYSTEM)

    production_run((_SECOND,), merge_rig, emitter)

    assert merge_rig.policy_system.nodes[("Capability", _CAP)]["embedding"] is not None
    assert_head_checkpoint(merge_rig, POLICY_SYSTEM)
    # one more checkpoint, not two: the rewired edges deferred to the backfill that followed
    assert len(_policy_system_checkpoints(merge_rig)) == 2
    assert _policy_system_checkpoints(merge_rig)[0] == head_after_first


def test_every_graph_the_merge_wrote_ends_with_a_head_checkpoint(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    production_run((_FIRST, _SECOND), merge_rig, emitter)

    assert_head_checkpoints(merge_rig)


def test_a_checkpoint_that_cannot_be_recorded_does_not_fail_the_merge(
    merge_rig: MergeRig, emitter: LogEmitter
) -> None:
    merge_rig.store.checkpoint_fault = Fault(redis.exceptions.ConnectionError("down"))

    production_run((_FIRST, _SECOND), merge_rig, emitter)

    assert ("Capability", _CAP) in merge_rig.policy_system.nodes
    assert_no_checkpoint(merge_rig)
