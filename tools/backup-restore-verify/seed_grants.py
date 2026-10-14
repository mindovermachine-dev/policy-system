#!/usr/bin/env python3
"""Seed access-role grants so a backup and restore round trip has authorization data to verify.

Run inside the ps-service pod (it reads the service's own configuration from the environment):

    kubectl exec -i deploy/policy-system-ps-service -- python - < seed_grants.py

Grants go through `PsycopgAccessRoleStore.grant`, so each assignment row and its audit event are
written by product code in one transaction. The MCP grant path needs an interactive identity
provider session, so it is not used here.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from ps_service.audit.store import PsycopgAuditStore
from ps_service.authz.models import AccessRole
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.config import load_config

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ps_service.authz.store import AccessRoleStore

SEED_ACTOR_SUBJECT = "system:backup-restore-verify"
SEED_ROLES = (
    AccessRole.SYSTEM_ADMIN,
    AccessRole.POLICY_MANAGER,
    AccessRole.COMPLIANCE_OFFICER,
)


def seed_grants(
    store: AccessRoleStore, *, issuer: str, prefix: str, roles: Sequence[AccessRole] = SEED_ROLES
) -> list[tuple[str, AccessRole]]:
    """Grant one access role to each of a set of synthetic principals.

    Args:
        store: Access-role store that records each grant and its audit event.
        issuer: Identity issuer recorded for the actor and every granted principal.
        prefix: Prefix of each synthetic subject; the lower-cased role value is appended.
        roles: Roles to grant, one principal per role; defaults to `SEED_ROLES`.

    Returns:
        `(subject, role)` pairs in the order granted.

    Raises:
        Exception: Whatever the store raises when a grant is rejected; earlier grants stay.
    """
    actor = (SEED_ACTOR_SUBJECT, issuer)
    granted: list[tuple[str, AccessRole]] = []
    for role in roles:
        subject = f"{prefix}-{role.value.lower()}"
        store.grant(actor=actor, target=(subject, issuer), access_role=role)
        granted.append((subject, role))
    return granted


def main(argv: Sequence[str] | None = None) -> int:
    """Seed the default role grants using the service's own configuration.

    Args:
        argv: Command-line arguments without the program name; `None` reads `sys.argv`.
            `--subject-prefix` sets the synthetic subject prefix.

    Returns:
        0 once every grant is recorded.

    Raises:
        Exception: Any configuration or store failure propagates unchanged.

    Side Effects:
        Inserts assignment rows and audit events in PS Postgres and logs one event line per
        grant to stderr.
    """
    parser = argparse.ArgumentParser(description="Seed access-role grants through the store")
    parser.add_argument("--subject-prefix", default="backup-restore-verify")
    args = parser.parse_args(argv)
    config = load_config()
    issuer = config.authz_bootstrap_owner_issuer or "https://backup-restore-verify.invalid"
    store = PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))
    for subject, role in seed_grants(store, issuer=issuer, prefix=args.subject_prefix):
        sys.stderr.write(f"event=seed_grant subject={subject} access_role={role.value}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
