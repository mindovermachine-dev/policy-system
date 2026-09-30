"""Thin adapter mapping the catalog-source override onto the runtime-config store (issue #130).

The override is one registered key (`CATALOG_SOURCE_KEY`) in the `runtime_config` table of the
PS state Postgres. This module keeps a three-function seam for `resolve.py` and the MCP tools;
value validation (the shared `validate_source_url`), the same-transaction audit row and the
fail-closed error discipline all belong to `ps_service.runtime_config`, not here.

Nothing here reads or writes the graph database (the pre-#130 singleton override node in it is
left dead; an override set before this change is silently ignored, so re-run
`set-catalog-source`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ps_service.curated_source.config_key import CATALOG_SOURCE_KEY

if TYPE_CHECKING:
    from ps_service.runtime_config import RuntimeConfigStore

__all__ = ["get_override", "reset_override", "set_override"]


def get_override(store: RuntimeConfigStore) -> str | None:
    """Return the persisted curated-content source override URL, or `None` if unset.

    Raises:
        RuntimeConfigUnavailableError: the store could not be read -- never `None`, so a
            failed read can never look like "no override" (fail closed).
        RuntimeConfigInvalidValueError: the stored value no longer validates.
    """
    # `RuntimeConfigStore.get` returns a value already type-checked against the key (`str`).
    return cast("str | None", store.get(CATALOG_SOURCE_KEY))


def set_override(store: RuntimeConfigStore, url: str, *, actor: tuple[str, str]) -> None:
    """Persist `url` as the effective curated-content source override, audited as `actor`.

    Raises:
        RuntimeConfigInvalidValueError: `url` failed `validate_source_url`; nothing was written.
        RuntimeConfigUnavailableError / RuntimeConfigPersistenceError: see `RuntimeConfigStore.set`.
    """
    store.set(CATALOG_SOURCE_KEY, url, actor=actor)


def reset_override(store: RuntimeConfigStore, *, actor: tuple[str, str]) -> None:
    """Delete the persisted override, if any, audited as `actor` (a no-op when none is set).

    Raises:
        RuntimeConfigUnavailableError / RuntimeConfigPersistenceError: see
            `RuntimeConfigStore.reset`.
    """
    store.reset(CATALOG_SOURCE_KEY, actor=actor)
