"""A group staged on the caller's transaction, waiting for the caller's commit.

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
        """Take the step that finishes the submission and the step that releases its lock.

        Args:
            status: `staged` if a group was appended on the caller's transaction, `unchanged` if
                nothing was appended.
            finish: Applies the committed entries and returns the outcome.
            release: Releases the graph's in-process lock.
        """
        self._status: Literal["staged", "unchanged"] = status
        self._finish = finish
        self._release = release
        self._open = True

    @property
    def status(self) -> Literal["staged", "unchanged"]:
        """Whether the group was appended.

        Returns:
            `staged` if a group was appended on the caller's transaction, `unchanged` if not.
        """
        return self._status

    def complete(self) -> GroupOutcome:
        """Apply the committed entries, advance the marker and return the outcome.

        Call after the caller's transaction committed. The submission is closed afterwards, also
        when this raises.

        Returns:
            The outcome of applying the group.

        Raises:
            StagedSubmissionClosedError: already completed or aborted.
            StagedGroupNotCommittedError: the group is not in the log yet.

        Side Effects:
            Applies the entries to the graph, advances the applied marker and releases the lock.
        """
        if not self._open:
            message = "the staged submission is already closed"
            raise StagedSubmissionClosedError(message)
        try:
            return self._finish()
        finally:
            self.abort()

    def abort(self) -> None:
        """Release the graph lock without applying anything (the caller rolled back); idempotent.

        Side Effects:
            Releases the graph's in-process lock on the first call; later calls do nothing.
        """
        if self._open:
            self._open = False
            self._release()

    def __enter__(self) -> Self:
        """Enter the block that spans the caller's commit.

        Returns:
            This submission.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the lock if the submission is still open; never applies.

        Args:
            exc_type: Type of the exception that ended the block, if any.
            exc: The exception that ended the block, if any; it is not suppressed.
            traceback: Traceback of that exception, if any.
        """
        self.abort()
