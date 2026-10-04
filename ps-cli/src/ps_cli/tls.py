"""TLS trust for every `httpx.Client` ps-cli builds (issue #175).

ps-cli and `ps-cli-mcp-bridge` verify server certificates against the operating
system trust store via `truststore`, as the single trust mechanism -- so an
evaluator whose local CA is trusted by the OS (`deploy-ps-eval.sh --apply-host-setup`)
needs no environment tweaks, including when Claude Desktop spawns the bridge with its
own environment. Certificate verification is never disabled.

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
    """Return a verifying `SSLContext` backed by the operating system trust store."""
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def is_certificate_verification_error(exc: BaseException) -> bool:
    """True if `exc`, or anything in its cause/context chain, is a failed certificate check."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False
