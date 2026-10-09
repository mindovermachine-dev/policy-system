"""Gateway walking skeleton: submit an upsert-node group through log, apply and outcome (#206).

Every test drives the public `GraphWriteGateway.submit_group` against the real validation models,
entry codec and Cypher builders, with the log store and the graph replaced by Protocol fakes.
Covers AC-BI-003, AC-BI-007 and AC-BI-013 (seed).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import ValidationError

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway import gateway as gateway_module
from ps_service.graph_gateway.models import MutationGroup, UpsertNode

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.logging import LogEmitter

    MakeEmitter = Callable[[], tuple[LogEmitter, Path]]
    ReadLines = Callable[[Path], list[dict[str, object]]]

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_EMBEDDING = (0.1, -0.0, 1.7976931348623157e308, 5e-324, 0.30000000000000004)


def _node(node_id: str, **extra: object) -> UpsertNode:
    return UpsertNode(label="Capability", id=node_id, properties={"name": node_id, **extra})


def _group(*nodes: UpsertNode) -> MutationGroup:
    return MutationGroup(graph=_GRAPH, audit_event_id=_AUDIT_EVENT_ID, primitives=nodes)


_Rig = GatewayRig


def test_submit_group_commits_to_log_then_applies_and_returns_applied() -> None:
    rig = _Rig()

    outcome = rig.gateway.submit_group(_group(_node("cap-1"), _node("cap-2")))

    assert rig.events == ["graph_read", "log_append", "graph_write"]
    assert (outcome.graph, outcome.first_position, outcome.last_position) == (_GRAPH, 1, 2)
    assert outcome.status == "applied"
    assert rig.store.read_applied_position(_GRAPH) == rig.store.last_position(_GRAPH) == 2
    assert set(rig.graphs.open(_GRAPH).nodes) == {("Capability", "cap-1"), ("Capability", "cap-2")}
    assert rig.store.groups[0].audit_event_id == _AUDIT_EVENT_ID


@pytest.mark.parametrize(
    "fields",
    [
        {"label": "Capability", "properties": {}},
        {"label": "Capability", "id": "  ", "properties": {}},
        {"label": "Capability", "id": "", "properties": {}},
    ],
)
def test_submit_group_rejects_group_missing_caller_ids(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        UpsertNode.model_validate(fields)


def test_mutation_group_requires_the_causing_command_id() -> None:
    primitive = _node("cap-1").model_dump()
    with pytest.raises(ValidationError):
        MutationGroup.model_validate({"graph": _GRAPH, "primitives": [primitive]})
    with pytest.raises(ValidationError):
        MutationGroup.model_validate(
            {"graph": _GRAPH, "audit_event_id": "not-a-uuid", "primitives": [primitive]}
        )


def test_gateway_log_entries_carry_only_caller_supplied_values_byte_for_byte() -> None:
    rig = _Rig()
    node = UpsertNode(
        label="Capability",
        id="cap-1",
        properties={"name": "Retention", "weight": 1.0, "tags": ["a", "b"]},
        embedding=_EMBEDDING,
    )

    rig.gateway.submit_group(_group(node))

    (entry,) = rig.store.entries[_GRAPH]
    assert (entry.name, entry.identity) == ("Capability", "cap-1")
    assert entry.content == {
        "op": "upsert_node",
        "properties": {"name": "Retention", "weight": 1.0, "tags": ["a", "b"]},
    }
    assert json.dumps(entry.content["properties"]) == (
        '{"name": "Retention", "weight": 1.0, "tags": ["a", "b"]}'
    )
    assert entry.embedding == _EMBEDDING


def test_applied_node_embedding_reads_back_from_graph_bit_identical() -> None:
    rig = _Rig()
    node = UpsertNode(label="Capability", id="cap-1", embedding=_EMBEDDING)

    rig.gateway.submit_group(_group(node))

    stored = rig.graphs.open(_GRAPH).nodes[("Capability", "cap-1")]["embedding"]
    assert isinstance(stored, list)
    assert [float(v).hex() for v in cast("list[float]", stored)] == [v.hex() for v in _EMBEDDING]


def test_row_without_embedding_never_wipes_an_existing_embedding() -> None:
    rig = _Rig()
    rig.gateway.submit_group(_group(UpsertNode(label="Capability", id="c", embedding=(1.0, 2.0))))

    rig.gateway.submit_group(_group(_node("c", extra="x")))

    assert rig.graphs.open(_GRAPH).nodes[("Capability", "c")]["embedding"] == [1.0, 2.0]


@pytest.mark.parametrize(
    "properties",
    [
        {"nested": {"a": 1}},
        {"nothing": None},
        {"mixed": [1, "a"]},
        {"matrix": [[1], [2]]},
        {"empty": []},
        {"huge": 2**63},
        {"not_finite": float("nan")},
        {"id": "other"},
        {"embedding": [1.0]},
    ],
)
def test_nested_map_property_rejected_before_logging(properties: dict[str, object]) -> None:
    rig = _Rig()

    with pytest.raises(ValidationError):
        rig.gateway.submit_group(
            _group(UpsertNode(label="Capability", id="cap-1", properties=properties))
        )

    assert rig.store.entries == {}
    assert rig.events == []


def test_submit_group_emits_apply_log_entry_with_graph_and_positions_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = _Rig(emitter)
    secret = "sentinel-secret-payload"

    rig.gateway.submit_group(_group(_node("cap-1", note=secret)))
    emitter.flush()

    lines = [line for line in read_lines(log_path) if line.get("action") == "apply_group"]
    assert len(lines) == 1
    line = lines[0]
    assert (line["component"], line["outcome"]) == ("graph_gateway", "success")
    assert (line["graph"], line["first_position"], line["last_position"]) == (_GRAPH, 1, 1)
    assert line["audit_event_id"] == _AUDIT_EVENT_ID
    assert secret not in json.dumps(line)
    assert "properties" not in line
    assert "embedding" not in line


_GATEWAY_MODULES = ("gateway", "entry_codec", "cypher", "applier")
_FORBIDDEN_IMPORTS = {"uuid", "time", "datetime", "random", "secrets"}


def _imported_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    return roots


def test_gateway_modules_do_not_import_id_time_or_randomness_sources() -> None:
    package = Path(gateway_module.__file__).parent
    for name in _GATEWAY_MODULES:
        source = (package / f"{name}.py").read_text(encoding="utf-8")
        assert not _imported_roots(source) & _FORBIDDEN_IMPORTS, name
