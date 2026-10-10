"""D6 (#207): the replay sentinel label is never reachable by ordinary writers."""

from __future__ import annotations

import pytest

from graph_gateway._fakes import GatewayRig
from ps_service.domain_schema import vocabulary_exceptions as exceptions
from ps_service.graph_gateway import label_allow_list
from ps_service.graph_gateway.errors import UnlistedNameError
from ps_service.graph_gateway.models import DeleteNode, MutationGroup, UpsertNode

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_SENTINEL = "GraphReplayState"


def test_sentinel_label_not_in_allow_list() -> None:
    assert _SENTINEL not in label_allow_list.ALLOWED_NODE_LABELS
    assert _SENTINEL not in exceptions.OPERATIONAL_LABELS
    assert _SENTINEL in exceptions.REPLAY_SENTINEL_LABELS


def test_upsert_of_sentinel_label_is_rejected() -> None:
    rig = GatewayRig()
    group = MutationGroup(
        graph="compliance",
        audit_event_id=_AUDIT_EVENT_ID,
        primitives=(UpsertNode(label=_SENTINEL, id="forged"),),
    )

    with pytest.raises(UnlistedNameError):
        rig.gateway.submit_group(group)

    assert rig.store.entries == {}


def test_delete_of_sentinel_label_is_rejected() -> None:
    rig = GatewayRig()
    group = MutationGroup(
        graph="compliance",
        audit_event_id=_AUDIT_EVENT_ID,
        primitives=(DeleteNode(label=_SENTINEL, id="x"),),
    )

    with pytest.raises(UnlistedNameError):
        rig.gateway.submit_group(group)

    assert rig.store.entries == {}
