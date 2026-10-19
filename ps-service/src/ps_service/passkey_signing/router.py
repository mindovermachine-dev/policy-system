"""Companion-browser signing ceremony: the `/approvals/{id}` routes and their router factory.

A privileged action (for example merging two Capabilities) is not executed when it is
requested. The requester receives a link to a pending approval; the approver opens it in a
companion browser, enrols a passkey if none exists for that host, and signs the action with a
WebAuthn assertion. Only a verified assertion causes the action to run.

Routes (all under `/approvals`):
    GET  /{id}                -- fixed HTML shell that drives the ceremony from the browser.
    POST /{id}/summary        -- verify the code; return the display summary and whether the
                                 approver must enrol a passkey first.
    POST /{id}/enroll/options -- WebAuthn registration options.
    POST /{id}/enroll/verify  -- verify a registration response; store the new credential.
    POST /{id}/sign/options   -- WebAuthn authentication options.
    POST /{id}/sign/verify    -- verify the assertion, consume the approval, run the action.

Authorization: the routes sit under the bearer-token exemption for `/approvals`
(`ps_service.auth.middleware`), so the only per-request credential is the opaque approval `code`.
The code travels only in the request body, and before that in the link's URL fragment, never in
the URL path or query; every POST route verifies it against the stored hash.

Rejection: every failure a caller can cause (unknown id, wrong code, expired or already-consumed
approval, unknown or wrong-actor credential, lost consumption race, malformed or invalid
WebAuthn payload) raises the identical `PendingApprovalInvalidOrExpiredError`, so the response
never reveals why. The real reason is logged server-side only.

Execution: a signed approval runs the executor registered for its `tool_name`
(`ps_service.passkey_signing.executors`); the component that owns the action audits its own write
(for example a Capability merge writes its `capability.merge` rows). The single-use approval is
consumed by `mark_signed` before the executor runs, so a failed action never leaves it reusable:
the stored outcome is the executor's sanitized error and the approver must request a new
approval. An approval whose `tool_name` has no registered executor is rejected before
`mark_signed` and stays pending until it expires.

Audit: only a successful enrolment writes a row, `passkey.enroll`, attributed to the approval's
bound actor with `proof="approval_code"` (the caller is identified by the code, not by
authentication). Rejected steps stay in the operator log, and signing writes no row of its own.

Exports:
    build_passkey_signing_router: builds the `APIRouter` that `ps_service.main.create_app`
        mounts alongside the REST API router.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Annotated

import webauthn
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from webauthn.helpers import base64url_to_bytes

from ps_service.api.dependencies import (
    get_service_config,
    provide_audit_store,
    provide_pending_approval_store,
)
from ps_service.api.errors import (
    PendingApprovalInvalidOrExpiredError,
    SigningHostNotAllowedError,
)
from ps_service.audit import (
    AuditContext,
    AuditStore,
    AuditTarget,
    record_follow_up_row,
)
from ps_service.config import (
    ServiceConfig,  # noqa: TC001 -- FastAPI resolves the endpoint annotation at runtime
)
from ps_service.logging import emit_log_entry
from ps_service.passkey_signing.audit_actions import (
    PASSKEY_ENROLL_ACTION,
    PENDING_APPROVAL_RESOURCE_TYPE,
)
from ps_service.passkey_signing.error_handlers import reject, reject_on_webauthn_failure
from ps_service.passkey_signing.executors import resolve_approval_executor
from ps_service.passkey_signing.service import (
    _compute_sign_challenge,  # pyright: ignore[reportPrivateUsage]
    _require_pending_and_unexpired,  # pyright: ignore[reportPrivateUsage]
)
from ps_service.passkey_signing.signing_credential_store import (
    PsycopgSigningCredentialStore,
    SigningCredentialStore,
)
from ps_service.passkey_signing.store import (
    PendingApprovalStore,  # noqa: TC001 -- FastAPI resolves the endpoint annotation at runtime
)
from ps_service.passkey_signing.webauthn_rp import (
    build_authentication_options,
    build_registration_options,
    request_host_is_allowed,
    rp_id_and_origin,
    verify_authentication,
    verify_registration,
)

if TYPE_CHECKING:
    from ps_service.passkey_signing.models import PendingApprovalRow, SigningCredentialRow

__all__ = ["build_passkey_signing_router"]


def provide_signing_credential_store(
    config: Annotated[ServiceConfig, Depends(get_service_config)],
) -> SigningCredentialStore:
    """Provide the signing-credential store that the ceremony routes read and write.

    A plain provider (not a generator), overridable through `app.dependency_overrides`.

    Args:
        config: The running service's configuration (injected from the application state);
            supplies the Postgres connection settings.

    Returns:
        A `PsycopgSigningCredentialStore` bound to `config`. It opens no connection until one
        of its methods is called, so building one per request is cheap.
    """
    return PsycopgSigningCredentialStore(config)


def require_allowed_signing_host(request: Request) -> None:
    """Refuse a POST whose `Host` is not one of the configured allowed origins.

    Placed first on every POST route, so a wrong `Host` is rejected before the approval id or
    code is read: it is no oracle for approval ids and no challenge is issued. With no
    configured origins every host passes.

    Args:
        request: The incoming HTTP request (injected).

    Raises:
        SigningHostNotAllowedError: the request's `Host` matches no configured origin (HTTP 421,
            fixed body naming nothing).

    Side Effects:
        On rejection, emits one failed-outcome log entry with reason `host_not_allowed`; the
        host value itself is not logged.
    """
    if request_host_is_allowed(request):
        return
    emit_log_entry(
        component="passkey_signing",
        action="host_check",
        outcome="failed",
        extra={"reason": "host_not_allowed"},
    )
    raise SigningHostNotAllowedError


class _ApprovalCodeRequest(BaseModel):
    """Request body of the POST routes that carry only the approval code.

    Attributes:
        code: The opaque capability code from the approval link; compared against the stored
            hash. Required.
    """

    code: str


class _EnrollVerifyRequest(BaseModel):
    """Request body of `POST /approvals/{id}/enroll/verify`.

    Attributes:
        code: The opaque capability code from the approval link. Required.
        credential: The JSON form of the browser's WebAuthn registration response
            (`navigator.credentials.create()`). Required; validated by the WebAuthn library.
    """

    code: str
    credential: dict[str, object]


class _SignVerifyRequest(BaseModel):
    """Request body of `POST /approvals/{id}/sign/verify`.

    Attributes:
        code: The opaque capability code from the approval link. Required.
        credential: The JSON form of the browser's WebAuthn assertion
            (`navigator.credentials.get()`), including `rawId`. Required; validated by the
            WebAuthn library.
    """

    code: str
    credential: dict[str, object]


def _verify_code(
    *,
    store: PendingApprovalStore,
    pending_approval_id: str,
    code: str,
    action: str,
) -> PendingApprovalRow:
    """Load a pending approval and check its code, status and expiry, rejecting generically.

    Every failure raises the identical `PendingApprovalInvalidOrExpiredError`, so the response
    never distinguishes an unknown id, a wrong code and an expired or consumed approval; each
    condition is logged server-side by `reject`.

    Args:
        store: The pending-approval store to read from.
        pending_approval_id: The approval id from the URL path (never the secret).
        code: The capability code from the request body; compared in constant time against
            the stored SHA-256 hash.
        action: The calling route's short name, used as the log `action` field.

    Returns:
        The verified row: status `pending` and not yet expired.

    Raises:
        PendingApprovalInvalidOrExpiredError: the id is unknown, the code does not match, or
            the row is already signed or has expired.
        PendingApprovalPersistenceError: reading the approval from Postgres failed.

    Side Effects:
        Emits one failed-outcome log entry (via `reject`) per rejection.
    """
    row = store.get_by_id(pending_approval_id)
    if row is None:
        reject(
            action=action,
            pending_approval_id=pending_approval_id,
            reason="unknown_pending_approval_id",
        )
    expected_hash = hashlib.sha256(code.encode()).digest()
    if not hmac.compare_digest(expected_hash, row.code_hash):
        reject(action=action, pending_approval_id=pending_approval_id, reason="code_mismatch")
    try:
        _require_pending_and_unexpired(row)
    except PendingApprovalInvalidOrExpiredError:
        # `_require_pending_and_unexpired` alone decides pending/unexpired; this branch
        # only labels the decision for the server-side log.
        reason = "already_signed" if row.status != "pending" else "expired"
        reject(action=action, pending_approval_id=pending_approval_id, reason=reason)
    return row


# --- GET /approvals/{id}: generic companion-browser shell ---------------------------

_SHELL_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Approve signing request</title>
</head>
<body>
<main id="app"><p>Loading approval details&hellip;</p></main>
<script>
(function () {
  "use strict";

  function base64urlToBuffer(value) {
    var padded = value.replace(/-/g, "+").replace(/_/g, "/");
    while (padded.length % 4) { padded += "="; }
    var binary = atob(padded);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) { bytes[i] = binary.charCodeAt(i); }
    return bytes.buffer;
  }

  function bufferToBase64url(buffer) {
    var bytes = new Uint8Array(buffer);
    var binary = "";
    for (var i = 0; i < bytes.length; i++) { binary += String.fromCharCode(bytes[i]); }
    return btoa(binary).replace(/\\+/g, "-").replace(/\\//g, "_").replace(/=+$/, "");
  }

  var app = document.getElementById("app");
  function render(html) { app.innerHTML = html; }

  var pathParts = window.location.pathname.split("/").filter(Boolean);
  var approvalPath = "/" + pathParts.join("/");
  var code = window.location.hash.replace(/^#/, "");

  function postJson(suffix, body) {
    return fetch(approvalPath + suffix, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (response) {
      if (!response.ok) {
        var rejected = new Error("request rejected");
        rejected.fromServer = true;
        throw rejected;
      }
      return response.json();
    });
  }

  function runEnrollment() {
    return postJson("/enroll/options", { code: code }).then(function (options) {
      var publicKey = options.publicKey || options;
      publicKey.challenge = base64urlToBuffer(publicKey.challenge);
      publicKey.user.id = base64urlToBuffer(publicKey.user.id);
      return navigator.credentials.create({ publicKey: publicKey });
    }).then(function (credential) {
      var response = credential.response;
      return postJson("/enroll/verify", {
        code: code,
        credential: {
          id: credential.id,
          rawId: bufferToBase64url(credential.rawId),
          type: credential.type,
          response: {
            clientDataJSON: bufferToBase64url(response.clientDataJSON),
            attestationObject: bufferToBase64url(response.attestationObject),
          },
        },
      });
    });
  }

  function runSigning() {
    return postJson("/sign/options", { code: code }).then(function (options) {
      var publicKey = options.publicKey || options;
      publicKey.challenge = base64urlToBuffer(publicKey.challenge);
      (publicKey.allowCredentials || []).forEach(function (entry) {
        entry.id = base64urlToBuffer(entry.id);
      });
      return navigator.credentials.get({ publicKey: publicKey });
    }).then(function (credential) {
      var response = credential.response;
      return postJson("/sign/verify", {
        code: code,
        credential: {
          id: credential.id,
          rawId: bufferToBase64url(credential.rawId),
          type: credential.type,
          response: {
            clientDataJSON: bufferToBase64url(response.clientDataJSON),
            authenticatorData: bufferToBase64url(response.authenticatorData),
            signature: bufferToBase64url(response.signature),
          },
        },
      });
    });
  }

  if (!code) {
    render("<p>This approval link is missing its code. " +
      "Please use the full link you were given.</p>");
    return;
  }

  postJson("/summary", { code: code }).then(function (summary) {
    render("<p>Preparing your passkey&hellip;</p>");
    var ceremony = summary.needs_enrollment ? runEnrollment().then(runSigning) : runSigning();
    return ceremony.then(function (result) {
      // A signed response still carries an `error` when the signature was
      // valid but the action it authorised could not be completed; saying
      // "Approved" there would report a merge that never happened. The
      // server's own message is not interpolated: `render` assigns
      // innerHTML and an executor outcome is not trusted markup.
      if (result && result.error) {
        render("<p>Your approval was signed, but the action it authorised could not " +
          "be completed, and nothing was changed. Ask for a new approval.</p>");
        return;
      }
      render("<p>Approved. You may close this window.</p>");
    });
  }).catch(function (error) {
    if (error && error.fromServer) {
      render("<p>This approval link is no longer valid.</p>");
      return;
    }
    // The server accepted the link; the browser's own passkey step failed
    // (e.g. a WebAuthn SecurityError on an IP-address host such as 127.0.0.1).
    var detail = error && error.name ? " (" + error.name + ")" : "";
    render("<p>Your browser could not complete the passkey step" + detail + ". " +
      "The link itself is still valid. Passkeys do not work on an IP address; " +
      "if the address bar shows one, open this link using a hostname such as " +
      "localhost instead.</p>");
  });
})();
</script>
</body>
</html>
"""


async def get_approval_shell(pending_approval_id: str) -> HTMLResponse:
    """Serve the HTML page that runs the approval ceremony in the approver's browser.

    The page is byte-identical whether the approval is pending, expired, already signed or
    unknown: the handler never looks the approval up, so no server-side state can leak into
    the markup. The page's script reads the approval code from the URL fragment (browsers
    never send a fragment to a server) and the id from the URL path, then calls the POST
    routes to enrol a passkey if needed and sign.

    Args:
        pending_approval_id: The approval id from the URL path. Accepted for routing and
            ignored.

    Returns:
        An HTML response with the fixed page and the header `Referrer-Policy: no-referrer`.
        It is always HTTP 200.
    """
    del pending_approval_id
    return HTMLResponse(content=_SHELL_HTML, headers={"Referrer-Policy": "no-referrer"})


async def post_approval_summary(
    pending_approval_id: str,
    request_body: _ApprovalCodeRequest,
    http_request: Request,
    store: Annotated[PendingApprovalStore, Depends(provide_pending_approval_store)],
    credential_store: Annotated[SigningCredentialStore, Depends(provide_signing_credential_store)],
) -> dict[str, object]:
    """Verify the approval code and return what the approver is being asked to sign.

    This is the first call the approval page makes. `needs_enrollment` is answered per host
    (the request's WebAuthn `rp_id`), not per actor alone: a credential enrolled on another
    host cannot sign here, so it must not count as already enrolled.

    Args:
        pending_approval_id: The approval id from the URL path.
        request_body: JSON body `{"code": str}` with the approval code.
        http_request: The incoming request; its host determines the `rp_id`.
        store: Pending-approval store (injected).
        credential_store: Signing-credential store (injected).

    Returns:
        A dict with `status` (the approval status, `pending`), `display_summary` (the
        human-readable description of the action to sign) and `needs_enrollment` (true when
        the approver has no passkey enrolled for this host).

    Raises:
        PendingApprovalInvalidOrExpiredError: the id is unknown, the code is wrong, or the
            approval is expired or already signed (HTTP 404, generic body).
        PendingApprovalPersistenceError: reading the approval failed.
        SigningCredentialPersistenceError: looking up the approver's credentials failed.

    Side Effects:
        None beyond a failed-outcome log entry on rejection; nothing is written.
    """
    row = _verify_code(
        store=store,
        pending_approval_id=pending_approval_id,
        code=request_body.code,
        action="approval_summary",
    )
    # Scoped to this request's `rp_id`: a credential enrolled on another host cannot sign
    # here, and counting it would strand the actor with no way to enrol again.
    rp_id, _origin = rp_id_and_origin(http_request)
    needs_enrollment = not credential_store.has_any_for_actor(
        actor_subject=row.actor_subject, actor_issuer=row.actor_issuer, rp_id=rp_id
    )
    return {
        "status": row.status,
        "display_summary": row.display_summary,
        "needs_enrollment": needs_enrollment,
    }


async def post_enroll_options(
    pending_approval_id: str,
    request_body: _ApprovalCodeRequest,
    http_request: Request,
    store: Annotated[PendingApprovalStore, Depends(provide_pending_approval_store)],
) -> dict[str, object]:
    """Generate the WebAuthn registration options for enrolling a passkey.

    The code, status and expiry are checked before any options are generated, so a stale or
    already-consumed approval never reaches the WebAuthn library. The user identity in the
    options is the approval's actor, and the challenge is derived from the approval row.

    Args:
        pending_approval_id: The approval id from the URL path.
        request_body: JSON body `{"code": str}` with the approval code.
        http_request: The incoming request; its host and scheme determine the WebAuthn
            `rp_id` and origin.
        store: Pending-approval store (injected).

    Returns:
        The WebAuthn `PublicKeyCredentialCreationOptions` as a JSON object.

    Raises:
        PendingApprovalInvalidOrExpiredError: the id is unknown, the code is wrong, or the
            approval is expired or already signed (HTTP 404, generic body).
        PendingApprovalPersistenceError: reading the approval failed.

    Side Effects:
        None beyond a failed-outcome log entry on rejection; nothing is written.
    """
    row = _verify_code(
        store=store,
        pending_approval_id=pending_approval_id,
        code=request_body.code,
        action="enroll_options",
    )
    options = build_registration_options(request=http_request, row=row)
    return json.loads(webauthn.options_to_json(options))


async def post_enroll_verify(
    pending_approval_id: str,
    request_body: _EnrollVerifyRequest,
    http_request: Request,
    store: Annotated[PendingApprovalStore, Depends(provide_pending_approval_store)],
    credential_store: Annotated[SigningCredentialStore, Depends(provide_signing_credential_store)],
    audit_store: Annotated[AuditStore, Depends(provide_audit_store)],
) -> dict[str, object]:
    """Verify a WebAuthn registration response and store the new passkey for the approver.

    The code, status and expiry are re-checked here rather than trusted from the earlier
    options call, because either may have changed in between. The credential is stored for
    the approval's actor under the request's `rp_id`.

    Args:
        pending_approval_id: The approval id from the URL path.
        request_body: JSON body `{"code": str, "credential": {...}}` with the approval code
            and the browser's WebAuthn registration response.
        http_request: The incoming request; its host and scheme determine the WebAuthn
            `rp_id` and origin the response is verified against.
        store: Pending-approval store (injected).
        credential_store: Signing-credential store (injected).
        audit_store: Audit store the `passkey.enroll` row is written to (injected).

    Returns:
        `{"status": "enrolled"}`.

    Raises:
        PendingApprovalInvalidOrExpiredError: the id is unknown, the code is wrong, the
            approval is expired or already signed, the credential payload is malformed, or
            WebAuthn verification fails (HTTP 404, generic body).
        PendingApprovalPersistenceError: reading the approval failed.
        SigningCredentialPersistenceError: storing the credential failed.

    Side Effects:
        Inserts one row into the signing-credential table (credential id, public key, sign
        count, actor, `rp_id`), then writes one `passkey.enroll` audit row attributed to the
        approval's bound actor (best-effort: a failed audit write is logged by class name and
        the enrolment still succeeds). A rejection writes no audit row and logs a failed-outcome
        entry. The approval itself stays pending.
    """
    row = _verify_code(
        store=store,
        pending_approval_id=pending_approval_id,
        code=request_body.code,
        action="enroll_verify",
    )
    with reject_on_webauthn_failure(
        action="enroll_verify", pending_approval_id=pending_approval_id
    ):
        verified = verify_registration(
            request=http_request, row=row, credential=request_body.credential
        )
    enrolled_rp_id, _origin = rp_id_and_origin(http_request)
    credential_store.create_signing_credential(
        actor_subject=row.actor_subject,
        actor_issuer=row.actor_issuer,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        rp_id=enrolled_rp_id,
    )
    record_follow_up_row(
        AuditContext((row.actor_subject, row.actor_issuer), audit_store),
        AuditTarget(PASSKEY_ENROLL_ACTION, PENDING_APPROVAL_RESOURCE_TYPE, pending_approval_id),
        component="passkey_signing",
        outcome="applied",
        details={"proof": "approval_code"},
    )
    return {"status": "enrolled"}


# --- POST /approvals/{id}/sign/*: signing ceremony ----------------------------------


async def post_sign_options(
    pending_approval_id: str,
    request_body: _ApprovalCodeRequest,
    http_request: Request,
    store: Annotated[PendingApprovalStore, Depends(provide_pending_approval_store)],
    credential_store: Annotated[SigningCredentialStore, Depends(provide_signing_credential_store)],
) -> dict[str, object]:
    """Generate the WebAuthn authentication options for signing the approval.

    The code, status and expiry are checked first. The challenge is recomputed from the
    approval's tool name, arguments, actor and nonce rather than stored, so it binds the
    signature to exactly the action being approved. The allowed credentials are the
    approver's passkeys enrolled under this request's `rp_id`.

    Args:
        pending_approval_id: The approval id from the URL path.
        request_body: JSON body `{"code": str}` with the approval code.
        http_request: The incoming request; its host and scheme determine the WebAuthn
            `rp_id` and origin.
        store: Pending-approval store (injected).
        credential_store: Signing-credential store (injected).

    Returns:
        The WebAuthn `PublicKeyCredentialRequestOptions` as a JSON object.

    Raises:
        PendingApprovalInvalidOrExpiredError: the id is unknown, the code is wrong, or the
            approval is expired or already signed (HTTP 404, generic body).
        PendingApprovalPersistenceError: reading the approval failed.
        SigningCredentialPersistenceError: listing the approver's credentials failed.

    Side Effects:
        None beyond a failed-outcome log entry on rejection; nothing is written.
    """
    row = _verify_code(
        store=store,
        pending_approval_id=pending_approval_id,
        code=request_body.code,
        action="sign_options",
    )
    challenge = _compute_sign_challenge(row)
    # Only this host's credentials: advertising one enrolled elsewhere leaves the
    # authenticator with nothing to match.
    sign_rp_id, _origin = rp_id_and_origin(http_request)
    allow_credentials = credential_store.list_for_actor(
        actor_subject=row.actor_subject, actor_issuer=row.actor_issuer, rp_id=sign_rp_id
    )
    options = build_authentication_options(
        request=http_request, challenge=challenge, allow_credentials=allow_credentials
    )
    return json.loads(webauthn.options_to_json(options))


def _resolve_credential_for_signing(
    *,
    credential_store: SigningCredentialStore,
    row: PendingApprovalRow,
    credential_payload: dict[str, object],
) -> SigningCredentialRow:
    """Resolve the assertion's `rawId` to an enrolled credential owned by the approval's actor.

    The ownership check runs before cryptographic verification, so a credential belonging to
    another actor never reaches the WebAuthn library.

    Args:
        credential_store: Signing-credential store to look the credential up in.
        row: The pending approval being signed; its actor is the required owner.
        credential_payload: The assertion JSON from the request; `rawId` must be a base64url
            string.

    Returns:
        The enrolled credential whose id equals `rawId` and whose actor equals the row's.

    Raises:
        PendingApprovalInvalidOrExpiredError: `rawId` is missing or not base64url, no such
            credential is enrolled, or it belongs to a different actor. Each condition is
            logged with its own reason; the response is identical.
        SigningCredentialPersistenceError: the credential lookup failed.

    Side Effects:
        Emits one failed-outcome log entry per rejection.
    """
    raw_id_value = credential_payload.get("rawId")
    if not isinstance(raw_id_value, str):
        reject(
            action="sign_verify", pending_approval_id=row.id, reason="missing_or_malformed_raw_id"
        )
    try:
        raw_id = base64url_to_bytes(raw_id_value)
    except Exception as exc:  # noqa: BLE001 -- base64url_to_bytes' failure modes are untyped; any failure maps to the same generic rejection, with the real exception logged server-side by `reject`
        reject(
            action="sign_verify",
            pending_approval_id=row.id,
            reason=f"raw_id_not_base64url: {exc!r}",
        )
    credential = credential_store.get_by_credential_id(raw_id)
    if credential is None:
        reject(action="sign_verify", pending_approval_id=row.id, reason="unknown_credential_id")
    if (credential.actor_subject, credential.actor_issuer) != (row.actor_subject, row.actor_issuer):
        reject(
            action="sign_verify",
            pending_approval_id=row.id,
            reason="credential_belongs_to_a_different_actor",
        )
    return credential


_EXECUTOR_FAILED_MESSAGE = (
    "this action could not be completed; if you still intend to proceed, ask for a new approval"
)


def _run_registered_executor_and_record_outcome(
    *, row: PendingApprovalRow, store: PendingApprovalStore, config: ServiceConfig
) -> dict[str, object]:
    """Run the executor registered for the approval's tool and store its outcome on the row.

    The signature is already consumed. An executor that raises (or, as a defence, a tool name
    with no executor, which `post_sign_verify` rejects earlier) stores one generic safe error;
    the real reason is logged server-side only and nothing is retried.

    Args:
        row: The signed approval; `tool_name` selects the executor.
        store: Pending-approval store; receives the outcome.
        config: The resolved service configuration, passed to the executor.

    Returns:
        The executor's outcome dict, or `{"error": <fixed message>}` when no executor is
        registered or the executor raised.

    Raises:
        PendingApprovalPersistenceError: storing the outcome failed.

    Side Effects:
        Whatever the executor does; writes the outcome to the approval row; emits a
        failed-outcome log entry when no executor exists or it raised.
    """
    executor = resolve_approval_executor(row.tool_name)
    outcome: dict[str, object] = {"error": _EXECUTOR_FAILED_MESSAGE}
    if executor is None:
        emit_log_entry(
            component="passkey_signing",
            action="sign_verify_executor",
            outcome="failed",
            extra={"pending_approval_id": row.id, "reason": "no_executor_registered"},
        )
    else:
        try:
            outcome = executor(row, config)
        except Exception as exc:  # noqa: BLE001 -- the signature is consumed; an executor must never leak a raw failure through the HTTP boundary
            emit_log_entry(
                component="passkey_signing",
                action="sign_verify_executor",
                outcome="failed",
                extra={"pending_approval_id": row.id, "reason": repr(exc)},
            )
    store.set_outcome(row.id, outcome)
    return outcome


async def post_sign_verify(
    pending_approval_id: str,
    request_body: _SignVerifyRequest,
    http_request: Request,
    store: Annotated[PendingApprovalStore, Depends(provide_pending_approval_store)],
    credential_store: Annotated[SigningCredentialStore, Depends(provide_signing_credential_store)],
    config: Annotated[ServiceConfig, Depends(get_service_config)],
) -> dict[str, object]:
    """Verify the approver's WebAuthn assertion and, if valid, run the approved action.

    Steps, in order: (1) check the code, status and expiry; (2) recompute the expected
    challenge from the approval (a client-supplied challenge is never trusted); (3) resolve
    the asserted credential and require that it belongs to the approval's actor; (4) verify
    the assertion cryptographically; (5) require an executor registered for the approval's
    tool; (6) atomically flip the approval from `pending` to `signed`, so it can be used once;
    (7) persist the credential's new sign count; (8) run the registered executor and store its
    outcome. A failed action does not undo the signature.

    Args:
        pending_approval_id: The approval id from the URL path.
        request_body: JSON body `{"code": str, "credential": {...}}` with the approval code
            and the browser's WebAuthn assertion.
        http_request: The incoming request; its host and scheme determine the WebAuthn
            `rp_id` and origin the assertion is verified against.
        store: Pending-approval store (injected).
        credential_store: Signing-credential store (injected).
        config: The resolved service configuration (injected).

    Returns:
        `{"status": "signed", ...outcome}`. The outcome is the registered executor's result, or
        `{"error": <fixed message>}` when the signature was valid but the action did not
        complete (including a Capability merge whose graph write could not be made). The
        signature is consumed in every case; the approver must then request a new approval.

    Raises:
        PendingApprovalInvalidOrExpiredError: the id is unknown, the code is wrong, the
            approval is expired or already signed, the credential is unknown or belongs to
            another actor, the payload is malformed, WebAuthn verification fails, or another
            request consumed the approval first, or no executor is registered for the
            approval's tool (HTTP 404, generic body; in the last case the row stays pending).
        PendingApprovalPersistenceError: reading the approval, consuming it or storing the
            outcome failed.
        SigningCredentialPersistenceError: reading or updating the credential failed.

    Side Effects:
        Marks the approval signed, updates the credential's sign count, runs the executor
        registered for the approval's tool (which owns any graph write and its audit rows),
        stores the action outcome on the approval row, and logs a failed-outcome entry for each
        rejection or failed action.
    """
    row = _verify_code(
        store=store,
        pending_approval_id=pending_approval_id,
        code=request_body.code,
        action="sign_verify",
    )
    challenge = _compute_sign_challenge(row)
    credential = _resolve_credential_for_signing(
        credential_store=credential_store, row=row, credential_payload=request_body.credential
    )
    # A verification failure (tampered challenge, wrong origin, bad signature, non-monotonic
    # sign_count) or a garbage payload maps to the same generic rejection as every other
    # failure, instead of reaching the app-wide 500 handler.
    with reject_on_webauthn_failure(action="sign_verify", pending_approval_id=pending_approval_id):
        verified = verify_authentication(
            request=http_request,
            challenge=challenge,
            credential=request_body.credential,
            credential_public_key=credential.public_key,
            credential_current_sign_count=credential.sign_count,
        )

    if resolve_approval_executor(row.tool_name) is None:
        # No executor can run this action (for example an in-flight approval of a retired tool),
        # so refuse before the single-use approval is consumed; the row stays pending and
        # expires. The response is the same generic rejection as every other failure.
        reject(
            action="sign_verify",
            pending_approval_id=pending_approval_id,
            reason="no_executor_registered",
        )
    if not store.mark_signed(pending_approval_id):
        # Lost the atomic compare-and-set, or the row was consumed or expired after the
        # earlier check: never run the action.
        reject(
            action="sign_verify",
            pending_approval_id=pending_approval_id,
            reason="mark_signed_lost_the_cas_or_row_already_consumed",
        )
    credential_store.update_sign_count(
        credential_id=credential.credential_id, sign_count=verified.new_sign_count
    )

    outcome = _run_registered_executor_and_record_outcome(row=row, store=store, config=config)
    return {"status": "signed", **outcome}


def build_passkey_signing_router() -> APIRouter:
    """Build the router that serves the companion-browser signing ceremony.

    The caller mounts it with `app.include_router`; the bearer-token middleware exempts the
    `/approvals` prefix, so each route authorizes itself with the approval code. Every POST
    route also refuses a `Host` outside the configured allowed origins before reading anything.

    Returns:
        A new `APIRouter` with prefix `/approvals` exposing `GET /{pending_approval_id}`
        (HTML shell) and `POST /{pending_approval_id}/summary`, `/enroll/options`,
        `/enroll/verify`, `/sign/options` and `/sign/verify`.

    Side Effects:
        None; it only registers routes on the new router.
    """
    router = APIRouter(prefix="/approvals")
    host_guard = [Depends(require_allowed_signing_host)]
    router.add_api_route(
        "/{pending_approval_id}",
        get_approval_shell,
        methods=["GET"],
        response_class=HTMLResponse,
    )
    router.add_api_route(
        "/{pending_approval_id}/summary",
        post_approval_summary,
        methods=["POST"],
        dependencies=host_guard,
    )
    router.add_api_route(
        "/{pending_approval_id}/enroll/options",
        post_enroll_options,
        methods=["POST"],
        dependencies=host_guard,
    )
    router.add_api_route(
        "/{pending_approval_id}/enroll/verify",
        post_enroll_verify,
        methods=["POST"],
        dependencies=host_guard,
    )
    router.add_api_route(
        "/{pending_approval_id}/sign/options",
        post_sign_options,
        methods=["POST"],
        dependencies=host_guard,
    )
    router.add_api_route(
        "/{pending_approval_id}/sign/verify",
        post_sign_verify,
        methods=["POST"],
        dependencies=host_guard,
    )
    return router
