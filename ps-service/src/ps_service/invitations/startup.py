"""Process-startup resolution of the Authentik service credential (issue #140).

`require_authentik_credential_configured` is called synchronously inside
`create_app(config)`'s own body (see `ps_service.main`), immediately after
the existing `require_bootstrap_owner_configured(config)` call -- a
config-completeness fact that cannot change for the life of the process
belongs in the synchronous constructor path, so it fails before `create_app`
ever returns an app.

Unlike `ps_service.authz.startup.require_bootstrap_owner_configured`, this
check is deliberately **unconditional**: it does not copy that function's
local-test-bypass skip. AC-BI-003's wording ("WHEN `PS_AUTHENTIK_API_TOKEN`
is missing or empty at startup THEN ps-service fails config validation")
carries no bypass carve-out, unlike AC-BI-002's explicit one for the
`invite_user` role check -- CHANGES.md #140 Row 2 resolves this literally,
also closing the worse failure mode a bypass-skip would leave open (a crash
mid-request the first time `invite_user` is actually called under bypass,
instead of a fail-fast at boot).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.invitations.errors import AuthentikCredentialConfigurationError

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig


def require_authentik_credential_configured(config: ServiceConfig) -> None:
    """Fail closed unless both Authentik service-credential fields are configured (AC-BI-003).

    Unconditional: does not check `config.is_local_test_bypass_active`. Raises
    `AuthentikCredentialConfigurationError` naming exactly which field(s) are
    unset when either `config.authentik_api_token` or `config.authentik_base_url`
    is `None`; returns `None` when both are set.
    """
    missing = [
        name
        for name, value in (
            ("authentik_api_token", config.authentik_api_token),
            ("authentik_base_url", config.authentik_base_url),
        )
        if value is None
    ]
    if missing:
        raise AuthentikCredentialConfigurationError(
            f"Missing required configuration: {', '.join(missing)}"
        )
