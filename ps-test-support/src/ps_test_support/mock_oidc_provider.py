"""A real, local, generic mock OIDC provider for tests.

Serves its own ``/.well-known/openid-configuration`` discovery document,
``/jwks.json`` JWK Set, and device-authorization + token endpoints over a real
``http.server.ThreadingHTTPServer`` bound to an ephemeral loopback port -- a real local
HTTP server, not a monkeypatched transport, because ``jwt.PyJWKClient.fetch_data`` calls
``urllib.request.urlopen`` directly with no injectable seam.

Deliberately generic: nothing here is shaped like any one real-world IdP (no tenant ids,
no vendor-specific claim names). It is the one mock OIDC provider shared by every test
that needs an issuer: 200 path and principal checks, MCP, ``rotate_key`` for the
kid-miss-refetch-once test, the algorithm allow-list, audit logging, and the
device-authorization + token endpoints -- never a second mock provider.

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

    Purpose: records what the original `POST /device_authorization` request asked for and
    where the simulated user approval stands, so `POST /token` can answer polls.

    Attributes:
        client_id: The request's `client_id` form field ("" when absent).
        audience: The request's `audience` form field, or None when absent; read back
            to prove audience passthrough.
        scope: The request's `scope` form field, or None when absent; read back to prove
            a login request asked for `offline_access`.
        status: Drives `POST /token`'s response: `"pending"` (default) ->
            `authorization_pending`; `"approved"` -> mints a token; `"expired"` ->
            `expired_token`; `"denied"` -> `access_denied`.
        sub: The `sub` claim of the token minted once `status` is `"approved"`.
        slow_down_once: When True, exactly the next poll returns `slow_down`, then the
            flag resets and `status` applies again.
    """

    client_id: str
    audience: str | None
    scope: str | None = None
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

    Purpose: gives tests a genuine HTTP issuer to verify tokens and run device-flow logins
    against, without any real identity provider.

    Attributes:
        jwks_request_count: How many times `/jwks.json` has been fetched, so a test can
            assert "refetch once, not repeatedly".
        last_token_request_form: The most recent `POST /token` request's raw form fields
            (e.g. its `scope` key proves a refresh request carried `offline_access`
            forward). Overwritten on every call, by design: tests only need the one
            request they just made. Empty until the first token request.

    Device-authorization + token endpoint simulation: `POST /device_authorization` mints a
    fresh `device_code`/`user_code` pair; `POST /token` polls or exchanges it. Test code
    drives the server-side state machine directly via `complete_device_flow`,
    `simulate_slow_down_once`, `expire_device_code`, `deny_device_code` -- never by
    actually visiting a verification URI.

    Side Effects:
        Construction starts a daemon HTTP server thread on `127.0.0.1` and generates an
        RSA keypair; call `shutdown()` (the fixture does) to stop the thread.
    """

    def __init__(self) -> None:
        """Start a fresh RSA signing key and a background HTTP server on an ephemeral port.

        Raises:
            OSError: If a loopback socket cannot be bound.

        Side Effects:
            Generates a 2048-bit RSA keypair (`kid` `key-1`, the active key) and starts a
            daemon thread serving HTTP on `127.0.0.1` until `shutdown()` is called.
        """
        self._keys: dict[str, _RsaSigningKey] = {"key-1": _RsaSigningKey("key-1")}
        self._active_kid = "key-1"
        self.jwks_request_count = 0
        self._device_flow_states: dict[str, DeviceFlowState] = {}
        self._issued_refresh_tokens: dict[str, str] = {}
        self.last_token_request_form: dict[str, str] = {}

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _build_handler_class(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        """This provider's own base URL, e.g. `http://127.0.0.1:54321`.

        Returns:
            The `http://host:port` string of the running server, with no trailing slash.
        """
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}"

    @property
    def issuer(self) -> str:
        """The `iss` value this provider advertises -- its own base URL.

        Returns:
            The same string as `base_url`; also the default `iss` of minted tokens.
        """
        return self.base_url

    @property
    def jwks_uri(self) -> str:
        """The `jwks_uri` this provider's discovery document advertises.

        Returns:
            `base_url` followed by `/jwks.json`.
        """
        return f"{self.base_url}/jwks.json"

    def discovery_document(self) -> dict[str, Any]:
        """The RFC 8414/OIDC-Discovery document served at `/.well-known/openid-configuration`.

        Returns:
            A dict with `issuer`, `jwks_uri`, `id_token_signing_alg_values_supported`
            (`["RS256"]`), `device_authorization_endpoint` and `token_endpoint`.
        """
        return {
            "issuer": self.issuer,
            "jwks_uri": self.jwks_uri,
            "id_token_signing_alg_values_supported": [_ALG],
            "device_authorization_endpoint": f"{self.base_url}/device_authorization",
            "token_endpoint": f"{self.base_url}/token",
        }

    def jwks_document(self) -> dict[str, Any]:
        """The JWK Set served at `/jwks.json` -- every key this provider currently knows about.

        Returns:
            A dict `{"keys": [...]}` holding the public JWK (with `kid`, `use` of `sig`
            and `alg` of `RS256`) of every key, including keys added by `rotate_key`.
        """
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

        Purpose: produce tokens a verifier configured against this provider accepts, or
        deliberately malformed variants (expired, unknown `kid`, wrong `iss`/`aud`).

        Args:
            sub: The `sub` claim.
            aud: The `aud` claim; defaults to `ps-service`.
            iss: The `iss` claim; defaults to this provider's own `issuer`.
            exp_delta: Seconds from now until `exp`; negative mints an
                already-expired token.
            kid: The signing key's `kid`. Defaults to the currently active
                key. A `kid` naming neither the original nor a
                `rotate_key()`-added keypair mints a token signed by a
                *freshly generated, never-published* key -- simulating a
                JWKS the issuer never actually advertised (the
                still-unknown-`kid` case).
            alg: The JWS `alg` header. Defaults to `RS256`. This provider only holds
                RSA keys, so a symmetric algorithm such as `HS256` cannot be signed
                here and raises; a test needing one must build that token itself.
            extra_claims: Additional/overriding claims merged in last (so a
                test can override `exp`/`iat` etc. directly).

        Returns:
            The encoded JWT (compact JWS string) whose header carries the signing `kid`.

        Raises:
            TypeError: If `alg` is a symmetric algorithm such as `HS256`.
            jwt.exceptions.InvalidKeyError: If `alg` is incompatible with an RSA key
                (e.g. `none`).
            NotImplementedError: If `alg` is not a known JWS algorithm.
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
        minted tokens use the new one, letting a test mint a token whose
        `kid` is genuinely absent from whatever JWKS a verifier fetched
        *before* this call.

        Returns:
            The new key's `kid` (`key-<n>`, with n one more than the number of keys held).

        Side Effects:
            Generates a new RSA keypair, adds it to the JWKS and makes it the key used by
            `mint_token` when no `kid` is passed.
        """
        new_kid = f"key-{len(self._keys) + 1}"
        self._keys[new_kid] = _RsaSigningKey(new_kid)
        self._active_kid = new_kid
        return new_kid

    def device_flow_state(self, device_code: str) -> DeviceFlowState:
        """Return the recorded `DeviceFlowState` for `device_code` (client_id/audience included).

        Lets a test read back what a `POST /device_authorization` request
        recorded -- e.g. asserting `audience` was passed through unchanged.

        Parameters:
            device_code: A `device_code` previously returned by `POST /device_authorization`.

        Returns:
            The live `DeviceFlowState` object (mutations are visible to the server).

        Raises:
            KeyError: If `device_code` was never issued by this provider.
        """
        return self._device_flow_states[device_code]

    def complete_device_flow(self, device_code: str, *, sub: str = "test-subject") -> None:
        """Mark `device_code` approved as `sub`; subsequent `/token` polls succeed.

        Parameters:
            device_code: A `device_code` previously issued by this provider.
            sub: The `sub` claim of the token the next successful poll mints.

        Returns:
            None.

        Raises:
            KeyError: If `device_code` was never issued by this provider.

        Side Effects:
            Sets the grant's status to `"approved"` and records `sub`.
        """
        self._device_flow_states[device_code].status = "approved"
        self._device_flow_states[device_code].sub = sub

    def simulate_slow_down_once(self, device_code: str) -> None:
        """Make exactly the next `/token` poll for `device_code` return `slow_down`.

        Reverts to `device_code`'s underlying status (still `pending` unless
        also completed/expired/denied) on the poll after that.

        Parameters:
            device_code: A `device_code` previously issued by this provider.

        Returns:
            None.

        Raises:
            KeyError: If `device_code` was never issued by this provider.

        Side Effects:
            Sets the grant's one-shot `slow_down_once` flag.
        """
        self._device_flow_states[device_code].slow_down_once = True

    def expire_device_code(self, device_code: str) -> None:
        """Make subsequent `/token` polls for `device_code` return `expired_token`.

        Parameters:
            device_code: A `device_code` previously issued by this provider.

        Returns:
            None.

        Raises:
            KeyError: If `device_code` was never issued by this provider.

        Side Effects:
            Sets the grant's status to `"expired"`.
        """
        self._device_flow_states[device_code].status = "expired"

    def deny_device_code(self, device_code: str) -> None:
        """Make subsequent `/token` polls for `device_code` return `access_denied`.

        Parameters:
            device_code: A `device_code` previously issued by this provider.

        Returns:
            None.

        Raises:
            KeyError: If `device_code` was never issued by this provider.

        Side Effects:
            Sets the grant's status to `"denied"`.
        """
        self._device_flow_states[device_code].status = "denied"

    def handle_device_authorization_request(self, form: Mapping[str, str]) -> dict[str, Any]:
        """Mint a fresh `device_code`/`user_code` pair for `POST /device_authorization`.

        Records the request's `client_id`/`audience`/`scope` on the resulting
        `DeviceFlowState`, readable back via `device_flow_state`. Not
        called directly by test code -- driven through the real HTTP
        server via `do_POST`; public so it is reachable from the handler
        closure without tripping ruff's private-member-access check.

        Parameters:
            form: The decoded form fields of the request; `client_id`, `audience` and
                `scope` are read, each optional.

        Returns:
            The JSON response body: `device_code`, `user_code` (8 upper-case hex chars),
            `verification_uri`, `verification_uri_complete`, `expires_in` (600) and
            `interval` (1).

        Side Effects:
            Stores a new pending `DeviceFlowState` under the new `device_code`.
        """
        device_code = secrets.token_urlsafe(16)
        user_code = secrets.token_hex(4).upper()
        self._device_flow_states[device_code] = DeviceFlowState(
            client_id=form.get("client_id", ""),
            audience=form.get("audience"),
            scope=form.get("scope"),
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

        Parameters:
            form: The decoded form fields of the request; `grant_type` selects the branch
                (the device-code URN or `refresh_token`), then `device_code`, or
                `client_id` and `refresh_token`, are read.

        Returns:
            A `(http_status, json_body)` pair: 200 with `access_token`, `refresh_token`,
            `expires_in` and `token_type` on success; 400 with an `error` of
            `authorization_pending`, `slow_down`, `expired_token`, `access_denied`,
            `invalid_grant` (unknown or stale refresh token) or `unsupported_grant_type`.

        Side Effects:
            Overwrites `last_token_request_form`. A successful device-code exchange or
            refresh issues a new refresh token per `client_id`, invalidating the previous.
        """
        self.last_token_request_form = dict(form)
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
        """Stop the background HTTP server thread. Called by the `mock_oidc_provider` fixture.

        Returns:
            None.

        Side Effects:
            Shuts the server down and joins its thread for up to 5 seconds; the provider
            must not be used afterwards.
        """
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
    The module docstring shows the exact import shape.

    Function-scoped (not session/module): tests mutate provider state
    (`rotate_key`, `jwks_request_count`), and a fresh instance per test is
    what keeps those tests independent of run order -- the fixed cost of a
    real RSA keypair generation + thread start is milliseconds.

    Yields:
        A running `MockOidcProvider`, on a fresh ephemeral loopback port.

    Side Effects:
        Starts an HTTP server thread; stops it (via `shutdown()`) on teardown even when
        the test fails.
    """
    provider = MockOidcProvider()
    try:
        yield provider
    finally:
        provider.shutdown()
