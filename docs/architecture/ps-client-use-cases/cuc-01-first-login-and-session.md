<!-- © 2026 Cartman ApS. All rights reserved. -->
# CUC-01: First login & session

**Status:** Specified
**Actor:** Any user with an account at the configured OIDC IdP — the deploy-created first
user, or an invitee (CUC-02)
**Goal:** An authenticated ps-client session: identity visible, surfaces unlocked, tokens
handled invisibly, and a clean return to the Login Screen on expiry or logout.
**Realizes:** —
**Interaction class:** —
**v1:** yes

## Entry points

- Launching ps-client (desktop or web) without a valid session — the Login Screen is the
  only surface an unauthenticated client shows.
- An enrollment-invite link (CUC-02's output) for a first-ever sign-in.

## Preconditions

- A deployed PS Service whose configured OIDC issuer is reachable. The IdP is bundled
  Authentik by default and may federate upstream — invisible either way: the client
  speaks OIDC to the configured issuer and nothing else.
- The account exists at the IdP: created by the deployment (the first user arrives as
  IdP admin with a passkey-enrolment link — no password exists anywhere in the journey)
  or by an invite.

## Beats

1. **Locked.** Unauthenticated, the client shows the Login Screen alone — no User Pane,
   no Chat or Artifact Pane, no cached data from a previous user. Fail closed, visibly. "Don't Panick!" is visble in large friendly letters i the middle of the screen.
2. **Sign in.** One action starts an OIDC authorization-code + PKCE flow against the
   IdP; the user authenticates there with their **login passkey**, on the IdP's own
   pages. On a first-ever sign-in via an invite link, the IdP's enrollment flow has the
   user enroll that passkey first. This passkey is the IdP's; PS Service's
   transaction-signing passkey (CUC-06/07/10) is a different credential at a different
   relying party, enrolled later, at first signing.
3. **Land.** The Login Screen disappears. The User Pane shows the signed-in identity,
   the Inbox, and the Library — both empty for a brand-new user. Every PS Service call
   from here carries the session's bearer token; what the user can *do* is a matter of
   AccessRole grants, not of logging in.
4. **First principal.** The very first authenticated principal of a fresh deployment is
   auto-bootstrapped to `SystemOwner` on its first authorized call — the evaluator
   journey depends on it: that session proceeds directly to inviting users (CUC-02) and
   granting roles (CUC-03). Every later principal starts as `AuthenticatedUser` alone.
5. **Session life.** Token refresh is silent; the user never sees an OAuth artifact
   after landing. When the session expires or is revoked mid-work, surfaces fail closed
   and visibly (greyed, not stale) and the Login Screen returns — losing nothing,
   because conversations, artifacts, and drafts are server-held. Re-authenticating
   resumes where the user left off (CUC-11).
6. **Logout.** A deliberate action in the User Pane: local tokens are discarded, the
   Login Screen returns. Nothing server-held is touched.

## Artifacts

None. This use case is the only one that completes without the Chat or Artifact Pane.

## Approvals & notifications

None. The login passkey ceremony belongs to the IdP, not to PS Service's signing RP; no
Inbox items or notifications are involved.

## Contracts touched

- **PSC-9 Auth flows** — the login half: code+PKCE from the desktop shell and from a
  plain browser page (SP-5 is the evidence; how the redirect lands differs per TD-2
  shell, and TD-5 decides where tokens live).
- **H-5 Session persistence** — re-authentication restores the prior session's
  conversations and tabs from server state.
- **PSC-6 Conversation persistence** — the reason expiry loses nothing.
- **PSC-12 Caller's own grants** — what the User Pane's identity display and every
  role-gated affordance read.
- **NFR-1** — the same login UX must hold on desktop and web.
- **NFR-3** — expiry and unreachable-issuer states grey out, never show stale data as
  current.
- **NFR-6** — all access through PS Service's authenticated surfaces; the client holds
  no credential but the user's own session.

## Open points

- **Token custody — decided, following industry standards.** Web: the BFF pattern from
  the IETF BCP *OAuth 2.0 for Browser-Based Applications* (building on RFC 9700) — a
  server-side component runs the OIDC flow and holds the tokens; the browser holds only an
  HttpOnly, Secure, SameSite session cookie, so no token is reachable from page
  JavaScript. Desktop: RFC 8252 — sign-in through the system browser, never an embedded
  webview, with tokens in the OS keychain. This settles TD-5 in favour of BFF.
  On web the BFF carries *all* PS Service traffic (REST, MCP, the document-sync channel),
  because it is the only holder of the token; same-origin calls remove the CORS concern.
  Desktop calls PS Service directly with keychain-held tokens. The BFF is a separate
  deployable beside PS Service (same Helm chart) that also serves the web client's static
  files, built on an off-the-shelf OIDC proxy (e.g. oauth2-proxy) rather than custom code
  — not inside PS Service, which stays a stateless, IdP-independent resource server, and
  not an IdP-specific proxy. Everything is routed under one host so the passkey-signing
  RP origin matches the page (SP-4). To confirm by spike: SP-3 (sync/streaming through
  the proxy) and SP-5 (login).
- **Self-roles surface — decided: PSC-12.** `list-access-roles` is SystemAdmin/SystemOwner-
  gated, so the client could not ask "who am I and what grants do I hold". A self-scoped
  "my grants" read is added to the PSC register (PSC-12); the client shapes every
  role-gated CUC's affordances and the User Pane's identity display from it, rather than
  trying actions and rendering denials. Denials are still rendered by name as the
  backstop.
- **First-principal bootstrap — decided: no special message.** The client does not
  announce the bootstrap moment; the User Pane's identity display (PSC-12) already shows
  `SystemOwner`. The known bootstrap-race hardening (#144) may change the mechanism
  underneath without touching the client.
- **Session duration — decided: no expiry warning.** Session lifetime, refresh-token
  lifetime, and idle behavior are IdP configuration; the client expresses none of it.
  Refresh is silent, and when it fails the client fails closed and visibly (beat 5) —
  nothing is lost, and re-authentication resumes where the user left off.
