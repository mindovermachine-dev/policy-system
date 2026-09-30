"""The catalog-source override's runtime-config key (issue #130).

Registers `CATALOG_SOURCE_KEY` with `ps_service.runtime_config` at import time. Registration
lives here, in the component that owns the value: `runtime_config` depends on `audit` only,
`curated_source` depends on `runtime_config`, and nothing else depends on `curated_source`'s
persistence. `ps_service.curated_source`'s package front door imports this module, so the key
is registered whenever any part of this component is used.

The validator is the shared `validate_source_url` itself (AC-BI-006): the runtime-config store
rejects exactly what startup configuration rejects, with the same message, before any write.
`audit_value` is the credential-free, query-free projection of the URL (D-REDACT):
`validate_source_url` only checks the scheme, so an operator could put `user:pass@` or a token
query string into the URL, and audit `details` (and the log) must never carry either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from ps_service.curated_source.errors import CuratedSourceConfigurationError
from ps_service.curated_source.source_url import validate_source_url
from ps_service.runtime_config import define_runtime_config_key, register_runtime_config_key

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

CATALOG_SOURCE_KEY = "curated_source.base_url"
"""Registry name of the runtime override of `ServiceConfig.curated_source_base_url`."""


def _validate(url: str, config: ServiceConfig) -> str:
    return validate_source_url(url, allow_insecure_http=config.curated_source_allow_insecure_http)


def _audit_value(url: str) -> str:
    """`url` without userinfo, query string or fragment -- the only form audit details carry."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:  # a bracket-less IPv6 literal from `hostname`; restore the brackets
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port is not None else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


register_runtime_config_key(
    define_runtime_config_key(
        CATALOG_SOURCE_KEY,
        str,
        validate=_validate,
        audit_value=_audit_value,
        validation_errors=(CuratedSourceConfigurationError,),
    )
)

__all__ = ["CATALOG_SOURCE_KEY"]
