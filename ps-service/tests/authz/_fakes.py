"""Shared test doubles for the `ps_service.authz` test package.

`tests/authz/` is an importable package (it has an `__init__.py`), so
`tests/mcp_interface/test_access_role_tools.py` imports these from here
instead of redeclaring them, mirroring `tests/passkey_signing/_fakes.py`'s
own established convention.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ps_service.authz.errors import AccessRolePostgresConnectionError
from ps_service.authz.models import AccessRole, AccessRoleAssignmentRow, AccessRoleGrantEvent

_BOOTSTRAP_SENTINEL = "system:bootstrap"  # mirrors `store.py`'s own sentinel


@dataclass
class FakeAccessRoleStore:
    """In-memory `AccessRoleStore` (structural `Protocol` match, no real Postgres).

    Mirrors `PsycopgAccessRoleStore`'s own bootstrap/read behavior exactly
    (`ps_service.authz.store`), so a test exercising this fake proves the
    same contract the real store must uphold, without a live Postgres.
    `grant`/`revoke` are not implemented here either -- they land in Slice 2,
    matching `PsycopgAccessRoleStore`'s own current scope.
    """

    _rows: list[AccessRoleAssignmentRow] = field(default_factory=list)
    _events: list[AccessRoleGrantEvent] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def bootstrap_first_owner(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Advisory-locked (via `self._lock`) check-empty-then-insert (PLAN.md §0.10)."""
        with self._lock:
            if self._rows:
                return frozenset({AccessRole.AUTHENTICATED_USER})
            subject, issuer = principal
            now = datetime.now(UTC)
            for access_role in (AccessRole.AUTHENTICATED_USER, AccessRole.SYSTEM_OWNER):
                self._rows.append(
                    AccessRoleAssignmentRow(
                        principal_subject=subject,
                        principal_issuer=issuer,
                        access_role=access_role,
                        granted_at=now,
                        granted_by_subject=_BOOTSTRAP_SENTINEL,
                        granted_by_issuer=_BOOTSTRAP_SENTINEL,
                    )
                )
            self._events.append(
                AccessRoleGrantEvent(
                    id=str(uuid.uuid4()),
                    event_type="bootstrap",
                    actor_subject=_BOOTSTRAP_SENTINEL,
                    actor_issuer=_BOOTSTRAP_SENTINEL,
                    target_subject=subject,
                    target_issuer=issuer,
                    access_role=AccessRole.SYSTEM_OWNER,
                    occurred_at=now,
                )
            )
            return frozenset({AccessRole.AUTHENTICATED_USER, AccessRole.SYSTEM_OWNER})

    def active_roles_for(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Current active `AccessRole` set for `principal` (no `AUTHENTICATED_USER` implied)."""
        subject, issuer = principal
        return frozenset(
            row.access_role
            for row in self._rows
            if row.principal_subject == subject and row.principal_issuer == issuer
        )

    def grant(
        self, *, actor: tuple[str, str], target: tuple[str, str], access_role: AccessRole
    ) -> None:
        """Idempotent insert + one `'grant'` audit event (PLAN.md §4 Slice 2).

        Mirrors `PsycopgAccessRoleStore.grant`'s own plain, unlocked path --
        used for every grantable role, `SYSTEM_OWNER` included.
        """
        with self._lock:
            target_subject, target_issuer = target
            actor_subject, actor_issuer = actor
            already_present = any(
                row.principal_subject == target_subject
                and row.principal_issuer == target_issuer
                and row.access_role is access_role
                for row in self._rows
            )
            if not already_present:
                self._rows.append(
                    AccessRoleAssignmentRow(
                        principal_subject=target_subject,
                        principal_issuer=target_issuer,
                        access_role=access_role,
                        granted_at=datetime.now(UTC),
                        granted_by_subject=actor_subject,
                        granted_by_issuer=actor_issuer,
                    )
                )
            self._events.append(
                AccessRoleGrantEvent(
                    id=str(uuid.uuid4()),
                    event_type="grant",
                    actor_subject=actor_subject,
                    actor_issuer=actor_issuer,
                    target_subject=target_subject,
                    target_issuer=target_issuer,
                    access_role=access_role,
                    occurred_at=datetime.now(UTC),
                )
            )

    def revoke(
        self, *, actor: tuple[str, str], target: tuple[str, str], access_role: AccessRole
    ) -> None:
        """`DELETE` the matching row (no-op if absent) + one `'revoke'` audit event.

        This fake's own scope mirrors `PsycopgAccessRoleStore.revoke`'s
        current scope (PLAN.md §4 Slice 2): the plain, unlocked path, used
        for `SYSTEM_ADMIN`/`POLICY_MANAGER` this slice; `SYSTEM_OWNER`'s
        advisory-locked floor-check branch lands in Slice 3.
        """
        with self._lock:
            target_subject, target_issuer = target
            actor_subject, actor_issuer = actor
            self._rows = [
                row
                for row in self._rows
                if not (
                    row.principal_subject == target_subject
                    and row.principal_issuer == target_issuer
                    and row.access_role is access_role
                )
            ]
            self._events.append(
                AccessRoleGrantEvent(
                    id=str(uuid.uuid4()),
                    event_type="revoke",
                    actor_subject=actor_subject,
                    actor_issuer=actor_issuer,
                    target_subject=target_subject,
                    target_issuer=target_issuer,
                    access_role=access_role,
                    occurred_at=datetime.now(UTC),
                )
            )

    def count_active_system_owners(self) -> int:
        """Return how many principals currently hold an active `SYSTEM_OWNER` assignment."""
        return sum(1 for row in self._rows if row.access_role is AccessRole.SYSTEM_OWNER)

    def list_all_assignments(self) -> tuple[AccessRoleAssignmentRow, ...]:
        """Return every row currently held by this fake, in insertion order."""
        return tuple(self._rows)


@dataclass
class RaisingAfterGateAccessRoleStore(FakeAccessRoleStore):
    """A `FakeAccessRoleStore` whose roster-read methods raise, once the RBAC gate has passed.

    Isolates `ps_service.authz.service.list_assignments`'s *second*
    fail-closed try/except (wrapping `list_all_assignments`/
    `count_active_system_owners`, reached only after `require_role`'s own
    gate already succeeded) from `RaisingAccessRoleStore`'s own coverage of
    the *first* one (`resolve_active_roles`, reached before any gate check).
    """

    def list_all_assignments(self) -> tuple[AccessRoleAssignmentRow, ...]:
        """Always raise, simulating an outage discovered only after the RBAC gate passed."""
        raise AccessRolePostgresConnectionError("simulated Authz Postgres outage")

    def count_active_system_owners(self) -> int:
        """Always raise, simulating an outage discovered only after the RBAC gate passed."""
        raise AccessRolePostgresConnectionError("simulated Authz Postgres outage")


@dataclass
class RaisingAccessRoleStore:
    """An `AccessRoleStore`-shaped fake whose every method raises a connection error.

    Simulates an unreachable Authz Postgres instance (AC-BI-011) -- narrower
    than the full `Protocol` (only the methods Slice 1's own call sites
    reach need to raise), mirroring `test_near_miss_tools.py`'s own
    deliberately-narrower-than-the-Protocol `_RaisingStore` fake.
    """

    def bootstrap_first_owner(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Always raise, simulating an unreachable store."""
        del principal
        raise AccessRolePostgresConnectionError("simulated Authz Postgres outage")

    def active_roles_for(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Always raise, simulating an unreachable store."""
        del principal
        raise AccessRolePostgresConnectionError("simulated Authz Postgres outage")

    def grant(
        self, *, actor: tuple[str, str], target: tuple[str, str], access_role: AccessRole
    ) -> None:
        """Not exercised by this fake's own tests -- present only for `Protocol` conformance."""
        del actor, target, access_role
        raise AccessRolePostgresConnectionError("simulated Authz Postgres outage")

    def revoke(
        self, *, actor: tuple[str, str], target: tuple[str, str], access_role: AccessRole
    ) -> None:
        """Not exercised by this fake's own tests -- present only for `Protocol` conformance."""
        del actor, target, access_role
        raise AccessRolePostgresConnectionError("simulated Authz Postgres outage")

    def list_all_assignments(self) -> tuple[AccessRoleAssignmentRow, ...]:
        """Always raise, simulating an unreachable store."""
        raise AccessRolePostgresConnectionError("simulated Authz Postgres outage")

    def count_active_system_owners(self) -> int:
        """Always raise, simulating an unreachable store."""
        raise AccessRolePostgresConnectionError("simulated Authz Postgres outage")
