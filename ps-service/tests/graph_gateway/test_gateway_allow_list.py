"""Label allow-list of the Graph Write Gateway (issue #206, AC-BI-001).

The allow-list is derived from `DOMAIN_SCHEMA` plus the three named exception sets; these tests
recompute it independently, drive rejection through the public `submit_group`, and check the
point-of-use guard in the Cypher builder.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from graph_gateway._fakes import GatewayRig
from ps_service.domain_schema import DOMAIN_SCHEMA
from ps_service.domain_schema import vocabulary_exceptions as exceptions
from ps_service.graph_gateway import label_allow_list
from ps_service.graph_gateway.cypher import require_safe_identifier, upsert_node_query
from ps_service.graph_gateway.errors import GraphWriteRejectedError, UnlistedNameError
from ps_service.graph_gateway.models import MutationGroup, UpsertNode

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging import LogEmitter

    MakeEmitter = Callable[[], tuple[LogEmitter, Path]]
    ReadLines = Callable[[Path], list[dict[str, object]]]

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_OFF_LIST = "SecretCustomerLabel"


class _Rig(GatewayRig):
    def submit(self, graph: str, *labels: str) -> None:
        nodes = tuple(UpsertNode(label=label, id=f"{label}-1") for label in labels)
        self.gateway.submit_group(
            MutationGroup(graph=graph, audit_event_id=_AUDIT_EVENT_ID, primitives=nodes)
        )


def test_group_naming_unlisted_label_is_rejected_before_logging() -> None:
    rig = _Rig()

    with pytest.raises(UnlistedNameError) as raised:
        rig.submit("compliance", "Capability", _OFF_LIST)

    assert isinstance(raised.value, GraphWriteRejectedError)
    assert _OFF_LIST not in str(raised.value)
    assert rig.store.entries == {}
    assert rig.events == []


def test_native_structural_labels_accepted_but_not_exempt() -> None:
    rig = _Rig()

    rig.submit("gdpr_native", *exceptions.CELLAR_ELI_NATIVE_LABELS)
    with pytest.raises(UnlistedNameError):
        rig.submit("gdpr_native", "TITLE", _OFF_LIST)

    assert {name for name, _ in rig.graphs.open("gdpr_native").nodes} == set(
        exceptions.CELLAR_ELI_NATIVE_LABELS
    )


def test_operational_labels_are_accepted() -> None:
    rig = _Rig()

    rig.submit("compliance", *exceptions.OPERATIONAL_LABELS)

    assert rig.store.last_position("compliance") == len(exceptions.OPERATIONAL_LABELS)


def test_allow_list_is_union_of_domain_schema_and_named_sets() -> None:
    expected_nodes = (
        {node.label for node in DOMAIN_SCHEMA.nodes}
        | set(exceptions.OPERATIONAL_LABELS)
        | set(exceptions.CELLAR_ELI_NATIVE_LABELS)
    )
    expected_edges = {edge.type for edge in DOMAIN_SCHEMA.edges} | set(
        exceptions.SYSTEM_MINTED_EDGE_TYPES
    )

    assert frozenset(expected_nodes) == label_allow_list.ALLOWED_NODE_LABELS
    assert frozenset(expected_edges) == label_allow_list.ALLOWED_RELATIONSHIP_TYPES
    assert {"ReingestProgress", "MergedObligation", "PendingReview"} <= expected_nodes


def test_allow_list_has_no_runtime_registration_api() -> None:
    public = [name for name in vars(label_allow_list) if not name.startswith("_")]

    assert not [n for n in public if n.startswith(("register", "add", "extend", "allow"))]
    assert isinstance(label_allow_list.ALLOWED_NODE_LABELS, frozenset)
    assert isinstance(label_allow_list.ALLOWED_RELATIONSHIP_TYPES, frozenset)


def test_cypher_builder_rechecks_the_allow_list_at_point_of_use() -> None:
    with pytest.raises(UnlistedNameError):
        upsert_node_query(_OFF_LIST)
    with pytest.raises(UnlistedNameError):
        upsert_node_query("Capability) DETACH DELETE (n")
    with pytest.raises(ValueError, match="identifier"):
        require_safe_identifier("Capability) DETACH DELETE (n")


def test_rejection_log_entry_carries_the_error_class_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = _Rig(emitter)

    with pytest.raises(UnlistedNameError):
        rig.submit("compliance", _OFF_LIST)
    emitter.flush()

    (line,) = [x for x in read_lines(log_path) if x.get("action") == "submit_rejected"]
    assert (line["component"], line["outcome"]) == ("graph_gateway", "failure")
    assert (line["graph"], line["error_class"]) == ("compliance", "UnlistedNameError")
    assert _OFF_LIST not in json.dumps(line)


def test_vocabulary_exceptions_docstring_states_runtime_use_and_restore_independence() -> None:
    doc = exceptions.__doc__ or ""

    assert "Graph Write Gateway" in doc
    assert "restore allow-lists must stay independent" in doc
    assert "Imported by tests only" not in doc
