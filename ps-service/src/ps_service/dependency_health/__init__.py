"""Public API for `ps_service.dependency_health`.

Purpose: a process-wide live-health registry for the external dependencies PS Service
relies on (FalkorDB, the LLM Interface, Cellar/ELI, the Passkey Signing Postgres, the PS
state Postgres and the startup graph replay). Callers record the outcome of calls they
already make, so the `/ready` endpoint can report current reachability without any extra
polling cost.

Exports: the dependency-name constants, `mark_healthy`, `mark_unhealthy`, `is_healthy`,
`all_healthy` and `reset_for_tests`.
"""

from ps_service.dependency_health.registry import (
    CELLAR_ELI,
    FALKORDB,
    GRAPH_REPLAY,
    LLM_INTERFACE,
    PASSKEY_SIGNING_POSTGRES,
    STATE_POSTGRES,
    all_healthy,
    is_healthy,
    mark_healthy,
    mark_unhealthy,
    reset_for_tests,
)

__all__ = [
    "CELLAR_ELI",
    "FALKORDB",
    "GRAPH_REPLAY",
    "LLM_INTERFACE",
    "PASSKEY_SIGNING_POSTGRES",
    "STATE_POSTGRES",
    "all_healthy",
    "is_healthy",
    "mark_healthy",
    "mark_unhealthy",
    "reset_for_tests",
]
