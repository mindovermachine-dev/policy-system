"""Injectable-transport Authentik invitation-stage HTTP call.

Follows the `ps_service.curated_source.http_fetch` shape (injectable transport, timeout, request
construction, two-clause error handling) so business logic never constructs its own infrastructure
client inline. `urllib` is the only outbound-HTTP mechanism this codebase's services use.

Deliberately differs from `http_fetch.fetch_bytes` in its error messages: that function safely
echoes the raw exception because neither it nor the URL ever carries a secret. Here the outgoing
request carries `PS_AUTHENTIK_API_TOKEN` as a bearer token and some transport exceptions' own
`str()` can echo request internals, so `AuthentikInvitationError`'s message names only the HTTP
status code or the exception's type name -- never the exception object, never the token.
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

    A context manager whose `read()` yields the body bytes, matching what
    `urllib.request.urlopen` actually returns.
    """

    def read(self) -> bytes:
        """Read the whole response body.

        Returns:
            The raw body bytes; empty bytes for an empty body.

        Raises:
            OSError: The connection failed while reading.

        Side Effects:
            Consumes the response stream.
        """
        ...

    def __enter__(self) -> Self:
        """Enter the response context.

        Returns:
            The response itself.

        Raises:
            Nothing: entering never fails.

        Side Effects:
            None.
        """
        ...

    def __exit__(self, *exc_info: object) -> None:
        """Leave the response context and release the connection.

        Args:
            *exc_info: The exception type, value and traceback in flight, or `None` values.

        Returns:
            `None`; exceptions in flight are not suppressed.

        Raises:
            Nothing: closing never fails.

        Side Effects:
            Closes the underlying connection.
        """
        ...


class AuthentikTransport(Protocol):
    """The injection seam `create_invitation` calls through.

    Matches `urllib.request.urlopen`'s call shape, so the real `urlopen` is the default transport
    with no adapter, while a test substitutes a fake transport without monkeypatching `urllib`.
    """

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _FetchResponse:
        """Perform the HTTP round-trip for `request`.

        Args:
            request: The prepared HTTP request (POST with bearer credential and JSON body).
            timeout: Seconds to wait for the connection and each read; positive.

        Returns:
            The open response, to be used as a context manager.

        Raises:
            urllib.error.HTTPError: The server answered with a non-2xx status.
            urllib.error.URLError: The connection could not be made.
            TimeoutError: The server did not answer within `timeout`.

        Side Effects:
            One outbound network request.
        """
        ...


@dataclass(frozen=True, slots=True)
class InvitationResult:
    """The caller-facing result of a successful Authentik invitation call.

    Attributes:
        itoken: Authentik's invitation primary key (`pk`), the redemption token; a secret until
            redeemed. Never audited or logged.
        invite_url: The enrollment-flow URL the invitee opens, carrying `itoken` as a query
            parameter; built on the public URL when configured.
    """

    itoken: str
    invite_url: str


def create_invitation(
    config: ServiceConfig,
    email: str,
    *,
    transport: AuthentikTransport = urllib.request.urlopen,
) -> InvitationResult:
    """Create a single-use Authentik enrollment invite for `email`.

    Calls `POST {config.authentik_base_url}/api/v3/stages/invitation/invitations/` using
    `config.authentik_api_token` as the bearer credential -- PS Service's own configured service
    credential, never a caller-supplied token. The request body is
    `{"name": "ps-invite-<random>", "single_use": true, "fixed_data": {"email": email},
    "expires": <now + 30 minutes, timezone-aware ISO-8601>}`. `name` carries a random suffix, not
    the raw email, because Authentik requires `name` to be unique and a repeat invite to the same
    address must not collide with a still-pending one. Invites lapse 30 minutes after creation
    (fixed, not configurable) so an unredeemed invite URL does not stay redeemable indefinitely.

    The invitee-facing link is built from `config.authentik_public_url` when set (the API base may
    be an in-cluster URL the invitee cannot reach), else from `config.authentik_base_url`.

    Args:
        config: Supplies `authentik_base_url`, `authentik_api_token` (both required; startup
            fails closed otherwise) and the optional `authentik_public_url`.
        email: The invitee's email, pre-filled into the enrollment flow's `email` prompt field via
            `fixed_data`. Not validated here.
        transport: Callable performing the HTTP round-trip; defaults to the real
            `urllib.request.urlopen`. Called with a 30-second timeout. Tests inject a fake.

    Returns:
        An `InvitationResult` with the created invitation's `itoken` (Authentik's `pk`) and the
        redemption URL built from it.

    Raises:
        AuthentikInvitationError: The call failed, with `reason_code` `upstream_http_error`
            (non-2xx status; message names the status), `upstream_unreachable` (DNS, connection,
            timeout or an unparseable body; message names the exception type) or
            `unexpected_error` (the response has no `pk`). The message never contains the token
            or the raw response body.

    Side Effects:
        One outbound HTTPS POST that creates an invitation in Authentik.
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
