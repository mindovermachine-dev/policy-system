"""Domain types for `ps_service.ingestion_runs`: the run lifecycle status and the stored row."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import (
    datetime,  # noqa: TC003 -- dataclass field annotation, kept importable at runtime
)
from typing import Literal

IngestionRunStatus = Literal["running", "succeeded", "failed"]
"""The lifecycle of one run: `running` until exactly one terminal write flips it."""


@dataclass(frozen=True, slots=True)
class IngestionRunRow:
    """One `ingestion_runs` row: the persisted state of an asynchronously submitted run.

    A plain frozen dataclass, as `passkey_signing.models` is.

    Attributes:
        run_id: UUID string identifying the run.
        celex: CELEX identifier submitted for ingestion.
        short_name: Short name the instrument is ingested under.
        actor_subject: Subject of the principal that submitted the run.
        actor_issuer: Issuer of that principal.
        status: `running` until exactly one terminal write flips it to `succeeded` or `failed`.
        result: The success dict `ingest_regulation` would have returned; set only when
            `status == "succeeded"`, otherwise `None`.
        error: Sanitized `error:` text; set only when `status == "failed"`, otherwise `None`.
        submitted_at: When the run was accepted.
        finished_at: When the terminal write happened; `None` while `running`.
    """

    run_id: str
    celex: str
    short_name: str
    actor_subject: str
    actor_issuer: str
    status: IngestionRunStatus
    result: dict[str, object] | None
    error: str | None
    submitted_at: datetime
    finished_at: datetime | None
