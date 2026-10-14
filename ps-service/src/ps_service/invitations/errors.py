"""Domain-specific exception types for `ps_service.invitations`.

One exception type per distinct failure boundary this component owns, never a generic
`Exception`/`ValueError`; follows the shape of `ps_service.authz.errors`.
"""

from __future__ import annotations

from typing import Literal

type InvitationFailureReason = Literal[
    "upstream_http_error", "upstream_unreachable", "unexpected_error"
]
"""Enumerated cause of a failed invite, recorded as the `user.invite` `reason_code`.

Never free text.
`upstream_http_error`: Authentik answered with a non-2xx status. `upstream_unreachable`: the
request could not be completed or the body could not be read. `unexpected_error`: the response was
malformed or the failure has no more specific category.
"""


class AuthentikCredentialConfigurationError(Exception):
    """`PS_AUTHENTIK_API_TOKEN`/`PS_AUTHENTIK_BASE_URL` are missing at startup.

    Raised by `ps_service.invitations.startup.require_authentik_credential_configured`, a
    process-configuration boundary of the same kind as
    `ps_service.authz.errors.AccessRoleBootstrapConfigurationError` but owned by this component.
    Unlike that sibling, the check has no local-test-bypass exemption. The message names exactly
    which configuration field(s) are unset. The caller should set them and restart; the service
    does not start without them.
    """


class AuthentikInvitationError(Exception):
    """`create_invitation`'s Authentik round trip failed.

    Raised by `ps_service.invitations.client.create_invitation` on a non-2xx HTTP response, any
    other transport failure (DNS, connection refused, timeout, unreadable body) or a malformed
    response. The message names only the HTTP status code or exception type -- never the raw
    response body, the configured `PS_AUTHENTIK_API_TOKEN` value, or an interpolated exception
    object -- so it is safe to show to the caller. The cause is chained as `__cause__`.

    Attributes:
        reason_code: The enumerated `InvitationFailureReason` recorded on the `failed` audit row.
    """

    def __init__(
        self, message: str, *, reason_code: InvitationFailureReason = "unexpected_error"
    ) -> None:
        """Carry the caller-safe `message` plus the enumerated `reason_code` for the audit row.

        Args:
            message: Caller-safe text naming only an HTTP status or exception type; never a
                secret or a raw upstream body.
            reason_code: The enumerated failure cause; defaults to `unexpected_error`.
        """
        super().__init__(message)
        self.reason_code: InvitationFailureReason = reason_code
