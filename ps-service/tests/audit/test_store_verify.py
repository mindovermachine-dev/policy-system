"""Fast tests for mapping the `audit_log.verify_chain()` result row to the domain model.

The SQL itself runs against a real Postgres in `test_integrity_verify_live.py`; this proves only
the boundary mapping (`_verification_from_record`), which needs no connection.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from ps_service.audit.models import AuditChainBreak, AuditChainVerification
from ps_service.audit.store import _verification_from_record  # pyright: ignore[reportPrivateUsage]

_WHEN = datetime(2026, 3, 4, 5, 6, 7, 123456, tzinfo=UTC)


def test_an_intact_record_maps_to_an_intact_verification_without_a_break() -> None:
    record = (True, 7, 7, None, None, None, None)

    assert _verification_from_record(record) == AuditChainVerification(
        intact=True, rows_checked=7, head_chain_seq=7, first_break=None
    )


def test_a_row_break_record_maps_to_a_break_with_the_row_id_as_text() -> None:
    row_id = uuid.uuid4()
    record = (False, 10, 10, "row_hash_mismatch", 4, row_id, _WHEN)

    assert _verification_from_record(record) == AuditChainVerification(
        intact=False,
        rows_checked=10,
        head_chain_seq=10,
        first_break=AuditChainBreak(
            kind="row_hash_mismatch", chain_seq=4, event_id=str(row_id), occurred_at=_WHEN
        ),
    )


def test_a_head_mismatch_record_maps_to_a_break_with_no_row() -> None:
    record = (False, 8, 10, "head_mismatch", None, None, None)

    assert _verification_from_record(record) == AuditChainVerification(
        intact=False,
        rows_checked=8,
        head_chain_seq=10,
        first_break=AuditChainBreak(
            kind="head_mismatch", chain_seq=None, event_id=None, occurred_at=None
        ),
    )
