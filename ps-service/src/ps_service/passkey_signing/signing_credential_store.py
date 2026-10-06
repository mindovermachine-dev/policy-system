"""PostgreSQL persistence for `signing_credentials` (issue #131, PLAN.md §1.2).

`SigningCredentialStore` is the `Protocol` the companion-browser router
(`passkey_signing.router`) depends on -- `PsycopgSigningCredentialStore` is
the real implementation. Mirrors `ps_service.passkey_signing.store`'s exact
connection strategy: one short-lived `psycopg.connect(...)` per call, opened
via the same `connect_from_config`, closed via `with`, no pool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

import psycopg

from ps_service.passkey_signing.errors import SigningCredentialPersistenceError
from ps_service.passkey_signing.models import SigningCredentialRow
from ps_service.passkey_signing.store import connect_from_config

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from ps_service.config import ServiceConfig


class SigningCredentialStore(Protocol):
    """Persistence seam for `signing_credentials` rows.

    Constructor-injected wherever it is needed (no DI framework, L2's "plain
    constructor injection" rule), mirroring `PendingApprovalStore`'s own
    shape exactly.
    """

    def create_signing_credential(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        rp_id: str | None,
    ) -> SigningCredentialRow:
        """Insert a newly-enrolled credential; return the created row.

        `rp_id` is the relying-party id the enrollment ceremony actually ran
        under, so the credential can later be matched to the host presenting
        the approval link (issue #196).
        """
        ...

    def list_for_actor(
        self, *, actor_subject: str, actor_issuer: str, rp_id: str
    ) -> tuple[SigningCredentialRow, ...]:
        """Return this actor's credentials enrolled under `rp_id`.

        Scoped to one `rp_id` (issue #196): a credential enrolled elsewhere
        cannot produce an assertion here, so advertising it in
        `allowCredentials` only yields an unexplained browser-side failure.
        """
        ...

    def has_any_for_actor(self, *, actor_subject: str, actor_issuer: str, rp_id: str) -> bool:
        """Return whether at least one credential is enrolled for this actor under `rp_id`.

        Backs `POST /approvals/{id}/summary`'s `needs_enrollment` field
        (CHANGES.md F2) and AC-BI-003's "opening a pending approval guides
        them through one-time self-serve enrollment" branch -- which must
        trigger again on a host this actor has not enrolled on (issue #196).
        """
        ...

    def get_by_credential_id(self, credential_id: bytes) -> SigningCredentialRow | None:
        """Return the row whose `credential_id` matches, or `None` if none exists.

        Issue #131 Slice 3: backs `.../sign/verify`'s actor-identity binding
        check (AC-BI-005) -- the credential resolved by the assertion's own
        `rawId` must belong to the pending approval's own actor.
        """
        ...

    def update_sign_count(self, *, credential_id: bytes, sign_count: int) -> None:
        """Persist the authenticator's latest reported `sign_count`.

        Called after a successful `webauthn.verify_authentication_response`
        (issue #131 Slice 3, PLAN.md §3 step (b)).
        """
        ...


__all__ = [
    "PsycopgSigningCredentialStore",
    "SigningCredentialStore",
]

_INSERT_SIGNING_CREDENTIAL = """
INSERT INTO signing_credentials (
    actor_subject, actor_issuer, credential_id, public_key, sign_count, rp_id
) VALUES (
    %(actor_subject)s, %(actor_issuer)s, %(credential_id)s, %(public_key)s, %(sign_count)s,
    %(rp_id)s
)
RETURNING id, actor_subject, actor_issuer, credential_id, public_key, sign_count, created_at,
    rp_id
"""

_SELECT_COLUMNS = (
    "id, actor_subject, actor_issuer, credential_id, public_key, sign_count, created_at, rp_id"
)
# `rp_id = %(rp_id)s` excludes a NULL `rp_id` on its own (SQL three-valued
# logic), which is exactly the wanted behaviour for a row predating the column
# (AC-BI-015) -- no explicit IS NOT NULL needed.
_SELECT_BY_ACTOR = (
    f"SELECT {_SELECT_COLUMNS} FROM signing_credentials "  # noqa: S608 - fixed literal, no interpolated user input
    "WHERE actor_subject = %(actor_subject)s AND actor_issuer = %(actor_issuer)s "
    "AND rp_id = %(rp_id)s"
)
_SELECT_BY_CREDENTIAL_ID = (
    f"SELECT {_SELECT_COLUMNS} FROM signing_credentials "  # noqa: S608 - fixed literal, no interpolated user input
    "WHERE credential_id = %(credential_id)s"
)
_UPDATE_SIGN_COUNT = """
UPDATE signing_credentials SET sign_count = %(sign_count)s
WHERE credential_id = %(credential_id)s
"""


def _row_from_record(record: Sequence[object]) -> SigningCredentialRow:
    """Map one raw `psycopg` result row (fixed column order, see `_SELECT_COLUMNS`) to a row.

    `cast()` is unavoidable at exactly this one boundary, mirroring
    `store._row_from_record`'s own documented rationale (L2 cast() policy).
    """
    (
        row_id,
        actor_subject,
        actor_issuer,
        credential_id,
        public_key,
        sign_count,
        created_at,
        rp_id,
    ) = record
    return SigningCredentialRow(
        id=str(row_id),
        actor_subject=cast("str", actor_subject),
        actor_issuer=cast("str", actor_issuer),
        credential_id=cast("bytes", credential_id),
        public_key=cast("bytes", public_key),
        sign_count=cast("int", sign_count),
        created_at=cast("datetime", created_at),
        rp_id=cast("str | None", rp_id),
    )


class PsycopgSigningCredentialStore:
    """Real `SigningCredentialStore` backed by PostgreSQL via `psycopg[binary]` (PLAN.md §0.6).

    Every method opens, uses, and closes its own connection -- no pool, no
    cached connection held across calls, mirroring
    `PsycopgPendingApprovalStore` exactly.
    """

    def __init__(self, config: ServiceConfig) -> None:
        """Store `config`; no connection is opened until a method is called."""
        self._config = config

    def create_signing_credential(
        self,
        *,
        actor_subject: str,
        actor_issuer: str,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        rp_id: str | None,
    ) -> SigningCredentialRow:
        """Insert a newly-enrolled credential; return the created row."""
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(
                    _INSERT_SIGNING_CREDENTIAL,
                    {
                        "actor_subject": actor_subject,
                        "actor_issuer": actor_issuer,
                        "credential_id": credential_id,
                        "public_key": public_key,
                        "sign_count": sign_count,
                        "rp_id": rp_id,
                    },
                )
                record = cur.fetchone()
                conn.commit()
        except psycopg.Error as exc:
            raise SigningCredentialPersistenceError(
                f"failed to create a signing credential: {exc}"
            ) from exc
        if record is None:  # pragma: no cover - INSERT...RETURNING always yields exactly one row
            message = "INSERT...RETURNING for signing_credentials unexpectedly returned no row"
            raise SigningCredentialPersistenceError(message)
        return _row_from_record(record)

    def list_for_actor(
        self, *, actor_subject: str, actor_issuer: str, rp_id: str
    ) -> tuple[SigningCredentialRow, ...]:
        """Return this actor's credentials enrolled under `rp_id`."""
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(
                    _SELECT_BY_ACTOR,
                    {
                        "actor_subject": actor_subject,
                        "actor_issuer": actor_issuer,
                        "rp_id": rp_id,
                    },
                )
                records = cur.fetchall()
        except psycopg.Error as exc:
            raise SigningCredentialPersistenceError(
                f"failed to look up signing credentials for actor: {exc}"
            ) from exc
        return tuple(_row_from_record(record) for record in records)

    def has_any_for_actor(self, *, actor_subject: str, actor_issuer: str, rp_id: str) -> bool:
        """Return whether at least one credential is enrolled for this actor under `rp_id`."""
        return bool(
            self.list_for_actor(actor_subject=actor_subject, actor_issuer=actor_issuer, rp_id=rp_id)
        )

    def get_by_credential_id(self, credential_id: bytes) -> SigningCredentialRow | None:
        """Return the row whose `credential_id` matches, or `None` if none exists."""
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(_SELECT_BY_CREDENTIAL_ID, {"credential_id": credential_id})
                record = cur.fetchone()
        except psycopg.Error as exc:
            raise SigningCredentialPersistenceError(
                f"failed to look up a signing credential by credential_id: {exc}"
            ) from exc
        return _row_from_record(record) if record is not None else None

    def update_sign_count(self, *, credential_id: bytes, sign_count: int) -> None:
        """Persist the authenticator's latest reported `sign_count`."""
        try:
            with connect_from_config(self._config) as conn, conn.cursor() as cur:
                cur.execute(
                    _UPDATE_SIGN_COUNT,
                    {"credential_id": credential_id, "sign_count": sign_count},
                )
                conn.commit()
        except psycopg.Error as exc:
            raise SigningCredentialPersistenceError(
                f"failed to update sign_count for a signing credential: {exc}"
            ) from exc
