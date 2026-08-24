"""Public API for ps_service.dependency_health.

A process-wide live-health registry for FalkorDB, LLM Interface, and
Cellar/ELI, fed by their own real-traffic exception handling so PS Service's
`/ready` can reflect current reachability without any extra polling cost.
"""

from ps_service.dependency_health.registry import (
    CELLAR_ELI,
    FALKORDB,
    LLM_INTERFACE,
    all_healthy,
    is_healthy,
    mark_healthy,
    mark_unhealthy,
    reset_for_tests,
)

__all__ = [
    "CELLAR_ELI",
    "FALKORDB",
    "LLM_INTERFACE",
    "all_healthy",
    "is_healthy",
    "mark_healthy",
    "mark_unhealthy",
    "reset_for_tests",
]
