"""Process-wide registry of whether each real external dependency was reachable on its last call.

Covers FalkorDB, the LLM Interface, and Cellar/ELI. Fed by the same call
sites that already handle each dependency's exceptions for their own
purposes (`falkordb_client.check_connectivity`, `graph_writer`'s
`graph.query()` calls, `llm_interface.completion`/`embedding`,
`cellar_eli.fetch_xhtml`) — this module adds no probing of its own, it only
records outcomes those call sites already observe.

Mirrors `ps_service.logging.facade`'s process-wide-singleton-plus-`reset_for_tests`
shape rather than living on `app.state`: most callers here (graph_writer,
llm_interface, the Cellar/ELI adapter) run outside any FastAPI request/app
context and have no `app.state` to write into. `main.py` reads this registry via
`all_healthy()` for `/ready`; it does not own it.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

FALKORDB = "falkordb"
LLM_INTERFACE = "llm_interface"
CELLAR_ELI = "cellar_eli"

_lock = threading.Lock()
_unhealthy: dict[str, str] = {}  # dependency name -> last error message; absent means healthy


def mark_healthy(dependency: str) -> None:
    """Record that `dependency`'s most recent call succeeded."""
    with _lock:
        _unhealthy.pop(dependency, None)


def mark_unhealthy(dependency: str, *, error: Exception) -> None:
    """Record that `dependency`'s most recent call failed."""
    with _lock:
        _unhealthy[dependency] = str(error)


def is_healthy(dependency: str) -> bool:
    """Whether `dependency`'s most recent recorded call succeeded.

    A dependency with no recorded call yet is considered healthy — by the
    time `main.py`'s startup gate can ever report ready, its own startup
    probe has already recorded an outcome for all three, so this default
    only matters before any check has run at all.
    """
    with _lock:
        return dependency not in _unhealthy


def all_healthy(dependencies: Iterable[str]) -> bool:
    """Whether every named dependency's most recent recorded call succeeded."""
    return all(is_healthy(dependency) for dependency in dependencies)


def reset_for_tests() -> None:
    """Clear all recorded state so each test starts with every dependency healthy."""
    with _lock:
        _unhealthy.clear()
