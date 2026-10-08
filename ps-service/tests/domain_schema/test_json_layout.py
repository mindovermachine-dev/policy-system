"""`dumps_hand_layout` reproduces the committed intake schema's hand-edited layout."""

from __future__ import annotations

import json
from pathlib import Path

from ps_service.domain_schema.json_layout import EXPANDED_THEN_LABELS, Json, dumps_hand_layout

_COMMITTED = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "artifacts"
    / "schemas"
    / "internal-regulation-intake.v1.schema.json"
)
_WIDTH = 120


def _wrap(value: Json) -> str:
    """Render `value` as the only member of an outer object, so it is not the root."""
    return dumps_hand_layout({"outer": {"k": 1, "inner": value}})


def test_committed_schema_round_trips_byte_for_byte() -> None:
    """AC-BI-005: parse then re-render gives the committed bytes."""
    committed = _COMMITTED.read_bytes()

    rendered = dumps_hand_layout(json.loads(committed))

    assert rendered.encode("utf-8") == committed


def test_output_has_one_trailing_newline_and_keeps_non_ascii() -> None:
    rendered = dumps_hand_layout({"a": "café — ok"})

    assert rendered.endswith("}\n")
    assert not rendered.endswith("\n\n")
    assert "café — ok" in rendered


def test_root_object_is_always_expanded() -> None:
    assert dumps_hand_layout({"a": 1}) == '{\n  "a": 1\n}\n'


def test_scalar_list_renders_inline_without_inner_padding() -> None:
    assert '"inner": ["a", "b"]' in _wrap(["a", "b"])


def test_scalar_list_of_ten_items_stays_inline_even_when_wider_than_the_limit() -> None:
    items = [f"item-number-{n:02d}-padding" for n in range(10)]

    assert f'"inner": {json.dumps(items)}' in _wrap(items)


def test_scalar_list_of_more_than_ten_items_expands_when_too_wide() -> None:
    items = [f"item-number-{n:02d}-padding" for n in range(11)]

    rendered = _wrap(items)

    assert '"inner": [\n' in rendered
    assert '      "item-number-00-padding",\n' in rendered
    assert '      "item-number-10-padding"\n    ]' in rendered


def test_scalar_list_of_more_than_ten_items_stays_inline_when_it_fits() -> None:
    items = [str(n) for n in range(11)]

    assert f'"inner": {json.dumps(items)}' in _wrap(items)


def test_list_containing_containers_is_expanded() -> None:
    rendered = _wrap([{"a": 1}])

    assert '"inner": [\n      { "a": 1 }\n    ]' in rendered


def test_flat_dict_renders_inline_with_padding() -> None:
    assert '"inner": { "type": "string", "minLength": 1 }' in _wrap(
        {"type": "string", "minLength": 1}
    )


def test_dict_wider_than_the_limit_is_expanded() -> None:
    long_value = "x" * _WIDTH

    rendered = _wrap({"d": long_value})

    assert f'"inner": {{\n      "d": "{long_value}"\n    }}' in rendered


def test_dict_with_a_multi_key_dict_value_is_expanded() -> None:
    rendered = _wrap({"a": {"b": 1, "c": 2}})

    assert '"inner": {\n      "a": { "b": 1, "c": 2 }\n    }' in rendered


def test_multi_key_dict_with_any_dict_value_is_expanded() -> None:
    rendered = _wrap({"a": 1, "b": {"c": 2}})

    assert '"inner": {\n      "a": 1,\n      "b": { "c": 2 }\n    }' in rendered


def test_single_key_chain_of_dicts_renders_inline() -> None:
    assert '"inner": { "a": { "b": { "c": 1 } } }' in _wrap({"a": {"b": {"c": 1}}})


def _if_then(label: str) -> dict[str, Json]:
    return {
        "if": {"properties": {"label": {"const": label}}},
        "then": {"properties": {"properties": {"$ref": "#/$defs/x"}}},
    }


def test_then_of_a_hinted_label_is_expanded() -> None:
    label = min(EXPANDED_THEN_LABELS)

    rendered = dumps_hand_layout({"allOf": [_if_then(label)]})

    assert '"then": {\n' in rendered
    assert '"properties": { "properties": { "$ref": "#/$defs/x" } }\n' in rendered


def test_then_of_an_unhinted_label_is_inline() -> None:
    rendered = dumps_hand_layout({"allOf": [_if_then("Unhinted")]})

    assert '"then": { "properties": { "properties": { "$ref": "#/$defs/x" } } }' in rendered


def test_hinted_labels_are_the_four_whose_then_is_expanded_in_the_committed_file() -> None:
    assert (
        frozenset({"RegulatoryInstrument", "Requirement", "Obligation", "Capability"})
        == EXPANDED_THEN_LABELS
    )
