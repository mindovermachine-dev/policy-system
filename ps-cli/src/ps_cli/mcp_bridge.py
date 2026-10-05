"""`ps-cli-mcp-bridge`: a local stdio MCP server proxying to PS Service.

A separate console-script entry point (`pyproject.toml`'s `[project.scripts]`),
never a subcommand of `ps-cli` itself -- no operator ever types
`ps-cli-mcp-bridge` by hand, the same way nobody types `git-credential-manager`
directly. An MCP host (a Claude plugin's `.mcp.json` declaring a `type: "stdio"`
server) spawns it, speaks MCP JSON-RPC to it over stdin/stdout, and it forwards
each message to PS Service's real Streamable HTTP endpoint over plain HTTPS,
attaching a bearer token when one is available.

Exists because interactive OAuth against a customer's own Microsoft Entra
tenant cannot be performed by Claude's host itself (spike #65: Entra's
`resource`-parameter enforcement conflicts with the MCP auth spec for any
platform-hosted MCP server). Login and refresh stay `ps-cli auth login`'s job
(issue #57) -- this module only reads what that command already stored, via
the exact same `ps_cli.device_flow.ensure_valid_access_token` every other
authenticated ps-cli command uses, so there is nothing about token refresh to
keep in sync by hand.

Serves both a real deployment (`ps-cli auth login` once, then every call is
authenticated) and a local-test/`PS_SERVICE_LOCAL_TEST_BYPASS` deployment
(nothing stored, every call proceeds with no `Authorization` header at all,
mirroring `PsServiceClient`'s own unauthenticated-call shape) -- one plugin
connector definition covers both, no separate local-only workaround needed.
See `_resolve_access_token()`.

Credential changes while the bridge runs (issue #181): the bridge reads the keychain at
the first message and afterwards only when the credential's modification time moves
(`bridge_credentials.BridgeCredentialView`), so a valid cached access token costs one
`stat`, not a keychain read. A logout after the bridge had used a credential fails closed
with the "no stored credentials" error and the `ps-cli auth login` hint (a context that
never had one still forwards unauthenticated); a re-login drops the cached access token.
A keychain read failing with -67701 falls back to the last-known refresh token, and a
failed persist of a rotated refresh token is best-effort. Every keychain call is bounded
(5 s) so one stall cannot block the single-threaded loop. Each forwarded message's log line
reports `token_resolution_latency`, `upstream_latency` (when the POST ran) and the total
`latency`. No log line or reply carries a token value.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import httpx

from ps_cli.bridge_credentials import BridgeCredentialView
from ps_cli.config import load_config
from ps_cli.credentials import build_credential_store
from ps_cli.device_flow import AccessTokenCache, ensure_valid_access_token
from ps_cli.errors import PsCliError
from ps_cli.targets import resolve_auth_override, resolve_config_dir
from ps_cli.tls import build_ssl_context

if TYPE_CHECKING:
    from pathlib import Path
    from typing import TextIO

    from ps_cli.targets import AuthOverrides

_MCP_PATH = "/mcp/"
_LOG_FILE_NAME = "mcp-bridge.log"

_HTTP_ACCEPTED = 202
_HTTP_BAD_REQUEST = 400

# Issue #119, AC-BI-005: JSON-RPC 2.0 §5.1 reserves -32000..-32099 for
# implementation-defined "Server error" codes; this one specific value means "could
# not obtain an access token" -- distinct from a transport/PS-Service-side failure,
# which still has no reply at all (see `_forward_message`'s other failure branches).
_JSONRPC_AUTH_ERROR_CODE = -32001

# Issue #120, AC-BI-004: JSON-RPC 2.0 §5.1's -32000..-32099 "Server error" range,
# same convention as _JSONRPC_AUTH_ERROR_CODE above -- this one specific value means
# "PS Service itself rejected the forwarded request with a >=400 status," distinct
# from the auth-resolution failure above and from a transport-level failure (which
# still has no reply at all -- see _forward_message's other failure branches).
_JSONRPC_UPSTREAM_ERROR_CODE = -32002

# The MCP SDK's own fixed literal for an unknown/expired session (see
# mcp.server.streamable_http_manager). Detected by exact string match: this
# text is never composed by ps-service's own code, only echoed verbatim by the SDK.
_SESSION_NOT_FOUND_MESSAGE = "Session not found"

# Matches the existing resp.text[:500] truncation already used when logging a >=400
# response (see the log line just above this branch) -- bounds how much upstream text
# this bridge will ever re-emit, even into its own JSON-RPC reply.
_UPSTREAM_ERROR_MESSAGE_MAX_LEN = 500


@dataclass(frozen=True)
class _BridgeContext:
    """Per-run wiring `_forward_message` needs, bundled to stay within the max-args budget.

    Everything here is resolved once in `main()` and stays constant for the process's
    lifetime -- only `session_id` changes message-to-message, so it stays a separate
    argument rather than living on this dataclass.
    """

    mcp_url: str
    context_name: str | None
    service_url: str
    auth_override: AuthOverrides | None
    credential_store: BridgeCredentialView
    access_token_cache: AccessTokenCache
    log_file: TextIO | None
    # Issue #121 Slice 2: the same constructor-injection seam `PsServiceClient`
    # already threads into `ensure_valid_access_token` (`http_client.py::self._transport`)
    # -- lets one `httpx.MockTransport` answer PS Service's resource-metadata/refresh
    # endpoints *and* the forwarded business call, for a genuine wire-level test of
    # AC-BI-003/004 at this entrypoint. Production callers (`main()`) pass the same
    # `transport` they already accept for the forwarding `httpx.Client`; defaults to
    # `None` (real network) so no existing `_BridgeContext(...)` construction breaks.
    transport: httpx.BaseTransport | None = None


def _log(message: str, *, log_file: TextIO | None = None) -> None:
    """Write diagnostics to stderr (always) and to `log_file` (when given).

    stderr depends entirely on whatever host spawned this process choosing to capture
    it -- proven unreliable (issue #118). `log_file` is this bridge's own durable,
    host-independent record, at a fixed, discoverable location (see `_open_log_file`).
    """
    line = f"{datetime.now(UTC).isoformat(timespec='milliseconds')} [ps-cli-mcp-bridge] {message}"
    print(line, file=sys.stderr, flush=True)
    if log_file is not None:
        print(line, file=log_file, flush=True)


def _open_log_file(path: Path) -> TextIO | None:
    """Open `path` for appending, creating it with non-world-readable permissions.

    Returns `None` (never raises) when the file can't be opened -- diagnostics must
    never be the reason the proxy itself fails to start (AC-BI-008). `mode=0o600` only
    applies at creation; an already-existing file keeps whatever permissions it has.
    Creates `path`'s parent directory if missing (a genuinely first-run `config_dir`,
    with no `ps-cli auth login`/`config set-context` ever run, may not exist yet).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    except OSError as exc:
        _log(f"could not open log file {path}: {exc}")
        return None
    return os.fdopen(fd, "a", encoding="utf-8")


def _message_method_and_id(message: object) -> tuple[str | None, object]:
    """Best-effort `method`/`id` extraction from a JSON-RPC message, for logging only."""
    if not isinstance(message, dict):
        return None, None
    body = cast("dict[str, object]", message)
    method = body.get("method")
    return (method if isinstance(method, str) else None, body.get("id"))


def _resolve_access_token(
    *,
    context_name: str | None,
    service_url: str,
    auth_override: AuthOverrides | None,
    credential_store: BridgeCredentialView,
    access_token_cache: AccessTokenCache,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Return a valid access token, or `None` when no auth applies or nothing is stored.

    `context_name is None` -- no `ps-cli` context was ever configured, only the
    packaged-default/single-target fallback (`config.py::load_config`'s own
    case 4, what the local-test walkthrough's steps 6-8 actually leave in
    place: no `config set-context` call at all) -- means there is no name to
    key a stored credential under in the first place, exactly
    `PsServiceClient.__init__`'s own `context: str | None = None` shape
    (D-57-6): no context, no credential store lookup, no `Authorization`
    header, ever.

    A *named* context with nothing stored for it yet is the same outcome for
    a different reason: mirrors `PsServiceClient`'s unauthenticated-call shape
    for a deployment with no auth configured at all (e.g. local-test's
    `PS_SERVICE_LOCAL_TEST_BYPASS`, which has nothing to log into) -- proceed
    with no `Authorization` header, exactly as `mcp-remote` did for the old
    manual local-test workaround this module replaces. Checked via
    `credential_store.sync()`/`holds_credential` (a plain read of the stored credential,
    no refresh attempt; since issue #181 stat-gated, so the keychain is read only at
    the first message and when the credential's modification time moved) rather than
    by inspecting `ensure_valid_access_token`'s
    exception, so this never conflates "nothing stored" with "something is
    stored but its refresh just failed" -- the latter still raises up to the
    caller, surfaced as a log line, not silently downgraded to unauthenticated
    (an expired/revoked session is a real problem worth surfacing, not a cue
    to guess that no auth was intended).
    """
    if context_name is None:
        return None
    if credential_store.sync(context_name):
        access_token_cache.token = None
        access_token_cache.expires_at = None
    if not credential_store.holds_credential and not credential_store.ever_seen:
        return None
    return ensure_valid_access_token(
        context=context_name,
        service_url=service_url,
        auth_override=auth_override,
        credential_store=credential_store,
        access_token_cache=access_token_cache,
        transport=transport,
    )


def _parse_response_body(resp: httpx.Response) -> dict[str, object] | None:
    r"""Extract the JSON-RPC response object from a plain-JSON or SSE body.

    PS Service's Streamable HTTP transport answers either as a bare JSON body
    or as a single-event `text/event-stream` (`event: message\ndata: {...}`)
    depending on the request -- both carry exactly one JSON-RPC response.
    A 202 (notification acknowledged, e.g. `notifications/initialized`) or an
    empty body correctly produces no reply, matching JSON-RPC's own
    no-response-to-notifications rule.
    """
    content_type = resp.headers.get("content-type", "")
    if resp.status_code == _HTTP_ACCEPTED or not resp.content:
        return None
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:") :].strip())
        return None
    return resp.json()


def _extract_upstream_error_message(resp: httpx.Response) -> str | None:
    """Best-effort extraction of PS Service's own JSON-RPC error `message` from `resp`.

    Returns `None` when the body isn't the JSON-RPC error shape PS Service's MCP
    transport actually emits (e.g. a non-JSON body from a proxy/gateway in front of
    PS Service, or a shape this bridge doesn't recognize) -- callers fall back to a
    generic, body-free message in that case. Never returns anything beyond this one
    extracted string, truncated to _UPSTREAM_ERROR_MESSAGE_MAX_LEN -- the raw body,
    headers, and any other field of the upstream error object are discarded here,
    which is what makes AC-BI-007 hold at the one place this bridge ever looks inside
    a >=400 response body.
    """
    try:
        body = _parse_response_body(resp)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    error_body = cast("dict[str, object]", error)
    message = error_body.get("message")
    if not isinstance(message, str):
        return None
    return message[:_UPSTREAM_ERROR_MESSAGE_MAX_LEN]


def _upstream_error_reply(message_id: object, resp: httpx.Response) -> dict[str, object]:
    """Build the JSON-RPC error reply for a >=400 PS Service response to a genuine request.

    AC-BI-004: always carries PS Service's own sanitized message text instead of
    silence -- or a generic, body-free fallback (just the status code) when the body
    can't be parsed into that shape. AC-BI-006: PS Service's fixed "Session not found"
    text (see module-level docstring note) is rewrapped so the MCP host -- and the
    human behind it -- can tell an expired/reset session (e.g. after a ps-service pod
    restart) apart from any other >=400 failure, while still literally including PS
    Service's own message text, satisfying AC-BI-004 and AC-BI-006 simultaneously
    rather than choosing one over the other. AC-BI-007: this function never touches
    resp.text/resp.headers/the request's own Authorization header directly -- only
    the one string _extract_upstream_error_message already sanitized, plus this
    bridge's own literal status-code note.
    """
    upstream_message = _extract_upstream_error_message(resp)
    if upstream_message == _SESSION_NOT_FOUND_MESSAGE:
        text = (
            "PS Service session expired or was reset (e.g. after a service "
            "restart) -- reconnect and retry. "
            f'(PS Service: "{_SESSION_NOT_FOUND_MESSAGE}")'
        )
    elif upstream_message is not None:
        text = f"PS Service rejected the request (status {resp.status_code}): {upstream_message}"
    else:
        text = f"PS Service rejected the request with status {resp.status_code}."
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": _JSONRPC_UPSTREAM_ERROR_CODE, "message": text},
    }


def _ge400_reply(message_id: object, resp: httpx.Response) -> dict[str, object] | None:
    """Decide `_forward_message`'s reply for a >=400 PS Service response.

    AC-BI-005: a notification (`message_id is None`) still gets no reply -- JSON-RPC
    2.0 §4.1 forbids replying to one. Otherwise delegates to `_upstream_error_reply`
    for AC-BI-004/006/007. Split out from `_forward_message` itself purely to keep
    that function's own branching within L2's mccabe budget (`max-complexity = 8`) --
    this one `if`/`else` mirrors the shape of the token-resolution-failure branch
    one function up, kept here instead of inline for the same complexity reason.
    """
    if message_id is None:
        return None
    return _upstream_error_reply(message_id, resp)


class _Phase:
    """A started monotonic timer; `elapsed()` is the seconds since construction."""

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed(self) -> float:
        """Seconds since this timer started."""
        return time.monotonic() - self._start


def _latency_fields(*, token_resolution: float, total: float, upstream: float | None = None) -> str:
    """Format the latency log fields (issue #181, AC-BI-009); `upstream` omitted when no POST ran.

    `latency` stays the total so existing greps keep matching; the two split fields
    attribute it to token resolution (keychain/IdP) versus the upstream POST.
    """
    fields = f"token_resolution_latency={token_resolution:.3f}s "
    if upstream is not None:
        fields += f"upstream_latency={upstream:.3f}s "
    return fields + f"latency={total:.3f}s"


def _auth_error_reply(message_id: object, exc: PsCliError) -> dict[str, object] | None:
    """Build the JSON-RPC reply for a token-resolution failure (issues #119, #181).

    Issue #119, AC-BI-005/007: surfaced to the MCP host as a real reply -- not a silent
    no-response -- so Claude Desktop shows the actual cause. Only for a genuine request
    (`message_id is not None`): a notification (JSON-RPC 2.0 §4.1) never gets a reply.
    AC-BI-006: `str(exc)` is `PsCliError`'s own msg/hint text, which never carries a
    token value (see `device_flow.py`'s own AC-BI-018 guarantee).
    """
    if message_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": _JSONRPC_AUTH_ERROR_CODE, "message": str(exc)},
    }


def _forward_message(
    client: httpx.Client,
    message: object,
    *,
    session_id: str | None,
    ctx: _BridgeContext,
) -> tuple[dict[str, object] | None, str | None]:
    """Forward one JSON-RPC `message` to PS Service; return `(reply, updated session_id)`.

    `reply` is `None` on a transport failure, or a notification (no `id`) with no
    response either way. A token-resolution failure or a >=400 PS Service response
    for a genuine request (issues #119/#120, AC-BI-004/005/007) instead returns a
    JSON-RPC error object -- unlike a transport failure, which stays a silent
    no-reply since there is no PS Service response to report PS Service's own
    message from. Every failure is logged (stderr + `ctx.log_file`) and swallowed
    here, never raised, so one bad message never kills the whole proxy loop
    mid-session. Every outcome -- success included -- is logged with `method`/`id`
    (never the message's `params`, which may carry sensitive content), outcome, latency
    (total, token-resolution and, when the POST ran, upstream -- issue #181, AC-BI-009),
    and the session id in play, per AC-BI-003/004; secrets (the bearer token) never
    appear in any logged line, per AC-BI-009.
    """
    method, message_id = _message_method_and_id(message)
    total = _Phase()
    resolution = _Phase()

    try:
        token = _resolve_access_token(
            context_name=ctx.context_name,
            service_url=ctx.service_url,
            auth_override=ctx.auth_override,
            credential_store=ctx.credential_store,
            access_token_cache=ctx.access_token_cache,
            transport=ctx.transport,
        )
    except PsCliError as exc:
        latency = _latency_fields(token_resolution=resolution.elapsed(), total=total.elapsed())
        _log(
            f"could not get access token: {exc} "
            f"(method={method} id={message_id!r} {latency} session={session_id})",
            log_file=ctx.log_file,
        )
        return _auth_error_reply(message_id, exc), session_id
    token_resolution = resolution.elapsed()

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id

    upstream = _Phase()
    try:
        resp = client.post(ctx.mcp_url, json=message, headers=headers)
    except httpx.HTTPError as exc:
        latency = _latency_fields(
            token_resolution=token_resolution, upstream=upstream.elapsed(), total=total.elapsed()
        )
        _log(
            f"request to PS Service failed: {exc} "
            f"(method={method} id={message_id!r} {latency} session={session_id})",
            log_file=ctx.log_file,
        )
        return None, session_id

    latency = _latency_fields(
        token_resolution=token_resolution, upstream=upstream.elapsed(), total=total.elapsed()
    )
    updated_session_id = resp.headers.get("mcp-session-id") or session_id
    if resp.status_code >= _HTTP_BAD_REQUEST:
        _log(
            f"PS Service returned {resp.status_code}: {resp.text[:500]} "
            f"(method={method} id={message_id!r} {latency} session={updated_session_id})",
            log_file=ctx.log_file,
        )
        # Issue #120, AC-BI-004/005: surface this to the MCP host as a real reply --
        # not a silent no-response -- mirroring issue #119's auth-error branch above.
        # `_ge400_reply` keeps the notification-vs-request decision out of this
        # function's own branching (see its docstring for why).
        return _ge400_reply(message_id, resp), updated_session_id

    _log(
        f"forwarded: method={method} id={message_id!r} outcome=ok "
        f"{latency} session={updated_session_id}",
        log_file=ctx.log_file,
    )
    return _parse_response_body(resp), updated_session_id


def _run_proxy_loop(client: httpx.Client, ctx: _BridgeContext) -> None:
    """Read JSON-RPC messages from stdin, forward each via `client`, write replies to stdout."""
    session_id: str | None = None
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _log(f"skipping non-JSON line: {line!r}", log_file=ctx.log_file)
            continue

        reply, session_id = _forward_message(client, message, session_id=session_id, ctx=ctx)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


def main(*, transport: httpx.BaseTransport | None = None) -> None:
    """Run the stdio<->Streamable-HTTP proxy loop until stdin closes.

    Resolves the current context/auth exactly once at startup (mirroring every
    other ps-cli command's `load_config()`/`build_credential_store()` call
    shape), then re-derives a valid access token before every forwarded
    message -- `ensure_valid_access_token` itself decides whether the cached
    token is still valid or needs refreshing, so this loop never has to.

    Never refuses to start over a missing context: `config.context_name` is
    `None` for the packaged-default/single-target fallback (no `ps-cli config
    set-context` ever run -- what the local-test walkthrough's steps 6-8
    actually leave in place), which is a legitimate, fully-supported
    unauthenticated shape (`_resolve_access_token`'s own docstring), not an
    error state to reject.

    Logs a startup banner (PID, parent PID, service URL, context) and an exit
    line (clean stdin EOF vs. an unhandled exception, which is always logged
    before it propagates) to both stderr and `<config_dir>/mcp-bridge.log`
    (issue #118) -- independent of whatever host does or doesn't capture this
    process's stderr. `transport` is the constructor-injection seam tests use
    to substitute a fake PS Service, mirroring `device_flow.py`'s own seam;
    production callers (the `ps-cli-mcp-bridge` console script) never pass it.
    """
    config_dir = resolve_config_dir()
    config = load_config(config_dir=config_dir)
    auth_override = resolve_auth_override(config, config_dir)
    mcp_url = config.service_url.rstrip("/") + _MCP_PATH
    context_name = config.context_name

    log_file = _open_log_file(config_dir / _LOG_FILE_NAME)
    credential_store = BridgeCredentialView(
        build_credential_store(),
        logger=lambda message: _log(message, log_file=log_file),
    )
    _log(
        f"starting: pid={os.getpid()} ppid={os.getppid()} "
        f"service_url={config.service_url} context={context_name!r}",
        log_file=log_file,
    )
    ctx = _BridgeContext(
        mcp_url=mcp_url,
        context_name=context_name,
        service_url=config.service_url,
        auth_override=auth_override,
        credential_store=credential_store,
        access_token_cache=AccessTokenCache(),
        log_file=log_file,
        transport=transport,
    )

    try:
        with httpx.Client(timeout=30, transport=transport, verify=build_ssl_context()) as client:
            _run_proxy_loop(client, ctx)
    except Exception as exc:  # log before propagating (AC-BI-006), never swallowed
        _log(f"exiting: unhandled exception: {exc!r}", log_file=log_file)
        raise
    else:
        _log("exiting: stdin closed", log_file=log_file)
    finally:
        if log_file is not None:
            log_file.close()


if __name__ == "__main__":
    main()
