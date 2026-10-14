"""Resolve the effective curated-content source: persisted override, else configured default.

`resolve_effective_source` is the one place that `GET /catalog`, the on-demand artifact fetch
and the `get-catalog-source` MCP tool check for a persisted override
(`ps_service.curated_source.store`) before falling back to
`ServiceConfig.curated_source_base_url`.

The resolution fails closed. The override lives in the PS state Postgres, a hard startup
dependency, and serving the configured default when the override cannot be read could silently
serve the wrong source, because the operator set the override to change where curated content
comes from. Any failure reading the override therefore raises, after one `failure` log entry
naming only the exception class; the fallback happens only when the store answered and holds no
override. The failure is per call: once the store is readable again the very next call sees the
live override.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ps_service.curated_source.store import get_override
from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry
from ps_service.runtime_config import RuntimeConfigError

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig
    from ps_service.logging import LogEmitter
    from ps_service.runtime_config import RuntimeConfigStore

__all__ = ["EffectiveCatalogSource", "resolve_effective_source"]

_COMPONENT = "curated_source"
_ACTION = "resolve_effective_source"


@dataclass(frozen=True, slots=True)
class EffectiveCatalogSource:
    """The curated-content source URL a caller should fetch from, and whether it is an override.

    Attributes:
        url: Base URL of the curated-content source.
        is_override: `True` when `url` is a persisted runtime override, `False` when it is the
            configured default.
    """

    url: str
    is_override: bool


def resolve_effective_source(
    config: ServiceConfig,
    *,
    store: RuntimeConfigStore,
    emitter: LogEmitter | None = None,
) -> EffectiveCatalogSource:
    """Return the effective curated-content source, failing closed when the override read fails.

    Args:
        config: The resolved service configuration; `config.curated_source_base_url` is used
            when no override is persisted.
        store: The runtime-config store the override lives in, injected by the caller so a test
            can substitute an in-memory fake.
        emitter: Log emitter for the failure entry; `None` uses the process-wide default emitter.

    Returns:
        `EffectiveCatalogSource(url=<override>, is_override=True)` when an override is
        persisted, else `EffectiveCatalogSource(url=config.curated_source_base_url,
        is_override=False)`. The latter is returned only when the store was read successfully.

    Raises:
        RuntimeConfigError: The override could not be read (unreachable store, or a stored value
            that no longer validates). Never swallowed into the default source.

    Side Effects:
        One read of the runtime-config store; on a read failure, one `failure` log entry
        (component `curated_source`, action `resolve_effective_source`) carrying only the
        exception class name. The entry is skipped when no default emitter is configured.
    """
    try:
        override_url = get_override(store)
    except RuntimeConfigError as exc:
        # Diagnostics only: the caller must get the original error even when no default
        # emitter is configured (e.g. a bare script).
        with contextlib.suppress(LoggingLifecycleError):
            emit_log_entry(
                component=_COMPONENT,
                action=_ACTION,
                outcome="failure",
                extra={"reason": type(exc).__name__},
                emitter=emitter,
            )
        raise
    if override_url is None:
        return EffectiveCatalogSource(url=config.curated_source_base_url, is_override=False)
    return EffectiveCatalogSource(url=override_url, is_override=True)
