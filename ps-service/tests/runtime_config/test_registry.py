"""Tests for the runtime-config key registry (issue #130, Slice 3)."""

from __future__ import annotations

import uuid

import pytest

from ps_service.runtime_config import (
    define_runtime_config_key,
    register_runtime_config_key,
    resolve_runtime_config_key,
)


def _unique_name() -> str:
    return f"test.registry.{uuid.uuid4().hex[:12]}"


def _identity_key(name: str):
    return define_runtime_config_key(
        name, int, validate=lambda value, _config: value, audit_value=lambda value: value
    )


def test_register_duplicate_key_raises_value_error() -> None:
    name = _unique_name()
    register_runtime_config_key(_identity_key(name))

    with pytest.raises(ValueError, match="already registered"):
        register_runtime_config_key(_identity_key(name))


def test_resolve_unknown_key_returns_none() -> None:
    assert resolve_runtime_config_key(_unique_name()) is None


def test_resolve_returns_the_registered_key() -> None:
    name = _unique_name()
    key = _identity_key(name)
    register_runtime_config_key(key)

    assert resolve_runtime_config_key(name) is key
