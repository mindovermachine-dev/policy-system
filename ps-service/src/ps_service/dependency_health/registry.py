"""Process-wide registry of whether each external dependency was reachable on its last call.

Purpose: let the readiness probe and `/status` report current reachability of FalkorDB, the LLM
Interface, Cellar/ELI, the Passkey Signing Postgres, the PS state Postgres and the startup
graph replay without any extra probing. The module adds no probes of its own; it records the
outcomes that call sites (graph writes, LLM calls, Cellar/ELI fetches, Postgres connectivity
checks) already observe while handling each dependency's exceptions.

`GRAPH_REPLAY` differs from the other names: it is unhealthy from before the first request
until the startup replay of the graph mutation log ends, and is set by the replay's own task
rather than by a call site.

The registry is a process-wide singleton rather than request-scoped application state,
because most callers (graph writer, LLM Interface, Cellar/ELI adapter) run outside any web
request and have no application state to write into. The service entry point reads it through
`all_healthy()` for `/readyz` and does not own it.

Thread safety: all reads and writes are guarded by one lock.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

FALKORDB = "falkordb"
LLM_INTERFACE = "llm_interface"
CELLAR_ELI = "cellar_eli"
PASSKEY_SIGNING_POSTGRES = "passkey_signing_postgres"
STATE_POSTGRES = "state_postgres"
GRAPH_REPLAY = "graph_replay"

_lock = threading.Lock()
_unhealthy: dict[str, str] = {}  # dependency name -> last error message; absent means healthy


def mark_healthy(dependency: str) -> None:
    """Record that a dependency's most recent call succeeded.

    Args:
        dependency: Registry name of the dependency (one of the module's name constants,
            for example `FALKORDB`). Any string is accepted; it is not validated.

    Returns:
        None.

    Side Effects:
        Removes any recorded failure for `dependency` from the process-wide registry.
        Marking a dependency that has no recorded failure is a no-op.
    """
    with _lock:
        _unhealthy.pop(dependency, None)


def mark_unhealthy(dependency: str, *, error: Exception) -> None:
    """Record that a dependency's most recent call failed.

    Args:
        dependency: Registry name of the dependency (one of the module's name constants,
            for example `LLM_INTERFACE`). Any string is accepted; it is not validated.
        error: The exception the failed call raised. Only its string form is kept.

    Returns:
        None.

    Side Effects:
        Stores `str(error)` as the dependency's last error in the process-wide registry,
        replacing any earlier recorded error for the same dependency.
    """
    with _lock:
        _unhealthy[dependency] = str(error)


def is_healthy(dependency: str) -> bool:
    """Report whether a dependency's most recent recorded call succeeded.

    A dependency with no recorded failure is considered healthy. This default only matters
    before any check has run, because the service's startup probes record an outcome for each
    dependency before the readiness endpoint can report ready.

    Args:
        dependency: Registry name of the dependency (one of the module's name constants).

    Returns:
        True when no failure is currently recorded for `dependency` (including a name that
        was never recorded), False when its most recent recorded outcome was a failure.
    """
    with _lock:
        return dependency not in _unhealthy


def all_healthy(dependencies: Iterable[str]) -> bool:
    """Report whether every named dependency's most recent recorded call succeeded.

    Args:
        dependencies: Registry names to check. May be empty.

    Returns:
        True when each named dependency is healthy per `is_healthy`, and also when
        `dependencies` is empty; False when at least one has a recorded failure.
    """
    return all(is_healthy(dependency) for dependency in dependencies)


def reset_for_tests() -> None:
    """Clear all recorded state so every dependency reads as healthy again.

    Intended for test isolation: call it in test setup or teardown so one test's recorded
    failures do not leak into the next.

    Returns:
        None.

    Side Effects:
        Empties the process-wide registry of recorded failures.
    """
    with _lock:
        _unhealthy.clear()
