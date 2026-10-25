"""The client status endpoint `GET /status` of the PS Service REST API.

One always-200 answer for an external client (ps-cli, a monitor): the service version and the
readiness Process Harness has recorded, plus, for a caller whose bearer token verifies, the
names of the unhealthy dependencies and of the graphs that refuse writes. The handler never
probes a dependency and never writes the dependency health registry or the app's readiness: it
only reads the `RecordedReadiness` that Process Harness publishes on `app.state`. The route is
on the REST bearer middleware's open list, so a missing or rejected token is never a 401; the
handler checks an optional token itself and treats an unverified caller as anonymous.
"""

from __future__ import annotations

import asyncio
from importlib.metadata import version as installed_version
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from fastapi import Depends, Request

from ps_service.api.dependencies import (
    get_service_config,
    provide_recorded_readiness,
    provide_token_verifier,
)
from ps_service.api.models import ServiceStatusResponse
from ps_service.auth.verifier import (
    PsTokenVerifier,  # noqa: TC001 -- FastAPI resolves the handler's annotations at runtime
)
from ps_service.config import (
    ServiceConfig,  # noqa: TC001 -- FastAPI resolves the handler's annotations at runtime
)
from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_BEARER_PREFIX = "Bearer "
_LOG_COMPONENT = "entrypoint"
_LOG_ACTION = "status_token_check"


class RecordedReadiness(Protocol):
    """The readiness Process Harness has recorded; every method is a pure read."""

    def status(self) -> Literal["ready", "not_ready"]:
        """The overall status the readiness probe would report for the recorded state."""
        ...

    def unhealthy_dependencies(self) -> list[str]:
        """The tracked dependencies recorded unhealthy, by name, in a fixed order."""
        ...

    def gated_graphs(self) -> list[str]:
        """The graphs that refuse writes until an operator acts; empty when there are none."""
        ...


async def is_verified_caller(request: Request, verifier: PsTokenVerifier | None) -> bool:
    """Whether `request` carries a bearer token the verifier accepts.

    No header, a non-`Bearer` scheme, an empty token, no verifier (local-test bypass) and a token
    the verifier rejects all answer `False`; nothing here ever raises or answers 401.

    Args:
        request: The request whose `Authorization` header is inspected.
        verifier: The process-wide token verifier, or `None` when the bypass is active.

    Returns:
        `True` only when a bearer token is present and verifies.

    Side Effects:
        One verifier call (and its single log line) per request that presents a bearer token.
    """
    if verifier is None:
        return False
    token = _bearer_token(request)
    return token is not None and await verifier.verify_token(token) is not None


def _bearer_token(request: Request) -> str | None:
    value = request.headers.get("authorization")
    if value is None or not value.startswith(_BEARER_PREFIX):
        return None
    return value[len(_BEARER_PREFIX) :].strip() or None


class _TokenDeadlineLog:
    """Logs token checks that miss the deadline once per transition, never once per poll.

    The first overrun logs `failed`; the next presented token checked within the deadline logs
    `succeeded`; polls in between log nothing. One instance per router, on one event loop, so
    the flag needs no lock.
    """

    def __init__(self) -> None:
        self._overrunning = False

    def record(self, *, overran: bool) -> None:
        """Note one token check's outcome and log it if it changes the overrun state.

        Args:
            overran: Whether the check missed the deadline.

        Side Effects:
            Emits one `status_token_check` log entry on a transition.
        """
        if overran and not self._overrunning:
            self._overrunning = True
            emit_log_entry(
                component=_LOG_COMPONENT,
                action=_LOG_ACTION,
                outcome="failed",
                extra={"reason": "deadline_exceeded"},
            )
        elif not overran and self._overrunning:
            self._overrunning = False
            emit_log_entry(
                component=_LOG_COMPONENT,
                action=_LOG_ACTION,
                outcome="succeeded",
                extra={"reason": "deadline_met"},
            )


async def _is_verified_within(
    request: Request,
    verifier: PsTokenVerifier | None,
    deadline_seconds: float,
    deadline_log: _TokenDeadlineLog,
) -> bool:
    """Check the caller's bearer token, treating a check unfinished by the deadline as failed.

    A call without a bearer token, and the local-test bypass, answer `False` at once and leave
    the deadline log untouched.
    """
    if verifier is None or _bearer_token(request) is None:
        return False
    try:
        verified = await asyncio.wait_for(is_verified_caller(request, verifier), deadline_seconds)
    except TimeoutError:
        deadline_log.record(overran=True)
        return False
    deadline_log.record(overran=False)
    return verified


def build_service_status_handler() -> Callable[..., Awaitable[ServiceStatusResponse]]:
    """Build the `GET /status` handler, with its own token-deadline log state.

    Returns:
        The handler, ready for `add_api_route`.
    """
    deadline_log = _TokenDeadlineLog()

    async def get_service_status(
        request: Request,
        config: Annotated[ServiceConfig, Depends(get_service_config)],
        readiness: Annotated[RecordedReadiness, Depends(provide_recorded_readiness)],
        verifier: Annotated[PsTokenVerifier | None, Depends(provide_token_verifier)],
    ) -> ServiceStatusResponse:
        """Report the version and the recorded readiness; add the detail for a verified caller.

        The token check gets at most `config.ready_reprobe_deadline_seconds`; a check not done
        by then counts as anonymous, so a slow issuer never stalls this answer.

        Args:
            request: The incoming request; only its `Authorization` header is read.
            config: Supplies the token-check deadline.
            readiness: The readiness Process Harness has recorded.
            verifier: The process-wide token verifier, or `None` under the local-test bypass.

        Returns:
            `status` and `version`, plus `unhealthy_dependencies` and `gated_graphs` when the
            caller's bearer token verifies. Always HTTP 200.

        Side Effects:
            One verifier call per presented bearer token; a `status_token_check` log entry when
            the token check starts or stops missing the deadline.
        """
        version = installed_version("ps-service")
        verified = await _is_verified_within(
            request, verifier, config.ready_reprobe_deadline_seconds, deadline_log
        )
        if not verified:
            return ServiceStatusResponse(status=readiness.status(), version=version)
        return ServiceStatusResponse(
            status=readiness.status(),
            version=version,
            unhealthy_dependencies=readiness.unhealthy_dependencies(),
            gated_graphs=readiness.gated_graphs(),
        )

    return get_service_status
