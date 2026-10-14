"""Resolve who an audited operation is attributed to.

One shared decision for every emission path (MCP tools, REST routes, the passkey approval path):
the verified `(subject, issuer)` pair when there is one, the fixed local-test-bypass sentinel
when there is none and the bypass is active, and a hard failure otherwise (fail-closed). Making
the decision in one place keeps `audit_events.actor_*` (NOT NULL) honest: a row is never
attributed to a guessed identity.

Public names: `LOCAL_TEST_BYPASS_AUDIT_ACTOR` (the sentinel) and `resolve_audit_actor`.
"""

from __future__ import annotations

from ps_service.audit.errors import AuditActorUnresolvedError

LOCAL_TEST_BYPASS_AUDIT_ACTOR = "system:local-test-bypass"
"""Actor value (used for both subject and issuer) recorded when the local-test bypass is active.

The bypass carries no identity, `audit_events` actors are NOT NULL, and this value cannot collide
with an identity-provider-issued `sub` because of its `system:` prefix.
"""


def resolve_audit_actor(
    actor: tuple[str, str] | None, *, is_local_test_bypass_active: bool
) -> tuple[str, str]:
    """Return the `(subject, issuer)` pair an audited operation is recorded under.

    Business Rules:
        A verified caller identity always wins. The sentinel is used only when no identity is
        available and the local-test bypass is active; with neither, attribution is refused so
        an operation is never recorded under a guessed actor.

    Args:
        actor: The verified caller as `(subject, issuer)`, or `None` when the request carries
            no verified identity.
        is_local_test_bypass_active: Whether the service runs with the local-test authentication
            bypass. Keyword-only.

    Returns:
        `actor` unchanged when it is not `None`; otherwise
        `(LOCAL_TEST_BYPASS_AUDIT_ACTOR, LOCAL_TEST_BYPASS_AUDIT_ACTOR)` when the bypass is
        active.

    Raises:
        AuditActorUnresolvedError: `actor` is `None` and the local-test bypass is not active.
            The caller must reject the operation rather than record it.

    Side Effects:
        None; pure function.
    """
    if actor is not None:
        return actor
    if is_local_test_bypass_active:
        return (LOCAL_TEST_BYPASS_AUDIT_ACTOR, LOCAL_TEST_BYPASS_AUDIT_ACTOR)
    message = "no verified caller identity is available to attribute the audited operation to"
    raise AuditActorUnresolvedError(message)
