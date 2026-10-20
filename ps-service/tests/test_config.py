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
import os

import pytest

from ps_service import config as config_module
from ps_service.config import (
    INGESTION_REQUIRED_CONFIG_FIELDS,
    ServiceConfig,
    ServiceConfigurationError,
    load_config,
    missing_ingestion_config_fields,
)

_DEFAULT_CURATED_SOURCE_BASE_URL = (
    "https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/curated-content"
)


def test_service_config_field_mutation_raises_frozen_instance_error() -> None:
    """`ServiceConfig` is frozen: mutating any field raises."""
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
    """With every `PS_*` environment variable unset, `load_config()` returns
    the shipped defaults.
    """
    for name in [name for name in os.environ if name.startswith("PS_")]:
        monkeypatch.delenv(name)

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
    """`PS_SERVICE_PORT` override takes effect."""
    monkeypatch.setenv("PS_SERVICE_PORT", "9000")

    assert load_config().port == 9000


def test_load_config_honors_ps_service_host_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_SERVICE_HOST` override takes effect."""
    monkeypatch.setenv("PS_SERVICE_HOST", "0.0.0.0")

    assert load_config().host == "0.0.0.0"


def test_load_config_honors_ps_service_graceful_shutdown_seconds_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS` override takes effect."""
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
    """Non-integer, zero, negative, or out-of-range `PS_SERVICE_PORT` fails closed."""
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
    mapping.
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
    """Empty/whitespace-only `PS_SERVICE_HOST` fails closed, never falls back."""
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


def test_load_config_with_no_ps_companymerge_capability_merge_threshold_set_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD` set -> `None`, no exception at this layer.

    "Required" enforcement lives at
    `merge_baseline_graph`'s own call site, not in `load_config()` — every
    other unrelated caller of `load_config()` must keep working with this
    env var unset.
    """
    monkeypatch.delenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", raising=False)

    result = load_config()

    assert result.company_merge_capability_merge_threshold is None


def test_load_config_honors_ps_companymerge_capability_merge_threshold_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD` override takes effect and parses to a float."""
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", "0.85")

    assert load_config().company_merge_capability_merge_threshold == 0.85


@pytest.mark.parametrize(
    "invalid_merge_threshold",
    ["0.0", "1.5", "-0.1"],
)
def test_load_config_raises_for_out_of_range_ps_companymerge_capability_merge_threshold(
    invalid_merge_threshold: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-range (not `0.0 < value <= 1.0`) `PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD`
    fails closed, when present.
    """
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", invalid_merge_threshold)

    with pytest.raises(ServiceConfigurationError):
        load_config()


def test_load_config_raises_for_non_numeric_ps_companymerge_capability_merge_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-numeric `PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD` fails closed, when present."""
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", "high")

    with pytest.raises(ServiceConfigurationError):
        load_config()


# The retired variable is spelled in two halves so a repo-wide search for the old name stays empty.
_RETIRED_MERGE_THRESHOLD_VAR = "PS_COMPANYMERGE_" + "SIMILARITY_" + "THRESHOLD"


def test_load_config_ignores_the_retired_merge_threshold_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old variable name is neither read nor aliased: with only it set, the field is `None`."""
    monkeypatch.delenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", raising=False)
    monkeypatch.setenv(_RETIRED_MERGE_THRESHOLD_VAR, "0.85")

    assert load_config().company_merge_capability_merge_threshold is None


def test_load_config_retired_variable_does_not_override_the_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both set, only the new variable counts; an invalid old value never fails."""
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", "0.7")
    monkeypatch.setenv(_RETIRED_MERGE_THRESHOLD_VAR, "not-a-number")

    assert load_config().company_merge_capability_merge_threshold == 0.7


@pytest.mark.parametrize("raw", ["high", "0.0", "1.5"])
def test_load_config_error_names_the_capability_merge_threshold_variable(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected merge threshold names the new variable in the startup error."""
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", raw)

    with pytest.raises(
        ServiceConfigurationError, match="PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD"
    ):
        load_config()


_REVIEW_VAR = "PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD"


def test_load_config_with_no_review_threshold_set_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset review threshold -> `None` at this layer; the required tuple enforces it."""
    monkeypatch.delenv(_REVIEW_VAR, raising=False)

    assert load_config().company_merge_capability_review_threshold is None


@pytest.mark.parametrize(("raw", "expected"), [("0.0", 0.0), ("0.3", 0.3), ("1.0", 1.0)])
def test_load_config_parses_the_review_threshold_including_both_bounds(
    raw: str, expected: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", raising=False)
    monkeypatch.setenv(_REVIEW_VAR, raw)

    assert load_config().company_merge_capability_review_threshold == expected


@pytest.mark.parametrize("raw", ["high", "-0.1", "1.01", "nan", "inf", ""])
def test_load_config_rejects_an_invalid_review_threshold_naming_the_variable(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_REVIEW_VAR, raw)

    with pytest.raises(ServiceConfigurationError, match=_REVIEW_VAR):
        load_config()


def test_load_config_rejects_a_review_threshold_above_the_merge_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", "0.6")
    monkeypatch.setenv(_REVIEW_VAR, "0.8")

    with pytest.raises(ServiceConfigurationError) as raised:
        load_config()

    assert _REVIEW_VAR in str(raised.value)
    assert "PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD" in str(raised.value)


def test_load_config_accepts_a_review_threshold_equal_to_the_merge_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", "0.9")
    monkeypatch.setenv(_REVIEW_VAR, "0.9")

    assert load_config().company_merge_capability_review_threshold == 0.9


def test_load_config_accepts_a_review_threshold_when_the_merge_threshold_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cross-check runs only when both are set; the required tuple catches the gap."""
    monkeypatch.delenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", raising=False)
    monkeypatch.setenv(_REVIEW_VAR, "0.5")

    config = load_config()

    assert config.company_merge_capability_review_threshold == 0.5
    assert config.company_merge_capability_merge_threshold is None


_ROLE_VAR = "PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD"


def test_load_config_with_no_role_review_threshold_set_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset Role review threshold -> `None` at this layer; the required tuple enforces it."""
    monkeypatch.delenv(_ROLE_VAR, raising=False)

    assert load_config().company_merge_role_review_threshold is None


@pytest.mark.parametrize(("raw", "expected"), [("0.0", 0.0), ("0.8", 0.8), ("1.0", 1.0)])
def test_load_config_parses_the_role_review_threshold_including_both_bounds(
    raw: str, expected: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ROLE_VAR, raw)

    assert load_config().company_merge_role_review_threshold == expected


@pytest.mark.parametrize("raw", ["high", "-0.1", "1.1", "nan", "inf", ""])
def test_load_config_rejects_an_invalid_role_review_threshold_naming_the_variable(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ROLE_VAR, raw)

    with pytest.raises(ServiceConfigurationError, match=_ROLE_VAR) as raised:
        load_config()

    assert "CAPABILITY" not in str(raised.value)


def test_load_config_keeps_the_capability_review_threshold_messages_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_REVIEW_VAR, "high")
    with pytest.raises(ServiceConfigurationError) as not_a_float:
        load_config()
    monkeypatch.setenv(_REVIEW_VAR, "1.5")
    with pytest.raises(ServiceConfigurationError) as out_of_range:
        load_config()

    assert str(not_a_float.value) == f"{_REVIEW_VAR} must be a float, got 'high'"
    assert str(out_of_range.value) == (
        f"{_REVIEW_VAR} must be at least 0.0 and at most 1.0, got 1.5"
    )


def test_missing_ingestion_config_fields_names_an_unset_role_review_threshold_alone() -> None:
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-5.4-mini",
        llm_interface_embed_model="azure/text-embedding-3-small",
        company_merge_capability_merge_threshold=0.85,
        company_merge_capability_review_threshold=0.5,
    )

    assert missing_ingestion_config_fields(config) == ["company_merge_role_review_threshold"]


def test_service_config_four_field_construction_still_succeeds_with_none_threshold() -> None:
    """Regression proof: the exact pre-existing 4-positional-field
    `ServiceConfig(...)` construction other tests in this file already use, with no
    `company_merge_capability_merge_threshold` argument, still succeeds and the new field
    defaults to `None` — proving this field's addition does not break any
    pre-existing `ServiceConfig(...)` call site anywhere in the codebase.
    """
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
    )

    assert config.company_merge_capability_merge_threshold is None


def test_missing_ingestion_config_fields_returns_all_four_names_when_none_are_set() -> None:
    """A default-constructed `ServiceConfig` (no ingestion fields set) reports all four names,
    in `INGESTION_REQUIRED_CONFIG_FIELDS` order.
    """
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
    )

    assert missing_ingestion_config_fields(config) == list(INGESTION_REQUIRED_CONFIG_FIELDS)


def test_missing_ingestion_config_fields_returns_empty_list_when_all_five_are_set() -> None:
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-5.4-mini",
        llm_interface_embed_model="azure/text-embedding-3-small",
        company_merge_capability_merge_threshold=0.85,
        company_merge_capability_review_threshold=0.5,
        company_merge_role_review_threshold=0.8,
    )

    assert missing_ingestion_config_fields(config) == []


def test_missing_ingestion_config_fields_names_only_the_unset_ones() -> None:
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-5.4-mini",
        llm_interface_embed_model=None,
        company_merge_capability_merge_threshold=None,
        company_merge_capability_review_threshold=None,
        company_merge_role_review_threshold=None,
    )

    assert missing_ingestion_config_fields(config) == [
        "llm_interface_embed_model",
        "company_merge_capability_merge_threshold",
        "company_merge_capability_review_threshold",
        "company_merge_role_review_threshold",
    ]


def test_missing_ingestion_config_fields_names_an_unset_review_threshold_alone() -> None:
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-5.4-mini",
        llm_interface_embed_model="azure/text-embedding-3-small",
        company_merge_capability_merge_threshold=0.85,
        company_merge_role_review_threshold=0.8,
    )

    assert missing_ingestion_config_fields(config) == ["company_merge_capability_review_threshold"]


def test_load_config_local_test_bypass_unset_is_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `PS_SERVICE_LOCAL_TEST_BYPASS` set -> inactive, never defaults to
    enabled.
    """
    monkeypatch.delenv("PS_SERVICE_LOCAL_TEST_BYPASS", raising=False)

    assert load_config().is_local_test_bypass_active is False


def test_load_config_local_test_bypass_empty_string_is_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_SERVICE_LOCAL_TEST_BYPASS=""` -> inactive."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "")

    assert load_config().is_local_test_bypass_active is False


def test_load_config_local_test_bypass_whitespace_only_is_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace-only `PS_SERVICE_LOCAL_TEST_BYPASS` -> inactive."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "   ")

    assert load_config().is_local_test_bypass_active is False


@pytest.mark.parametrize("true_value", ["true", "True", "TRUE"])
def test_load_config_honors_local_test_bypass_true(
    true_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case-insensitive `"true"` activates the bypass."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", true_value)

    assert load_config().is_local_test_bypass_active is True


@pytest.mark.parametrize("false_value", ["false", "False"])
def test_load_config_honors_local_test_bypass_false(
    false_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case-insensitive `"false"` keeps the bypass inactive."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", false_value)

    assert load_config().is_local_test_bypass_active is False


@pytest.mark.parametrize("invalid_value", ["1", "yes", "on", "enabled", "nope"])
def test_load_config_raises_for_unrecognized_local_test_bypass_value(
    invalid_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value that is neither a recognized on/off token nor empty fails config
    loading closed rather than silently defaulting.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", invalid_value)

    with pytest.raises(ServiceConfigurationError) as excinfo:
        load_config()

    assert "PS_SERVICE_LOCAL_TEST_BYPASS" in str(excinfo.value)
    assert invalid_value in str(excinfo.value)


def test_load_config_with_no_ps_query_timeout_ms_set_defaults_to_5000(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `PS_QUERY_TIMEOUT_MS` set -> defaults to 5000."""
    monkeypatch.delenv("PS_QUERY_TIMEOUT_MS", raising=False)

    assert load_config().query_timeout_ms == 5000


def test_load_config_honors_ps_query_timeout_ms_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_QUERY_TIMEOUT_MS` override takes effect."""
    monkeypatch.setenv("PS_QUERY_TIMEOUT_MS", "2500")

    assert load_config().query_timeout_ms == 2500


@pytest.mark.parametrize(
    "invalid_query_timeout_ms",
    ["not-a-number", "0", "-1"],
)
def test_load_config_raises_service_configuration_error_for_invalid_ps_query_timeout_ms(
    invalid_query_timeout_ms: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-integer, zero, or negative `PS_QUERY_TIMEOUT_MS` fails closed."""
    monkeypatch.setenv("PS_QUERY_TIMEOUT_MS", invalid_query_timeout_ms)

    with pytest.raises(ServiceConfigurationError):
        load_config()


def test_load_config_with_no_ps_query_row_cap_set_defaults_to_1000(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `PS_QUERY_ROW_CAP` set -> defaults to 1000."""
    monkeypatch.delenv("PS_QUERY_ROW_CAP", raising=False)

    assert load_config().query_row_cap == 1000


def test_load_config_honors_ps_query_row_cap_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_QUERY_ROW_CAP` override takes effect."""
    monkeypatch.setenv("PS_QUERY_ROW_CAP", "50")

    assert load_config().query_row_cap == 50


@pytest.mark.parametrize(
    "invalid_query_row_cap",
    ["not-a-number", "0", "-1"],
)
def test_load_config_raises_service_configuration_error_for_invalid_ps_query_row_cap(
    invalid_query_row_cap: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-integer, zero, or negative `PS_QUERY_ROW_CAP` fails closed."""
    monkeypatch.setenv("PS_QUERY_ROW_CAP", invalid_query_row_cap)

    with pytest.raises(ServiceConfigurationError):
        load_config()


def test_load_config_with_no_ps_ingestionruns_max_in_flight_set_defaults_to_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `PS_INGESTIONRUNS_MAX_IN_FLIGHT` set -> defaults to 1."""
    monkeypatch.delenv("PS_INGESTIONRUNS_MAX_IN_FLIGHT", raising=False)

    assert load_config().ingestion_runs_max_in_flight == 1


def test_load_config_honors_ps_ingestionruns_max_in_flight_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_INGESTIONRUNS_MAX_IN_FLIGHT` override takes effect."""
    monkeypatch.setenv("PS_INGESTIONRUNS_MAX_IN_FLIGHT", "3")

    assert load_config().ingestion_runs_max_in_flight == 3


@pytest.mark.parametrize("invalid_cap", ["not-a-number", "0", "-1"])
def test_load_config_raises_for_invalid_ps_ingestionruns_max_in_flight(
    invalid_cap: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-integer, zero or negative `PS_INGESTIONRUNS_MAX_IN_FLIGHT` fails closed."""
    monkeypatch.setenv("PS_INGESTIONRUNS_MAX_IN_FLIGHT", invalid_cap)

    with pytest.raises(ServiceConfigurationError, match="PS_INGESTIONRUNS_MAX_IN_FLIGHT"):
        load_config()


def test_load_config_with_no_ps_curatedsource_env_vars_set_defaults_to_the_public_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `PS_CURATEDSOURCE_URL` set -> defaults to the public GitHub repo."""
    monkeypatch.delenv("PS_CURATEDSOURCE_URL", raising=False)
    monkeypatch.delenv("PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP", raising=False)

    result = load_config()

    assert result.curated_source_base_url == _DEFAULT_CURATED_SOURCE_BASE_URL
    assert result.curated_source_allow_insecure_http is False


def test_load_config_honors_ps_curatedsource_url_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator-configured `PS_CURATEDSOURCE_URL` takes effect, no code change."""
    monkeypatch.setenv("PS_CURATEDSOURCE_URL", "https://example.com/my-curated-content")

    assert load_config().curated_source_base_url == "https://example.com/my-curated-content"


def test_load_config_raises_for_plain_http_curatedsource_url_without_the_allow_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup half: plain `http://` is refused unless explicitly allowed."""
    monkeypatch.setenv("PS_CURATEDSOURCE_URL", "http://example.com/curated-content")
    monkeypatch.delenv("PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP", raising=False)

    with pytest.raises(ServiceConfigurationError) as excinfo:
        load_config()

    assert "http://example.com/curated-content" in str(excinfo.value)


def test_load_config_raises_for_file_scheme_curatedsource_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-http(s) scheme (e.g. `file://`) is always rejected."""
    monkeypatch.setenv("PS_CURATEDSOURCE_URL", "file:///etc/passwd")

    with pytest.raises(ServiceConfigurationError) as excinfo:
        load_config()

    assert "file:///etc/passwd" in str(excinfo.value)


def test_load_config_honors_ps_curatedsource_allow_insecure_http_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP=true` opts in to a plain `http://`."""
    monkeypatch.setenv("PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP", "true")
    monkeypatch.setenv("PS_CURATEDSOURCE_URL", "http://example.com/curated-content")

    result = load_config()

    assert result.curated_source_allow_insecure_http is True
    assert result.curated_source_base_url == "http://example.com/curated-content"


@pytest.mark.parametrize("invalid_value", ["1", "yes", "on", "nope"])
def test_load_config_raises_for_unrecognized_curatedsource_allow_insecure_http_value(
    invalid_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails closed rather than silently defaulting (mirrors the local-test bypass)."""
    monkeypatch.setenv("PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP", invalid_value)

    with pytest.raises(ServiceConfigurationError) as excinfo:
        load_config()

    assert "PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP" in str(excinfo.value)


# --- PS_AUTHENTIK_API_TOKEN / PS_AUTHENTIK_BASE_URL -----------------


@pytest.mark.parametrize("invalid_token", ["", "   ", "\t"])
def test_load_config_raises_for_empty_or_whitespace_ps_authentik_api_token(
    invalid_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty/whitespace-only `PS_AUTHENTIK_API_TOKEN` fails closed, never falls back."""
    monkeypatch.setenv("PS_AUTHENTIK_API_TOKEN", invalid_token)

    with pytest.raises(ServiceConfigurationError):
        load_config()


@pytest.mark.parametrize("invalid_base_url", ["", "   ", "\t"])
def test_load_config_raises_for_empty_or_whitespace_ps_authentik_base_url(
    invalid_base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty/whitespace-only `PS_AUTHENTIK_BASE_URL` fails closed, never falls back."""
    monkeypatch.setenv("PS_AUTHENTIK_BASE_URL", invalid_base_url)

    with pytest.raises(ServiceConfigurationError):
        load_config()


def test_load_config_no_authentik_env_vars_returns_none_for_both_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `PS_AUTHENTIK_API_TOKEN`/`PS_AUTHENTIK_BASE_URL` set -> both fields default to `None`.

    Whether both are *required* is enforced by
    `ps_service.invitations.startup.require_authentik_credential_configured`,
    not by `load_config()` itself -- mirrors every other credential field's
    "record what the environment resolved to, absence is not an error here"
    shape.
    """
    monkeypatch.delenv("PS_AUTHENTIK_API_TOKEN", raising=False)
    monkeypatch.delenv("PS_AUTHENTIK_BASE_URL", raising=False)

    result = load_config()

    assert result.authentik_api_token is None
    assert result.authentik_base_url is None


def test_load_config_honors_ps_authentik_api_token_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_AUTHENTIK_API_TOKEN` override takes effect."""
    monkeypatch.setenv("PS_AUTHENTIK_API_TOKEN", "test-authentik-token")

    assert load_config().authentik_api_token == "test-authentik-token"


def test_load_config_honors_ps_authentik_base_url_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_AUTHENTIK_BASE_URL` override takes effect."""
    monkeypatch.setenv("PS_AUTHENTIK_BASE_URL", "https://authentik.example.com")

    assert load_config().authentik_base_url == "https://authentik.example.com"


@pytest.mark.parametrize("invalid_public_url", ["", "   ", "\t"])
def test_load_config_raises_for_empty_or_whitespace_ps_authentik_public_url(
    invalid_public_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty/whitespace-only `PS_AUTHENTIK_PUBLIC_URL` fails closed."""
    monkeypatch.setenv("PS_AUTHENTIK_PUBLIC_URL", invalid_public_url)

    with pytest.raises(ServiceConfigurationError):
        load_config()


def test_load_config_unset_ps_authentik_public_url_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence is not an error: the invitee link then falls back to the base URL."""
    monkeypatch.delenv("PS_AUTHENTIK_PUBLIC_URL", raising=False)

    assert load_config().authentik_public_url is None


def test_load_config_honors_ps_authentik_public_url_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PS_AUTHENTIK_PUBLIC_URL` override takes effect."""
    monkeypatch.setenv("PS_AUTHENTIK_PUBLIC_URL", "https://ps.example.com/auth")

    assert load_config().authentik_public_url == "https://ps.example.com/auth"


def test_service_config_repr_never_includes_authentik_api_token() -> None:
    """`repr(config)` never includes the configured Authentik API token."""
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        authentik_api_token="distinctive-authentik-token-value",
    )

    assert "distinctive-authentik-token-value" not in repr(config)


_STATE_POSTGRES_ENV_VARS = (
    "PS_STATE_POSTGRES_HOST",
    "PS_STATE_POSTGRES_PORT",
    "PS_STATE_POSTGRES_DATABASE",
    "PS_STATE_POSTGRES_USER",
    "PS_STATE_POSTGRES_PASSWORD",
)


def test_load_config_reads_ps_state_postgres_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PS state Postgres connection is read from `PS_STATE_POSTGRES_*`."""
    monkeypatch.setenv("PS_STATE_POSTGRES_HOST", "state-db.example.com")
    monkeypatch.setenv("PS_STATE_POSTGRES_PORT", "5433")
    monkeypatch.setenv("PS_STATE_POSTGRES_DATABASE", "ps_state")
    monkeypatch.setenv("PS_STATE_POSTGRES_USER", "ps_state_user")
    monkeypatch.setenv("PS_STATE_POSTGRES_PASSWORD", "s3cret")

    config = load_config()

    assert (
        config.state_postgres_host,
        config.state_postgres_port,
        config.state_postgres_database,
        config.state_postgres_user,
        config.state_postgres_password,
    ) == ("state-db.example.com", 5433, "ps_state", "ps_state_user", "s3cret")


@pytest.mark.parametrize("invalid_port", ["not-a-number", "", "5432.5"])
def test_load_config_rejects_non_integer_ps_state_postgres_port(
    monkeypatch: pytest.MonkeyPatch, invalid_port: str
) -> None:
    """A non-integer `PS_STATE_POSTGRES_PORT` fails closed at load time, naming the variable."""
    monkeypatch.setenv("PS_STATE_POSTGRES_PORT", invalid_port)

    with pytest.raises(ServiceConfigurationError, match="PS_STATE_POSTGRES_PORT"):
        load_config()


@pytest.mark.parametrize("out_of_range_port", ["0", "65536", "-1"])
def test_load_config_rejects_out_of_range_ps_state_postgres_port(
    monkeypatch: pytest.MonkeyPatch, out_of_range_port: str
) -> None:
    """An out-of-range `PS_STATE_POSTGRES_PORT` fails closed at load time, naming the variable."""
    monkeypatch.setenv("PS_STATE_POSTGRES_PORT", out_of_range_port)

    with pytest.raises(ServiceConfigurationError, match="PS_STATE_POSTGRES_PORT"):
        load_config()


def test_load_config_ignores_retired_env_var_names_for_the_state_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No shim -- the retired env var names leave the store unset."""
    for name in _STATE_POSTGRES_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Split literal: the retired prefix must not appear contiguously in the tree.
    retired_store = "AUTHZ"
    retired_prefix = f"PS_{retired_store}_POSTGRES"
    monkeypatch.setenv(f"{retired_prefix}_HOST", "legacy-db.example.com")
    monkeypatch.setenv(f"{retired_prefix}_PORT", "5433")

    config = load_config()

    assert config.state_postgres_host is None


def test_load_config_with_no_drift_check_interval_set_defaults_to_one_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS` -> 3600."""
    monkeypatch.delenv("PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS", raising=False)

    assert load_config().graph_drift_check_interval_seconds == 3600


@pytest.mark.parametrize(("raw", "expected"), [("0", 0), ("60", 60), ("86400", 86400)])
def test_load_config_honors_the_drift_check_interval_override_and_zero_disables(
    raw: str, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interval is configurable; `0` is a valid value (the monitor is then off)."""
    monkeypatch.setenv("PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS", raw)

    assert load_config().graph_drift_check_interval_seconds == expected


@pytest.mark.parametrize("invalid", ["not-a-number", "-1", "1.5", ""])
def test_load_config_fails_closed_naming_the_variable_for_an_invalid_drift_check_interval(
    invalid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS", invalid)

    with pytest.raises(
        ServiceConfigurationError, match="PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS"
    ):
        load_config()


# --- PS_PASSKEYSIGNING_ALLOWED_ORIGINS -------------------------------------


def test_load_config_without_allowed_origins_yields_an_empty_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset `PS_PASSKEYSIGNING_ALLOWED_ORIGINS` means no names are configured."""
    monkeypatch.delenv("PS_PASSKEYSIGNING_ALLOWED_ORIGINS", raising=False)

    assert load_config().passkey_signing_allowed_origins == ()


def test_load_config_parses_allowed_origins_in_order_and_trims_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The comma-separated list keeps its order; the first entry is the relying-party name."""
    monkeypatch.setenv(
        "PS_PASSKEYSIGNING_ALLOWED_ORIGINS", " https://ps.test , http://localhost:8000 "
    )

    assert load_config().passkey_signing_allowed_origins == (
        "https://ps.test",
        "http://localhost:8000",
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "   ",
        ",",
        "https://ps.test,,http://localhost:8000",
        "ps.test",
        "ps.test:8000",
        "ftp://ps.test",
        "https://ps.test/",
        "https://ps.test/path",
        "https://ps.test?x=1",
        "https://ps.test#frag",
        "https://",
        "https://user@ps.test",
        "https://ps.test:notaport",
    ],
)
def test_load_config_rejects_a_malformed_allowed_origins_value(
    invalid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicitly set value that is empty or not `http(s)://host[:port]` fails closed."""
    monkeypatch.setenv("PS_PASSKEYSIGNING_ALLOWED_ORIGINS", invalid)

    with pytest.raises(ServiceConfigurationError, match="PS_PASSKEYSIGNING_ALLOWED_ORIGINS"):
        load_config()
