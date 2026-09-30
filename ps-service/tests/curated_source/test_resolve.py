"""Tests for `ps_service.curated_source.resolve.resolve_effective_source` (issues #125, #130).

AC-BI-013 (a persisted override takes precedence over the env-var/default), AC-BI-008 (a reset
falls back to it) and, since issue #130, D-FAILCLOSED: a failed override read raises and is
logged with the exception class only -- the env-var/default is never served in its place
(AC-BI-010). Runs against `InMemoryRuntimeConfigStore`, a hand-written fake at the
persistence boundary that still applies the catalog key's real type check and validator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from curated_source._fakes import InMemoryRuntimeConfigStore
from ps_service.config import ServiceConfig
from ps_service.curated_source.config_key import CATALOG_SOURCE_KEY
from ps_service.curated_source.resolve import EffectiveCatalogSource, resolve_effective_source
from ps_service.runtime_config import RuntimeConfigInvalidValueError, RuntimeConfigUnavailableError

if TYPE_CHECKING:
    from api._fakes import MakeEmitter, ReadLines

_DEFAULT_URL = "https://example.com/default-source"
_OVERRIDE_URL = "https://example.com/persisted-override"
_ACTOR = ("actor-subject", "https://issuer.example.com/")


def _config(*, default_url: str = _DEFAULT_URL) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        curated_source_base_url=default_url,
    )


def test_returns_override_when_set() -> None:
    config = _config()
    store = InMemoryRuntimeConfigStore(config)
    store.set(CATALOG_SOURCE_KEY, _OVERRIDE_URL, actor=_ACTOR)

    result = resolve_effective_source(config, store=store)

    assert result == EffectiveCatalogSource(url=_OVERRIDE_URL, is_override=True)


def test_returns_env_default_when_no_override_persisted() -> None:
    config = _config()

    result = resolve_effective_source(config, store=InMemoryRuntimeConfigStore(config))

    assert result == EffectiveCatalogSource(url=_DEFAULT_URL, is_override=False)


def test_reset_falls_back_to_env_default() -> None:
    config = _config()
    store = InMemoryRuntimeConfigStore(config)
    store.set(CATALOG_SOURCE_KEY, _OVERRIDE_URL, actor=_ACTOR)
    store.reset(CATALOG_SOURCE_KEY, actor=_ACTOR)

    result = resolve_effective_source(config, store=store)

    assert result == EffectiveCatalogSource(url=_DEFAULT_URL, is_override=False)


def test_raises_unavailable_error_when_override_read_fails_and_never_returns_default(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    config = _config()
    store = InMemoryRuntimeConfigStore(config, fail_reads=True)

    with pytest.raises(RuntimeConfigUnavailableError):
        resolve_effective_source(config, store=store, emitter=emitter)

    emitter.flush()
    (entry,) = read_lines(log_path)
    assert entry["component"] == "curated_source"
    assert entry["action"] == "resolve_effective_source"
    assert entry["outcome"] == "failure"
    assert entry["reason"] == "RuntimeConfigUnavailableError"


def test_failure_log_entry_carries_the_exception_class_never_its_text(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    config = _config()
    store = InMemoryRuntimeConfigStore(config, fail_reads=True)

    with pytest.raises(RuntimeConfigUnavailableError):
        resolve_effective_source(config, store=store, emitter=emitter)

    emitter.flush()
    assert "temporarily unavailable" not in str(read_lines(log_path))


def test_a_stored_value_that_no_longer_validates_fails_closed() -> None:
    config = _config()
    store = InMemoryRuntimeConfigStore(config)
    store.rows[CATALOG_SOURCE_KEY] = "file:///etc/passwd"  # bypasses `set`, like a hand edit

    with pytest.raises(RuntimeConfigInvalidValueError):
        resolve_effective_source(config, store=store)


def test_override_precedence_returns_once_the_store_recovers() -> None:
    """The fail-closed error is per call: nothing sticky suppresses a persisted override."""
    config = _config()
    store = InMemoryRuntimeConfigStore(config)
    store.set(CATALOG_SOURCE_KEY, _OVERRIDE_URL, actor=_ACTOR)
    store.fail_reads = True
    with pytest.raises(RuntimeConfigUnavailableError):
        resolve_effective_source(config, store=store)

    store.fail_reads = False

    assert resolve_effective_source(config, store=store) == EffectiveCatalogSource(
        url=_OVERRIDE_URL, is_override=True
    )


def test_failure_surfaces_the_read_error_even_when_no_default_emitter_is_configured() -> None:
    """Logging is diagnostics: a missing process emitter must not mask the fail-closed error."""
    config = _config()
    store = InMemoryRuntimeConfigStore(config, fail_reads=True)

    with pytest.raises(RuntimeConfigUnavailableError):
        resolve_effective_source(config, store=store)
