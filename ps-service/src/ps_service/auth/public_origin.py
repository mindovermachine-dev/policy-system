"""The origin PS Service publishes for itself in OAuth protected-resource discovery.

Both the RFC 9728 metadata documents and the `WWW-Authenticate` challenge name the service by
origin. The configured `PS_PUBLIC_URL` is the only source of that origin; a request header is
consulted solely when no public URL is configured, which startup permits only in local-test
mode.
"""

from __future__ import annotations


def resolve_public_origin(public_url: str | None, *, scheme: str, host: str) -> str:
    """Return the origin to publish: the configured public URL, else the request's own.

    Args:
        public_url: The configured public origin, or `None` when none is configured.
        scheme: The request's URL scheme, used only when `public_url` is `None`.
        host: The request's `Host` header value, used only when `public_url` is `None`.

    Returns:
        `public_url` unchanged when set; otherwise `scheme://host`.
    """
    if public_url is not None:
        return public_url
    return f"{scheme}://{host}"
