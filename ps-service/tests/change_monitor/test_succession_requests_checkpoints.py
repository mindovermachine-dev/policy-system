"""The succession's terminal groups request a digest checkpoint.

Terminal = the last group a re-ingest writes to a graph: `supersede_in_single_tenant` on
`policy_system` and `clear_marker` on the native graph. `mark_stage_complete`, `link_and_supersede`
and `set_new_version_property` are not. `clear_marker` of an absent marker logs nothing, so the
head does not move and the checkpoint the graph already has stays valid.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import redis.exceptions
from graph_gateway._fakes import Fault, GatewayRig
from graph_gateway.checkpoint_checks import (
    assert_head_checkpoint,
    assert_no_checkpoint,
    head_checkpoint_digest,
)

from change_monitor._gateway_fakes import SuccessionRig
from ps_service.change_monitor.graph_submission import SuccessionTarget
from ps_service.change_monitor.succession import (
    clear_marker,
    link_and_supersede,
    mark_stage_complete,
    set_new_version_property,
    supersede_in_single_tenant,
)
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.models import MutationGroup, UpsertNode

if TYPE_CHECKING:
    from collections.abc import Iterator

    from change_monitor._fakes import MakeEmitter

import pytest

_NATIVE = "cra_native"
_POLICY = "policy_system"
_PRIOR = "CRA-1.0"
_NEW = "CRA-2.0"


@pytest.fixture
def rig() -> Iterator[GatewayRig]:
    from graph_gateway._fakes import close_all_rigs

    gateway_rig = GatewayRig()
    yield gateway_rig
    close_all_rigs()


def _target(rig: GatewayRig, make_emitter: MakeEmitter) -> SuccessionTarget:
    audit_event_id = str(uuid.uuid4())
    rig.store.audit_rows.add(audit_event_id)
    return SuccessionTarget(
        rig.gateway, _NATIVE, _POLICY, lambda: audit_event_id, make_emitter()[0]
    )


def _seed_native(rig: GatewayRig, *, marker: str | None = None) -> None:
    native = rig.graphs.open(_NATIVE)
    SuccessionRig.seed_instrument(native, _PRIOR)
    SuccessionRig.seed_instrument(native, _NEW)
    if marker is not None:
        SuccessionRig.seed_marker(native, _NEW, marker)


def _seed_policy(rig: GatewayRig) -> None:
    policy = rig.graphs.open(_POLICY)
    SuccessionRig.seed_instrument(policy, _PRIOR)
    SuccessionRig.seed_instrument(policy, _NEW)


def test_superseding_in_policy_system_requests_a_head_checkpoint(
    rig: GatewayRig, make_emitter: MakeEmitter
) -> None:
    _seed_policy(rig)

    supersede_in_single_tenant(_target(rig, make_emitter), _PRIOR, _NEW)

    assert_head_checkpoint(rig, _POLICY)


def test_clearing_the_marker_requests_a_head_checkpoint_on_the_native_graph(
    rig: GatewayRig, make_emitter: MakeEmitter
) -> None:
    _seed_native(rig, marker="linked")

    clear_marker(_target(rig, make_emitter), _NEW)

    assert "ReingestProgress" not in {label for label, _ in rig.graphs.open(_NATIVE).nodes}
    assert_head_checkpoint(rig, _NATIVE)


def test_clearing_an_absent_marker_moves_no_head_so_the_earlier_checkpoint_stays_valid(
    rig: GatewayRig, make_emitter: MakeEmitter
) -> None:
    _seed_native(rig)
    rig.gateway.submit_group(
        MutationGroup(
            graph=_NATIVE,
            audit_event_id=_target(rig, make_emitter).audit_anchor(),
            primitives=(UpsertNode(label="ARTICLE", id="a1", properties={"text": "t"}),),
            checkpoint_requested=True,
        )
    )
    before = head_checkpoint_digest(rig, _NATIVE)
    assert before is not None

    clear_marker(_target(rig, make_emitter), _NEW)

    assert head_checkpoint_digest(rig, _NATIVE) == before
    assert before == canonical_digest(rig.graphs.open(_NATIVE))


def test_the_non_terminal_succession_steps_request_no_checkpoint(
    rig: GatewayRig, make_emitter: MakeEmitter
) -> None:
    _seed_native(rig)
    target = _target(rig, make_emitter)

    mark_stage_complete(target, _NEW, "ingestion")
    set_new_version_property(target, _NEW, "2.0")
    link_and_supersede(target, _PRIOR, _NEW)

    assert rig.store.last_position(_NATIVE) > 0
    assert_no_checkpoint(rig)


def test_a_checkpoint_that_cannot_be_recorded_does_not_fail_the_succession(
    rig: GatewayRig, make_emitter: MakeEmitter
) -> None:
    _seed_policy(rig)
    rig.store.checkpoint_fault = Fault(redis.exceptions.ConnectionError("down"))

    supersede_in_single_tenant(_target(rig, make_emitter), _PRIOR, _NEW)

    assert SuccessionRig.status_of(rig.graphs.open(_POLICY), _PRIOR) == "superseded"
    assert_no_checkpoint(rig)
