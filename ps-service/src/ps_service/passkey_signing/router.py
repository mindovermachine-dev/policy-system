"""Companion-browser signing ceremony: the `/approvals/{id}` routes and their router factory.

A privileged action (for example resolving a near-miss as a merge) is not executed when it is
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

Audit: the signed near-miss merge is audited as `near_miss.resolve` with the approver as actor
and the approval id in the details. The single-use approval is consumed (`mark_signed` precedes
the merge) even when the opening audit row cannot be written; the merge then does not run,
nothing is audited as applied, the stored outcome is `_AUDIT_UNAVAILABLE_MESSAGE` and the
approver must request a new approval. No audit row is written before `mark_signed`, because it
would be orphaned if the consumption race were lost. The graph write gateway is checked before
`mark_signed`, so a missing gateway answers 503 and leaves the approval pending.

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
    provide_near_miss_review_dependencies,
    provide_pending_approval_store,
)
from ps_service.api.errors import PendingApprovalInvalidOrExpiredError, PendingReviewNotFoundError
from ps_service.api.graph_write_access import graph_write_access
from ps_service.api.near_miss_review_orchestration import (
    NearMissReviewDependencies,
    run_resolve_near_miss,
)
from ps_service.audit import (
    AuditContext,
    AuditStore,
    AuditTrailUnavailableError,
)
from ps_service.config import (
    ServiceConfig,  # noqa: TC001 -- FastAPI resolves the endpoint annotation at runtime
)
from ps_service.logging import emit_log_entry
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
        count, actor, `rp_id`) and logs a failed-outcome entry on rejection. The approval
        itself stays pending.
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


_MERGE_FAILED_MESSAGE = (
    "this action could not be completed; if you still intend to merge, ask for a new approval"
)
_AUDIT_UNAVAILABLE_MESSAGE = (
    "The audit trail is temporarily unavailable; the merge was not performed. "
    "Request a new approval."
)


def _log_merge_failure(*, pending_approval_id: str, review_id: str | None, reason: str) -> None:
    """Log a failed-outcome entry saying why a signed merge did nothing.

    The entry carries the approval id, the review id when the row has one, and `reason`; it
    never carries the code, the credential or any passkey material.
    """
    extra: dict[str, object] = {"pending_approval_id": pending_approval_id, "reason": reason}
    if review_id is not None:
        extra["review_id"] = review_id
    emit_log_entry(
        component="passkey_signing", action="sign_verify_merge", outcome="failed", extra=extra
    )


def _execute_merge_and_record_outcome(
    *,
    row: PendingApprovalRow,
    store: PendingApprovalStore,
    near_miss_dependencies: NearMissReviewDependencies,
    config: ServiceConfig,
    audit_store: AuditStore,
) -> dict[str, object]:
    """Run the near-miss merge for a signed approval and store its outcome on the row.

    Called only after `mark_signed` has committed, so the signature stays consumed whatever
    the merge does. The merge is performed by `run_resolve_near_miss`, audited as
    `near_miss.resolve` with the approver (the approval's actor) as actor and the approval id
    in the details; its opening `applied` audit row precedes the write. No failure of the
    merge is re-raised: each is logged and stored as a fixed safe error message.

    Args:
        row: The signed approval; `normalized_args["review_id"]` names the review to merge.
        store: Pending-approval store; receives the outcome.
        near_miss_dependencies: The near-miss review dependency bundle used by the merge.
        config: The resolved service configuration.
        audit_store: Store receiving the merge's `near_miss.resolve` audit rows.

    Returns:
        The stored outcome: `{"winner_id", "loser_id"}` on success, otherwise
        `{"error": <fixed message>}`. The message is a missing-`review_id` notice, the
        audit-unavailable notice (the merge did not run) or a generic failed notice (the
        review is gone, or any other failure).

    Raises:
        PendingApprovalPersistenceError: storing the outcome failed.

    Side Effects:
        Merges the near-miss entities in the graph and writes audit rows through
        `run_resolve_near_miss` on success; writes the outcome to the approval row; emits a
        failed-outcome log entry for every non-success path.
    """
    review_id = row.normalized_args.get("review_id")
    if not isinstance(review_id, str):
        _log_merge_failure(pending_approval_id=row.id, review_id=None, reason="missing_review_id")
        outcome: dict[str, object] = {
            "error": "pending approval is missing its review_id; the merge could not be run"
        }
        store.set_outcome(row.id, outcome)
        return outcome
    try:
        result = run_resolve_near_miss(
            review_id,
            "merge",
            config=config,
            dependencies=near_miss_dependencies,
            audit=AuditContext((row.actor_subject, row.actor_issuer), audit_store),
            approval_id=row.id,
        )
    except AuditTrailUnavailableError:
        _log_merge_failure(
            pending_approval_id=row.id, review_id=review_id, reason="audit_opening_failed"
        )
        outcome = {"error": _AUDIT_UNAVAILABLE_MESSAGE}
    except PendingReviewNotFoundError as exc:
        # The response and stored outcome stay generic; the log is the only record of the reason.
        _log_merge_failure(pending_approval_id=row.id, review_id=review_id, reason=str(exc))
        outcome = {"error": _MERGE_FAILED_MESSAGE}
    except Exception as exc:  # noqa: BLE001 -- the signature is already consumed, so letting a persistence, Redis, configuration or index error escape would store no outcome, log nothing, and 500 the caller into the shell's "this approval link is no longer valid". Not silent: `_log_merge_failure` records the real exception server-side.
        _log_merge_failure(pending_approval_id=row.id, review_id=review_id, reason=repr(exc))
        outcome = {"error": _MERGE_FAILED_MESSAGE}
    else:
        outcome = {"winner_id": result.winner_id, "loser_id": result.loser_id}
    store.set_outcome(row.id, outcome)
    return outcome


_NEAR_MISS_TOOL_NAME = "near_misses_resolve"
_EXECUTOR_FAILED_MESSAGE = (
    "this action could not be completed; if you still intend to proceed, ask for a new approval"
)


def _run_registered_executor_and_record_outcome(
    *, row: PendingApprovalRow, store: PendingApprovalStore, config: ServiceConfig
) -> dict[str, object]:
    """Run the executor registered for the approval's tool and store its outcome on the row.

    The signature is already consumed. An unregistered tool name, or an executor that raises,
    stores one generic safe error; the real reason is logged server-side only and nothing is
    retried.

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
    near_miss_dependencies: Annotated[
        NearMissReviewDependencies, Depends(provide_near_miss_review_dependencies)
    ],
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    audit_store: Annotated[AuditStore, Depends(provide_audit_store)],
) -> dict[str, object]:
    """Verify the approver's WebAuthn assertion and, if valid, run the approved action.

    Steps, in order: (1) check the code, status and expiry; (2) recompute the expected
    challenge from the approval (a client-supplied challenge is never trusted); (3) resolve
    the asserted credential and require that it belongs to the approval's actor; (4) verify
    the assertion cryptographically; (5) for a near-miss merge, require a graph write gateway
    before consuming the approval; (6) atomically flip the approval from `pending` to
    `signed`, so it can be used once; (7) persist the credential's new sign count; (8) run the
    action (the near-miss merge, or the executor registered for the approval's tool) and store
    its outcome. A failed action does not undo the signature.

    Args:
        pending_approval_id: The approval id from the URL path.
        request_body: JSON body `{"code": str, "credential": {...}}` with the approval code
            and the browser's WebAuthn assertion.
        http_request: The incoming request; its host and scheme determine the WebAuthn
            `rp_id` and origin the assertion is verified against.
        store: Pending-approval store (injected).
        credential_store: Signing-credential store (injected).
        near_miss_dependencies: The near-miss review dependency bundle (injected) used when
            the approval is a near-miss merge.
        config: The resolved service configuration (injected).
        audit_store: Store receiving the merge's `near_miss.resolve` audit rows (injected).

    Returns:
        `{"status": "signed", ...outcome}`. The outcome is `winner_id` and `loser_id` after a
        successful merge, the executor's result for other tools, or `{"error": <fixed message>}`
        when the signature was valid but the action did not complete. The signature is consumed
        in every case; the approver must then request a new approval.

    Raises:
        PendingApprovalInvalidOrExpiredError: the id is unknown, the code is wrong, the
            approval is expired or already signed, the credential is unknown or belongs to
            another actor, the payload is malformed, WebAuthn verification fails, or another
            request consumed the approval first (HTTP 404, generic body).
        GraphWriteUnavailableError: a near-miss merge was approved but no graph write gateway
            is installed (HTTP 503). The approval stays pending and can be retried.
        PendingApprovalPersistenceError: reading the approval, consuming it or storing the
            outcome failed.
        SigningCredentialPersistenceError: reading or updating the credential failed.

    Side Effects:
        Marks the approval signed, updates the credential's sign count, runs the approved
        action (graph merge and audit rows for a near-miss merge), stores the action outcome on
        the approval row, and logs a failed-outcome entry for each rejection or failed action.
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

    if row.tool_name == _NEAR_MISS_TOOL_NAME:
        # The merge writes through the graph write gateway. Check it BEFORE the single-use
        # approval is consumed, so a missing gateway is a 503 the same approval can retry,
        # not a burned approval. Side-effect free; no audit row exists yet.
        graph_write_access(config)
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

    if row.tool_name == _NEAR_MISS_TOOL_NAME:
        outcome = _execute_merge_and_record_outcome(
            row=row,
            store=store,
            near_miss_dependencies=near_miss_dependencies,
            config=config,
            audit_store=audit_store,
        )
    else:
        outcome = _run_registered_executor_and_record_outcome(row=row, store=store, config=config)
    return {"status": "signed", **outcome}


def build_passkey_signing_router() -> APIRouter:
    """Build the router that serves the companion-browser signing ceremony.

    The caller mounts it with `app.include_router`; the bearer-token middleware exempts the
    `/approvals` prefix, so each route authorizes itself with the approval code.

    Returns:
        A new `APIRouter` with prefix `/approvals` exposing `GET /{pending_approval_id}`
        (HTML shell) and `POST /{pending_approval_id}/summary`, `/enroll/options`,
        `/enroll/verify`, `/sign/options` and `/sign/verify`.

    Side Effects:
        None; it only registers routes on the new router.
    """
    router = APIRouter(prefix="/approvals")
    router.add_api_route(
        "/{pending_approval_id}",
        get_approval_shell,
        methods=["GET"],
        response_class=HTMLResponse,
    )
    router.add_api_route("/{pending_approval_id}/summary", post_approval_summary, methods=["POST"])
    router.add_api_route(
        "/{pending_approval_id}/enroll/options", post_enroll_options, methods=["POST"]
    )
    router.add_api_route(
        "/{pending_approval_id}/enroll/verify", post_enroll_verify, methods=["POST"]
    )
    router.add_api_route("/{pending_approval_id}/sign/options", post_sign_options, methods=["POST"])
    router.add_api_route("/{pending_approval_id}/sign/verify", post_sign_verify, methods=["POST"])
    return router
