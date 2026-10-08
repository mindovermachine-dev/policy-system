"""Policy `status` in the schema covers the statuses the lifecycle code uses (PLAN D4).

The document's Policy table once omitted `proposed`; the lifecycle moves Policies
`draft -> proposed -> approved|draft` and `approved -> deprecated`. This pins the one
fact that was stale so the schema cannot silently diverge from `rules.py` again.
"""

from __future__ import annotations

from ps_service.domain_schema import DOMAIN_SCHEMA, EnumType
from ps_service.policy_lifecycle import rules


def _policy_status_values() -> tuple[str, ...]:
    [policy] = [node for node in DOMAIN_SCHEMA.nodes if node.label == "Policy"]
    [status] = [prop for prop in policy.properties if prop.name == "status"]
    assert isinstance(status.type, EnumType)
    return status.type.values


def test_policy_status_enum_matches_lifecycle_rules() -> None:
    """AC-BI-001: every status an action requires is a schema status, including `proposed`."""
    required_by_actions = set(
        rules._REQUIRED_STATUS_BY_ACTION.values()  # pyright: ignore[reportPrivateUsage]  # pin test reads the lifecycle's own status table by design
    )

    assert required_by_actions <= set(_policy_status_values())
    assert "proposed" in required_by_actions


def test_policy_status_enum_is_the_full_lifecycle_order() -> None:
    """AC-BI-001: the schema lists draft, proposed, approved, deprecated in lifecycle order."""
    assert _policy_status_values() == ("draft", "proposed", "approved", "deprecated")
