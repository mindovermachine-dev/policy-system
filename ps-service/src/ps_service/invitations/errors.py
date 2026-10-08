"""Domain-specific exception types for `ps_service.invitations` (issue #140).

One exception type per distinct failure boundary this component owns, never
a generic `Exception`/`ValueError` (L1/L2 Error Handling) -- mirrors the
shape of `ps_service.authz.errors` exactly.
"""

from __future__ import annotations

from typing import Literal

type InvitationFailureReason = Literal[
    "upstream_http_error", "upstream_unreachable", "unexpected_error"
]
"""Enumerated cause of a failed invite; the `user.invite` `reason_code` (never free text)."""


class AuthentikCredentialConfigurationError(Exception):
    """`PS_AUTHENTIK_API_TOKEN`/`PS_AUTHENTIK_BASE_URL` are missing (issue #140, AC-BI-003).

    Raised by :func:`ps_service.invitations.startup.require_authentik_credential_configured`
    -- a distinct process-configuration boundary, same category as
    `ps_service.authz.errors.AccessRoleBootstrapConfigurationError` but owned
    by this component, not `ps_service.authz` (this module's own docstring:
    one exception type per distinct failure boundary). Unlike that sibling
    error, this check carries no local-test-bypass exemption (CHANGES.md
    #140 Row 2): AC-BI-003's wording has no bypass carve-out, unlike
    AC-BI-002's. The message names exactly which variable(s) are unset.
    """


class AuthentikInvitationError(Exception):
    """`create_invitation`'s Authentik round trip failed (issue #140, AC-BI-008).

    Raised by :func:`ps_service.invitations.client.create_invitation` on any
    failure of the outbound call to Authentik's invitation-stage API -- a
    non-2xx HTTP response or any other transport failure (DNS, connection
    refused, timeout). This module's own docstring: one exception type per
    distinct failure boundary. The message names only the HTTP status code
    or exception type -- never the raw response body, never the configured
    `PS_AUTHENTIK_API_TOKEN` value, and never an interpolated raw exception
    object (PLAN.md Slice 2's explicit deviation from
    `ps_service.curated_source.http_fetch.fetch_bytes`'s own message shape,
    which is safe to echo raw because it never carries a bearer token).
    """

    def __init__(
        self, message: str, *, reason_code: InvitationFailureReason = "unexpected_error"
    ) -> None:
        """Carry the caller-safe `message` plus the enumerated `reason_code` for the audit row."""
        super().__init__(message)
        self.reason_code: InvitationFailureReason = reason_code
