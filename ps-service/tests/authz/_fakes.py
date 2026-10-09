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
from typing import TYPE_CHECKING

from ps_service.audit.errors import AuditPostgresUnavailableError
from ps_service.audit.models import AuditQueryPage
from ps_service.authz.models import AccessRole, AccessRoleAssignmentRow, AccessRoleGrantEvent
from ps_service.persistence import StatePostgresConnectionError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ps_service.audit.models import AuditQueryFilters

_BOOTSTRAP_SENTINEL = "system:bootstrap"  # mirrors `store.py`'s own sentinel


@dataclass(frozen=True, slots=True)
class RejectedAuditRecord:
    """One `record_grant_rejected`/`record_revoke_rejected` call `FakeAccessRoleStore` observed.

    Lets `tests/authz/test_service.py` assert AC-BI-012's "attempting actor,
    target, and reason code" directly, without a real Postgres/`audit_events`
    row (issue #147, Slice 3).
    """

    action: str
    """`"grant"` or `"revoke"` -- which of the two store methods was called."""

    actor: tuple[str, str]
    target: tuple[str, str]
    access_role: AccessRole
    reason_code: str


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
    rejected_records: list[RejectedAuditRecord] = field(default_factory=list)
    """Every `record_grant_rejected`/`record_revoke_rejected` call observed so far (issue #147)."""
    expected_owner: tuple[str, str] | None = None
    """The operator-configured expected first-owner identity (issue #144, D-6).

    Mirrors `PsycopgAccessRoleStore`'s own `(config.authz_bootstrap_owner_subject,
    config.authz_bootstrap_owner_issuer)` comparison (D-3). Defaults to `None`,
    which matches no principal -- fail-closed by default, consistent with the
    real store's own default-deny posture.
    """

    def bootstrap_first_owner(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Advisory-locked (via `self._lock`) check-empty-then-insert (PLAN.md §0.10)."""
        with self._lock:
            if self._rows:
                return frozenset({AccessRole.AUTHENTICATED_USER})
            if principal != self.expected_owner:
                # AC-BI-004/AC-BI-005: the store is empty, but this principal
                # is not the configured expected owner -- grant nothing,
                # leave the table empty, and record a distinct rejection
                # audit event naming the rejected principal (mirrors D-5's
                # real-store `access_role_grant_events` row shape, since
                # Slice 4's real-SQL insert doesn't exist yet).
                subject, issuer = principal
                self._events.append(
                    AccessRoleGrantEvent(
                        id=str(uuid.uuid4()),
                        event_type="bootstrap_rejected",
                        actor_subject=_BOOTSTRAP_SENTINEL,
                        actor_issuer=_BOOTSTRAP_SENTINEL,
                        target_subject=subject,
                        target_issuer=issuer,
                        access_role=AccessRole.SYSTEM_OWNER,
                        occurred_at=datetime.now(UTC),
                    )
                )
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

    def record_grant_rejected(
        self,
        *,
        actor: tuple[str, str],
        target: tuple[str, str],
        access_role: AccessRole,
        reason_code: str,
    ) -> None:
        """Append one `RejectedAuditRecord` -- an observable spy call, no real Postgres write."""
        with self._lock:
            self.rejected_records.append(
                RejectedAuditRecord(
                    action="grant",
                    actor=actor,
                    target=target,
                    access_role=access_role,
                    reason_code=reason_code,
                )
            )

    def record_revoke_rejected(
        self,
        *,
        actor: tuple[str, str],
        target: tuple[str, str],
        access_role: AccessRole,
        reason_code: str,
    ) -> None:
        """Append one `RejectedAuditRecord` -- an observable spy call, no real Postgres write."""
        with self._lock:
            self.rejected_records.append(
                RejectedAuditRecord(
                    action="revoke",
                    actor=actor,
                    target=target,
                    access_role=access_role,
                    reason_code=reason_code,
                )
            )


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
        raise StatePostgresConnectionError("simulated PS state Postgres outage")

    def count_active_system_owners(self) -> int:
        """Always raise, simulating an outage discovered only after the RBAC gate passed."""
        raise StatePostgresConnectionError("simulated PS state Postgres outage")


@dataclass
class RaisingAuditOnRejectAccessRoleStore(FakeAccessRoleStore):
    """A `FakeAccessRoleStore` whose denial-audit recording always raises (issue #147, Slice 3).

    Isolates the "the denial-audit write itself fails" branch of
    `ps_service.authz.service._record_rejected_or_raise_unavailable`
    hermetically: `grant_role`/`revoke_role` must convert
    `AuditPostgresUnavailableError` into `AuthorizationStoreUnavailableError`
    and never let the original `AccessDeniedError`/etc. through unaudited
    (AC-BI-011's fail-closed contract applied to the denial-recording path).
    """

    def record_grant_rejected(
        self,
        *,
        actor: tuple[str, str],
        target: tuple[str, str],
        access_role: AccessRole,
        reason_code: str,
    ) -> None:
        """Always raise, simulating the denial-audit write itself failing."""
        del actor, target, access_role, reason_code
        raise AuditPostgresUnavailableError("simulated audit store outage")

    def record_revoke_rejected(
        self,
        *,
        actor: tuple[str, str],
        target: tuple[str, str],
        access_role: AccessRole,
        reason_code: str,
    ) -> None:
        """Always raise, simulating the denial-audit write itself failing."""
        del actor, target, access_role, reason_code
        raise AuditPostgresUnavailableError("simulated audit store outage")


@dataclass
class RaisingAccessRoleStore:
    """An `AccessRoleStore`-shaped fake whose every method raises a connection error.

    Simulates an unreachable PS state Postgres instance (AC-BI-011) -- narrower
    than the full `Protocol` (only the methods Slice 1's own call sites
    reach need to raise), mirroring `test_near_miss_tools.py`'s own
    deliberately-narrower-than-the-Protocol `_RaisingStore` fake.
    """

    def bootstrap_first_owner(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Always raise, simulating an unreachable store."""
        del principal
        raise StatePostgresConnectionError("simulated PS state Postgres outage")

    def active_roles_for(self, principal: tuple[str, str]) -> frozenset[AccessRole]:
        """Always raise, simulating an unreachable store."""
        del principal
        raise StatePostgresConnectionError("simulated PS state Postgres outage")

    def grant(
        self, *, actor: tuple[str, str], target: tuple[str, str], access_role: AccessRole
    ) -> None:
        """Not exercised by this fake's own tests -- present only for `Protocol` conformance."""
        del actor, target, access_role
        raise StatePostgresConnectionError("simulated PS state Postgres outage")

    def revoke(
        self, *, actor: tuple[str, str], target: tuple[str, str], access_role: AccessRole
    ) -> None:
        """Not exercised by this fake's own tests -- present only for `Protocol` conformance."""
        del actor, target, access_role
        raise StatePostgresConnectionError("simulated PS state Postgres outage")

    def list_all_assignments(self) -> tuple[AccessRoleAssignmentRow, ...]:
        """Always raise, simulating an unreachable store."""
        raise StatePostgresConnectionError("simulated PS state Postgres outage")

    def record_grant_rejected(
        self,
        *,
        actor: tuple[str, str],
        target: tuple[str, str],
        access_role: AccessRole,
        reason_code: str,
    ) -> None:
        """Not exercised by this fake's own tests -- present only for `Protocol` conformance."""
        del actor, target, access_role, reason_code
        raise StatePostgresConnectionError("simulated PS state Postgres outage")

    def record_revoke_rejected(
        self,
        *,
        actor: tuple[str, str],
        target: tuple[str, str],
        access_role: AccessRole,
        reason_code: str,
    ) -> None:
        """Not exercised by this fake's own tests -- present only for `Protocol` conformance."""
        del actor, target, access_role, reason_code
        raise StatePostgresConnectionError("simulated PS state Postgres outage")

    def count_active_system_owners(self) -> int:
        """Always raise, simulating an unreachable store."""
        raise StatePostgresConnectionError("simulated PS state Postgres outage")


@dataclass
class FakeAuditStore:
    """In-memory `AuditStore` stub for `ps_service.authz.service.list_audit_events` tests
    (issue #147, Slice 4).

    `record`/`record_standalone` are present only for `Protocol` conformance
    (mirrors `RaisingAccessRoleStore`'s own "narrower than the full
    `Protocol`, only what this fake's own call sites reach" convention) --
    `list_audit_events`'s own tests never call either. `query` is a spy:
    every call is appended to `query_calls` (the exact `filters`/`cursor`/
    `page_size` it was called with), so AC-BI-002/AC-BI-008's "query was
    never called" assertions can check `query_calls == []`. Returns
    `query_result` (an empty page by default) unless `raise_on_query` is
    set, in which case it raises that instead -- simulates a Postgres outage
    discovered only once `query` itself runs (AC-BI-011).
    """

    query_result: AuditQueryPage = field(
        default_factory=lambda: AuditQueryPage(events=(), next_cursor=None)
    )
    raise_on_query: Exception | None = None
    query_calls: list[tuple[AuditQueryFilters, str | None, int]] = field(default_factory=list)

    def record(
        self,
        cur: object,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        details: Mapping[str, object],
    ) -> str:
        """Not exercised here -- present only for `Protocol` conformance."""
        del cur, actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details
        raise NotImplementedError

    def record_standalone(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        details: Mapping[str, object],
    ) -> None:
        """Not exercised here -- present only for `Protocol` conformance."""
        del actor_subject, actor_issuer, action, resource_type, resource_id, outcome, details
        raise NotImplementedError

    def query(
        self, *, filters: AuditQueryFilters, cursor: str | None, page_size: int
    ) -> AuditQueryPage:
        """Record the call, then return `query_result` or raise `raise_on_query`."""
        self.query_calls.append((filters, cursor, page_size))
        if self.raise_on_query is not None:
            raise self.raise_on_query
        return self.query_result
