"""Resolve the effective curated-content source: override, else env-var/default (D-FAILOPEN).

`resolve_effective_source` is the one place `GET /catalog`, the on-demand
artifact fetch, and the `get-catalog-source` MCP tool all check for a
persisted `CatalogSourceOverride` (`ps_service.curated_source.store`) before
falling back to `ServiceConfig.curated_source_base_url` (AC-BI-013).

D-FAILOPEN: `ps-service/tests/api/test_routes_catalog.py`'s
`test_get_catalog_succeeds_with_no_falkordb_or_llm_fixture_wired` is an
existing, documented guarantee that `GET /catalog` needs zero FalkorDB
dependency. Now that `GET /catalog` must check for an override on every
call, a FalkorDB outage during that check must not take the route down --
ANY exception raised while opening the graph or reading the override is
treated as "no override," falling through to the env-var/default value, with
a warning logged (never silently swallowed) so an operator can tell the
override check was skipped. This is a per-call, transient fallback: once
FalkorDB is reachable again, the very next call sees the live override
again -- it is never a sticky/cached decision that permanently suppresses a
persisted override.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ps_service.curated_source.store import get_override
from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.config import ServiceConfig
    from ps_service.curated_source.store import GraphHandle
    from ps_service.logging import LogEmitter

__all__ = ["EffectiveCatalogSource", "resolve_effective_source"]

_COMPONENT = "curated_source"
_ACTION = "resolve_effective_source"


@dataclass(frozen=True, slots=True)
class EffectiveCatalogSource:
    """The curated-content source URL a caller should fetch from, and whether it is an override."""

    url: str
    is_override: bool


def resolve_effective_source(
    config: ServiceConfig,
    *,
    open_graph: Callable[[], GraphHandle],
    emitter: LogEmitter | None = None,
) -> EffectiveCatalogSource:
    """Return the effective curated-content source (AC-BI-013), fail-open on FalkorDB outage.

    Args:
        config: The resolved service configuration -- names the env-var/
            default URL to fall back to (`config.curated_source_base_url`).
        open_graph: Opens a `GraphHandle` for the singleton `policy_system`
            graph the override lives in. Zero-arg and call-site injectable
            so a test can script a graph-open failure without a real
            FalkorDB instance.
        emitter: The `LogEmitter` to emit the fallback warning through --
            `None` (the default) falls back to the process-wide default
            emitter, matching every other `emitter: LogEmitter | None = None`
            call site in this codebase.

    Returns:
        `EffectiveCatalogSource(url=<override>, is_override=True)` when a
        persisted override exists and the override check succeeded;
        `EffectiveCatalogSource(url=config.curated_source_base_url,
        is_override=False)` when no override is persisted, OR when opening
        the graph or reading the override raised for any reason (D-FAILOPEN
        -- never raised to the caller).
    """
    try:
        graph = open_graph()
        override_url = get_override(graph)
    except Exception as exc:  # noqa: BLE001 -- D-FAILOPEN: any override-check failure falls through, never raised to the caller
        emit_log_entry(
            component=_COMPONENT,
            action=_ACTION,
            outcome="fallback",
            extra={"reason": str(exc)},
            emitter=emitter,
        )
        return EffectiveCatalogSource(url=config.curated_source_base_url, is_override=False)
    if override_url is None:
        return EffectiveCatalogSource(url=config.curated_source_base_url, is_override=False)
    return EffectiveCatalogSource(url=override_url, is_override=True)
