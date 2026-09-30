"""Tests for the catalog-source runtime-config key (issue #130, Slice 4).

AC-BI-006: the key's validator IS `validate_source_url` (rejects exactly what the shared
validator rejects, with its message). D-REDACT: only the credential-free, query-free
projection of a URL may reach an audit row's `details`. CHANGES F8: registration happens on
importing `ps_service.curated_source`, so no caller can reach the registry without the key.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from ps_service.config import ServiceConfig
from ps_service.curated_source import config_key
from ps_service.curated_source.errors import CuratedSourceConfigurationError
from ps_service.curated_source.source_url import validate_source_url
from ps_service.runtime_config import (
    RuntimeConfigInvalidValueError,
    prepare_runtime_config_value,
    require_runtime_config_key,
    resolve_runtime_config_key,
)


def _config(*, allow_insecure_http: bool = False) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        curated_source_allow_insecure_http=allow_insecure_http,
    )


@pytest.mark.parametrize("entry_module", ["store", "catalog_client", "resolve"])
def test_registry_resolves_catalog_key_after_importing_only_the_store_and_api_dependency_path(
    entry_module: str,
) -> None:
    """A fresh interpreter that imports only one entry module (never `config_key` itself)."""
    script = (
        f"import ps_service.curated_source.{entry_module}\n"
        "from ps_service.runtime_config import resolve_runtime_config_key\n"
        f"assert resolve_runtime_config_key({config_key.CATALOG_SOURCE_KEY!r}) is not None\n"
    )

    completed = subprocess.run(  # noqa: S603 -- fixed interpreter and a literal script
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr


def test_catalog_key_is_registered_under_its_documented_name() -> None:
    key = resolve_runtime_config_key(config_key.CATALOG_SOURCE_KEY)

    assert key is not None
    assert key.name == "curated_source.base_url"


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/x", "", "   ", "http://example.com/plain"]
)
def test_catalog_key_validator_is_validate_source_url(url: str) -> None:
    key = require_runtime_config_key(config_key.CATALOG_SOURCE_KEY)

    with pytest.raises(CuratedSourceConfigurationError) as direct:
        validate_source_url(url, allow_insecure_http=False)
    with pytest.raises(CuratedSourceConfigurationError) as via_key:
        key.validate(url, _config())
    with pytest.raises(RuntimeConfigInvalidValueError) as via_store_path:
        prepare_runtime_config_value(_config(), key, url)

    assert str(via_key.value) == str(direct.value)
    assert str(via_store_path.value) == str(direct.value)


def test_catalog_key_honours_the_insecure_http_opt_in_from_config() -> None:
    key = require_runtime_config_key(config_key.CATALOG_SOURCE_KEY)

    assert (
        prepare_runtime_config_value(
            _config(allow_insecure_http=True), key, "http://example.com/plain"
        )
        == "http://example.com/plain"
    )


def test_catalog_key_rejects_a_non_string_value() -> None:
    key = require_runtime_config_key(config_key.CATALOG_SOURCE_KEY)

    with pytest.raises(RuntimeConfigInvalidValueError):
        prepare_runtime_config_value(_config(), key, 12)


@pytest.mark.parametrize(
    ("raw", "projected"),
    [
        ("https://user:hunter2@example.com/curated", "https://example.com/curated"),
        ("https://example.com/curated?token=hunter2", "https://example.com/curated"),
        ("https://user:pw@example.com:8443/c?a=b#frag", "https://example.com:8443/c"),
        ("https://[::1]:8443/curated?x=1", "https://[::1]:8443/curated"),
        ("https://example.com/plain", "https://example.com/plain"),
    ],
)
def test_details_projection_strips_url_userinfo_and_query(raw: str, projected: str) -> None:
    key = require_runtime_config_key(config_key.CATALOG_SOURCE_KEY)

    assert key.project(raw) == projected
