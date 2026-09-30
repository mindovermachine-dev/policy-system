"""Typed `details` models and the extensible per-action/resource-type registries (issue #147).

`AuditDetails` is the base Pydantic type every action's typed `details`
payload subclasses; `register_audit_action`/`resolve_details_model` form a
plain module-level registry, not a closed enum, so a new action can be
registered later from another module entirely -- e.g. #134's policy-lifecycle
actions, #136's supersede-fork action, #140's `user.invite` action -- without
ever editing this file (the access-role component registers its four `access_role.*`
actions the same way). Mirrors
`ps_service.mcp_interface.mcp_server`'s own "module import triggers
registration" idiom for `@server.tool()`.

`register_audit_resource_type`/`is_known_resource_type` (Slice 4) are the
same extensibility contract applied to `list-audit-events`'s `resource_type`
filter (AC-BI-008's "unknown resource type" rejection needs some
registered/known set to check against).

`AuditEventRow`/`AuditQueryFilters`/`AuditQueryPage` (Slice 4) are the shapes
`AuditStore.query` accepts/returns -- plain frozen dataclasses, not
pydantic, mirroring the other `ps_service` components' own "plain frozen
dataclass, not LLM/API-boundary Pydantic" convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Literal


class AuditDetails(BaseModel):
    """Base type for every action's typed `details` payload (AC-BI-009).

    Every subclass sets `model_config = ConfigDict(extra="forbid")` (already
    set here, inherited by every subclass) so `details` can only ever
    contain fields the model declares -- the validation control behind
    AC-BI-009's "details can only contain declared, typed fields."
    """

    model_config = ConfigDict(extra="forbid")


_ACTION_DETAILS_REGISTRY: dict[str, type[AuditDetails]] = {}
_RESOURCE_TYPES: set[str] = set()


def register_audit_action(action: str, details_model: type[AuditDetails]) -> None:
    """Register `details_model` as the one typed shape `action`'s `details` must match.

    Called once, at import time, by the component that emits `action` (the
    access-role component's own audit-actions module; future components
    register their own the same way) -- a plain module-level dict, never a closed
    enum, so a new action never requires editing this module.

    Raises:
        ValueError: `action` is already registered -- defends against two
            components silently colliding on one namespaced action string.
    """
    if action in _ACTION_DETAILS_REGISTRY:
        message = f"audit action {action!r} is already registered"
        raise ValueError(message)
    _ACTION_DETAILS_REGISTRY[action] = details_model


def resolve_details_model(action: str) -> type[AuditDetails] | None:
    """Return the typed `details` model registered for `action`, or `None` if unregistered."""
    return _ACTION_DETAILS_REGISTRY.get(action)


def register_audit_resource_type(resource_type: str) -> None:
    """Register `resource_type` as a name `list-audit-events` may filter on (AC-BI-008).

    Same extensibility contract as `register_audit_action` -- a plain
    module-level set, never a closed enum, so a new resource type never
    requires editing this module. Unlike `register_audit_action`, silently
    accepts re-registering the same name (multiple actions from the same
    component may share one resource type, e.g. the four `access_role.*` actions
    all use `"principal"` -- there is nothing to
    defend against by rejecting that).
    """
    _RESOURCE_TYPES.add(resource_type)


def is_known_resource_type(resource_type: str) -> bool:
    """Whether `resource_type` has been registered via `register_audit_resource_type`."""
    return resource_type in _RESOURCE_TYPES


@dataclass(frozen=True, slots=True)
class AuditEventRow:
    """One `audit_events` row, as returned by `AuditStore.query` (issue #147, Slice 4).

    Mirrors the other `ps_service` components' own "plain frozen
    dataclass, not LLM/API-boundary Pydantic" convention.
    """

    id: str
    occurred_at: datetime
    actor_subject: str
    actor_issuer: str
    action: str
    resource_type: str
    resource_id: str
    outcome: Literal["applied", "rejected", "failed"]
    details: dict[str, object]


@dataclass(frozen=True, slots=True)
class AuditQueryFilters:
    """Every filter `list-audit-events` accepts (AC-BI-007), all optional/combinable."""

    actor_subject: str | None = None
    actor_issuer: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    action: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuditQueryPage:
    """One page of `AuditStore.query`'s result: newest-first rows plus the next cursor."""

    events: tuple[AuditEventRow, ...]
    next_cursor: str | None


__all__ = [
    "AuditDetails",
    "AuditEventRow",
    "AuditQueryFilters",
    "AuditQueryPage",
    "is_known_resource_type",
    "register_audit_action",
    "register_audit_resource_type",
    "resolve_details_model",
]
