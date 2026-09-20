"""A real, local, generic mock OIDC provider (issue #58, AC-BI-017).

Serves its own ``/.well-known/openid-configuration`` discovery document and
``/jwks.json`` JWK Set over a real ``http.server.ThreadingHTTPServer`` bound
to an ephemeral loopback port -- a real local HTTP server, not a
monkeypatched transport, because ``jwt.PyJWKClient.fetch_data`` calls
``urllib.request.urlopen`` directly with no injectable seam (PLAN.md §0.2).

Deliberately generic: nothing here is shaped like Auth0 or Entra ID (no
tenant ids, no vendor-specific claim names) -- the **one** mock OIDC provider
AC-BI-017 requires, reused unchanged by every later slice
(Slice 4: 200 path/principal; Slice 5: MCP; Slice 6: `rotate_key` for the
kid-miss-refetch-once test; Slice 7: algorithm allow-list; Slice 10: audit
logging) -- never a second mock provider.

Import convention for cross-package reuse (there is no existing repo
precedent for a test fixture shared *across* `tests/<package>/` boundaries --
`ps-service/tests/` itself has no `__init__.py`, deliberately, per the root
`pyproject.toml`'s own INP001 comment, so a *root* `tests/conftest.py` cannot
reliably `import tests.auth...` -- `ps-service` is not yet on `sys.path` at
the point pytest loads the root conftest, only once test-module collection
begins): a consuming test module imports this file directly, e.g.
``from tests.auth.mock_oidc_provider import MockOidcProvider``. The
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

    from tests.auth.mock_oidc_provider import (
        mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    )
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_ALG = "RS256"
_DEFAULT_AUDIENCE = "ps-service"
_DEFAULT_EXP_DELTA_SECONDS = 3600.0


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


def _serve_json(handler: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _build_handler_class(provider: MockOidcProvider) -> type[BaseHTTPRequestHandler]:
    """Build a `BaseHTTPRequestHandler` subclass closing over `provider`.

    A closure-based factory (not a class attribute set after the fact) so
    each `MockOidcProvider` instance's server serves that instance's own
    state -- `ThreadingHTTPServer` requires a handler *class*, not an
    instance, so `provider` must be captured some way other than `self`.
    """

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
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

    return _Handler


class MockOidcProvider:
    """A real, ephemeral-port local OIDC provider: discovery doc + JWKS + token minting.

    `jwks_request_count` lets a test assert exactly how many times `/jwks.json`
    was fetched (Slice 6's "refetch once, not repeatedly" assertion).
    """

    def __init__(self) -> None:
        self._keys: dict[str, _RsaSigningKey] = {"key-1": _RsaSigningKey("key-1")}
        self._active_kid = "key-1"
        self.jwks_request_count = 0

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
