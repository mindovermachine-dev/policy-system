"""Typed key registry for runtime configuration (issue #130).

Each runtime-mutable config value is declared once, in code, as a `RuntimeConfigKey`: its
name, its value type, a validator, and an `audit_value` projection that decides what of the
value may reach an audit row's `details`. The store rejects unregistered keys and invalid
values before any write, so no writer of `runtime_config` can bypass a key's validator.
Adding a config value therefore costs one `register_runtime_config_key` call, not a new
storage pattern.

`define_runtime_config_key` keeps the authoring side typed (`value_type: type[T]`,
`validate: (T, ServiceConfig) -> T`) and erases `T` at registration, mirroring
`ps_service.audit.models`'s plain module-level registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pydantic import TypeAdapter

from ps_service.runtime_config.errors import (
    RuntimeConfigInvalidValueError,
    RuntimeConfigUnknownKeyError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.config import ServiceConfig

type AuditScalar = str | int | bool | None
"""The only value shapes an audit row's `details` may carry for a config value."""

_TYPE_MISMATCH_MESSAGE = "runtime config key {name!r} was given a value of the wrong type"


@dataclass(frozen=True, slots=True)
class RuntimeConfigKey:
    """One registered runtime config key (type-erased; see `define_runtime_config_key`)."""

    name: str
    parse: Callable[[object], object]
    """Strict type check of a raw value (a stored jsonb value or a caller's input)."""
    validate: Callable[[object, ServiceConfig], object]
    """The key's own validator, called only on an already type-checked value."""
    project: Callable[[object], AuditScalar]
    """What of a validated value may be written to an audit row's `details`."""
    validation_errors: tuple[type[Exception], ...]
    """Extra exception types `validate` may raise to reject a value (`ValueError` always counts)."""


def define_runtime_config_key[T](
    name: str,
    value_type: type[T],
    *,
    validate: Callable[[T, ServiceConfig], T],
    audit_value: Callable[[T], AuditScalar],
    validation_errors: tuple[type[Exception], ...] = (),
) -> RuntimeConfigKey:
    """Build a `RuntimeConfigKey` from a typed validator and audit projection.

    Args:
        name: The key's unique name, also its `runtime_config.key` value and audit `resource_id`.
        value_type: The value's type; a stored or supplied value must match it strictly.
        validate: Returns the (possibly normalised) value or raises to reject it. It receives
            the `ServiceConfig` because a validator may depend on process configuration.
        audit_value: Projects a validated value to the scalar recorded in audit `details`
            (e.g. a URL with credentials and query removed). Never called on unvalidated data.
        validation_errors: Exception types, beyond `ValueError`, that `validate` raises.
    """
    adapter = TypeAdapter(value_type)

    def _parse(raw: object) -> object:
        return adapter.validate_python(raw, strict=True)

    def _validate(value: object, config: ServiceConfig) -> object:
        return validate(cast("T", value), config)

    def _project(value: object) -> AuditScalar:
        return audit_value(cast("T", value))

    return RuntimeConfigKey(
        name=name,
        parse=_parse,
        validate=_validate,
        project=_project,
        validation_errors=validation_errors,
    )


_REGISTRY: dict[str, RuntimeConfigKey] = {}


def register_runtime_config_key(key: RuntimeConfigKey) -> None:
    """Register `key`; called once, at import time, by the component that owns the value.

    Raises:
        ValueError: `key.name` is already registered (same idiom as `register_audit_action`).
    """
    if key.name in _REGISTRY:
        message = f"runtime config key {key.name!r} is already registered"
        raise ValueError(message)
    _REGISTRY[key.name] = key


def resolve_runtime_config_key(name: str) -> RuntimeConfigKey | None:
    """Return the key registered under `name`, or `None` if unregistered."""
    return _REGISTRY.get(name)


def require_runtime_config_key(name: str) -> RuntimeConfigKey:
    """Return the key registered under `name`.

    Raises:
        RuntimeConfigUnknownKeyError: `name` is not registered.
    """
    key = resolve_runtime_config_key(name)
    if key is None:
        message = f"runtime config key {name!r} is not registered"
        raise RuntimeConfigUnknownKeyError(message)
    return key


def prepare_runtime_config_value(
    config: ServiceConfig, key: RuntimeConfigKey, raw: object
) -> object:
    """Type-check `raw` for `key`, then run the key's validator; return the validated value.

    The one path every value takes before it is written, and again when a stored row is read
    back (so a hand-edited row cannot smuggle a wrong shape or a value the validator rejects).

    Raises:
        RuntimeConfigInvalidValueError: `raw` is the wrong type or the validator rejected it.
            The type-mismatch message never repeats `raw`.
    """
    try:
        parsed = key.parse(raw)
    except ValueError as exc:
        raise RuntimeConfigInvalidValueError(_TYPE_MISMATCH_MESSAGE.format(name=key.name)) from exc
    try:
        return key.validate(parsed, config)
    except (ValueError, *key.validation_errors) as exc:
        raise RuntimeConfigInvalidValueError(str(exc)) from exc
