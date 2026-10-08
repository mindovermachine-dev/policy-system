"""#199 AC-BI-004: hand-copied status/action Literals equal the domain schema.

`rules.py`, `service.py` and `audit_actions.py` declare their `Literal`s under
`from __future__ import annotations` and/or `TYPE_CHECKING`, so they are not
resolvable at runtime; they are read from source with `ast` (no `eval`).
"""

from __future__ import annotations

import ast
import inspect
from typing import TYPE_CHECKING

import pytest

from ps_service.domain_schema import enum_values
from ps_service.policy_lifecycle import audit_actions, rules, service

if TYPE_CHECKING:
    from types import ModuleType


def _literal_values(annotation: ast.expr) -> tuple[str, ...]:
    """Return the string values of a `Literal[...]` annotation node, in source order.

    Args:
        annotation: The annotation expression; must be a `Literal[...]` subscript.

    Returns:
        The literal string values in declaration order.
    """
    assert isinstance(annotation, ast.Subscript)
    assert isinstance(annotation.value, ast.Name)
    assert annotation.value.id == "Literal"
    elements = (
        annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    )
    values: list[str] = []
    for element in elements:
        assert isinstance(element, ast.Constant)
        assert isinstance(element.value, str)
        values.append(element.value)
    return tuple(values)


def _alias_annotation(module: ModuleType, name: str) -> ast.expr:
    """Return the right-hand side of the `name = Literal[...]` alias in a module's source.

    Args:
        module: The module whose source is parsed.
        name: The alias name.

    Returns:
        The aliased expression.
    """
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.value
    raise AssertionError(f"alias {name} not found in {module.__name__}")


def _field_annotation(module: ModuleType, class_name: str, field: str) -> ast.expr:
    """Return the annotation of `field` on class `class_name` in a module's source.

    Args:
        module: The module whose source is parsed.
        class_name: The class declaring the field.
        field: The annotated attribute name.

    Returns:
        The field's annotation expression.
    """
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and stmt.target.id == field
                ):
                    return stmt.annotation
    raise AssertionError(f"{class_name}.{field} not found in {module.__name__}")


_CONTEXT = "PolicyLifecycleRuleContext"


@pytest.mark.parametrize("label", ["Policy", "Standard", "Control"])
def test_rules_current_status_literal_equals_schema_status_enum(label: str) -> None:
    """#199 AC-BI-004: the rules `current_status` Literal equals each lifecycle label's status enum.

    Args:
        label: A label whose `status` enum the rule context shares.
    """
    literal = _literal_values(_field_annotation(rules, _CONTEXT, "current_status"))
    assert literal == enum_values(label, "status")


def test_rules_action_literal_is_required_status_keys_plus_read() -> None:
    """#199 AC-BI-004: rules `action` Literal is the required-status action keys plus `read`."""
    literal = _literal_values(_field_annotation(rules, _CONTEXT, "action"))
    required = rules._REQUIRED_STATUS_BY_ACTION  # pyright: ignore[reportPrivateUsage]
    assert set(literal) == {*required, "read"}
    assert len(literal) == len(set(literal))


def test_service_policy_status_literal_equals_schema_status_enum() -> None:
    """#199 AC-BI-004: `service.PolicyStatus` equals the Policy `status` enum."""
    literal = _literal_values(_alias_annotation(service, "PolicyStatus"))
    assert literal == enum_values("Policy", "status")


def test_audit_transition_status_literals_match_schema_status_enum() -> None:
    """#199 AC-BI-004: `to_status` is the whole enum; `from_status` omits terminal `deprecated`."""
    statuses = enum_values("Policy", "status")
    to_status = _literal_values(
        _field_annotation(audit_actions, "PolicyTransitionDetails", "to_status")
    )
    from_status = _literal_values(
        _field_annotation(audit_actions, "PolicyTransitionDetails", "from_status")
    )
    assert to_status == statuses
    assert from_status == tuple(s for s in statuses if s != "deprecated")
