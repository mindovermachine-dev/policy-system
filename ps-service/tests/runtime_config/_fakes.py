"""Shared test doubles for the `ps_service.runtime_config` tests (issue #130)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Literal

    import psycopg
    from psycopg.rows import TupleRow

    from ps_service.audit.models import AuditQueryFilters, AuditQueryPage


class RecordingAuditStore:
    """An `AuditStore`-shaped fake that only remembers what `record` was asked to write."""

    def __init__(self) -> None:
        self.recorded: list[dict[str, object]] = []

    def record(
        self,
        cur: psycopg.Cursor[TupleRow],
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> str:
        """Remember the call; never touches `cur`."""
        del cur
        self.recorded.append(
            {
                "actor_subject": actor_subject,
                "actor_issuer": actor_issuer,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "outcome": outcome,
                "details": dict(details),
            }
        )
        return str(uuid.uuid4())

    def record_standalone(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: Literal["applied", "rejected", "failed"],
        details: Mapping[str, object],
    ) -> None:
        """Not used by the runtime-config store; present for `Protocol` conformance."""
        del actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details
        raise NotImplementedError

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        """Not used by the runtime-config store; present for `Protocol` conformance."""
        del filters, cursor, page_size
        raise NotImplementedError
