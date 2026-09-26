"""Runtime fetch of `catalog.json` from the configured curated-content source (AC-BI-003).

`fetch_catalog` GETs `{base_url}/catalog.json` (D-DEFAULT-URL's layout) and
parses it into the exact same `CuratedInstrumentEntry` shape
`ps_service.api.catalog.load_regulation_catalog` already parses from the
build-time-packaged copy (field-for-field reuse of that shape, not its
`importlib.resources` plumbing) -- so `GET /catalog`'s route handler and
response model need no change beyond swapping where the tuple comes from.

Also holds this component's FastAPI dependency-injection seam
(:class:`CuratedCatalogDependencies`, :func:`build_default_curated_catalog_dependencies`),
mirroring `ps_service.api.restore_orchestration.RestoreDependencies`'s exact
shape -- a plain provider a test can swap wholesale via
`app.dependency_overrides`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from ps_service.api.catalog import CuratedInstrumentEntry
from ps_service.curated_source.errors import CuratedSourceFetchError
from ps_service.curated_source.http_fetch import CuratedSourceTransport, fetch_bytes
from ps_service.curated_source.resolve import EffectiveCatalogSource, resolve_effective_source
from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ps_service.config import ServiceConfig
    from ps_service.curated_source.store import GraphHandle
    from ps_service.logging import LogEmitter

_CATALOG_FILENAME = "catalog.json"
_COMPONENT = "curated_source"
_ACTION = "fetch_catalog"

_EXTERNAL = "external"
_INTERNAL = "internal"


def _require_str(body: dict[str, object], field: str, *, url: str) -> str:
    """Return `body[field]` if it is a string; raise `CuratedSourceFetchError` otherwise."""
    value = body.get(field)
    if not isinstance(value, str):
        message = (
            f"malformed catalog.json from curated source {url!r}: "
            f"field {field!r} is missing or not a string"
        )
        raise CuratedSourceFetchError(message)
    return value


def _require_optional_str(body: dict[str, object], field: str, *, url: str) -> str | None:
    """Return `body[field]` if it is `None`/a string; raise `CuratedSourceFetchError` otherwise."""
    value = body.get(field)
    if value is not None and not isinstance(value, str):
        message = (
            f"malformed catalog.json from curated source {url!r}: "
            f"field {field!r} must be a string or null"
        )
        raise CuratedSourceFetchError(message)
    return value


def _parse_one_entry(item: object, *, url: str) -> CuratedInstrumentEntry:
    """Parse one raw `catalog.json` array element into a `CuratedInstrumentEntry`.

    Raises:
        CuratedSourceFetchError: `item` is not an object, a required field is
            missing/mistyped, or `source_type` is neither `"external"` nor
            `"internal"`.
    """
    if not isinstance(item, dict):
        message = f"malformed catalog.json from curated source {url!r}: entry is not an object"
        raise CuratedSourceFetchError(message)
    body = cast("dict[str, object]", item)
    source_type = _require_str(body, "source_type", url=url)
    if source_type not in (_EXTERNAL, _INTERNAL):
        message = (
            f"malformed catalog.json from curated source {url!r}: "
            f"source_type must be 'external' or 'internal', got {source_type!r}"
        )
        raise CuratedSourceFetchError(message)
    return CuratedInstrumentEntry(
        instrument_id=_require_str(body, "instrument_id", url=url),
        celex=_require_optional_str(body, "celex", url=url),
        title=_require_str(body, "title", url=url),
        source_type=source_type,
        jurisdiction=_require_optional_str(body, "jurisdiction", url=url),
        short_name=_require_str(body, "short_name", url=url),
        version=_require_str(body, "version", url=url),
    )


def _parse_catalog_entries(raw_bytes: bytes, *, url: str) -> tuple[CuratedInstrumentEntry, ...]:
    """Parse `catalog.json`'s raw response bytes into `CuratedInstrumentEntry` objects.

    Raises:
        CuratedSourceFetchError: `raw_bytes` is not valid UTF-8 JSON, is not a
            JSON array, or any entry fails `_parse_one_entry` -- always names
            `url` (AC-BI-006). No partial result is ever returned.
    """
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        message = f"malformed catalog.json from curated source {url!r}: not valid JSON ({exc})"
        raise CuratedSourceFetchError(message) from exc
    if not isinstance(payload, list):
        message = f"malformed catalog.json from curated source {url!r}: expected a JSON array"
        raise CuratedSourceFetchError(message)
    items = cast("Iterable[object]", payload)
    return tuple(_parse_one_entry(item, url=url) for item in items)


def fetch_catalog(
    base_url: str,
    *,
    transport: CuratedSourceTransport | None = None,
    emitter: LogEmitter | None = None,
) -> tuple[CuratedInstrumentEntry, ...]:
    """Fetch and parse `catalog.json` from `{base_url}/catalog.json` (AC-BI-003).

    Emits one structured log entry on completion -- `outcome="success"` with
    the resolved instrument count, or `outcome="failed"` with the error
    message -- mirroring `ps_service.restore.restore_instrument`'s own
    `emit_log_entry` usage (matched here by the `curated_source`/
    `fetch_catalog` component/action pair).

    Args:
        base_url: The configured curated-content source's base URL
            (D-DEFAULT-URL layout: `{base_url}/catalog.json`,
            `{base_url}/{instrument_id}/...`).
        transport: The HTTP transport to use -- `None` (the default) keeps
            `http_fetch.fetch_bytes`'s own default (the real
            `urllib.request.urlopen`); call-site injectable so tests never
            reach real network.
        emitter: The `LogEmitter` to emit through -- `None` (the default)
            falls back to the process-wide default emitter `Logging.configure()`
            installs (matches every other `emitter: LogEmitter | None = None`
            call site in this codebase); call-site injectable so tests can
            substitute their own without a process-wide `configure()` call.

    Returns:
        Every curated entry, in file order, as `CuratedInstrumentEntry`
        tuples.

    Raises:
        CuratedSourceFetchError: The source is unreachable, or the response
            is missing/malformed (AC-BI-006) -- never a silent fallback to
            stale data.
    """
    url = f"{base_url}/{_CATALOG_FILENAME}"
    try:
        raw_bytes = (
            fetch_bytes(url, transport=transport) if transport is not None else fetch_bytes(url)
        )
        entries = _parse_catalog_entries(raw_bytes, url=url)
    except CuratedSourceFetchError as exc:
        emit_log_entry(
            component=_COMPONENT,
            action=_ACTION,
            outcome="failed",
            extra={"source": base_url, "error": str(exc)},
            emitter=emitter,
        )
        raise
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        outcome="success",
        extra={"source": base_url, "instrument_count": len(entries)},
        emitter=emitter,
    )
    return entries


class FetchCatalogCall(Protocol):
    """Call shape of :func:`fetch_catalog` -- the DI seam `list_curated_catalog` calls through."""

    def __call__(self, base_url: str) -> tuple[CuratedInstrumentEntry, ...]:
        """Fetch and parse `catalog.json` from `{base_url}/catalog.json`."""
        ...


class ResolveEffectiveSourceCall(Protocol):
    """Call shape of :func:`ps_service.curated_source.resolve.resolve_effective_source`.

    The DI seam `list_curated_catalog` calls through to check for a
    persisted override before fetching (issue #125, Slice 3, AC-BI-013).
    """

    def __call__(self, config: ServiceConfig) -> EffectiveCatalogSource:
        """Return the effective curated-content source for `config`."""
        ...


@dataclass(frozen=True, slots=True)
class CuratedCatalogDependencies:
    """Everything `GET /catalog`'s route handler needs that is not per-request.

    Mirrors `ps_service.api.restore_orchestration.RestoreDependencies`'s own
    injection-seam shape.
    """

    fetch_catalog: FetchCatalogCall
    resolve_effective_source: ResolveEffectiveSourceCall


def _default_open_graph(config: ServiceConfig) -> GraphHandle:
    """Open the real single-tenant `policy_system` graph the override lives in.

    `ps_service.company_merge.falkordb_client` is imported **function-locally**
    so that importing `ps_service.main` never transitively loads
    `ps_service.company_merge` at module load (M6 / the Process Harness
    decoupling guarantee) -- mirrors `restore_orchestration._default_open_db`
    exactly.
    """
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Company Merge at import
        connect_from_config,
        select_graph,
        single_tenant_graph_name,
    )

    return select_graph(connect_from_config(config), single_tenant_graph_name())


def build_default_curated_catalog_dependencies() -> CuratedCatalogDependencies:
    """Wire the real :func:`fetch_catalog`/:func:`resolve_effective_source` into a bundle.

    Returns:
        A `CuratedCatalogDependencies` bound to the production `fetch_catalog`
        (the real HTTP transport, `http_fetch.fetch_bytes`'s own default) and
        the production `resolve_effective_source`, checking the persisted
        override against the real single-tenant `policy_system` graph
        (issue #125, Slice 3).
    """

    def _resolve_effective_source(config: ServiceConfig) -> EffectiveCatalogSource:
        return resolve_effective_source(config, open_graph=lambda: _default_open_graph(config))

    return CuratedCatalogDependencies(
        fetch_catalog=fetch_catalog, resolve_effective_source=_resolve_effective_source
    )
