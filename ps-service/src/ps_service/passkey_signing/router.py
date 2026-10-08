"""Companion-browser signing ceremony router (issue #131, PLAN.md §3, CHANGES.md F2).

``build_passkey_signing_router`` mirrors ``build_api_router``'s own factory
convention (``ps_service.api.routes``) -- ``ps_service.main.create_app``
mounts the returned ``APIRouter`` on the same app, alongside the REST API
router, via ``app.include_router``. Every route here sits under
``ps_service.auth.middleware``'s ``_APPROVALS_PREFIX`` exemption
(PLAN.md §0.5): unauthenticated at the bearer-token layer by design, with the
per-approval opaque ``code`` (never carried in the URL path -- only ever in
the client's request body or, before that, the link's URL *fragment*,
CHANGES.md F2) as the sole per-request authorization, verified inside this
router.

Slice 3 adds the signing ceremony (``.../sign/options``, ``.../sign/verify``):
for an actor who already has an enrolled credential, a verified WebAuthn
assertion causes the near-miss merge to actually execute
(``ps_service.api.near_miss_review_orchestration.run_resolve_near_miss``,
called for real, for the first time in this whole feature -- PLAN.md §0's
"delegate, don't reimplement" rule) -- see ``post_sign_verify`` below.

Slice 4 (PLAN.md §4) adds no new routes -- it hardens every route above via
``passkey_signing.error_handlers``: every rejection this router can produce
(unknown id, wrong/tampered code, expired, already-consumed, wrong-actor
credential, a lost ``mark_signed`` CAS race, a malformed WebAuthn payload, or
a genuine WebAuthn library verification failure) now raises the identical
``PendingApprovalInvalidOrExpiredError`` via ``error_handlers.reject``/
``reject_on_webauthn_failure``, which also records *why*, server-side only,
via ``emit_log_entry`` -- closing the one gap Slice 3 flagged (a raw
``webauthn`` verification failure previously fell through to the app-wide
generic 500 handler uncaught).

Issue #195 audits the signed merge as ``near_miss.resolve`` (approver as actor, ``approval_id``
in the details). The single-use approval is consumed (``mark_signed`` precedes the merge) even
when the opening audit row cannot be written: the merge then does not run, nothing is audited as
applied, the stored outcome is ``_AUDIT_UNAVAILABLE_MESSAGE`` and the user must request a new
approval. There is deliberately no pre-check: an audit row written before ``mark_signed`` would
be orphaned if the CAS were lost.
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
    """Return the production `SigningCredentialStore` (issue #131 Slice 2).

    A plain provider (not a generator), overridable in tests via
    `app.dependency_overrides` with a fake store -- mirrors
    `api.dependencies.provide_pending_approval_store` exactly.
    """
    return PsycopgSigningCredentialStore(config)


class _ApprovalCodeRequest(BaseModel):
    """Body shared by every `/approvals/{id}/*` POST route (CHANGES.md F2): just the code."""

    code: str


class _EnrollVerifyRequest(BaseModel):
    """Body for `POST /approvals/{id}/enroll/verify` (CHANGES.md F2)."""

    code: str
    credential: dict[str, object]


class _SignVerifyRequest(BaseModel):
    """Body for `POST /approvals/{id}/sign/verify` (CHANGES.md F2, issue #131 Slice 3)."""

    code: str
    credential: dict[str, object]


def _verify_code(
    *,
    store: PendingApprovalStore,
    pending_approval_id: str,
    code: str,
    action: str,
) -> PendingApprovalRow:
    """Look up `pending_approval_id`, verify `code` against `code_hash`, and apply F3's guard.

    Raises the identical `PendingApprovalInvalidOrExpiredError` for an
    unknown id, a wrong code, and an expired/consumed approval -- the three
    are never distinguished in the *response* (AC-BI-015). Issue #131 Slice
    4: each distinct condition is still recorded, server-side only, via
    `error_handlers.reject` -- the response staying generic never means the
    reason is simply lost.

    Args:
        store: The pending-approval store to read from.
        pending_approval_id: The `id` path segment (never the secret).
        code: The capability code from the request body (CHANGES.md F2 --
            never the URL path).
        action: This route's own short name, threaded through to `reject`
            for server-side log correlation (issue #131 Slice 4).

    Returns:
        The verified, still-pending, unexpired row.

    Raises:
        PendingApprovalInvalidOrExpiredError: id unknown, code wrong, or the
            row is no longer pending/has expired.
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
        # `_require_pending_and_unexpired` (service.py, Slice 2) is the one
        # place that *decides* pending/unexpired -- this `if` never competes
        # with that decision, it only labels it for the server-side log.
        reason = "already_signed" if row.status != "pending" else "expired"
        reject(action=action, pending_approval_id=pending_approval_id, reason=reason)
    return row


# --- GET /approvals/{id} -- generic companion-browser shell (CHANGES.md F2) --------

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
    """Render the generic companion-browser shell (CHANGES.md F2).

    Byte-identical regardless of whether `pending_approval_id` is pending,
    expired, already signed, or entirely unknown -- this handler never even
    looks the row up, so there is no server-side state that could leak into
    the markup (AC-BI-015). The page's own client-side JS reads `code` from
    `window.location.hash` (never sent to any server in a `Referer` header
    by spec; the `Referrer-Policy` header below is defense-in-depth) and
    `pending_approval_id` from `window.location.pathname`, then drives the
    real ceremony via the POST routes below.

    Args:
        pending_approval_id: The `id` path segment (unused -- see above).

    Returns:
        The fixed HTML shell, with `Referrer-Policy: no-referrer`.
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
    """Verify the code and return the WYSIWYS summary (CHANGES.md F2).

    The first call the companion-browser page's own JS makes, after reading
    `code` from `location.hash` -- this is where the opaque capability code
    is actually consumed for the first time server-side.

    Args:
        pending_approval_id: The `id` path segment.
        request_body: `{"code": str}`.
        http_request: The raw request, for `rp_id` derivation --
            `needs_enrollment` is answered per host, not per actor alone
            (issue #196).
        store: The pending-approval store (injected; overridden in tests).
        credential_store: The signing-credential store (injected; overridden
            in tests).

    Returns:
        `{"status", "display_summary", "needs_enrollment"}`.

    Raises:
        PendingApprovalInvalidOrExpiredError: id unknown, code wrong, or
            expired/consumed (HTTP 404, generic body, AC-BI-015).
    """
    row = _verify_code(
        store=store,
        pending_approval_id=pending_approval_id,
        code=request_body.code,
        action="approval_summary",
    )
    # Scoped to this request's own `rp_id` (issue #196): a credential enrolled
    # on another host cannot sign here, so it must not report this actor as
    # already enrolled and strand them with no way to enroll again.
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
    """Generate WebAuthn registration ("create") options (PLAN.md §3, issue #131 Slice 2).

    Verifies the code and F3's pending/unexpired guard before generating
    anything (never reaches `webauthn.generate_registration_options` for a
    stale or already-consumed approval).

    Args:
        pending_approval_id: The `id` path segment.
        request_body: `{"code": str}`.
        http_request: The raw request, for `rp_id`/`origin` derivation
            (`webauthn_rp.rp_id_and_origin`).
        store: The pending-approval store (injected; overridden in tests).

    Returns:
        The `PublicKeyCredentialCreationOptions`, JSON-shaped
        (`webauthn.options_to_json`'s own encoding).

    Raises:
        PendingApprovalInvalidOrExpiredError: id unknown, code wrong, or
            expired/consumed (HTTP 404, generic body, AC-BI-015).
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
    """Verify a WebAuthn registration response and persist the new credential.

    Re-verifies the code and F3's guard independently of `/enroll/options`
    (CHANGES.md F3: "codes/expiry can't be trusted to still hold between the
    two calls") -- before calling
    `webauthn.verify_registration_response`. On success, inserts a
    `signing_credentials` row for this approval's `(actor_subject,
    actor_issuer)` (AC-BI-001/AC-BI-003).

    Args:
        pending_approval_id: The `id` path segment.
        request_body: `{"code": str, "credential": <RegistrationCredential JSON>}`.
        http_request: The raw request, for `rp_id`/`origin` derivation.
        store: The pending-approval store (injected; overridden in tests).
        credential_store: The signing-credential store (injected; overridden
            in tests).

    Returns:
        `{"status": "enrolled"}`.

    Raises:
        PendingApprovalInvalidOrExpiredError: id unknown, code wrong,
            expired/consumed, a malformed WebAuthn payload, or a genuine
            WebAuthn verification failure (HTTP 404, generic body,
            AC-BI-015 -- issue #131 Slice 4: `verify_registration` failures
            are now mapped here too, never left to the app-wide generic 500
            handler).
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


# --- POST /approvals/{id}/sign/* -- signing ceremony (issue #131 Slice 3) ----------


async def post_sign_options(
    pending_approval_id: str,
    request_body: _ApprovalCodeRequest,
    http_request: Request,
    store: Annotated[PendingApprovalStore, Depends(provide_pending_approval_store)],
    credential_store: Annotated[SigningCredentialStore, Depends(provide_signing_credential_store)],
) -> dict[str, object]:
    """Generate WebAuthn authentication ("get") options (PLAN.md §3, issue #131 Slice 3).

    Verifies the code and F3's pending/unexpired guard (via `_verify_code`)
    before generating anything. The challenge is recomputed fresh from the
    row's own stored fields (`_compute_sign_challenge`, AC-BI-004/AC-BI-011)
    -- never a separately stored value -- and `allow_credentials` is built
    from every `signing_credentials` row already enrolled for this
    approval's own actor.

    Args:
        pending_approval_id: The `id` path segment.
        request_body: `{"code": str}`.
        http_request: The raw request, for `rp_id`/`origin` derivation.
        store: The pending-approval store (injected; overridden in tests).
        credential_store: The signing-credential store (injected; overridden
            in tests).

    Returns:
        The `PublicKeyCredentialRequestOptions`, JSON-shaped
        (`webauthn.options_to_json`'s own encoding).

    Raises:
        PendingApprovalInvalidOrExpiredError: id unknown, code wrong, or
            expired/consumed (HTTP 404, generic body, AC-BI-015).
    """
    row = _verify_code(
        store=store,
        pending_approval_id=pending_approval_id,
        code=request_body.code,
        action="sign_options",
    )
    challenge = _compute_sign_challenge(row)
    # Only this host's own credentials: advertising one enrolled elsewhere
    # leaves the authenticator with nothing to match (issue #196).
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
    """Resolve the assertion's `rawId` to an enrolled credential, enforcing AC-BI-005.

    The resolved credential's `(actor_subject, actor_issuer)` must match the
    pending approval's own -- checked here, explicitly, *before*
    `verify_authentication_response` is ever called (PLAN.md §3): a
    credential belonging to a different actor must never even reach the
    cryptographic verification step. A missing/malformed `rawId` and a
    wrong-actor credential are both the identical generic error
    (AC-BI-015) -- never distinguished from each other or from any other
    `.../sign/*` failure mode.

    Raises:
        PendingApprovalInvalidOrExpiredError: `rawId` is missing/malformed,
            no such credential is enrolled, or it belongs to a different
            actor than this pending approval. Issue #131 Slice 4: each of
            these three conditions is recorded, server-side only, via
            `error_handlers.reject`, distinguishable from one another in the
            log even though the response is identical.
    """
    raw_id_value = credential_payload.get("rawId")
    if not isinstance(raw_id_value, str):
        reject(
            action="sign_verify", pending_approval_id=row.id, reason="missing_or_malformed_raw_id"
        )
    try:
        raw_id = base64url_to_bytes(raw_id_value)
    except Exception as exc:  # noqa: BLE001 -- base64url_to_bytes' own failure modes are not documented/typed; any failure here maps to the same generic rejection (AC-BI-015), with the real exception logged server-side by `reject`
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
    """Record why a signed merge did nothing (AC-BI-006).

    `extra` carries the approval id, the review id when the row had one, and
    the reason -- never the `code`, the credential or any passkey material
    (AC-BI-001).
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
    """Run the real merge and persist its outcome onto the now-`'signed'` row.

    Issue #195: the merge is audited as `near_miss.resolve` with the APPROVER (the approval row's
    actor, whose credential just signed) as actor and `approval_id=row.id`; the `applied` row
    precedes the write. If that opening row cannot be written the merge does not run and the
    stored outcome is the fixed `_AUDIT_UNAVAILABLE_MESSAGE`. The single-use approval is consumed
    either way (`mark_signed` precedes this call; there is deliberately no pre-check, because an
    audit row written before `mark_signed` would be orphaned if the CAS were lost), so the user
    must request a new approval.

    Only ever called after `store.mark_signed` has already committed
    (PLAN.md §3: "post-signature merge-execution" -- the signature's
    validity is never contingent on the merge write's own success). Delegates
    to `run_resolve_near_miss` for the actual write (PLAN.md §0's "delegate,
    don't reimplement" rule) -- a `PendingReviewNotFoundError` for a
    since-gone-stale reference is caught here and recorded as a safe error
    message, never re-raised (the signature must stay consumed regardless).
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
        # The response and stored outcome stay generic; without this the
        # reason a signed merge did nothing is recorded nowhere.
        _log_merge_failure(pending_approval_id=row.id, review_id=review_id, reason=str(exc))
        outcome = {"error": _MERGE_FAILED_MESSAGE}
    except Exception as exc:  # noqa: BLE001 -- the signature is already consumed by now, so letting a `CompanyMergePersistenceError`/`redis.RedisError`/`CompanyMergeConfigurationError`/`IndexError` escape would store no outcome, log nothing, and 500 the caller into the shell's "this approval link is no longer valid" (AC-BI-012). Not silent: `_log_merge_failure` records the real exception server-side.
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
    """Run the executor registered for `row.tool_name` and persist its outcome (issue #190).

    The signature is already consumed. An unregistered `tool_name`, or an executor that
    raises, stores one generic safe error -- the real reason is logged server-side only and
    nothing is retried.
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
    """Verify a WebAuthn assertion and, on success, execute the merge (issue #131 Slice 3).

    Order (PLAN.md §3): (1) `_verify_code` -- code + F3's pending/unexpired
    guard, before any WebAuthn library call; (2) recompute the expected
    challenge fresh (never trust a client-supplied one); (3) resolve the
    assertion's credential and enforce AC-BI-005's actor-identity binding,
    *before* calling `verify_authentication_response`; (4) verify the
    assertion; (5) atomically flip `status` `'pending'` -> `'signed'`
    (`store.mark_signed`, AC-BI-012/013) -- a lost race/already-consumed row
    returns the identical generic "no longer valid" error, never attempting
    the merge; (6) bump the credential's `sign_count`; (7) only now, execute
    the real merge and record its outcome.

    Args:
        pending_approval_id: The `id` path segment.
        request_body: `{"code": str, "credential": <AuthenticationCredential JSON>}`.
        http_request: The raw request, for `rp_id`/`origin` derivation.
        store: The pending-approval store (injected; overridden in tests).
        credential_store: The signing-credential store (injected; overridden
            in tests).
        near_miss_dependencies: The near-miss review dependency bundle
            (injected; overridden in tests) -- the exact same bundle
            `near_misses_resolve`'s merge branch and
            `POST /near-misses/{review_id}/resolve` use.
        config: The resolved service configuration (injected).
        audit_store: Where the merge's `near_miss.resolve` audit rows go (injected; overridden in
            tests).

    Returns:
        `{"status": "signed", "winner_id", "loser_id"}` on a successful
        merge, or `{"status": "signed", "error": <safe message>}` if the
        signature was valid but the merge itself could not be completed
        (a since-gone-stale reference) -- the signature is consumed either
        way.

    Raises:
        PendingApprovalInvalidOrExpiredError: id unknown, code wrong,
            expired/consumed, the assertion's credential is unknown or
            belongs to a different actor (AC-BI-005), a malformed WebAuthn
            payload, a genuine WebAuthn verification failure, or the atomic
            CAS lost a race (HTTP 404, generic body, AC-BI-015 -- issue #131
            Slice 4: every one of these conditions is now mapped here, none
            left to the app-wide generic 500 handler).
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
    # Issue #131 Slice 4: a cryptographic verification failure (tampered
    # challenge, wrong origin, bad signature, non-monotonic sign_count) or a
    # malformed/garbage credential payload is caught here and mapped to the
    # identical generic rejection every other `.../sign/*`/`.../enroll/*`
    # failure mode already returns -- never left to the app-wide generic 500
    # handler (`error_handlers.reject_on_webauthn_failure`).
    with reject_on_webauthn_failure(action="sign_verify", pending_approval_id=pending_approval_id):
        verified = verify_authentication(
            request=http_request,
            challenge=challenge,
            credential=request_body.credential,
            credential_public_key=credential.public_key,
            credential_current_sign_count=credential.sign_count,
        )

    if not store.mark_signed(pending_approval_id):
        # Lost the atomic CAS race, or the row was consumed/expired between
        # `_verify_code`'s check and here -- never attempt the merge.
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
    """Build the companion-browser signing-ceremony `APIRouter` (issue #131 Slices 2-3).

    Returns:
        An `APIRouter` exposing `GET /approvals/{id}`,
        `POST /approvals/{id}/summary`, `POST /approvals/{id}/enroll/options`,
        `POST /approvals/{id}/enroll/verify`, `POST /approvals/{id}/sign/options`,
        and `POST /approvals/{id}/sign/verify` (CHANGES.md F2's route table).
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
