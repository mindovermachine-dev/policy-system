"""Domain types for `ps_service.ingestion_runs` (issue #194)."""

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
    """One `ingestion_runs` row.

    A plain frozen dataclass, as `passkey_signing.models` does. `result` is the success dict
    `ingest_regulation` would have returned (set only when `status == "succeeded"`); `error`
    is the sanitized `error:` text (set only when `status == "failed"`).
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
