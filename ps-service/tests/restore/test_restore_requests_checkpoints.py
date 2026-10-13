"""A restore requests a digest checkpoint on every leg of its one group (issue #215, S4a).

AC-BI-009: the restore's terminal group asks the gateway to record the digest at the head of every
graph it wrote; a checkpoint that cannot be recorded never fails the restore.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import redis.exceptions
from graph_gateway._fakes import Fault
from graph_gateway.checkpoint_checks import assert_head_checkpoints, assert_no_checkpoint

from ps_service.graph_gateway.models import GraphLeg, MutationGroup, UpsertNode
from ps_service.restore.restore_instrument import (
    _restore_group,  # pyright: ignore[reportPrivateUsage]
)
from restore._fixtures import build_restore_artifact, small_graphs

if TYPE_CHECKING:
    from ps_service.restore.models import RestoreArtifact
    from restore._gateway_fakes import RestoreRig

_INSTRUMENT_ID = "CP215-1.0"
_AUDIT = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"


def _artifact() -> RestoreArtifact:
    native, baseline = small_graphs(instrument_id=_INSTRUMENT_ID)
    return build_restore_artifact(
        instrument_id=_INSTRUMENT_ID,
        short_name="CP215",
        native_graph=native,
        baseline_graph=baseline,
    )


def test_a_restore_leaves_a_head_checkpoint_on_every_graph_it_wrote(
    restore_rig: RestoreRig,
) -> None:
    restore_rig.restore(_artifact())

    assert set(restore_rig.store.logged_graphs()) == {
        "cp215_native",
        "cp215_baseline",
        "policy_system",
    }
    assert_head_checkpoints(restore_rig)


def test_a_checkpoint_that_cannot_be_recorded_does_not_fail_the_restore(
    restore_rig: RestoreRig,
) -> None:
    restore_rig.store.checkpoint_fault = Fault(redis.exceptions.ConnectionError("down"))

    outcome = restore_rig.restore(_artifact())

    assert outcome.instrument_id == _INSTRUMENT_ID
    assert ("Capability", "cap_1") in restore_rig.graph("cp215_baseline").nodes
    assert_no_checkpoint(restore_rig)


def test_a_single_leg_restore_group_keeps_the_legs_checkpoint_request() -> None:
    primitive = UpsertNode(label="Capability", id="c1", properties={"name": "n"})
    leg = GraphLeg(graph="policy_system", primitives=(primitive,), checkpoint_requested=True)

    group = _restore_group(_AUDIT, (leg,))

    assert isinstance(group, MutationGroup)
    assert group.checkpoint_requested is True
