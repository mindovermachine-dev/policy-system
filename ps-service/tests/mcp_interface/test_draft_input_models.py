"""The draft-content input models derive their fields and enums from the domain schema."""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import ValidationError

from ps_service.domain_schema import (
    DateType,
    EnumType,
    Node,
    Presence,
    Property,
    Schema,
    StringType,
    enum_values,
    patchable_fields,
)
from ps_service.mcp_interface.draft_input_models import (
    PolicyDraftControlInput,
    derive_content_fields_model,
)

type _Properties = dict[str, dict[str, object]]


def _schema_properties(label: str) -> tuple[dict[str, object], _Properties]:
    model = derive_content_fields_model(label)
    schema = cast("dict[str, object]", model.model_json_schema())
    return schema, cast("_Properties", schema["properties"])


def test_string_only_model_declares_every_patchable_field_as_nullable_string() -> None:
    schema, properties = _schema_properties("Policy")

    assert set(properties) == patchable_fields("Policy")
    assert schema["additionalProperties"] is False
    for prop in properties.values():
        assert prop["anyOf"] == [{"type": "string"}, {"type": "null"}]
        assert "default" not in prop


def test_enum_property_is_published_with_its_values_in_schema_order() -> None:
    schema, properties = _schema_properties("Standard")

    assert set(properties) == patchable_fields("Standard")
    assert schema["additionalProperties"] is False
    status = properties["implementation_status"]
    assert status["anyOf"] == [
        {"enum": list(enum_values("Standard", "implementation_status")), "type": "string"},
        {"type": "null"},
    ]
    assert "default" not in status


def test_supplied_keeps_an_explicit_null_and_omits_absent_keys() -> None:
    model = derive_content_fields_model("Policy")

    parsed = model.model_validate({"description": None, "scope_in": "kept"})

    assert parsed.supplied() == {"description": None, "scope_in": "kept"}
    assert model.model_validate({}).supplied() == {}


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("Policy", {"title": "x"}),
        ("Policy", {"description": 3}),
        ("Standard", {"implementation_status": "bogus"}),
    ],
)
def test_model_rejects_unknown_keys_non_strings_and_out_of_enum_values(
    label: str, payload: dict[str, object]
) -> None:
    model = derive_content_fields_model(label)

    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_factory_follows_the_supplied_schema() -> None:
    altered = Schema(
        nodes=(
            Node(
                "Widget",
                (
                    Property("name", StringType(), Presence.REQUIRED, is_identity_bearing=True),
                    Property("note", StringType(), Presence.OPTIONAL),
                    Property("tier", EnumType(("gold", "silver", "bronze")), Presence.OPTIONAL),
                    Property("due", DateType(), Presence.OPTIONAL),
                ),
            ),
        )
    )

    model = derive_content_fields_model("Widget", schema=altered)
    properties = cast("_Properties", model.model_json_schema()["properties"])

    assert set(properties) == {"note", "tier", "due"}
    assert properties["tier"]["anyOf"] == [
        {"enum": ["gold", "silver", "bronze"], "type": "string"},
        {"type": "null"},
    ]
    assert properties["due"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    with pytest.raises(ValidationError):
        model.model_validate({"tier": "platinum"})


def test_new_policy_control_types_equal_the_schema_enum_in_order() -> None:
    schema = cast("dict[str, object]", PolicyDraftControlInput.model_json_schema())
    control_type = cast("dict[str, dict[str, object]]", schema["properties"])["control_type"]

    assert control_type["enum"] == list(enum_values("Control", "type"))
