"""Approval-executor and effect-verifier registries (issue #190).

A signed approval runs whatever executor is registered for its `tool_name`
(`POST /approvals/{id}/sign/verify`, after the signature is consumed). The component
that owns a privileged action registers its executor at import time -- the same
"module import triggers registration" idiom as `ps_service.audit.models.register_audit_action`
-- so this package never imports the components that use it.

`near_misses_resolve` keeps its original in-router path (it needs the REST layer's injected
near-miss dependencies); every other tool name dispatches through here.

An effect verifier answers "did this approval's edit actually reach the graph?" for a
signed row whose outcome was never recorded (a crash between the graph write and the
outcome write); the lazy reconciler in `graph_cleanup` uses it.
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
    """Register `executor` as the one function a signed `tool_name` approval runs.

    Raises:
        ValueError: `tool_name` already has an executor.
    """
    if tool_name in _EXECUTORS:
        message = f"an approval executor for {tool_name!r} is already registered"
        raise ValueError(message)
    _EXECUTORS[tool_name] = executor


def resolve_approval_executor(tool_name: str) -> ApprovalExecutor | None:
    """Return the executor registered for `tool_name`, or `None`."""
    return _EXECUTORS.get(tool_name)


def register_effect_verifier(tool_name: str, verifier: ApprovalEffectVerifier) -> None:
    """Register `verifier` as the effect check for `tool_name`'s approvals.

    Raises:
        ValueError: `tool_name` already has a verifier.
    """
    if tool_name in _VERIFIERS:
        message = f"an effect verifier for {tool_name!r} is already registered"
        raise ValueError(message)
    _VERIFIERS[tool_name] = verifier


def resolve_effect_verifier(tool_name: str) -> ApprovalEffectVerifier | None:
    """Return the effect verifier registered for `tool_name`, or `None`."""
    return _VERIFIERS.get(tool_name)
