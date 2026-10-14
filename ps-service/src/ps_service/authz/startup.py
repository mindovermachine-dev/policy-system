"""Process-startup check of the access-role bootstrap-owner configuration.

`require_bootstrap_owner_configured` is called synchronously inside `create_app(config)`
(see `ps_service.main`), immediately after `resolve_auth_context(config)`, in the same role
as `ps_service.auth.startup`: a configuration fact that cannot change for the life of the
process is checked in the synchronous constructor path, so the process fails before
`create_app` returns an application.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.authz.errors import AccessRoleBootstrapConfigurationError

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

_LOCAL_TEST_BYPASS_VAR = "PS_SERVICE_LOCAL_TEST_BYPASS"


def _format_missing_bootstrap_owner_config_message(missing: list[str]) -> str:
    """Build the configuration-error message naming the unset variables and the bypass option."""
    joined = " and ".join(missing)
    verb = "is" if len(missing) == 1 else "are"
    return (
        f"{joined} {verb} unset; set both PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT and "
        f"PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER, or set {_LOCAL_TEST_BYPASS_VAR}=true for "
        "local-only evaluation."
    )


def require_bootstrap_owner_configured(config: ServiceConfig) -> None:
    """Fail closed unless the access-role bootstrap-owner identity is configured.

    The first principal to authenticate against an empty assignment store becomes
    `SystemOwner` only if it matches the configured bootstrap-owner identity, so a missing
    identity would leave the system with no way to obtain an owner.

    Under the local-test bypass the check is skipped: every MCP call site that can reach
    `bootstrap_first_owner` refuses the bypass principal before calling into
    `ps_service.authz.service`, so bootstrap is unreachable under the bypass and requiring
    the configuration there would only add friction to local evaluation.

    Args:
        config: The service configuration. `is_local_test_bypass_active`,
            `authz_bootstrap_owner_subject` and `authz_bootstrap_owner_issuer` are read;
            the two identity fields are `None` when unset.

    Returns:
        None. Returning normally means the bypass is active, or both identity fields are
        set.

    Raises:
        AccessRoleBootstrapConfigurationError: the bypass is inactive and the bootstrap
            owner subject, issuer or both are unset. The message names exactly the unset
            variables and the bypass alternative.
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
