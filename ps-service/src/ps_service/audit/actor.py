"""Who an audited operation is attributed to (issue #195, AC-BI-001).

One shared decision for every emission path (MCP tools, REST routes, the passkey approval path):
the verified `(subject, issuer)` pair when there is one, the fixed local-test-bypass sentinel
when there is none and the bypass is active, and a hard failure otherwise (fail-closed).
"""

from __future__ import annotations

from ps_service.audit.errors import AuditActorUnresolvedError

LOCAL_TEST_BYPASS_AUDIT_ACTOR = "system:local-test-bypass"
"""Stands in for an identity the local-test bypass never has. `audit_events` actors are NOT NULL,
and this value can never collide with an IdP-issued `sub` (precedent: the access-role store's
`system:bootstrap`)."""


def resolve_audit_actor(
    actor: tuple[str, str] | None, *, is_local_test_bypass_active: bool
) -> tuple[str, str]:
    """Return the `(subject, issuer)` an audited operation is recorded under.

    Raises:
        AuditActorUnresolvedError: `actor` is missing and the local-test bypass is not active.
    """
    if actor is not None:
        return actor
    if is_local_test_bypass_active:
        return (LOCAL_TEST_BYPASS_AUDIT_ACTOR, LOCAL_TEST_BYPASS_AUDIT_ACTOR)
    message = "no verified caller identity is available to attribute the audited operation to"
    raise AuditActorUnresolvedError(message)
