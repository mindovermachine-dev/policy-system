"""PostgreSQL persistence for `signing_credentials`, the enrolled passkeys of approvers.

`SigningCredentialStore` is the `Protocol` that the companion-browser router
(`passkey_signing.router`) depends on; `PsycopgSigningCredentialStore` is the real implementation.
Connection strategy matches `ps_service.passkey_signing.store`: one short-lived `psycopg`
connection per call, opened by that module's `connect_from_config` and closed by a `with` block,
no pool. Driver failures surface as `SigningCredentialPersistenceError`.
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
    """Persistence seam for `signing_credentials` rows: enroll, list, look up and update a passkey.

    Constructor-injected into the ceremony router, which depends on this `Protocol` and never on
    `PsycopgSigningCredentialStore` directly, so a test can substitute an in-memory fake. Every
    method raises `SigningCredentialPersistenceError` when the Postgres call fails.
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
        """Insert a newly enrolled credential and return the created row.

        Args:
            actor_subject: Identity-provider subject of the approver who enrolled it.
            actor_issuer: Identity-provider issuer of `actor_subject`.
            credential_id: The authenticator's credential id (raw bytes).
            public_key: The credential's public key, as returned by registration verification.
            sign_count: The authenticator's initial signature counter.
            rp_id: The WebAuthn relying-party id the enrollment ceremony ran under, so the
                credential can later be matched to the host presenting the approval link; `None`
                records no binding.

        Returns:
            The stored `SigningCredentialRow` with its generated id and creation time.

        Raises:
            SigningCredentialPersistenceError: the insert failed, the connection could not be
                opened, or no row was returned. Nothing is stored.

        Side Effects:
            Opens a connection, inserts and commits one `signing_credentials` row.
        """
        ...

    def list_for_actor(
        self, *, actor_subject: str, actor_issuer: str, rp_id: str
    ) -> tuple[SigningCredentialRow, ...]:
        """Return this actor's credentials enrolled under `rp_id`.

        Scoped to one `rp_id`: a credential enrolled under another relying party cannot produce an
        assertion here, so advertising it in `allowCredentials` would only cause an unexplained
        browser-side failure. Rows with no recorded `rp_id` never match.

        Args:
            actor_subject: Identity-provider subject of the approver.
            actor_issuer: Identity-provider issuer of `actor_subject`.
            rp_id: The relying-party id of the host presenting the approval link.

        Returns:
            The matching `SigningCredentialRow`s; empty when the actor has none under `rp_id`.

        Raises:
            SigningCredentialPersistenceError: the lookup failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection and reads rows; writes nothing.
        """
        ...

    def has_any_for_actor(self, *, actor_subject: str, actor_issuer: str, rp_id: str) -> bool:
        """Return whether at least one credential is enrolled for this actor under `rp_id`.

        Backs the approval summary's `needs_enrollment` flag, so an approver who opens the link on
        a host they have not enrolled on is guided through enrollment again.

        Args:
            actor_subject: Identity-provider subject of the approver.
            actor_issuer: Identity-provider issuer of `actor_subject`.
            rp_id: The relying-party id of the host presenting the approval link.

        Returns:
            `True` if the actor has at least one credential under `rp_id`, `False` otherwise.

        Raises:
            SigningCredentialPersistenceError: the lookup failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection and reads rows; writes nothing.
        """
        ...

    def get_by_credential_id(self, credential_id: bytes) -> SigningCredentialRow | None:
        """Return the credential row with this credential id.

        Backs the signing verification's identity binding: the credential named by the assertion's
        `rawId` must belong to the pending approval's own actor.

        Args:
            credential_id: The authenticator's credential id (raw bytes).

        Returns:
            The matching `SigningCredentialRow`, or `None` if no credential has that id.

        Raises:
            SigningCredentialPersistenceError: the lookup failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection and reads one row; writes nothing.
        """
        ...

    def update_sign_count(self, *, credential_id: bytes, sign_count: int) -> None:
        """Persist the authenticator's latest reported signature counter.

        Call after a successful WebAuthn authentication verification, so the next assertion is
        checked against the new counter (cloned-authenticator detection).

        Args:
            credential_id: The authenticator's credential id (raw bytes).
            sign_count: The counter value reported by the authenticator's latest assertion.

        Returns:
            None.

        Raises:
            SigningCredentialPersistenceError: the update failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection, updates and commits the `sign_count` column. An unknown
            credential id updates nothing and raises nothing.
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
# `rp_id = %(rp_id)s` excludes a NULL `rp_id` on its own (SQL three-valued logic), which is the
# wanted behaviour for a credential enrolled before relying-party ids were recorded.
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
    """Map one raw `psycopg` result row (column order `_SELECT_COLUMNS`) to a credential row.

    `cast()` is needed at this single boundary because a tuple-row result has no per-column
    static type.
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
    """`SigningCredentialStore` backed by PostgreSQL via `psycopg`.

    Every method opens, uses and closes its own connection (no pool, no connection held across
    calls) and raises `SigningCredentialPersistenceError` on any driver failure.
    """

    def __init__(self, config: ServiceConfig) -> None:
        """Keep `config` for later connections; opens none.

        Args:
            config: Service configuration holding the `passkey_signing_postgres_*` settings.
        """
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
        """Insert a newly enrolled credential and return the created row.

        Args:
            actor_subject: Identity-provider subject of the approver who enrolled it.
            actor_issuer: Identity-provider issuer of `actor_subject`.
            credential_id: The authenticator's credential id (raw bytes).
            public_key: The credential's public key, as returned by registration verification.
            sign_count: The authenticator's initial signature counter.
            rp_id: The WebAuthn relying-party id the enrollment ran under; `None` records no
                binding.

        Returns:
            The stored `SigningCredentialRow` with its generated id and creation time.

        Raises:
            SigningCredentialPersistenceError: the insert failed, the connection could not be
                opened, or no row was returned. Nothing is stored.

        Side Effects:
            Opens a connection, inserts and commits one `signing_credentials` row.
        """
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
        """Return this actor's credentials enrolled under `rp_id`.

        Rows with no recorded `rp_id` never match.

        Args:
            actor_subject: Identity-provider subject of the approver.
            actor_issuer: Identity-provider issuer of `actor_subject`.
            rp_id: The relying-party id of the host presenting the approval link.

        Returns:
            The matching `SigningCredentialRow`s; empty when the actor has none under `rp_id`.

        Raises:
            SigningCredentialPersistenceError: the lookup failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection and reads rows; writes nothing.
        """
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
        """Return whether at least one credential is enrolled for this actor under `rp_id`.

        Args:
            actor_subject: Identity-provider subject of the approver.
            actor_issuer: Identity-provider issuer of `actor_subject`.
            rp_id: The relying-party id of the host presenting the approval link.

        Returns:
            `True` if the actor has at least one credential under `rp_id`, `False` otherwise.

        Raises:
            SigningCredentialPersistenceError: the lookup failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection and reads rows; writes nothing.
        """
        return bool(
            self.list_for_actor(actor_subject=actor_subject, actor_issuer=actor_issuer, rp_id=rp_id)
        )

    def get_by_credential_id(self, credential_id: bytes) -> SigningCredentialRow | None:
        """Return the credential row with this credential id.

        Args:
            credential_id: The authenticator's credential id (raw bytes).

        Returns:
            The matching `SigningCredentialRow`, or `None` if no credential has that id.

        Raises:
            SigningCredentialPersistenceError: the lookup failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection and reads one row; writes nothing.
        """
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
        """Persist the authenticator's latest reported signature counter.

        Args:
            credential_id: The authenticator's credential id (raw bytes).
            sign_count: The counter value reported by the authenticator's latest assertion.

        Returns:
            None.

        Raises:
            SigningCredentialPersistenceError: the update failed or the connection could not be
                opened.

        Side Effects:
            Opens a connection, updates and commits the `sign_count` column. An unknown
            credential id updates nothing and raises nothing.
        """
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
