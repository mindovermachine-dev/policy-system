"""Operator-log detail for a failed pipeline stage.

A stage failure's real cause is logged server-side while the API body stays ``"<stage> failed"``.
The detail an operator reads must be safe to leave in a shared log: secret-shaped spans, URLs,
host:port tokens and filesystem paths are redacted and the text is truncated. No traceback and
no ``repr`` (which can embed arguments) is logged.
"""

from __future__ import annotations

import re

from ps_service.api.error_handlers import (
    _scrub_text,  # pyright: ignore[reportPrivateUsage]  # shared scrubber, also used by ingestion_orchestration
)

STAGE_FAILURE_DETAIL_MAX_LEN = 300
"""Longest ``detail`` string written to the operator log."""

_REDACTION = "[redacted-secret]"
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[\w.~+/=\-]+")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:[\w\-]*(?:api[_-]?key|token|secret|password|passwd|authorization))\s*[=:]\s*\S+"
)


def stage_failure_detail(exc: Exception) -> str:
    """Return the message of a failed pipeline stage's exception, made safe for the operator log.

    The API response body stays generic; this text is what an operator reads server-side to find
    the real cause, so it must not leak credentials, addresses or local paths.

    Args:
        exc: The exception a pipeline stage raised. Only ``str(exc)`` is used; the traceback and
            ``repr`` (which can embed arguments) are never read.

    Returns:
        ``str(exc)`` with bearer/basic tokens and ``key=value`` secrets replaced by
        ``[redacted-secret]``, then URLs, host:port tokens and filesystem paths scrubbed, cut to
        at most ``STAGE_FAILURE_DETAIL_MAX_LEN`` characters. An exception with an empty message
        yields an empty string.

    Raises:
        Exception: ``str(exc)`` propagates any error raised by the exception's own ``__str__``.
    """
    text = _BEARER_RE.sub(_REDACTION, str(exc))
    text = _ASSIGNMENT_RE.sub(_REDACTION, text)
    return _scrub_text(text)[:STAGE_FAILURE_DETAIL_MAX_LEN]
