"""Domain-specific exception types for the Curated Source component.

One exception type per distinct failure boundary this component owns, so callers never have to
catch a generic `Exception` or `ValueError`. The shape matches the `errors.py` module of other
`ps_service` components, for example `ps_service.restore.errors`.
"""

from __future__ import annotations


class CuratedSourceConfigurationError(Exception):
    """The configured curated-content source URL is invalid.

    Raised by :func:`ps_service.curated_source.source_url.validate_source_url` for an empty URL,
    a disallowed scheme (for example ``file://``) or a plain ``http://`` URL when TLS has not
    been explicitly opted out of. Both `ps_service.config.load_config()` (startup) and the
    runtime `set-catalog-source` MCP tool validate through that one function, so both fail the
    same way on the same input. The caller should correct the configured URL.
    """


class CuratedSourceFetchError(Exception):
    """Fetching from the configured curated-content source failed.

    Raised by :func:`ps_service.curated_source.http_fetch.fetch_bytes` (a network or HTTP
    failure) and by the catalog and manifest parsers (a malformed or structurally invalid
    response body). The message always names the source URL and the specific failure. The
    caller must not fall back to stale data on this error; it should report the failure and
    let the operator fix the source or retry.
    """


class CuratedSourceAmbiguousInstrumentIdError(Exception):
    """A requested ``instrument_id`` matches more than one catalog entry case-insensitively.

    Raised by :func:`ps_service.curated_source.instrument_lookup.resolve_canonical_instrument_id`.
    The message names the requested id and every colliding canonical id. Nothing is fetched when
    this is raised; the caller should ask for the exact catalog spelling.
    """


class CuratedSourceUnknownInstrumentIdError(Exception):
    """A requested ``instrument_id`` matches no catalog entry in any case.

    Raised by :func:`ps_service.curated_source.instrument_lookup.resolve_canonical_instrument_id`.
    The message names the requested id and the closest candidate ids in the catalog. Nothing is
    fetched when this is raised; the caller should offer those candidates.
    """
