"""Domain-specific exception types for `ps_service.ingestion_runs` (issue #194).

The two store-boundary errors share the base `IngestionRunStoreError` so a tool body can map
both with one `except`. Their messages are fixed and detail-free: never host, port or driver
text. The driver error is chained via `from` for server-side diagnosis only.
"""

from __future__ import annotations

UNAVAILABLE_MESSAGE = "The ingestion run store is temporarily unavailable."
PERSISTENCE_MESSAGE = "The ingestion run could not be recorded."


class IngestionRunCapacityExceededError(Exception):
    """Admitting another run would exceed the configured in-flight cap (AC-BI-012).

    Not a store error: nothing was written. The message names the limit and is safe to return.
    """

    def __init__(self, max_in_flight_runs: int) -> None:
        """Build the fixed rate-limit text from the configured cap."""
        super().__init__(
            f"too many ingestion runs are already in progress (limit {max_in_flight_runs}); "
            "wait for one to finish, then try again"
        )


class IngestionRunAlreadyInProgressError(Exception):
    """A run for the same `short_name` is already in flight. Not a store error."""

    def __init__(self, short_name: str) -> None:
        """Build the fixed duplicate-submission text from the regulation's short name."""
        super().__init__(
            f"an ingestion run for short_name '{short_name}' is already in progress; "
            "wait for it to finish instead of submitting it again"
        )


class IngestionRunStoreError(Exception):
    """Base type of every `ingestion_runs` store error."""


class IngestionRunStoreUnavailableError(IngestionRunStoreError):
    """The PS state Postgres could not be reached or read. Fixed, detail-free message."""

    def __init__(self, message: str = UNAVAILABLE_MESSAGE) -> None:
        """Default to the one fixed message; never build it from driver or connection text."""
        super().__init__(message)


class IngestionRunPersistenceError(IngestionRunStoreError):
    """A write failed and was rolled back. Fixed, detail-free message."""

    def __init__(self, message: str = PERSISTENCE_MESSAGE) -> None:
        """Default to the one fixed message; never build it from driver text."""
        super().__init__(message)
