"""Domain-specific exception types for `ps_service.graph_cleanup`.

One type per failure boundary this component owns, so callers can map each to a distinct
response (L2 Error Handling).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.company_merge.errors import CompanyMergePersistenceError

if TYPE_CHECKING:
    from ps_service.graph_cleanup.models import CapabilityMergePreview


class GraphCleanupPersistenceError(CompanyMergePersistenceError):
    """A FalkorDB read or write for the single-tenant graph could not be completed.

    It is a `CompanyMergePersistenceError`, so a near-miss merge whose gateway group is refused
    raises the persistence error the near-miss path already names; callers that catch the
    cleanup type keep working unchanged.

    Raised when the graph or the gateway is unreachable or rejects the operation. Carries a
    generic message only; the underlying driver error is chained as `__cause__` and logged
    server-side, never surfaced across the MCP boundary. The caller may retry the operation.
    """


class GraphCleanupValidationError(Exception):
    """A requested cleanup operation is not allowed on the current graph state.

    Raised for a self-merge, a missing node, a non-active node or an unsupported governance
    case. The message is written for the Compliance Officer and contains no internal detail; the
    caller should show it and not retry unchanged.
    """


class GraphCleanupStaleStateError(Exception):
    """The graph changed between the preview and the guarded write; nothing was written.

    Raised when the write guard finds the previewed state no longer holds. The caller should
    request a fresh preview and decide again.
    """


class GraphCleanupAcknowledgmentRequiredError(Exception):
    """A merge that changes governance was requested without `acknowledge_governance_change`.

    Raised for the case where exactly one of the two Capabilities has a governing policy; no
    approval has been created. Carries the `preview` so the caller can show it. The message is
    the acknowledgment text the Compliance Officer must accept before the call is repeated
    with the flag.

    Attributes:
        preview: The previewed merge the Compliance Officer is asked to acknowledge.
    """

    def __init__(self, preview: CapabilityMergePreview, message: str) -> None:
        """Keep the previewed merge alongside the acknowledgment text.

        Args:
            preview: The previewed merge to show the Compliance Officer.
            message: The acknowledgment text; non-empty, shown to the Compliance Officer.
        """
        super().__init__(message)
        self.preview = preview
