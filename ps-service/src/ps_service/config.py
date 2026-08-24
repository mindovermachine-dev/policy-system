"""PS Service composition-root configuration: `ServiceConfig` and `load_config()`.

`main.py` is the process harness's composition root — it resolves the full
config surface exactly once via `load_config()` and injects the result
explicitly into everything that needs it (`uvicorn.run`, `Logging.configure`),
rather than letting components independently read `os.environ`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_DEFAULT_GRACEFUL_SHUTDOWN_SECONDS = 10
_MIN_PORT = 1
_MAX_PORT = 65535


class ServiceConfigurationError(Exception):
    """`PS_SERVICE_*`/`PS_LOGGING_DIR` could not be resolved into a valid `ServiceConfig`."""


@dataclass(frozen=True)
class ServiceConfig:
    """Fully-resolved PS Service process configuration.

    Immutable by design: once `load_config()` resolves the environment into
    a `ServiceConfig`, nothing downstream may mutate it.

    `llm_interface_model`/`llm_interface_embed_model` (from
    `PS_LLMINTERFACE_MODEL`/`PS_LLMINTERFACE_EMBED_MODEL`) are `<provider>/<model>`
    strings passed straight through to `litellm.completion`/`litellm.embedding` —
    see `_parse_model_string` and CONTRIBUTING.md for format/examples.
    """

    host: str
    port: int
    graceful_shutdown_seconds: int
    logging_dir: Path | None
    llm_interface_model: str | None = None
    llm_interface_embed_model: str | None = None


def _parse_port(raw: str) -> int:
    """Parse and range-check `PS_SERVICE_PORT`, failing closed on any invalid value."""
    try:
        port = int(raw)
    except ValueError as exc:
        message = f"PS_SERVICE_PORT must be an integer, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if not (_MIN_PORT <= port <= _MAX_PORT):
        message = f"PS_SERVICE_PORT must be between {_MIN_PORT} and {_MAX_PORT}, got {port}"
        raise ServiceConfigurationError(message)
    return port


def _parse_host(raw: str) -> str:
    """Validate `PS_SERVICE_HOST`, rejecting an explicitly-set empty/whitespace-only value.

    Never widens to a fallback value (e.g. `0.0.0.0`) on a bad value —
    "fails closed" means raising, not silently substituting a wider bind.
    """
    if not raw.strip():
        message = "PS_SERVICE_HOST must not be empty or whitespace-only"
        raise ServiceConfigurationError(message)
    return raw


def _parse_graceful_shutdown_seconds(raw: str) -> int:
    """Parse and validate `PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS`.

    Not covered by a dedicated AC, but validated for consistency with the
    host/port validation surface: a malformed value fails closed with the
    same typed error rather than raising a raw `ValueError`.
    """
    try:
        graceful_shutdown_seconds = int(raw)
    except ValueError as exc:
        message = f"PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS must be an integer, got {raw!r}"
        raise ServiceConfigurationError(message) from exc
    if graceful_shutdown_seconds < 0:
        message = (
            "PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS must not be negative, "
            f"got {graceful_shutdown_seconds}"
        )
        raise ServiceConfigurationError(message)
    return graceful_shutdown_seconds


def _parse_model_string(raw: str, *, env_var_name: str) -> str:
    """Validate a `PS_LLMINTERFACE_MODEL`/`PS_LLMINTERFACE_EMBED_MODEL` value.

    Mirrors `_parse_host`'s "never widen to a fallback" style: rejects an
    explicitly-set empty/whitespace-only value rather than silently treating
    it as unset.

    Only checks non-empty — this is *not* where format is enforced. The
    expected shape is a LiteLLM-recognized `<provider>/<model-or-deployment-name>`
    string, e.g. `azure/gpt-5.4-mini` or `ollama/phi3:mini`. Provider
    credentials (API keys, base URLs) are separate env vars that LiteLLM
    resolves itself — never part of `ServiceConfig`. See CONTRIBUTING.md
    ("Configure the LLM Interface") for the full worked examples.
    """
    if not raw.strip():
        message = f"{env_var_name} must not be empty or whitespace-only"
        raise ServiceConfigurationError(message)
    return raw


def load_config() -> ServiceConfig:
    """Resolve `PS_SERVICE_*`/`PS_LOGGING_DIR` into a `ServiceConfig`.

    Reads the environment exactly once. Unset variables fall back to the
    defaults matching issue #12's originally-shipped hardcoded values.
    Raises `ServiceConfigurationError` if any resolved value is invalid,
    before any other part of the configuration is used.
    """
    host = _parse_host(os.environ.get("PS_SERVICE_HOST", _DEFAULT_HOST))
    port = _parse_port(os.environ.get("PS_SERVICE_PORT", str(_DEFAULT_PORT)))
    graceful_shutdown_seconds = _parse_graceful_shutdown_seconds(
        os.environ.get(
            "PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS",
            str(_DEFAULT_GRACEFUL_SHUTDOWN_SECONDS),
        )
    )
    logging_dir_raw = os.environ.get("PS_LOGGING_DIR")
    logging_dir = Path(logging_dir_raw) if logging_dir_raw is not None else None

    llm_interface_model_raw = os.environ.get("PS_LLMINTERFACE_MODEL")
    llm_interface_model = (
        _parse_model_string(llm_interface_model_raw, env_var_name="PS_LLMINTERFACE_MODEL")
        if llm_interface_model_raw is not None
        else None
    )
    llm_interface_embed_model_raw = os.environ.get("PS_LLMINTERFACE_EMBED_MODEL")
    llm_interface_embed_model = (
        _parse_model_string(
            llm_interface_embed_model_raw, env_var_name="PS_LLMINTERFACE_EMBED_MODEL"
        )
        if llm_interface_embed_model_raw is not None
        else None
    )

    return ServiceConfig(
        host=host,
        port=port,
        graceful_shutdown_seconds=graceful_shutdown_seconds,
        logging_dir=logging_dir,
        llm_interface_model=llm_interface_model,
        llm_interface_embed_model=llm_interface_embed_model,
    )
