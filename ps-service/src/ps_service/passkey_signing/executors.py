"""Registries of approval executors and effect verifiers, keyed by tool name.

A signed approval runs the executor registered for its `tool_name` (the `sign/verify` route calls
it after consuming the signature). The component that owns a privileged action registers its
executor at import time, the same "module import triggers registration" idiom as
`ps_service.audit.models.register_audit_action`, so this package never imports the components
that use it. The `near_misses_resolve` tool is the exception: it runs inside the router because
it needs the REST layer's injected near-miss dependencies. Every other tool name dispatches here.

An effect verifier answers "did this approval's edit actually reach the graph?" for a signed
approval whose outcome was never recorded (a crash between the graph write and the outcome
write); the lazy reconciler in `graph_cleanup` uses it.

Types:
    ApprovalExecutor: `(row, config) -> outcome dict`; runs the approved action.
    ApprovalEffectVerifier: `(row, graph) -> bool`; True when the approved change is in the graph.

The registries are module-level dicts filled at import time and read-only afterwards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.config import ServiceConfig
    from ps_service.passkey_signing.models import PendingApprovalRow

__all__ = [
    "ApprovalEffectVerifier",
    "ApprovalExecutor",
    "register_approval_executor",
    "register_effect_verifier",
    "resolve_approval_executor",
    "resolve_effect_verifier",
]

type ApprovalExecutor = Callable[[PendingApprovalRow, ServiceConfig], dict[str, object]]
type ApprovalEffectVerifier = Callable[[PendingApprovalRow, GraphHandle], bool]

_EXECUTORS: dict[str, ApprovalExecutor] = {}
_VERIFIERS: dict[str, ApprovalEffectVerifier] = {}


def register_approval_executor(tool_name: str, executor: ApprovalExecutor) -> None:
    """Register `executor` as the function a signed `tool_name` approval runs.

    Args:
        tool_name: Name of the privileged tool; at most one executor per name. The router
            never consults the registry for `near_misses_resolve`, which it runs itself.
        executor: Called with the signed approval row and the service configuration; returns
            the outcome dict stored on the row and returned to the approver.

    Raises:
        ValueError: `tool_name` already has an executor.

    Side Effects:
        Adds `executor` to the module-level executor registry.
    """
    if tool_name in _EXECUTORS:
        message = f"an approval executor for {tool_name!r} is already registered"
        raise ValueError(message)
    _EXECUTORS[tool_name] = executor


def resolve_approval_executor(tool_name: str) -> ApprovalExecutor | None:
    """Look up the executor registered for `tool_name`.

    Args:
        tool_name: Name of the privileged tool.

    Returns:
        The registered executor, or `None` when the name has none.
    """
    return _EXECUTORS.get(tool_name)


def register_effect_verifier(tool_name: str, verifier: ApprovalEffectVerifier) -> None:
    """Register `verifier` as the effect check for `tool_name`'s approvals.

    Args:
        tool_name: Name of the privileged tool; at most one verifier per name.
        verifier: Called with the signed approval row and a graph handle; returns True when
            the approved change is already present in the graph.

    Raises:
        ValueError: `tool_name` already has a verifier.

    Side Effects:
        Adds `verifier` to the module-level verifier registry.
    """
    if tool_name in _VERIFIERS:
        message = f"an effect verifier for {tool_name!r} is already registered"
        raise ValueError(message)
    _VERIFIERS[tool_name] = verifier


def resolve_effect_verifier(tool_name: str) -> ApprovalEffectVerifier | None:
    """Look up the effect verifier registered for `tool_name`.

    Args:
        tool_name: Name of the privileged tool.

    Returns:
        The registered verifier, or `None` when the name has none.
    """
    return _VERIFIERS.get(tool_name)
