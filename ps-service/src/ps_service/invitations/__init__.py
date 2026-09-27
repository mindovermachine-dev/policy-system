"""Invitations: PS Service's own Authentik invitation-stage integration (issue #140).

Owns the one responsibility of creating Authentik invitations on behalf of
the `invite_user` MCP tool, using PS Service's own configured service
credential (`PS_AUTHENTIK_API_TOKEN`/`PS_AUTHENTIK_BASE_URL`) -- never a
caller-supplied token. Named for what this component does (create
invitations), not the vendor it calls -- see
`docs/architecture/ps-solution-architecture.md`'s `ps.service.invitations`
Component-table row.

Re-exports `create_invitation`/`InvitationResult`/`AuthentikTransport`
(`ps_service.invitations.client`), `require_authentik_credential_configured`
(`ps_service.invitations.startup`), and this component's domain-specific
errors (`ps_service.invitations.errors`), matching every other component
package's own front-door re-export convention (e.g.
`ps_service.curated_source`, `ps_service.query_engine`).
"""

from __future__ import annotations

from ps_service.invitations.client import AuthentikTransport, InvitationResult, create_invitation
from ps_service.invitations.errors import (
    AuthentikCredentialConfigurationError,
    AuthentikInvitationError,
)
from ps_service.invitations.startup import require_authentik_credential_configured

__all__ = [
    "AuthentikCredentialConfigurationError",
    "AuthentikInvitationError",
    "AuthentikTransport",
    "InvitationResult",
    "create_invitation",
    "require_authentik_credential_configured",
]
