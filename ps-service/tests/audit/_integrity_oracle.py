"""Test-only oracle for the audit hash chain: the `ps-audit-v1` format, written independently.

It implements the canonical form and the chain hash in Python, so a live test can recompute a
stored `row_hash` without calling the database functions under test. A `jsonb` value is taken
as its database text form (`jsonb::text`), so `jsonb` normalisation itself is not checked here.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

GENESIS = "0" * 64
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _field(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return str(len(encoded)).encode("ascii") + b":" + encoded


def canonical_v1(
    *,
    chain_seq: int,
    row_id: object,
    occurred_at: datetime,
    strings: tuple[str, str, str, str, str, str],
    details_text: str,
) -> bytes:
    """Return the `ps-audit-v1` bytes of one row.

    Args:
        chain_seq: The row's chain sequence number.
        row_id: The row id (any value whose `str` is the canonical UUID text).
        occurred_at: The row's timezone-aware timestamp.
        strings: `actor_subject, actor_issuer, action, resource_type, resource_id, outcome`.
        details_text: The `details` value as the database renders `jsonb::text`.

    Returns:
        The canonical bytes.
    """
    micros = (occurred_at - _EPOCH) // timedelta(microseconds=1)
    out = b"ps-audit-v1"
    out += _field(str(chain_seq)) + _field(str(row_id).lower()) + _field(str(micros))
    for text in strings:
        out += _field(text)
    return out + _field(details_text)


def chain_hash(prev: str, canonical: bytes) -> str:
    """Return `sha256(prev as ASCII || canonical)` as lowercase hex."""
    return hashlib.sha256(prev.encode("utf-8") + canonical).hexdigest()
