"""A group staged on the caller's transaction, waiting for the caller's commit (issue #206).

`GraphWriteGateway.submit_group_in_transaction` returns one of these after the group was appended
on the caller's cursor. It holds the graph's in-process lock (lock order: see `graph_locks`)
until it is completed, aborted or the `with` block ends. Applying is deliberately not done on
`__exit__`: only the caller knows whether its transaction committed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self

from ps_service.graph_gateway.errors import StagedSubmissionClosedError

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from ps_service.graph_gateway.models import GroupOutcome


class StagedSubmission:
    """One submitted group between append and the caller's commit.

    Use as a context manager: after committing, call `complete()`; after rolling back, call
    `abort()`. Leaving the block without either releases the lock and applies nothing, and the
    entries (if they were committed) are applied by the next write to the graph.
    """

    def __init__(
        self,
        *,
        status: Literal["staged", "unchanged"],
        finish: Callable[[], GroupOutcome],
        release: Callable[[], None],
    ) -> None:
        """Take the step that finishes the submission and the step that releases its lock."""
        self._status: Literal["staged", "unchanged"] = status
        self._finish = finish
        self._release = release
        self._open = True

    @property
    def status(self) -> Literal["staged", "unchanged"]:
        """`staged`: a group was appended on the caller's transaction. `unchanged`: it was not."""
        return self._status

    def complete(self) -> GroupOutcome:
        """Apply the committed entries, advance the marker and return the outcome.

        Call after the caller's transaction committed. The submission is closed afterwards, also
        when this raises.

        Raises:
            StagedSubmissionClosedError: already completed or aborted.
            StagedGroupNotCommittedError: the group is not in the log yet.
        """
        if not self._open:
            message = "the staged submission is already closed"
            raise StagedSubmissionClosedError(message)
        try:
            return self._finish()
        finally:
            self.abort()

    def abort(self) -> None:
        """Release the graph lock without applying anything (the caller rolled back); idempotent."""
        if self._open:
            self._open = False
            self._release()

    def __enter__(self) -> Self:
        """Enter the block that spans the caller's commit."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the lock if the submission is still open; never applies."""
        self.abort()
