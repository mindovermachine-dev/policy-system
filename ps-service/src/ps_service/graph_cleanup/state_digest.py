"""Canonical digest of a previewed state for `ps_service.graph_cleanup`.

A passkey approval is bound to the state the officer previewed by a sha256 over that state. The
payload is serialised as canonical JSON (sorted keys, no whitespace), so two equal payloads always
give the same digest and any changed value gives a different one.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["canonical_digest"]


def canonical_digest(payload: Mapping[str, object]) -> str:
    """Return the sha256 hex digest of the canonical JSON of `payload`.

    Args:
        payload: JSON-native values only (str, int, float, bool, None, lists and mappings of
            them); list order is significant, so a caller sorts any set it includes.

    Returns:
        The 64-character lowercase hex digest.

    Raises:
        TypeError: a value is not JSON-serialisable.
    """
    canonical = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
