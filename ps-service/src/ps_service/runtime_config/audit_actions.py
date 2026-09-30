"""Typed `details` models for the two `runtime_config.*` audit actions (issue #130).

Registers each model with `ps_service.audit.models.register_audit_action` at import time
(the same "module import triggers registration" idiom `ps_service.authz.audit_actions` uses),
through `audit`'s public registry only -- `audit` has no config-specific hook.

`old_value`/`new_value` are scalars only: they hold the key's own `audit_value` projection of
a validated value, so a secret or an unvalidated blob cannot reach `details` (the models
forbid extra fields and non-scalar values). `old_value` is absent when there was no previous
value, and `exclude_none` at record time keeps that "absent", not `null`.
"""

from __future__ import annotations

from ps_service.audit import AuditDetails, register_audit_action, register_audit_resource_type


class RuntimeConfigSetDetails(AuditDetails):
    """`runtime_config.set` -- always `outcome='applied'` (a rejected value writes no row)."""

    key: str
    old_value: str | int | bool | None = None
    new_value: str | int | bool


class RuntimeConfigResetDetails(AuditDetails):
    """`runtime_config.reset` -- always `outcome='applied'`, also for a key with no row."""

    key: str
    old_value: str | int | bool | None = None


RUNTIME_CONFIG_SET_ACTION = "runtime_config.set"
RUNTIME_CONFIG_RESET_ACTION = "runtime_config.reset"
RUNTIME_CONFIG_RESOURCE_TYPE = "runtime_config"

register_audit_action(RUNTIME_CONFIG_SET_ACTION, RuntimeConfigSetDetails)
register_audit_action(RUNTIME_CONFIG_RESET_ACTION, RuntimeConfigResetDetails)
register_audit_resource_type(RUNTIME_CONFIG_RESOURCE_TYPE)

__all__ = [
    "RUNTIME_CONFIG_RESET_ACTION",
    "RUNTIME_CONFIG_RESOURCE_TYPE",
    "RUNTIME_CONFIG_SET_ACTION",
    "RuntimeConfigResetDetails",
    "RuntimeConfigSetDetails",
]
