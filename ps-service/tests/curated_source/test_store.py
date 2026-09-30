"""Tests for `ps_service.curated_source.store`, the thin catalog-override adapter (issue #130).

The adapter maps the three override operations onto the one registered runtime-config key
(`CATALOG_SOURCE_KEY`). Value validation, the audit row and transactionality belong to the
runtime-config store (`tests/runtime_config/`); here: the mapping, the validator being reached
through the store, and the actor being passed along. Uses `InMemoryRuntimeConfigStore`.
"""

from __future__ import annotations

import pytest

from curated_source._fakes import InMemoryRuntimeConfigStore, RecordedWrite
from ps_service.config import ServiceConfig
from ps_service.curated_source.config_key import CATALOG_SOURCE_KEY
from ps_service.curated_source.store import get_override, reset_override, set_override
from ps_service.runtime_config import RuntimeConfigInvalidValueError, RuntimeConfigUnavailableError

_URL = "https://example.com/override"
_ACTOR = ("actor-subject", "https://issuer.example.com/")


def _store() -> InMemoryRuntimeConfigStore:
    return InMemoryRuntimeConfigStore(
        ServiceConfig(host="127.0.0.1", port=8000, graceful_shutdown_seconds=10, logging_dir=None)
    )


def test_get_override_returns_none_when_no_override_persisted() -> None:
    assert get_override(_store()) is None


def test_set_override_then_get_override_round_trips() -> None:
    store = _store()

    set_override(store, _URL, actor=_ACTOR)

    assert get_override(store) == _URL


def test_set_override_writes_the_catalog_key_on_behalf_of_the_actor() -> None:
    store = _store()

    set_override(store, _URL, actor=_ACTOR)

    assert store.writes == [RecordedWrite("set", CATALOG_SOURCE_KEY, _ACTOR, _URL)]


def test_reset_override_removes_the_persisted_override() -> None:
    store = _store()
    set_override(store, _URL, actor=_ACTOR)

    reset_override(store, actor=_ACTOR)

    assert get_override(store) is None
    assert store.writes[-1] == RecordedWrite("reset", CATALOG_SOURCE_KEY, _ACTOR, None)


def test_reset_override_is_a_no_op_when_nothing_was_persisted() -> None:
    store = _store()

    reset_override(store, actor=_ACTOR)

    assert get_override(store) is None


def test_set_override_rejects_a_url_the_shared_validator_rejects_and_writes_nothing() -> None:
    store = _store()

    with pytest.raises(RuntimeConfigInvalidValueError, match="non-http"):
        set_override(store, "file:///etc/passwd", actor=_ACTOR)

    assert store.rows == {}
    assert store.writes == []


def test_get_override_propagates_a_failed_read_instead_of_returning_none() -> None:
    store = _store()
    store.fail_reads = True

    with pytest.raises(RuntimeConfigUnavailableError):
        get_override(store)
