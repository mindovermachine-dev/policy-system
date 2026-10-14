"""Process-startup check of the Authentik service credential.

`require_authentik_credential_configured` is called synchronously inside `create_app(config)`'s
body (see `ps_service.main`), immediately after `require_bootstrap_owner_configured(config)`:
a config-completeness fact that cannot change for the life of the process belongs in the
synchronous constructor path, so it fails before `create_app` ever returns an app.

Unlike `ps_service.authz.startup.require_bootstrap_owner_configured`, this check is deliberately
unconditional: it does not skip under the local-test bypass. A bypass-skip would leave open the
worse failure mode of a crash mid-request the first time `invite_user` is called, instead of a
fail-fast at boot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.invitations.errors import AuthentikCredentialConfigurationError

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig


def require_authentik_credential_configured(config: ServiceConfig) -> None:
    """Fail closed unless both Authentik service-credential fields are configured.

    Unconditional: does not consult `config.is_local_test_bypass_active`.

    Args:
        config: The service configuration; reads `authentik_api_token` and `authentik_base_url`.

    Returns:
        `None` when both fields are set.

    Raises:
        AuthentikCredentialConfigurationError: Either field is `None`; the message names exactly
            which field(s) are unset.
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
