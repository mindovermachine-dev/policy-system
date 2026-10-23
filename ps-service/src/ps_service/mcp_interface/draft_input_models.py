"""Typed input models of the policy-draft MCP tools.

The `fields` argument of each draft-content tool is a partial map over the content properties of a
Policy, Standard or Control. Its model is derived from the domain schema: the property names are
`patchable_fields(label)`, and an enum-typed property is restricted to its values in schema order,
so the input schema a client reads from `tools/list` cannot drift from the graph schema. Every
other property is a nullable string.

Business Rules:
    - Unknown keys, non-string values and out-of-enum values are rejected by validation.
    - A key sent as JSON `null` is distinct from an absent key: `supplied()` keeps the former
      (clear on update, leave unset on create) and omits the latter (leave any value untouched).
    - Enum properties are typed as `Literal` unions of the schema's values.
    - The `standards` tree of `create-policy-draft` is a static model: every entry needs a non-empty
      `title`, unknown keys are rejected, and a Control's `control_type` is `"automated"` or
      `"manual"` (default `"manual"`).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, create_model

from ps_service.domain_schema import DOMAIN_SCHEMA, patchable_fields, property_named
from ps_service.domain_schema.model import EnumType, Schema
from ps_service.policy_lifecycle.service import ControlDraftInput, StandardDraftInput


class PatchBase(BaseModel):
    """A partial update of a node's content fields; unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    def supplied(self) -> dict[str, str | None]:
        """Return the fields the caller sent, keyed by name.

        Returns:
            Every field that was present in the input, an explicit `None` included; absent fields
            are omitted.
        """
        return self.model_dump(exclude_unset=True)


def _drop_default(schema: dict[str, Any]) -> None:
    """Remove `default` from a property schema: an omitted key is not the same as `null`."""
    schema.pop("default", None)


def _annotation(label: str, name: str, schema: Schema) -> object:
    """Return the nullable annotation of property `name`: its enum values, or `str`."""
    prop_type = property_named(label, name, schema).type
    if isinstance(prop_type, EnumType):
        values = cast("Any", Literal[prop_type.values])  # pyright: ignore[reportInvalidTypeForm]  # the allowed values are only known from the schema at import time
        return values | None
    return str | None


def derive_content_fields_model(
    label: str,
    *,
    exclude: frozenset[str] = frozenset(),
    schema: Schema = DOMAIN_SCHEMA,
) -> type[PatchBase]:
    """Build the `fields` model for the patchable properties of node `label`.

    Args:
        label: A node label of `schema`.
        exclude: Patchable property names to leave out of the model.
        schema: The schema to derive from; defaults to `DOMAIN_SCHEMA`.

    Returns:
        A `PatchBase` subclass with one optional field per remaining patchable property, in name
        order, whose default is `None` and whose published schema carries no `default`.

    Raises:
        DomainSchemaError: `label` is not declared in `schema`.
    """
    definitions = {
        name: (
            _annotation(label, name, schema),
            Field(default=None, json_schema_extra=_drop_default),
        )
        for name in sorted(patchable_fields(label, schema) - exclude)
    }
    return create_model(f"{label}ContentFieldsBase", __base__=PatchBase, **cast("Any", definitions))


class PolicyContentFields(derive_content_fields_model("Policy")):
    """Content fields of a Policy: omit a key to leave it unchanged, send null to clear it."""


class StandardContentFields(derive_content_fields_model("Standard")):
    """Content fields of a Standard: omit a key to leave it unchanged, send null to clear it."""


class ControlContentFields(derive_content_fields_model("Control")):
    """Content fields of a Control: omit a key to leave it unchanged, send null to clear it."""


class NewControlContentFields(derive_content_fields_model("Control", exclude=frozenset({"type"}))):
    """Content fields of a new Control, without `type`: omit a key to leave it unset."""


class PolicyDraftControlInput(BaseModel):
    """One Control to create under a Standard when a draft Policy is created."""

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(min_length=1)]
    control_type: Literal["automated", "manual"] = "manual"

    def to_draft_input(self) -> ControlDraftInput:
        """Return the service input for this Control."""
        return ControlDraftInput(title=self.title, control_type=self.control_type)


class PolicyDraftStandardInput(BaseModel):
    """One Standard, with its optional Controls, to create when a draft Policy is created."""

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(min_length=1)]
    controls: list[PolicyDraftControlInput] = Field(default_factory=list)

    def to_draft_input(self) -> StandardDraftInput:
        """Return the service input for this Standard and its Controls."""
        return StandardDraftInput(
            title=self.title,
            controls=tuple(control.to_draft_input() for control in self.controls),
        )
