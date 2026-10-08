"""AC-BI-013 / AC-BI-022: `NodeLabel`/`EdgeType` are pinned to the code-defined domain schema.

The two `Literal` types are hand-written (pydantic needs them statically), so
this module is the single place that proves they cannot drift from
`DOMAIN_SCHEMA`; the docstrings in `internal_seed/models.py` point here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

from ps_service.domain_schema import DOMAIN_SCHEMA
from ps_service.domain_schema.intake import generate_intake_schema
from ps_service.domain_schema.vocabulary_exceptions import SYSTEM_MINTED_EDGE_TYPES, find_unpinned
from ps_service.ingestion.adapters.internal_seed import models
from ps_service.ingestion.adapters.internal_seed.models import EdgeType, NodeLabel

_SCHEMA_LABELS = frozenset(node.label for node in DOMAIN_SCHEMA.nodes)
_SCHEMA_EDGE_TYPES = frozenset(edge.type for edge in DOMAIN_SCHEMA.edges)
_MODELS_SOURCE = Path(models.__file__).read_text(encoding="utf-8")


def test_find_unpinned_reports_an_injected_name() -> None:
    assert find_unpinned(["A", "B", "Extra"], {"A", "B"}, ()) == ("Extra",)


def test_find_unpinned_accepts_a_named_exception() -> None:
    assert find_unpinned(["A", "Extra"], {"A"}, {"Extra": "reason"}) == ()


def test_node_label_equals_schema_labels() -> None:
    assert frozenset[str](get_args(NodeLabel)) == _SCHEMA_LABELS


def test_every_node_label_member_is_in_the_schema() -> None:
    assert find_unpinned(get_args(NodeLabel), _SCHEMA_LABELS, ()) == ()


def test_every_edge_type_member_is_in_the_schema() -> None:
    assert find_unpinned(get_args(EdgeType), _SCHEMA_EDGE_TYPES, ()) == ()


def test_every_schema_edge_type_is_an_edge_type_or_system_minted() -> None:
    assert (
        find_unpinned(
            _SCHEMA_EDGE_TYPES, frozenset[str](get_args(EdgeType)), SYSTEM_MINTED_EDGE_TYPES
        )
        == ()
    )


def test_system_minted_exceptions_are_not_stale() -> None:
    members = frozenset[str](get_args(EdgeType))
    for name in SYSTEM_MINTED_EDGE_TYPES:
        assert name in _SCHEMA_EDGE_TYPES, f"{name} left the schema; drop the exception"
        assert name not in members, f"{name} is now an intake EdgeType; drop the exception"


def test_generated_intake_schema_vocabulary_equals_the_literal_types() -> None:
    document = json.loads(generate_intake_schema(DOMAIN_SCHEMA))
    defs = document["$defs"]

    assert frozenset(defs["nodeLabel"]["enum"]) == frozenset[str](get_args(NodeLabel))
    assert frozenset(defs["edge"]["properties"]["type"]["enum"]) == frozenset[str](
        get_args(EdgeType)
    )


def test_no_docstring_claims_to_mirror_the_json_schema() -> None:
    assert "mirrors the packaged JSON Schema" not in _MODELS_SOURCE
    assert _MODELS_SOURCE.count("test_models_vocabulary_pin") == 2
