"""Operator command that revokes, or counts, one principal's signing passkeys.

Run in the PS Service pod as
`python -m ps_service.passkey_signing.revoke --subject <subject> --issuer <issuer> [--count-only]`;
the owner passkey reset of the deploy scripts runs it through `kubectl exec`. Without
`--count-only` it voids every pending approval and deletes every signing credential of the
`(subject, issuer)` pair and writes the `passkey.revoke` audit row
(`ps_service.passkey_signing.revocation`); with it, it only reads.

Output contract: exactly one compact JSON line on stdout, and nothing else there:

* revoked: `{"status":"revoked","credentials_deleted":N,"voided_approval_ids":[...]}`, exit 0;
* counted: `{"status":"counted","credentials":N,"pending_approvals":M}`, exit 0;
* failed: `{"status":"failed","reason_code":R}`, exit 1, where R is `configuration_invalid` (the
  configuration is malformed, or the signing or state database is not configured),
  `signing_store_unavailable` (the signing database failed; nothing was changed, or a COMMIT
  failed after the audit row was written) or `audit_trail_unavailable` (the audit row could not
  be written; nothing was changed).

A blank or missing `--subject`/`--issuer` is a usage error (argparse, stderr, exit 2). The
output never carries the subject, the issuer, a host, SQL or a driver message; the log entry
carries counts and the failure's class name only.

Logging: run as a module, the command installs the service's process-wide emitter the way the
service does at startup, so its `passkey_revoke` entry is appended to `ps-service.jsonl` in
`PS_LOGGING_DIR` next to the service's own entries, and a failed entry is also mirrored to
stderr. Nothing but the JSON line is written to stdout. A log directory that cannot be set up is
`configuration_invalid`, as it stops the service from starting; with no emitter to mirror it, one
stderr line names the error's class (never its message, which carries the path).

Environment: the service's own (`PS_PASSKEYSIGNING_POSTGRES_*` for the credentials,
`PS_STATE_POSTGRES_*` for the audit row), as the pod already has it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.audit.store import PsycopgAuditStore
from ps_service.config import ServiceConfigurationError, load_config
from ps_service.logging.errors import LoggingConfigurationError, LoggingLifecycleError
from ps_service.logging.facade import configure, emit_log_entry
from ps_service.passkey_signing.errors import SigningRevocationPersistenceError
from ps_service.passkey_signing.revocation import (
    PsycopgSigningRevocationStore,
    SigningRevocationStore,
    read_actor_signing_state,
    revoke_actor_signing,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ps_service.audit.store import AuditStore
    from ps_service.logging.emitter import LogEmitter

_PROG = "python -m ps_service.passkey_signing.revoke"
_COMPONENT = "passkey_signing"
_LOG_ACTION = "passkey_revoke"


@dataclass(frozen=True, slots=True)
class RevokeCollaborators:
    """The stores the command works on, and where it logs.

    Attributes:
        revocation_store: Where the signing credentials live.
        audit_store: Where the `passkey.revoke` row is written.
        emitter: Log emitter; `None` uses the process default, and the entry is skipped when
            no default is installed.
    """

    revocation_store: SigningRevocationStore
    audit_store: AuditStore
    emitter: LogEmitter | None = None


class _ConfigurationIncompleteError(Exception):
    """The signing or state database is not configured in this process."""


def _non_blank(value: str) -> str:
    """Argparse type: the value stripped; blank is a usage error."""
    stripped = value.strip()
    if not stripped:
        message = "must not be blank"
        raise argparse.ArgumentTypeError(message)
    return stripped


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Revoke (delete) every signing passkey of one principal, void its pending approvals "
            "and record the passkey.revoke audit row, or with --count-only report how many it "
            "has. Prints one JSON line."
        ),
    )
    parser.add_argument("--subject", required=True, type=_non_blank, help="identity subject")
    parser.add_argument("--issuer", required=True, type=_non_blank, help="identity issuer")
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="only count the signing credentials and pending approvals; change nothing",
    )
    return parser


def _collaborators_from_environment(emitter: LogEmitter) -> RevokeCollaborators:
    config = load_config()
    if config.passkey_signing_postgres_host is None or config.state_postgres_host is None:
        message = "the signing or state database is not configured"
        raise _ConfigurationIncompleteError(message)
    return RevokeCollaborators(
        revocation_store=PsycopgSigningRevocationStore(config),
        audit_store=PsycopgAuditStore(config),
        emitter=emitter,
    )


def _environment_collaborators(mode: str) -> RevokeCollaborators | int:
    """Install the service's log emitter, then build the Postgres stores from the environment.

    Returns:
        The collaborators, or the exit code of the failure already reported.

    Side Effects:
        Replaces the process-wide default emitter (`ps-service.jsonl` in `PS_LOGGING_DIR`,
        failed entries mirrored to stderr) and may create the log directory. When no emitter
        can be installed, writes one line naming the error's class to stderr instead.
    """
    try:
        emitter = configure(mirror_failures_to_stderr=True)
    except LoggingConfigurationError as exc:
        # No emitter exists to mirror the failure, so name its class on stderr directly.
        sys.stderr.write(f"{_PROG}: logging could not be set up ({type(exc).__name__})\n")
        return _fail(None, mode=mode, reason_code="configuration_invalid", error=exc)
    try:
        return _collaborators_from_environment(emitter)
    except (ServiceConfigurationError, _ConfigurationIncompleteError) as exc:
        return _fail(emitter, mode=mode, reason_code="configuration_invalid", error=exc)


def _emit(emitter: LogEmitter | None, *, outcome: str, extra: Mapping[str, object]) -> None:
    """Log one `passkey_revoke` entry; a process with no emitter skips it."""
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(
            component=_COMPONENT,
            action=_LOG_ACTION,
            outcome=outcome,
            extra=extra,
            emitter=emitter,
        )


def _print_line(payload: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")


def _fail(emitter: LogEmitter | None, *, mode: str, reason_code: str, error: Exception) -> int:
    _emit(emitter, outcome="failed", extra={"mode": mode, "reason": type(error).__name__})
    _print_line({"status": "failed", "reason_code": reason_code})
    return 1


def main(
    argv: Sequence[str] | None = None, *, collaborators: RevokeCollaborators | None = None
) -> int:
    """Revoke or count one principal's signing passkeys; print one JSON line.

    Args:
        argv: Command-line arguments; `None` reads `sys.argv`.
        collaborators: The stores to use; `None` installs the service's log emitter and builds
            the Postgres stores from the process configuration.

    Returns:
        0 when the revocation or the count succeeded, 1 on any failure.

    Raises:
        SystemExit: with code 2 on a usage error (blank or missing `--subject`/`--issuer`),
            raised by argparse before anything is read or changed.

    Side Effects:
        Without `--count-only`, voids the principal's pending approvals, deletes its signing
        credentials and writes the `passkey.revoke` audit row. Writes one line to stdout and
        emits one log entry. Without `collaborators`, replaces the process-wide default log
        emitter.
    """
    args = _build_parser().parse_args(argv)
    subject: str = args.subject
    issuer: str = args.issuer
    count_only: bool = args.count_only
    mode = "count" if count_only else "revoke"
    resolved = collaborators if collaborators is not None else _environment_collaborators(mode)
    if isinstance(resolved, int):
        return resolved
    if count_only:
        return _count(resolved, subject=subject, issuer=issuer)
    return _revoke(resolved, subject=subject, issuer=issuer)


def _count(collaborators: RevokeCollaborators, *, subject: str, issuer: str) -> int:
    emitter = collaborators.emitter
    try:
        state = read_actor_signing_state(
            actor_subject=subject,
            actor_issuer=issuer,
            revocation_store=collaborators.revocation_store,
        )
    except SigningRevocationPersistenceError as exc:
        return _fail(emitter, mode="count", reason_code="signing_store_unavailable", error=exc)
    pending = len(state.pending_approval_ids)
    _emit(
        emitter,
        outcome="succeeded",
        extra={"mode": "count", "credentials": state.credentials, "pending_approvals": pending},
    )
    _print_line(
        {"status": "counted", "credentials": state.credentials, "pending_approvals": pending}
    )
    return 0


def _revoke(collaborators: RevokeCollaborators, *, subject: str, issuer: str) -> int:
    emitter = collaborators.emitter
    try:
        revoked = revoke_actor_signing(
            actor_subject=subject,
            actor_issuer=issuer,
            revocation_store=collaborators.revocation_store,
            audit_store=collaborators.audit_store,
            emitter=emitter,
        )
    except SigningRevocationPersistenceError as exc:
        return _fail(emitter, mode="revoke", reason_code="signing_store_unavailable", error=exc)
    except AuditTrailUnavailableError as exc:
        return _fail(emitter, mode="revoke", reason_code="audit_trail_unavailable", error=exc)
    _emit(
        emitter,
        outcome="succeeded",
        extra={
            "mode": "revoke",
            "credentials_deleted": revoked.credentials_deleted,
            "approvals_voided": len(revoked.voided_approval_ids),
        },
    )
    _print_line(
        {
            "status": "revoked",
            "credentials_deleted": revoked.credentials_deleted,
            "voided_approval_ids": list(revoked.voided_approval_ids),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
