"""TLS trust for every `httpx.Client` ps-cli builds.

ps-cli verifies server certificates against the operating system trust store via
`truststore`, as the single trust mechanism -- so an evaluator whose local CA is trusted
by the OS (`deploy-ps-eval.sh --apply-host-setup`) needs no environment tweaks.
Certificate verification is never disabled.

On macOS and Windows `truststore` uses the native verifier and ignores
`SSL_CERT_FILE`/`SSL_CERT_DIR`; on Linux it uses OpenSSL's default paths, which
honour both and fall back to the system bundle.
"""

import ssl

import truststore

CERTIFICATE_ERROR_HINT = (
    "trust the issuing CA in your operating system's certificate store "
    "(on Linux: the system bundle, or SSL_CERT_FILE / SSL_CERT_DIR)"
)


def build_ssl_context() -> ssl.SSLContext:
    """Build the TLS context ps-cli uses for every outbound HTTPS connection.

    Verification is always on and uses the operating system trust store, so a CA
    trusted by the OS needs no environment configuration.

    Returns:
        A `truststore.SSLContext` (an `ssl.SSLContext`) for a TLS client with
        certificate and hostname verification enabled.

    Raises:
        Nothing is raised by this function itself.

    Side Effects:
        None; the OS trust store is consulted lazily at handshake time.
    """
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def is_certificate_verification_error(exc: BaseException) -> bool:
    """Tell whether a failure was caused by a rejected server certificate.

    Used to turn a low-level connection error into an actionable trust hint
    (`CERTIFICATE_ERROR_HINT`) instead of a raw TLS traceback.

    Args:
        exc: The exception to inspect; its `__cause__` / `__context__` chain is
            walked, with cycle protection.

    Returns:
        True if `exc` or any exception in its chain is an
        `ssl.SSLCertVerificationError`; False otherwise.

    Raises:
        Nothing.

    Side Effects:
        None.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False
