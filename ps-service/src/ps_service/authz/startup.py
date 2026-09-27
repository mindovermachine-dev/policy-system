"""Process-startup resolution of the RBAC bootstrap-owner identity (issue #144).

`require_bootstrap_owner_configured` is called synchronously inside
`create_app(config)`'s own body (see `ps_service.main`), immediately after
`resolve_auth_context(config)` -- mirrors `ps_service.auth.startup`'s own
role exactly (PLAN.md §2 Slice 1 D-2): a config-completeness fact that
cannot change for the life of the process belongs in the synchronous
constructor path, so it fails before `create_app` ever returns an app.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.authz.errors import AccessRoleBootstrapConfigurationError

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

_LOCAL_TEST_BYPASS_VAR = "PS_SERVICE_LOCAL_TEST_BYPASS"


def _format_missing_bootstrap_owner_config_message(missing: list[str]) -> str:
    """Build AC-BI-001's exact message shape: name the missing var(s) and the bypass alternative.

    Mirrors `ps_service.auth.startup._format_missing_auth_config_message`'s
    exact shape.
    """
    joined = " and ".join(missing)
    verb = "is" if len(missing) == 1 else "are"
    return (
        f"{joined} {verb} unset; set both PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT and "
        f"PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER, or set {_LOCAL_TEST_BYPASS_VAR}=true for "
        "local-only evaluation."
    )


def require_bootstrap_owner_configured(config: ServiceConfig) -> None:
    """Fail closed unless the RBAC bootstrap-owner identity is configured (AC-BI-001/AC-BI-002).

    - Local-test bypass (issue #67) active: returns immediately, no check.
      Every MCP call site that can reach `bootstrap_first_owner` already
      refuses the bypass principal outright before calling into
      `authz.service` -- bootstrap is structurally unreachable under bypass
      both before and after this fix, so requiring this config under bypass
      would be dead-weight friction inconsistent with bypass's own purpose
      (frictionless local/dev). Mirrors `resolve_auth_context`'s own
      bypass-first check exactly.
    - Bypass inactive and either `authz_bootstrap_owner_subject` or
      `authz_bootstrap_owner_issuer` is `None`: raises
      `AccessRoleBootstrapConfigurationError` naming exactly which var(s)
      are unset and the bypass alternative (AC-BI-001).
    - Bypass inactive and both are set: returns `None` (AC-BI-002).
    """
    if config.is_local_test_bypass_active:
        return

    if config.authz_bootstrap_owner_subject is None or config.authz_bootstrap_owner_issuer is None:
        missing = [
            name
            for name, value in (
                ("PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT", config.authz_bootstrap_owner_subject),
                ("PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER", config.authz_bootstrap_owner_issuer),
            )
            if value is None
        ]
        raise AccessRoleBootstrapConfigurationError(
            _format_missing_bootstrap_owner_config_message(missing)
        )
