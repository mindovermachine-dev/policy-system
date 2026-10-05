"""Domain-specific exception types for `ps_service.graph_cleanup` (issue #190).

One type per failure boundary this component owns (L2 Error Handling).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ps_service.graph_cleanup.models import CapabilityMergePreview


class GraphCleanupPersistenceError(Exception):
    """A FalkorDB read or write for the single-tenant graph could not be completed.

    Carries a generic message only; the underlying driver error is chained as
    `__cause__` and logged server-side, never surfaced across the MCP boundary.
    """


class GraphCleanupValidationError(Exception):
    """A requested cleanup operation is not allowed on the current graph state.

    The message is written for the Compliance Officer (self-merge, missing node,
    tombstone, unsupported governance case) and contains no internal detail.
    """


class GraphCleanupStaleStateError(Exception):
    """The graph changed between the preview and the guarded write; nothing was written."""


class GraphCleanupAcknowledgmentRequiredError(Exception):
    """A case-2 merge was requested without `acknowledge_governance_change`; no approval exists.

    Carries the `preview` so the caller can show it. The message is the acknowledgment text
    the Compliance Officer must accept before the call is repeated with the flag.
    """

    def __init__(self, preview: CapabilityMergePreview, message: str) -> None:
        """Keep the previewed merge alongside the acknowledgment text."""
        super().__init__(message)
        self.preview = preview
