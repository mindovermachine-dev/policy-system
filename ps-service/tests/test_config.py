"""Tests for `ps_service.config`: `ServiceConfig` and `load_config()`.

Note on `PS_LOGGING_DIR`: `tests/conftest.py`'s autouse `_isolate_logging`
fixture unconditionally sets `PS_LOGGING_DIR` to a per-test `tmp_path` for
every test in this suite. Any test here that expects `logging_dir=None`
from `load_config()` must `monkeypatch.delenv("PS_LOGGING_DIR",
raising=False)` first, or it will fail against a correct implementation.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect

import pytest
from ps_service import config as config_module
from ps_service.config import ServiceConfig, ServiceConfigurationError, load_config


def test_service_config_field_mutation_raises_frozen_instance_error() -> None:
    """`ServiceConfig` is frozen (AC-BI-001): mutating any field raises."""
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.port = 9000  # type: ignore[misc]


def test_load_config_with_no_relevant_env_vars_set_returns_default_service_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `PS_SERVICE_*`/`PS_LOGGING_DIR` set → defaults matching #12's shipped values (AC-BI-006)."""
    monkeypatch.delenv("PS_LOGGING_DIR", raising=False)
    monkeypatch.delenv("PS_SERVICE_HOST", raising=False)
    monkeypatch.delenv("PS_SERVICE_PORT", raising=False)
    monkeypatch.delenv("PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS", raising=False)

    result = load_config()

    assert result == ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
    )


def test_load_config_honors_ps_service_port_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_SERVICE_PORT` override takes effect (AC-BI-003)."""
    monkeypatch.setenv("PS_SERVICE_PORT", "9000")

    assert load_config().port == 9000


def test_load_config_honors_ps_service_host_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_SERVICE_HOST` override takes effect (AC-BI-004)."""
    monkeypatch.setenv("PS_SERVICE_HOST", "0.0.0.0")

    assert load_config().host == "0.0.0.0"


def test_load_config_honors_ps_service_graceful_shutdown_seconds_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS` override takes effect (AC-BI-005)."""
    monkeypatch.setenv("PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS", "30")

    assert load_config().graceful_shutdown_seconds == 30


@pytest.mark.parametrize(
    "invalid_port",
    ["not-a-number", "0", "-1", "65536", "99999"],
)
def test_load_config_raises_service_configuration_error_for_invalid_ps_service_port(
    invalid_port: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-integer, zero, negative, or out-of-range `PS_SERVICE_PORT` fails closed (AC-BI-009)."""
    monkeypatch.setenv("PS_SERVICE_PORT", invalid_port)

    with pytest.raises(ServiceConfigurationError):
        load_config()


def _find_bare_os_environ_references(tree: ast.AST) -> list[ast.Attribute]:
    """Return every `os.environ` `ast.Attribute` node not immediately used as `.get(...)`'s object.

    Catches `os.environ` on its own, `dict(os.environ)`, `os.environ.items()`,
    `**os.environ`, `for k in os.environ`, etc. — anything that could dump
    the full process environment rather than reading one named variable via
    `os.environ.get(...)`.
    """
    parent_by_node: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_by_node[child] = parent

    violations: list[ast.Attribute] = []
    for node in ast.walk(tree):
        is_os_environ = (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )
        if not is_os_environ:
            continue

        parent = parent_by_node.get(node)
        is_get_object = (
            isinstance(parent, ast.Attribute) and parent.attr == "get" and parent.value is node
        )
        grandparent = parent_by_node.get(parent) if parent is not None else None
        is_called = (
            is_get_object and isinstance(grandparent, ast.Call) and grandparent.func is parent
        )
        if not is_called:
            violations.append(node)  # type: ignore[arg-type]

    return violations


def test_config_module_never_references_bare_os_environ() -> None:
    """`config.py` only reads env vars via `os.environ.get(...)`, never the full mapping (AC-BI-012)."""
    source = inspect.getsource(config_module)
    tree = ast.parse(source)

    violations = _find_bare_os_environ_references(tree)

    assert violations == []


@pytest.mark.parametrize("invalid_graceful_shutdown_seconds", ["not-a-number", "-1"])
def test_load_config_raises_service_configuration_error_for_invalid_ps_service_graceful_shutdown_seconds(
    invalid_graceful_shutdown_seconds: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-integer/negative `PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS` raises, for validation-surface consistency."""
    monkeypatch.setenv(
        "PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS", invalid_graceful_shutdown_seconds
    )

    with pytest.raises(ServiceConfigurationError):
        load_config()


@pytest.mark.parametrize("invalid_host", ["", "   ", "\t"])
def test_load_config_raises_service_configuration_error_for_empty_or_whitespace_ps_service_host(
    invalid_host: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty/whitespace-only `PS_SERVICE_HOST` fails closed, never falls back (AC-BI-010)."""
    monkeypatch.setenv("PS_SERVICE_HOST", invalid_host)

    with pytest.raises(ServiceConfigurationError):
        load_config()
