"""Injectable-transport Authentik invitation-stage HTTP call (issue #140).

Mirrors `ps_service.curated_source.http_fetch`'s `CuratedSourceTransport` /
`fetch_bytes` shape exactly (timeout, request construction, two-except-clause
error handling) -- the proven, already-reviewed pattern this codebase uses
for an injectable outbound HTTP call (L2 DI: business logic must not
construct its own infrastructure clients inline). No `httpx`/`requests`
usage anywhere under `ps_service/src` (confirmed via repo grep) -- `urllib`
is the only outbound-HTTP mechanism this codebase's own services use.

Deliberately deviates from `http_fetch.fetch_bytes`'s own error-message
shape on one point (PLAN.md Slice 2): that function safely echoes the raw
exception/URL because neither ever carries a secret. Here, the outgoing
request carries `PS_AUTHENTIK_API_TOKEN` as a bearer token, and some
transport exceptions' own `str()` can echo request internals -- so
`AuthentikInvitationError`'s message names only the HTTP status code or the
exception's type name, never the exception object itself, never the token.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, Self

from ps_service.invitations.errors import AuthentikInvitationError

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

_TIMEOUT_SECONDS = 30.0
_INVITATION_TTL = timedelta(minutes=30)
_INVITATION_PATH = "/api/v3/stages/invitation/invitations/"
_ENROLLMENT_FLOW_PATH = "/if/flow/ps-invite-enrollment/"


class _FetchResponse(Protocol):
    """The minimal response shape `create_invitation` needs.

    A context manager whose `read()` yields the body bytes -- mirrors
    `ps_service.curated_source.http_fetch._FetchResponse` exactly, matching
    what `urllib.request.urlopen` actually returns.
    """

    def read(self) -> bytes: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *exc_info: object) -> None: ...


class AuthentikTransport(Protocol):
    """The DI seam `create_invitation` calls through.

    Structural mirror of `ps_service.curated_source.http_fetch.
    CuratedSourceTransport`: matches `urllib.request.urlopen`'s call shape
    exactly, so the real `urlopen` can be the default transport with no
    adapter/wrapper needed, while a test can substitute a fake transport
    without monkeypatching `urllib` itself.
    """

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _FetchResponse:
        """Perform the HTTP round-trip for `request`, returning the response."""
        ...


@dataclass(frozen=True, slots=True)
class InvitationResult:
    """The caller-facing result of a successful Authentik invitation call."""

    itoken: str
    invite_url: str


def create_invitation(
    config: ServiceConfig,
    email: str,
    *,
    transport: AuthentikTransport = urllib.request.urlopen,
) -> InvitationResult:
    """Create a single-use Authentik enrollment invite for `email` (AC-BI-005).

    Calls `POST {config.authentik_base_url}/api/v3/stages/invitation/invitations/`
    using `config.authentik_api_token` as the bearer credential -- PS
    Service's own configured service credential, never a caller-supplied
    token. The request body is `{"name": "ps-invite-<random>", "single_use":
    true, "fixed_data": {"email": email}, "expires": <now + 30 minutes,
    timezone-aware ISO-8601>}`: `name` carries a random suffix, not the raw
    email, because Authentik requires `name` unique and a repeat invite to
    the same address must not collide with a still-pending one. Invites lapse
    30 minutes after creation (fixed, not configurable) because an unredeemed
    invite URL would otherwise stay redeemable indefinitely; `expires` is
    computed per call.

    The invitee-facing link is built from `config.authentik_public_url` when
    set (issue #165: the API base may be an in-cluster URL the invitee cannot
    reach), else from `config.authentik_base_url`.

    Args:
        config: Supplies `authentik_base_url`/`authentik_api_token` --
            both required to be configured before this is ever reached
            (`ps_service.invitations.startup.require_authentik_credential_configured`
            fails closed at process startup otherwise).
        email: The invitee's target email, pre-associated with the
            enrollment flow's `email` prompt field via `fixed_data`.
        transport: The HTTP transport to use -- defaults to the real
            `urllib.request.urlopen`, but is call-site injectable (L2 DI) so
            tests never reach real network.

    Returns:
        The created invitation's `itoken` (Authentik's `pk`) and the
        redemption URL constructed from it.

    Raises:
        AuthentikInvitationError: The request failed for any reason (DNS,
            connection refused, timeout, a non-2xx HTTP status). The
            message names only the HTTP status/exception type -- never the
            token, never the raw response body.
    """
    body = json.dumps(
        {
            "name": f"ps-invite-{uuid.uuid4().hex[:12]}",
            "single_use": True,
            "fixed_data": {"email": email},
            "expires": (datetime.now(UTC) + _INVITATION_TTL).isoformat(),
        }
    ).encode()
    request = urllib.request.Request(  # noqa: S310 -- `config.authentik_base_url` is an operator-configured service credential's own base URL, not caller-supplied input
        f"{config.authentik_base_url}{_INVITATION_PATH}",
        data=body,
        headers={
            "Authorization": f"Bearer {config.authentik_api_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with transport(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise AuthentikInvitationError(
            f"Authentik invitation request failed: HTTP {exc.code}",
            reason_code="upstream_http_error",
        ) from exc
    except Exception as exc:
        raise AuthentikInvitationError(
            f"Authentik invitation request failed: {type(exc).__name__}",
            reason_code="upstream_unreachable",
        ) from exc
    try:
        itoken = payload["pk"]
    except (KeyError, TypeError) as exc:
        raise AuthentikInvitationError(
            "Authentik invitation response was malformed", reason_code="unexpected_error"
        ) from exc
    link_base = config.authentik_public_url or config.authentik_base_url
    invite_url = f"{link_base}{_ENROLLMENT_FLOW_PATH}?itoken={itoken}"
    return InvitationResult(itoken=itoken, invite_url=invite_url)
