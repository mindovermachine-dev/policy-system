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
        config.port = 9000  # pyright: ignore[reportAttributeAccessIssue]  # deliberately mutating a frozen dataclass to prove it raises


def test_load_config_no_relevant_env_vars_returns_default_service_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `PS_SERVICE_*`/`PS_LOGGING_DIR` set → defaults matching #12's shipped
    values (AC-BI-006).
    """
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
            violations.append(node)  # pyright: ignore[reportArgumentType]  # `node` is a narrowed `ast.Attribute` here (the `is_os_environ` guard above), but the compound boolean does not propagate the narrowing

    return violations


def test_config_module_never_references_bare_os_environ() -> None:
    """`config.py` only reads env vars via `os.environ.get(...)`, never the full
    mapping (AC-BI-012).
    """
    source = inspect.getsource(config_module)
    tree = ast.parse(source)

    violations = _find_bare_os_environ_references(tree)

    assert violations == []


@pytest.mark.parametrize("invalid_graceful_shutdown_seconds", ["not-a-number", "-1"])
def test_load_config_raises_for_invalid_ps_service_graceful_shutdown_seconds(
    invalid_graceful_shutdown_seconds: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-integer/negative `PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS` raises, for
    validation-surface consistency.
    """
    monkeypatch.setenv("PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS", invalid_graceful_shutdown_seconds)

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


def test_load_config_no_llm_interface_env_vars_returns_none_for_both_model_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `PS_LLMINTERFACE_MODEL`/`PS_LLMINTERFACE_EMBED_MODEL` set → both fields
    default to `None`.
    """
    monkeypatch.delenv("PS_LLMINTERFACE_MODEL", raising=False)
    monkeypatch.delenv("PS_LLMINTERFACE_EMBED_MODEL", raising=False)

    result = load_config()

    assert result.llm_interface_model is None
    assert result.llm_interface_embed_model is None


def test_load_config_honors_ps_llm_interface_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_LLMINTERFACE_MODEL` override takes effect."""
    monkeypatch.setenv("PS_LLMINTERFACE_MODEL", "azure/gpt-5.4-mini")

    assert load_config().llm_interface_model == "azure/gpt-5.4-mini"


def test_load_config_honors_ps_llm_interface_embed_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_LLMINTERFACE_EMBED_MODEL` override takes effect."""
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", "azure/text-embedding-3-large")

    assert load_config().llm_interface_embed_model == "azure/text-embedding-3-large"


@pytest.mark.parametrize("invalid_model", ["", "   ", "\t"])
def test_load_config_raises_for_empty_or_whitespace_ps_llm_interface_model(
    invalid_model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty/whitespace-only `PS_LLMINTERFACE_MODEL` fails closed, never falls back."""
    monkeypatch.setenv("PS_LLMINTERFACE_MODEL", invalid_model)

    with pytest.raises(ServiceConfigurationError):
        load_config()


@pytest.mark.parametrize("invalid_embed_model", ["", "   ", "\t"])
def test_load_config_raises_for_empty_or_whitespace_ps_llm_interface_embed_model(
    invalid_embed_model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty/whitespace-only `PS_LLMINTERFACE_EMBED_MODEL` fails closed, never falls back."""
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", invalid_embed_model)

    with pytest.raises(ServiceConfigurationError):
        load_config()


def test_load_config_with_no_ps_falkordb_env_vars_set_returns_default_falkordb_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `PS_FALKORDB_HOST`/`PS_FALKORDB_PORT` set -> defaults `127.0.0.1`/`6379`."""
    monkeypatch.delenv("PS_FALKORDB_HOST", raising=False)
    monkeypatch.delenv("PS_FALKORDB_PORT", raising=False)

    result = load_config()

    assert result.falkordb_host == "127.0.0.1"
    assert result.falkordb_port == 6379


def test_load_config_honors_ps_falkordb_host_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_FALKORDB_HOST` override takes effect."""
    monkeypatch.setenv("PS_FALKORDB_HOST", "falkordb.internal")

    assert load_config().falkordb_host == "falkordb.internal"


def test_load_config_honors_ps_falkordb_port_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_FALKORDB_PORT` override takes effect."""
    monkeypatch.setenv("PS_FALKORDB_PORT", "16379")

    assert load_config().falkordb_port == 16379


@pytest.mark.parametrize("invalid_falkordb_host", ["", "   ", "\t"])
def test_load_config_raises_service_configuration_error_for_empty_or_whitespace_ps_falkordb_host(
    invalid_falkordb_host: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty/whitespace-only `PS_FALKORDB_HOST` fails closed, never falls back."""
    monkeypatch.setenv("PS_FALKORDB_HOST", invalid_falkordb_host)

    with pytest.raises(ServiceConfigurationError):
        load_config()


@pytest.mark.parametrize(
    "invalid_falkordb_port",
    ["not-a-number", "0", "-1", "65536", "99999"],
)
def test_load_config_raises_service_configuration_error_for_invalid_ps_falkordb_port(
    invalid_falkordb_port: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-integer, zero, negative, or out-of-range `PS_FALKORDB_PORT` fails closed."""
    monkeypatch.setenv("PS_FALKORDB_PORT", invalid_falkordb_port)

    with pytest.raises(ServiceConfigurationError):
        load_config()


def test_load_config_with_no_ps_companymerge_similarity_threshold_set_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `PS_COMPANYMERGE_SIMILARITY_THRESHOLD` set -> `None`, no exception raised at this layer.

    Issue #16's B1 fix: "required" enforcement lives at
    `merge_baseline_graph`'s own call site, not in `load_config()` — every
    other unrelated caller of `load_config()` must keep working with this
    env var unset.
    """
    monkeypatch.delenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", raising=False)

    result = load_config()

    assert result.company_merge_similarity_threshold is None


def test_load_config_honors_ps_companymerge_similarity_threshold_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_COMPANYMERGE_SIMILARITY_THRESHOLD` override takes effect and parses to a float."""
    monkeypatch.setenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", "0.85")

    assert load_config().company_merge_similarity_threshold == 0.85


@pytest.mark.parametrize(
    "invalid_similarity_threshold",
    ["0.0", "1.5", "-0.1"],
)
def test_load_config_raises_for_out_of_range_ps_companymerge_similarity_threshold(
    invalid_similarity_threshold: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-range (not `0.0 < value <= 1.0`) `PS_COMPANYMERGE_SIMILARITY_THRESHOLD`
    fails closed, when present.
    """
    monkeypatch.setenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", invalid_similarity_threshold)

    with pytest.raises(ServiceConfigurationError):
        load_config()


def test_load_config_raises_for_non_numeric_ps_companymerge_similarity_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-numeric `PS_COMPANYMERGE_SIMILARITY_THRESHOLD` fails closed, when present."""
    monkeypatch.setenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", "high")

    with pytest.raises(ServiceConfigurationError):
        load_config()


def test_service_config_four_field_construction_still_succeeds_with_none_threshold() -> None:
    """Regression proof (issue #16's B1): the exact pre-existing 4-positional-field
    `ServiceConfig(...)` construction other tests in this file already use, with no
    `company_merge_similarity_threshold` argument, still succeeds and the new field
    defaults to `None` — proving this field's addition does not break any
    pre-existing `ServiceConfig(...)` call site anywhere in the codebase.
    """
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
    )

    assert config.company_merge_similarity_threshold is None
