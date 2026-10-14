"""Domain-specific exception types for `ps_service.ingestion_runs`.

The two store-boundary errors share the base `IngestionRunStoreError` so a tool body can map
both with one `except`. Their messages are fixed and detail-free: never host, port or driver
text. The driver error is chained via `from` for server-side diagnosis only.
"""

from __future__ import annotations

UNAVAILABLE_MESSAGE = "The ingestion run store is temporarily unavailable."
PERSISTENCE_MESSAGE = "The ingestion run could not be recorded."


class IngestionRunCapacityExceededError(Exception):
    """Admitting another run would exceed the configured in-flight cap.

    Raised by `dispatch.reserve_run_slot`. Not a store error: nothing was written. The message
    names the limit and is safe to return; the caller should wait for a run to finish and retry.
    """

    def __init__(self, max_in_flight_runs: int) -> None:
        """Build the fixed rate-limit text from the configured cap.

        Args:
            max_in_flight_runs: The configured cap, named in the message.
        """
        super().__init__(
            f"too many ingestion runs are already in progress (limit {max_in_flight_runs}); "
            "wait for one to finish, then try again"
        )


class IngestionRunAlreadyInProgressError(Exception):
    """A run for the same `short_name` is already in flight. Not a store error.

    Raised by `dispatch.reserve_run_slot`; the caller should wait for that run to finish
    instead of submitting the instrument again.
    """

    def __init__(self, short_name: str) -> None:
        """Build the fixed duplicate-submission text from the regulation's short name.

        Args:
            short_name: Short name of the instrument already being ingested.
        """
        super().__init__(
            f"an ingestion run for short_name '{short_name}' is already in progress; "
            "wait for it to finish instead of submitting it again"
        )


class IngestionRunInvalidCompletionError(Exception):
    """A completion call broke the contract: `reason_code` is required exactly when failed.

    A caller bug, not a store error: raised before anything is read or written, so the run
    stays `running` and no audit row is attempted. A failure must carry an enumerated cause.
    """

    def __init__(self) -> None:
        """Build the fixed text; it never echoes caller-supplied values."""
        super().__init__("reason_code is required exactly when the completion status is 'failed'")


class IngestionRunStoreError(Exception):
    """Base type of every `ingestion_runs` store error.

    Catch it to map both store failures with one `except`; the subclasses distinguish an
    unreachable store from a failed write.
    """


class IngestionRunStoreUnavailableError(IngestionRunStoreError):
    """The PS state Postgres could not be reached or read. Fixed, detail-free message.

    The caller may retry later; the driver error is chained for server-side diagnosis only.
    """

    def __init__(self, message: str = UNAVAILABLE_MESSAGE) -> None:
        """Default to the one fixed message; never build it from driver or connection text.

        Args:
            message: Error text; defaults to `UNAVAILABLE_MESSAGE`.
        """
        super().__init__(message)


class IngestionRunPersistenceError(IngestionRunStoreError):
    """A write failed and was rolled back. Fixed, detail-free message.

    Nothing was written; the caller may retry. The driver error is chained for server-side
    diagnosis only.
    """

    def __init__(self, message: str = PERSISTENCE_MESSAGE) -> None:
        """Default to the one fixed message; never build it from driver text.

        Args:
            message: Error text; defaults to `PERSISTENCE_MESSAGE`.
        """
        super().__init__(message)
