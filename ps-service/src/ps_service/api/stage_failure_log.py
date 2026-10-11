"""Operator-log detail for a failed pipeline stage (issue #227, AC-BI-009/010).

A stage failure's real cause is logged server-side while the API body stays ``"<stage> failed"``.
The detail an operator reads must be safe to leave in a shared log: secret-shaped spans, URLs,
host:port tokens and filesystem paths are redacted and the text is truncated. No traceback and
no ``repr`` (which can embed arguments) is logged.
"""

from __future__ import annotations

import re

from ps_service.api.error_handlers import (
    _scrub_text,  # pyright: ignore[reportPrivateUsage]  # shared scrubber, same reuse as ingestion_orchestration
)

STAGE_FAILURE_DETAIL_MAX_LEN = 300
"""Longest ``detail`` string written to the operator log."""

_REDACTION = "[redacted-secret]"
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[\w.~+/=\-]+")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:[\w\-]*(?:api[_-]?key|token|secret|password|passwd|authorization))\s*[=:]\s*\S+"
)


def stage_failure_detail(exc: Exception) -> str:
    """Return ``exc``'s message made safe for the operator log.

    Args:
        exc: The exception a pipeline stage raised.

    Returns:
        ``str(exc)`` with bearer tokens and ``key=value`` secrets redacted, then URLs, host:port
        tokens and paths scrubbed, truncated to ``STAGE_FAILURE_DETAIL_MAX_LEN`` characters.
    """
    text = _BEARER_RE.sub(_REDACTION, str(exc))
    text = _ASSIGNMENT_RE.sub(_REDACTION, text)
    return _scrub_text(text)[:STAGE_FAILURE_DETAIL_MAX_LEN]
