"""Thin adapter mapping the catalog-source override onto the runtime-config store.

The override is one registered key (`CATALOG_SOURCE_KEY`) in the `runtime_config` table of the
PS state Postgres. The module is a three-function seam for `resolve.py` and the MCP tools; value
validation (the shared `validate_source_url`), the same-transaction audit row and the
fail-closed error discipline all belong to `ps_service.runtime_config`, not here.

Nothing here reads or writes the graph database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ps_service.curated_source.config_key import CATALOG_SOURCE_KEY

if TYPE_CHECKING:
    from ps_service.runtime_config import RuntimeConfigStore

__all__ = ["get_override", "reset_override", "set_override"]


def get_override(store: RuntimeConfigStore) -> str | None:
    """Return the persisted curated-content source override URL, or `None` if unset.

    Args:
        store: The runtime-config store holding the override.

    Returns:
        The override URL, or `None` when the store answered and holds no override. A failed read
        never returns `None`, so it can never look like "no override" (fail closed).

    Raises:
        RuntimeConfigUnavailableError: The store could not be read.
        RuntimeConfigInvalidValueError: The stored value no longer validates.
    """
    # `RuntimeConfigStore.get` returns a value already type-checked against the key (`str`).
    return cast("str | None", store.get(CATALOG_SOURCE_KEY))


def set_override(store: RuntimeConfigStore, url: str, *, actor: tuple[str, str]) -> None:
    """Persist `url` as the effective curated-content source override, audited as `actor`.

    Args:
        store: The runtime-config store to write to.
        url: The new base URL; must pass `validate_source_url` under the service configuration.
        actor: The acting principal as `(subject, issuer)`, recorded on the audit row.

    Returns:
        None.

    Raises:
        RuntimeConfigInvalidValueError: `url` failed `validate_source_url`; nothing was written.
        RuntimeConfigUnavailableError: See `RuntimeConfigStore.set`.
        RuntimeConfigPersistenceError: See `RuntimeConfigStore.set`.

    Side Effects:
        Upserts the override row and writes one `runtime_config.set` audit row in one
        transaction.
    """
    store.set(CATALOG_SOURCE_KEY, url, actor=actor)


def reset_override(store: RuntimeConfigStore, *, actor: tuple[str, str]) -> None:
    """Delete the persisted override so the configured default source applies again.

    Args:
        store: The runtime-config store to delete from.
        actor: The acting principal as `(subject, issuer)`, recorded on the audit row.

    Returns:
        None. Resetting when no override is set deletes nothing and is not an error.

    Raises:
        RuntimeConfigUnavailableError: See `RuntimeConfigStore.reset`.
        RuntimeConfigPersistenceError: See `RuntimeConfigStore.reset`.

    Side Effects:
        Deletes the override row, if any, and writes one `runtime_config.reset` audit row in one
        transaction.
    """
    store.reset(CATALOG_SOURCE_KEY, actor=actor)
