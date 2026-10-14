"""Typed `details` models and the extensible action and resource-type registries for audit rows.

`AuditDetails` is the base Pydantic type that every action's typed `details` payload subclasses.
`register_audit_action` and `resolve_details_model` form a plain module-level registry, not a
closed enum, so any component can register its own actions at import time without editing this
module. `register_audit_resource_type` and `is_known_resource_type` apply the same contract to
the `resource_type` values that audit queries may filter on.

`AuditEventRow`, `AuditQueryFilters` and `AuditQueryPage` are the shapes that
`AuditStore.query` accepts and returns: plain frozen dataclasses, not Pydantic models, because
they never cross the LLM or HTTP boundary directly. `AUDIT_DETAILS_FILTER_KEYS` is the allow-list
of `details` keys that queries may filter on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from typing import Literal


class AuditDetails(BaseModel):
    """Base type for every action's typed `details` payload.

    Subclass it once per audit action and declare each permitted `details` field with a type.

    Business Rules:
        `model_config` forbids extra fields and is inherited by every subclass, so `details`
        can only contain fields the model declares. Validation fails on an undeclared field
        and on a missing required one.
    """

    model_config = ConfigDict(extra="forbid")


_ACTION_DETAILS_REGISTRY: dict[str, type[AuditDetails]] = {}
_RESOURCE_TYPES: set[str] = set()


def register_audit_action(action: str, details_model: type[AuditDetails]) -> None:
    """Register `details_model` as the one typed shape `action`'s `details` must match.

    Call once, at import time, from the component that emits `action`. The registry is a plain
    module-level dict, never a closed enum, so a new action never requires editing this module.

    Args:
        action: The namespaced action string recorded in `audit_events.action`
            (for example `"user.invite"`). Must not already be registered.
        details_model: The `AuditDetails` subclass that validates and shapes `details` for
            this action.

    Raises:
        ValueError: `action` is already registered. This stops two components from silently
            colliding on one action string.

    Side Effects:
        Adds `action` to the process-wide action registry.
    """
    if action in _ACTION_DETAILS_REGISTRY:
        message = f"audit action {action!r} is already registered"
        raise ValueError(message)
    _ACTION_DETAILS_REGISTRY[action] = details_model


def resolve_details_model(action: str) -> type[AuditDetails] | None:
    """Return the typed `details` model registered for `action`.

    Args:
        action: The namespaced action string to look up.

    Returns:
        The `AuditDetails` subclass registered for `action`, or `None` if the action has not
        been registered.
    """
    return _ACTION_DETAILS_REGISTRY.get(action)


def register_audit_resource_type(resource_type: str) -> None:
    """Register `resource_type` as a name that audit queries may filter on.

    Same extensibility contract as `register_audit_action`: a plain module-level set, never a
    closed enum, so a new resource type never requires editing this module.

    Args:
        resource_type: The resource-type name recorded in `audit_events.resource_type`.

    Business Rules:
        Unlike `register_audit_action`, re-registering the same name is accepted silently,
        because several actions from one component may share a resource type.

    Side Effects:
        Adds `resource_type` to the process-wide resource-type set.
    """
    _RESOURCE_TYPES.add(resource_type)


def is_known_resource_type(resource_type: str) -> bool:
    """Report whether `resource_type` has been registered with `register_audit_resource_type`.

    Args:
        resource_type: The resource-type name to check.

    Returns:
        `True` if the name is registered, `False` otherwise.
    """
    return resource_type in _RESOURCE_TYPES


@dataclass(frozen=True, slots=True)
class AuditEventRow:
    """One `audit_events` row, as returned by `AuditStore.query`.

    Attributes:
        id: The row's generated UUID, as a string.
        occurred_at: When the row was written (timestamp with time zone).
        actor_subject: The acting principal's identity-provider subject.
        actor_issuer: The identity-provider issuer of `actor_subject`.
        action: The registered, namespaced action string.
        resource_type: The kind of resource acted on.
        resource_id: The identifier of the resource acted on.
        outcome: Whether the effect happened: `"applied"`, `"rejected"` or `"failed"`.
        details: The action's validated `details` payload; fields left unset when recorded are
            absent.
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


AUDIT_DETAILS_FILTER_KEYS: Final[tuple[str, ...]] = (
    "celex",
    "regulatory_instrument_id",
    "instrument_id",
)
"""The only `details` keys that audit queries may filter on (exact match).

How to add an allow-listed key:

1. Append it to this tuple.
2. Add an expression index `((details ->> '<key>'))` in a new migration file under
   `audit/migrations/` (never edit an applied one) and add its fixed SQL fragment to
   `_DETAILS_FILTER_FRAGMENTS` in `audit/store.py` (the caller's key is never interpolated).
3. Make sure every action that should be searchable by it declares a field of that exact
   name in its `AuditDetails` model.
4. Add the key to the `list-audit-events` tool docstring and the `ps-list-audit-events` skill.
"""


@dataclass(frozen=True, slots=True)
class AuditQueryFilters:
    """Every filter an audit query accepts; all optional and combined with AND.

    A field left at `None` is omitted from the query. The store does not validate filter
    values: the caller must check them before querying.

    Attributes:
        actor_subject: Exact match on the actor's subject.
        actor_issuer: Exact match on the actor's issuer.
        resource_type: Exact match on the resource type; should be a registered one.
        resource_id: Exact match on the resource identifier.
        action: Exact match on the action string; should be a registered one.
        occurred_from: Inclusive lower bound on `occurred_at`.
        occurred_to: Inclusive upper bound on `occurred_at`; must not precede `occurred_from`.
        details: Exact-match filter on top-level string fields of `audit_events.details`. Keys
            must be members of `AUDIT_DETAILS_FILTER_KEYS`; an unknown key raises `KeyError`
            in the store.
    """

    actor_subject: str | None = None
    actor_issuer: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    action: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    details: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class AuditQueryPage:
    """One page of `AuditStore.query` results.

    Attributes:
        events: The page's rows, newest first; empty when nothing matches.
        next_cursor: Opaque token to pass as `cursor` to fetch the following page, or `None`
            when this is the last page.
    """

    events: tuple[AuditEventRow, ...]
    next_cursor: str | None


__all__ = [
    "AUDIT_DETAILS_FILTER_KEYS",
    "AuditDetails",
    "AuditEventRow",
    "AuditQueryFilters",
    "AuditQueryPage",
    "is_known_resource_type",
    "register_audit_action",
    "register_audit_resource_type",
    "resolve_details_model",
]
