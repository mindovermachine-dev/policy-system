"""Client helper for `scripts/grant-access-role.sh`.

Calls PS Service's `grant-access-role` MCP tool over its Streamable HTTP endpoint
(`initialize`, `notifications/initialized`, `tools/call`) with the bearer token that
`ps-cli auth login` stored, and prints the JSON-RPC reply of the tool call to stdout. The
shell script keeps flag parsing, role validation and reply classification.

Token handling: `GRANT_ACCESS_ROLE_TOKEN`, when set, is used verbatim. Otherwise the token
comes from ps-cli's credential store through `ps_cli.device_flow.ensure_valid_access_token`.
No context or nothing stored means no `Authorization` header (the local-test bypass shape);
a stored credential whose refresh fails raises and is never downgraded to unauthenticated.
The token is never written to stdout, stderr or argv.

Failures write exactly one line to stderr and exit 1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import TYPE_CHECKING, cast

import httpx

from ps_cli.config import load_config
from ps_cli.credentials import CredentialStore, TokenBundle, build_credential_store
from ps_cli.device_flow import AccessTokenCache, ensure_valid_access_token
from ps_cli.errors import PsCliError
from ps_cli.targets import resolve_auth_override, resolve_config_dir
from ps_cli.tls import (
    CERTIFICATE_ERROR_HINT,
    build_ssl_context,
    is_certificate_verification_error,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ps_cli.targets import AuthOverrides

TOKEN_ENV = "GRANT_ACCESS_ROLE_TOKEN"  # noqa: S105  # env var name, not a secret
MCP_PATH = "/mcp/"

_HTTP_ACCEPTED = 202
_HTTP_UNAUTHORIZED = 401
_REQUEST_TIMEOUT_SECONDS = 30
_SESSION_HEADER = "Mcp-Session-Id"
_TOOL_NAME = "grant-access-role"
_CLIENT_NAME = "grant-access-role.sh"


def resolve_bearer_token(
    *,
    context_name: str | None,
    service_url: str,
    auth_override: AuthOverrides | None,
    credential_store: CredentialStore,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Choose the bearer token for the grant call, or none for an unauthenticated call.

    Precedence: `GRANT_ACCESS_ROLE_TOKEN`; then no context -> `None`; then nothing stored ->
    `None`; otherwise a valid access token from the stored refresh token.

    Args:
        context_name: Active ps-cli context name, or `None` when no context is configured.
        service_url: PS Service base URL; the token refresh request goes there.
        auth_override: Per-context authentication overrides, or `None` for the defaults.
        credential_store: Store holding the context's tokens; read only when a context exists.
        transport: Optional HTTP transport for the refresh request (tests inject one).

    Returns:
        The token to send as a bearer credential, or `None` to send no `Authorization` header.

    Raises:
        PsCliError: A stored credential exists but refreshing it fails; the call is never
            downgraded to unauthenticated.
    """
    explicit = os.environ.get(TOKEN_ENV)
    if explicit:
        return explicit
    if context_name is None:
        return None
    if credential_store.get_tokens(context_name) is None:
        return None
    return ensure_valid_access_token(
        context=context_name,
        service_url=service_url,
        auth_override=auth_override,
        credential_store=credential_store,
        access_token_cache=AccessTokenCache(),
        transport=transport,
    )


def _parse_reply(resp: httpx.Response) -> dict[str, object] | None:
    r"""Extract the JSON-RPC object from a plain-JSON or single-event SSE body.

    A 202 or an empty body (a notification acknowledgement) yields `None`.
    """
    if resp.status_code == _HTTP_ACCEPTED or not resp.content:
        return None
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return cast("dict[str, object]", json.loads(line[len("data:") :].strip()))
        return None
    return cast("dict[str, object]", resp.json())


def call_tool(
    *,
    mcp_url: str,
    token: str | None,
    principal: str,
    role: str,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, object] | None:
    """Run the MCP handshake and call the `grant-access-role` tool once.

    Sends `initialize`, `notifications/initialized` and `tools/call` in order over one HTTP
    client, carrying the MCP session id from the first reply into later requests.

    Args:
        mcp_url: Full URL of PS Service's MCP endpoint.
        token: Bearer token, or `None` to send no `Authorization` header.
        principal: Subject of the principal receiving the role.
        role: Name of the access role to grant.
        transport: Optional HTTP transport (tests inject one).

    Returns:
        The decoded JSON-RPC reply to the tool call, or `None` when it carries no body.

    Raises:
        httpx.HTTPStatusError: Any of the three requests is answered with an error status.
        httpx.HTTPError: The service cannot be reached or the TLS handshake fails.
        json.JSONDecodeError: A reply body is not valid JSON.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    messages: list[dict[str, object]] = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": _CLIENT_NAME, "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": _TOOL_NAME,
                "arguments": {"principal_subject": principal, "access_role": role},
            },
        },
    ]
    reply: dict[str, object] | None = None
    with httpx.Client(
        timeout=_REQUEST_TIMEOUT_SECONDS, transport=transport, verify=build_ssl_context()
    ) as client:
        for message in messages:
            resp = client.post(mcp_url, json=message, headers=headers)
            resp.raise_for_status()
            session_id = resp.headers.get(_SESSION_HEADER)
            if session_id:
                headers[_SESSION_HEADER] = session_id
            parsed = _parse_reply(resp)
            if message.get("id") == 2:  # noqa: PLR2004  # the tools/call request id
                reply = parsed
    return reply


def _fail(message: str) -> int:
    """Write the single failure line to stderr and return the failure exit code."""
    sys.stderr.write(f"{message}\n")
    return 1


def _describe_ps_cli_error(exc: PsCliError) -> str:
    return f"{exc.msg} ({exc.hint})" if exc.hint else exc.msg


def _describe_http_error(exc: httpx.HTTPError, mcp_url: str) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == _HTTP_UNAUTHORIZED:
            return (
                "PS Service rejected the credentials (HTTP 401): "
                "run 'ps-cli auth login' for this context."
            )
        return f"PS Service answered HTTP {status}."
    if is_certificate_verification_error(exc):
        return (
            f"Cannot verify the TLS certificate of PS Service at {mcp_url}: "
            f"{CERTIFICATE_ERROR_HINT}"
        )
    return (
        f"Cannot reach PS Service at {mcp_url} ({type(exc).__name__}); is it running and reachable?"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Grant `--role` to `--principal` and print the tool-call reply as one JSON line.

    Args:
        argv: Command-line arguments without the program name; `None` reads `sys.argv`.
            Both `--principal` and `--role` are required.

    Returns:
        0 when the call completes (the reply, if any, is on stdout); 1 after writing one failure
        line to stderr when ps-cli configuration, credentials or the HTTP call fail.

    Side Effects:
        Sends the grant request to PS Service, which records the role assignment; may refresh and
        store the access token through ps-cli's credential store.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--principal", required=True)
    parser.add_argument("--role", required=True)
    args = parser.parse_args(argv)

    mcp_url = ""
    try:
        config_dir = resolve_config_dir()
        config = load_config(config_dir=config_dir)
        mcp_url = config.service_url.rstrip("/") + MCP_PATH
        token = resolve_bearer_token(
            context_name=config.context_name,
            service_url=config.service_url,
            auth_override=resolve_auth_override(config, config_dir),
            credential_store=_LazyCredentialStore(),
        )
        reply = call_tool(
            mcp_url=mcp_url, token=token, principal=str(args.principal), role=str(args.role)
        )
    except PsCliError as exc:
        return _fail(_describe_ps_cli_error(exc))
    except httpx.HTTPError as exc:
        return _fail(_describe_http_error(exc, mcp_url))

    if reply is not None:
        sys.stdout.write(f"{json.dumps(reply)}\n")
    return 0


class _LazyCredentialStore:
    """`CredentialStore` that opens the real store on first use.

    The explicit-token and no-context paths never read a store, so they never touch the
    operating system's credential storage.
    """

    def __init__(self) -> None:
        self._store: CredentialStore | None = None

    def _real(self) -> CredentialStore:
        if self._store is None:
            self._store = build_credential_store()
        return self._store

    def get_tokens(self, context: str) -> TokenBundle | None:
        return self._real().get_tokens(context)

    def set_tokens(self, context: str, tokens: TokenBundle) -> None:
        self._real().set_tokens(context, tokens)

    def delete_tokens(self, context: str) -> None:
        self._real().delete_tokens(context)


if __name__ == "__main__":
    sys.exit(main())
