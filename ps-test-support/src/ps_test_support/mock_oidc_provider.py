"""A real, local, generic mock OIDC provider (issue #58, AC-BI-017).

Serves its own ``/.well-known/openid-configuration`` discovery document,
``/jwks.json`` JWK Set, and (issue #57) device-authorization + token
endpoints over a real ``http.server.ThreadingHTTPServer`` bound to an
ephemeral loopback port -- a real local HTTP server, not a monkeypatched
transport, because ``jwt.PyJWKClient.fetch_data`` calls
``urllib.request.urlopen`` directly with no injectable seam (PLAN.md §0.2).

Deliberately generic: nothing here is shaped like Auth0 or Entra ID (no
tenant ids, no vendor-specific claim names) -- the **one** mock OIDC provider
AC-BI-017 requires, reused unchanged by every later slice
(Slice 4: 200 path/principal; Slice 5: MCP; Slice 6: `rotate_key` for the
kid-miss-refetch-once test; Slice 7: algorithm allow-list; Slice 10: audit
logging; issue #57 Slice 4: device-authorization + token endpoints) -- never
a second mock provider.

Import convention: installed as a normal package -- a consuming test module
imports this file directly, e.g. ``from ps_test_support.mock_oidc_provider
import MockOidcProvider, mock_oidc_provider_fixture``. The
`mock_oidc_provider` pytest fixture itself is defined here under the
*function* name `mock_oidc_provider_fixture`, registered under the
*fixture* name `"mock_oidc_provider"` via `@pytest.fixture(name=...)` --
pytest resolves a fixture by that explicit name, never by the local variable
name a module happens to bind an import to, so a consuming test module
imports the function under its own name (`mock_oidc_provider_fixture`, kept
only for pytest's discovery, never called directly) with no collision
against the same-named `mock_oidc_provider: MockOidcProvider` parameter
every consuming test function declares (which would otherwise trip ruff's
F811 "redefinition of unused name")::

    from ps_test_support.mock_oidc_provider import (
        mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    )
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_ALG = "RS256"
_DEFAULT_AUDIENCE = "ps-service"
_DEFAULT_EXP_DELTA_SECONDS = 3600.0
_DEFAULT_REFRESH_EXP_DELTA_SECONDS = 3600.0


class _RsaSigningKey:
    """One RSA keypair under one `kid`: servable as a JWK, and can sign claims."""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def public_jwk(self) -> dict[str, Any]:
        """Return this key's public half as a JWK dict (`jwt.algorithms.RSAAlgorithm.to_jwk`)."""
        jwk = RSAAlgorithm.to_jwk(self._private_key.public_key(), as_dict=True)
        jwk["kid"] = self.kid
        jwk["use"] = "sig"
        jwk["alg"] = _ALG
        return jwk

    def sign(self, claims: Mapping[str, object], *, alg: str = _ALG) -> str:
        """Sign `claims` with this key's private half, tagging the JWS header with this `kid`."""
        return jwt.encode(dict(claims), self._private_key, algorithm=alg, headers={"kid": self.kid})


@dataclass
class DeviceFlowState:
    """Server-side state for one in-flight device-authorization grant, keyed by `device_code`.

    `client_id`/`audience` are the values recorded off the original
    `POST /device_authorization` request -- read back via
    `MockOidcProvider.device_flow_state` to prove audience passthrough
    (AC-BI-003). `status` drives `POST /token`'s response:
    `"pending"` (default) -> `authorization_pending`; `"approved"` ->
    mints a token; `"expired"` -> `expired_token`; `"denied"` ->
    `access_denied`. `slow_down_once` makes exactly the next poll return
    `slow_down` before reverting to `status`.
    """

    client_id: str
    audience: str | None
    status: str = "pending"
    sub: str = "test-subject"
    slow_down_once: bool = False


def _serve_json(
    handler: BaseHTTPRequestHandler, body: dict[str, Any], *, status: int = 200
) -> None:
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _read_form_body(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    """Read and form-decode a `POST` request body (`application/x-www-form-urlencoded`)."""
    length = int(handler.headers.get("Content-Length", "0"))
    raw_body = handler.rfile.read(length) if length else b""
    parsed = parse_qs(raw_body.decode("utf-8"))
    return {key: values[0] for key, values in parsed.items() if values}


def _build_handler_class(provider: MockOidcProvider) -> type[BaseHTTPRequestHandler]:
    """Build a `BaseHTTPRequestHandler` subclass closing over `provider`.

    A closure-based factory (not a class attribute set after the fact) so
    each `MockOidcProvider` instance's server serves that instance's own
    state -- `ThreadingHTTPServer` requires a handler *class*, not an
    instance, so `provider` must be captured some way other than `self`.
    """

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Silence `BaseHTTPRequestHandler`'s default stderr access log.

            Parameter named `format` (not `log_format`) to match the base
            class's own signature exactly -- basedpyright's strict override
            checking requires it.
            """

        def do_GET(self) -> None:
            """Route the discovery-document and JWKS paths; anything else is a 404."""
            if self.path == "/.well-known/openid-configuration":
                _serve_json(self, provider.discovery_document())
            elif self.path == "/jwks.json":
                provider.jwks_request_count += 1
                _serve_json(self, provider.jwks_document())
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            """Route the device-authorization and token endpoints; anything else is a 404."""
            if self.path == "/device_authorization":
                form = _read_form_body(self)
                _serve_json(self, provider.handle_device_authorization_request(form))
            elif self.path == "/token":
                form = _read_form_body(self)
                status, body = provider.handle_token_request(form)
                _serve_json(self, body, status=status)
            else:
                self.send_response(404)
                self.end_headers()

    return _Handler


class MockOidcProvider:
    """A real, ephemeral-port local OIDC provider: discovery doc + JWKS + token minting.

    `jwks_request_count` lets a test assert exactly how many times `/jwks.json`
    was fetched (Slice 6's "refetch once, not repeatedly" assertion).

    Issue #57 Slice 4 adds device-authorization + token endpoint simulation:
    `POST /device_authorization` mints a fresh `device_code`/`user_code`
    pair; `POST /token` polls or exchanges it. Test code drives the
    server-side state machine directly via `complete_device_flow`,
    `simulate_slow_down_once`, `expire_device_code`, `deny_device_code` --
    never by actually visiting a verification URI.
    """

    def __init__(self) -> None:
        """Start a fresh RSA signing key and a background HTTP server on an ephemeral port."""
        self._keys: dict[str, _RsaSigningKey] = {"key-1": _RsaSigningKey("key-1")}
        self._active_kid = "key-1"
        self.jwks_request_count = 0
        self._device_flow_states: dict[str, DeviceFlowState] = {}
        self._issued_refresh_tokens: dict[str, str] = {}

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _build_handler_class(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        """This provider's own base URL, e.g. `http://127.0.0.1:54321`."""
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}"

    @property
    def issuer(self) -> str:
        """The `iss` value this provider advertises -- its own base URL."""
        return self.base_url

    @property
    def jwks_uri(self) -> str:
        """The `jwks_uri` this provider's discovery document advertises."""
        return f"{self.base_url}/jwks.json"

    def discovery_document(self) -> dict[str, Any]:
        """The RFC 8414/OIDC-Discovery document served at `/.well-known/openid-configuration`."""
        return {
            "issuer": self.issuer,
            "jwks_uri": self.jwks_uri,
            "id_token_signing_alg_values_supported": [_ALG],
            "device_authorization_endpoint": f"{self.base_url}/device_authorization",
            "token_endpoint": f"{self.base_url}/token",
        }

    def jwks_document(self) -> dict[str, Any]:
        """The JWK Set served at `/jwks.json` -- every key this provider currently knows about."""
        return {"keys": [key.public_jwk() for key in self._keys.values()]}

    def mint_token(
        self,
        *,
        sub: str = "test-subject",
        aud: str | None = None,
        iss: str | None = None,
        exp_delta: float = _DEFAULT_EXP_DELTA_SECONDS,
        kid: str | None = None,
        alg: str = _ALG,
        extra_claims: Mapping[str, object] | None = None,
    ) -> str:
        """Mint a signed JWT.

        Args:
            sub: The `sub` claim.
            aud: The `aud` claim; defaults to `ps-service`.
            iss: The `iss` claim; defaults to this provider's own `issuer`.
            exp_delta: Seconds from now until `exp`; negative mints an
                already-expired token (Slice 7).
            kid: The signing key's `kid`. Defaults to the currently active
                key. A `kid` naming neither the original nor a
                `rotate_key()`-added keypair mints a token signed by a
                *freshly generated, never-published* key -- simulating a
                JWKS the issuer never actually advertised (Slice 6/7's
                still-unknown-`kid` case).
            alg: The JWS `alg` header. Defaults to `RS256`; a caller passing
                a symmetric algorithm (e.g. `HS256`) must also pass a
                bytes/str `kid`-independent key of its own via
                `extra_claims`-adjacent test code, since this provider only
                holds RSA keys -- see `tests/auth/test_algorithm_allowlist.py`.
            extra_claims: Additional/overriding claims merged in last (so a
                test can override `exp`/`iat` etc. directly).

        Returns:
            The encoded JWT.
        """
        now = int(time.time())
        claims: dict[str, object] = {
            "sub": sub,
            "aud": aud if aud is not None else _DEFAULT_AUDIENCE,
            "iss": iss if iss is not None else self.issuer,
            "iat": now,
            "exp": now + int(exp_delta),
        }
        if extra_claims:
            claims.update(extra_claims)
        signing_kid = kid if kid is not None else self._active_kid
        key = self._keys.get(signing_kid)
        if key is None:
            # A `kid` this provider never published -- simulates a JWKS entry the
            # issuer never actually advertised (still-unknown-kid-after-refetch).
            key = _RsaSigningKey(signing_kid)
        return key.sign(claims, alg=alg)

    def rotate_key(self) -> str:
        """Add a second RSA keypair under a new `kid`, becoming the active signing key.

        The first key stays servable in the JWKS (both keys are advertised),
        so a token signed by the *old* key still verifies -- only newly
        minted tokens use the new one, letting Slice 6 mint a token whose
        `kid` is genuinely absent from whatever JWKS a verifier fetched
        *before* this call.

        Returns:
            The new key's `kid`.
        """
        new_kid = f"key-{len(self._keys) + 1}"
        self._keys[new_kid] = _RsaSigningKey(new_kid)
        self._active_kid = new_kid
        return new_kid

    def device_flow_state(self, device_code: str) -> DeviceFlowState:
        """Return the recorded `DeviceFlowState` for `device_code` (client_id/audience included).

        Lets a test read back what a `POST /device_authorization` request
        recorded -- e.g. asserting `audience` was passed through unchanged
        (AC-BI-003).
        """
        return self._device_flow_states[device_code]

    def complete_device_flow(self, device_code: str, *, sub: str = "test-subject") -> None:
        """Mark `device_code` approved as `sub`; subsequent `/token` polls succeed."""
        self._device_flow_states[device_code].status = "approved"
        self._device_flow_states[device_code].sub = sub

    def simulate_slow_down_once(self, device_code: str) -> None:
        """Make exactly the next `/token` poll for `device_code` return `slow_down`.

        Reverts to `device_code`'s underlying status (still `pending` unless
        also completed/expired/denied) on the poll after that.
        """
        self._device_flow_states[device_code].slow_down_once = True

    def expire_device_code(self, device_code: str) -> None:
        """Make subsequent `/token` polls for `device_code` return `expired_token`."""
        self._device_flow_states[device_code].status = "expired"

    def deny_device_code(self, device_code: str) -> None:
        """Make subsequent `/token` polls for `device_code` return `access_denied`."""
        self._device_flow_states[device_code].status = "denied"

    def handle_device_authorization_request(self, form: Mapping[str, str]) -> dict[str, Any]:
        """Mint a fresh `device_code`/`user_code` pair for `POST /device_authorization`.

        Records the request's `client_id`/`audience` on the resulting
        `DeviceFlowState`, readable back via `device_flow_state`. Not
        called directly by test code -- driven through the real HTTP
        server via `do_POST`; public so it is reachable from the handler
        closure without tripping ruff's private-member-access check.
        """
        device_code = secrets.token_urlsafe(16)
        user_code = secrets.token_hex(4).upper()
        self._device_flow_states[device_code] = DeviceFlowState(
            client_id=form.get("client_id", ""),
            audience=form.get("audience"),
        )
        return {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": f"{self.base_url}/device",
            "verification_uri_complete": f"{self.base_url}/device?user_code={user_code}",
            "expires_in": 600,
            "interval": 1,
        }

    def handle_token_request(self, form: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        """Dispatch `POST /token` by `grant_type`: device-code polling or refresh-token exchange.

        Not called directly by test code -- driven through the real HTTP
        server via `do_POST`; public for the same handler-closure reason as
        `handle_device_authorization_request`.
        """
        grant_type = form.get("grant_type")
        if grant_type == "urn:ietf:params:oauth:grant-type:device_code":
            return self._handle_device_code_grant(form)
        if grant_type == "refresh_token":
            return self._handle_refresh_token_grant(form)
        return 400, {"error": "unsupported_grant_type"}

    def _handle_device_code_grant(self, form: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        device_code = form.get("device_code", "")
        state = self._device_flow_states.get(device_code)
        if state is None:
            return 400, {"error": "expired_token"}
        if state.slow_down_once:
            state.slow_down_once = False
            return 400, {"error": "slow_down"}
        if state.status == "pending":
            return 400, {"error": "authorization_pending"}
        if state.status == "expired":
            return 400, {"error": "expired_token"}
        if state.status == "denied":
            return 400, {"error": "access_denied"}
        access_token = self.mint_token(sub=state.sub, aud=state.audience)
        refresh_token = secrets.token_urlsafe(32)
        self._issued_refresh_tokens[state.client_id] = refresh_token
        return 200, {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": int(_DEFAULT_EXP_DELTA_SECONDS),
            "token_type": "Bearer",
        }

    def _handle_refresh_token_grant(self, form: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        client_id = form.get("client_id", "")
        presented_refresh_token = form.get("refresh_token", "")
        current_refresh_token = self._issued_refresh_tokens.get(client_id)
        if current_refresh_token is None or presented_refresh_token != current_refresh_token:
            return 400, {"error": "invalid_grant"}
        access_token = self.mint_token()
        new_refresh_token = secrets.token_urlsafe(32)
        self._issued_refresh_tokens[client_id] = new_refresh_token
        return 200, {
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "expires_in": int(_DEFAULT_REFRESH_EXP_DELTA_SECONDS),
            "token_type": "Bearer",
        }

    def shutdown(self) -> None:
        """Stop the background HTTP server thread. Called by the `mock_oidc_provider` fixture."""
        self._server.shutdown()
        self._thread.join(timeout=5)


@pytest.fixture(name="mock_oidc_provider")
def mock_oidc_provider_fixture() -> Iterator[MockOidcProvider]:
    """Start a fresh `MockOidcProvider` for one test, shutting it down afterward.

    Registered under the explicit name `"mock_oidc_provider"` (rather than
    relying on this function's own name, `mock_oidc_provider_fixture`) so a
    consuming test module can import it under its *own* name --
    `mock_oidc_provider_fixture`, not `mock_oidc_provider` -- with no
    collision against the same-named `mock_oidc_provider: MockOidcProvider`
    parameter every test declares (ruff F811 "redefinition of unused name"):
    pytest resolves a fixture by `@pytest.fixture(name=...)`'s explicit
    name, not by the local variable name a module happens to import it as.
    See this module's own docstring for the exact import shape.

    Function-scoped (not session/module, despite PLAN.md's suggestion of
    either): several later slices (6, 7) mutate provider state
    (`rotate_key`, `jwks_request_count`), and a fresh instance per test is
    what keeps those tests independent of run order -- the fixed cost of a
    real RSA keypair generation + thread start is milliseconds, negligible
    against this suite's ~90s full run.
    """
    provider = MockOidcProvider()
    try:
        yield provider
    finally:
        provider.shutdown()
