"""#199 property flags: identity-bearing and lifecycle-managed markers on the real schema."""

from __future__ import annotations

import pytest

from ps_service.domain_schema import DOMAIN_SCHEMA, Node, Omit, Presence, Property, StringType
from ps_service.domain_schema.intake import INTAKE_PROFILE

_IDENTITY_BEARING = {
    ("Policy", "title"),
    ("Policy", "owner_id"),
    ("Policy", "owner_subject"),
    ("Policy", "owner_issuer"),
    ("Standard", "title"),
    ("Control", "title"),
}
_LIFECYCLE_MANAGED = {
    ("Policy", "status"),
    ("Policy", "version"),
    ("Standard", "status"),
    ("Standard", "version"),
    ("Control", "status"),
}


def _node(label: str) -> Node:
    [node] = [candidate for candidate in DOMAIN_SCHEMA.nodes if candidate.label == label]
    return node


def _property(label: str, name: str) -> Property:
    [prop] = [candidate for candidate in _node(label).properties if candidate.name == name]
    return prop


@pytest.mark.parametrize("label", ["Policy", "Standard", "Control"])
def test_flags_match_the_documented_exclusions(label: str) -> None:
    """#199 AC-BI-001: flagged properties are exactly the ones the lifecycle service excludes."""
    for prop in _node(label).properties:
        assert prop.is_identity_bearing == ((label, prop.name) in _IDENTITY_BEARING)
        assert prop.is_lifecycle_managed == ((label, prop.name) in _LIFECYCLE_MANAGED)


@pytest.mark.parametrize(
    ("label", "name"),
    [
        ("Control", "type"),
        ("Standard", "implementation_status"),
        ("Control", "implementation_status"),
    ],
)
def test_writable_workflow_properties_are_unflagged(label: str, name: str) -> None:
    """#199 AC-BI-001: type and implementation_status stay content-writable."""
    prop = _property(label, name)

    assert not prop.is_identity_bearing
    assert not prop.is_lifecycle_managed


def test_policy_defines_owner_subject_and_issuer_after_owner_id() -> None:
    """#199 AC-BI-009: authz owner fields are optional identity-bearing strings after owner_id."""
    names = [prop.name for prop in _node("Policy").properties]

    assert names.index("owner_subject") == names.index("owner_id") + 1
    assert names.index("owner_issuer") == names.index("owner_subject") + 1
    for name in ("owner_subject", "owner_issuer"):
        prop = _property("Policy", name)
        assert prop.presence is Presence.OPTIONAL
        assert isinstance(prop.type, StringType)
        assert prop.is_identity_bearing


def test_intake_profile_omits_authz_owner_fields() -> None:
    """#199 AC-BI-009: a submitter cannot set authz ownership through intake."""
    omitted = {
        (op.label, op.property_name) for op in INTAKE_PROFILE.operations if isinstance(op, Omit)
    }

    assert ("Policy", "owner_subject") in omitted
    assert ("Policy", "owner_issuer") in omitted
