"""Shared http(s) and TLS validation for the curated-content source URL.

One function, :func:`validate_source_url`, is called from both `ps_service.config.load_config()`
(startup) and the runtime `set-catalog-source` MCP tool, never two independent validators, so
both surfaces reject exactly the same inputs.

It follows the `https://`-only guard in `ps_service.ingestion.adapters.cellar_eli.fetch`
(`_fetch`), widened to accept `http://` only when `allow_insecure_http` is explicitly `True`.
"""

from __future__ import annotations

from ps_service.curated_source.errors import CuratedSourceConfigurationError

_HTTPS_PREFIX = "https://"
_HTTP_PREFIX = "http://"


def validate_source_url(url: str, *, allow_insecure_http: bool) -> str:
    """Validate a curated-content source base URL, returning it unchanged if valid.

    Args:
        url: The candidate base URL (e.g. a repo raw-content root such as
            ``https://raw.githubusercontent.com/<org>/<repo>/main/curated-content``).
        allow_insecure_http: Whether a plain ``http://`` URL is accepted.
            TLS is required unless this is explicitly `True`.

    Returns:
        `url` unchanged, once validated.

    Raises:
        CuratedSourceConfigurationError: `url` is empty/whitespace-only, uses
            any scheme other than `https://` or `http://` (for example `file:///etc/passwd`), or
            uses `http://` without `allow_insecure_http=True`.
    """
    if not url.strip():
        message = "curated-content source URL must not be empty or whitespace-only"
        raise CuratedSourceConfigurationError(message)
    if url.startswith(_HTTPS_PREFIX):
        return url
    if url.startswith(_HTTP_PREFIX):
        if allow_insecure_http:
            return url
        message = (
            f"refusing plain-http curated-content source URL {url!r}: "
            "set allow_insecure_http=True (PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP=true) "
            "to opt in to an unencrypted connection"
        )
        raise CuratedSourceConfigurationError(message)
    message = f"refusing non-http(s) curated-content source URL {url!r}"
    raise CuratedSourceConfigurationError(message)
