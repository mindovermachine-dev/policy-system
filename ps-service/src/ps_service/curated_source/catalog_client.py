"""Runtime fetch of `catalog.json` from the configured curated-content source.

`fetch_catalog` GETs `{base_url}/catalog.json` and parses it into `CuratedInstrumentEntry`, the
shape `ps_service.api.catalog.load_regulation_catalog` produces from the packaged copy (the
entry shape is shared; the `importlib.resources` plumbing is not), so `GET /catalog` needs only
a different source of the entry tuple.

The module also holds this component's FastAPI dependency-injection seam
(:class:`CuratedCatalogDependencies`, :func:`build_default_curated_catalog_dependencies`),
shaped like `ps_service.api.restore_orchestration.RestoreDependencies`: a plain provider that
a test can swap wholesale through `app.dependency_overrides`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from ps_service.api.catalog import CuratedInstrumentEntry
from ps_service.audit import PsycopgAuditStore
from ps_service.curated_source.errors import CuratedSourceFetchError
from ps_service.curated_source.http_fetch import CuratedSourceTransport, fetch_bytes
from ps_service.curated_source.resolve import EffectiveCatalogSource, resolve_effective_source
from ps_service.logging.facade import emit_log_entry
from ps_service.runtime_config import PsycopgRuntimeConfigStore

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ps_service.config import ServiceConfig
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
        CuratedSourceFetchError: `raw_bytes` is not valid UTF-8 JSON, is not a JSON array, or
            any entry fails `_parse_one_entry`. The message always names `url`. No partial
            result is ever returned.
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
    """Fetch and parse `catalog.json` from `{base_url}/catalog.json`.

    Every entry must be well formed; one bad entry fails the whole fetch, and there is never a
    silent fallback to stale data.

    Args:
        base_url: Base URL of the configured curated-content source. Normally has no trailing
            slash.
        transport: HTTP transport; `None` uses the default of `http_fetch.fetch_bytes` (real
            network access). Injectable so tests never reach the network.
        emitter: Log emitter; `None` uses the process-wide default emitter.

    Returns:
        Every curated entry as a `CuratedInstrumentEntry`, in file order. An empty JSON array
        yields an empty tuple.

    Raises:
        CuratedSourceFetchError: The source is unreachable, or the response is missing or
            malformed.

    Side Effects:
        One HTTP GET and one structured log entry (component `curated_source`, action
        `fetch_catalog`): outcome `success` with the instrument count, or `failed` with the
        error text.
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

    The dependency-injection seam that `list_curated_catalog` calls through to check for a
    persisted override before fetching.
    """

    def __call__(self, config: ServiceConfig) -> EffectiveCatalogSource:
        """Return the effective curated-content source for `config`.

        Args:
            config: Resolved service configuration; its `curated_source_base_url` applies when no
                override is persisted.

        Returns:
            The effective source: the persisted override, else the configured default.

        Raises:
            RuntimeConfigError: the persisted override could not be read.
        """
        ...


@dataclass(frozen=True, slots=True)
class CuratedCatalogDependencies:
    """Everything `GET /catalog`'s route handler needs that is not per-request.

    Shaped like `ps_service.api.restore_orchestration.RestoreDependencies`.

    Attributes:
        fetch_catalog: Callable that fetches and parses the catalog of a given base URL.
        resolve_effective_source: Callable that returns the effective source for a
            `ServiceConfig`, taking any persisted override into account.
    """

    fetch_catalog: FetchCatalogCall
    resolve_effective_source: ResolveEffectiveSourceCall


def build_default_curated_catalog_dependencies() -> CuratedCatalogDependencies:
    """Wire the production catalog fetcher and source resolver into a dependency bundle.

    Returns:
        A `CuratedCatalogDependencies` bound to :func:`fetch_catalog` (real HTTP transport) and
        to a resolver that checks the override persisted in the PS state Postgres `runtime_config`
        table. A failed override read raises instead of falling back to the default source.
    """

    def _resolve_effective_source(config: ServiceConfig) -> EffectiveCatalogSource:
        store = PsycopgRuntimeConfigStore(config, audit_store=PsycopgAuditStore(config))
        return resolve_effective_source(config, store=store)

    return CuratedCatalogDependencies(
        fetch_catalog=fetch_catalog, resolve_effective_source=_resolve_effective_source
    )
