"""Read the semantic `metrics` log entries a test produced."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ps_service.logging import facade

if TYPE_CHECKING:
    from pathlib import Path


def metrics_entries(log_path: Path, action: str, outcome: str = "failed") -> list[dict[str, Any]]:
    """Drain the default emitter, then return its `metrics` entries for `action` and `outcome`.

    The emitter is stopped, so call this once at the end of a test.
    """
    saved = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]
    facade.reset_for_tests()
    facade._atexit_registered = saved  # pyright: ignore[reportPrivateUsage]
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    return [
        entry
        for entry in lines
        if entry.get("component") == "metrics"
        and entry.get("action") == action
        and entry.get("outcome") == outcome
    ]
