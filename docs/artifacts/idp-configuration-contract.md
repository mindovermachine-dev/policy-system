# IdP Configuration Contract

PS Service is a generic OIDC resource server (issue #58): it validates bearer
tokens against **any** OIDC-compliant identity provider via standard OIDC
discovery — there is no provider-specific code. A deployment supplies up to
four configuration values (as `PS_AUTH_*` environment variables, or as the
corresponding `psService.auth.*` Helm values — see the
[Helm Chart Values Reference](./helm-chart-values-reference.md)). This page
documents what each value is, what IdP-side artifact it corresponds to, and a
worked example for the bundled [Authentik](https://goauthentik.io) instance —
PS Service's default, fixed IdP as of issue #129 (`scripts/deploy-ps-prod.sh`'s
production profile and, since issue #165, `scripts/deploy-ps-eval.sh`'s evaluator profile
both bundle Authentik and point PS Service at it always). A customer wanting their own upstream IdP's identities to log in can
additionally federate the bundled Authentik to it — see [Optional: federate to
an upstream IdP](#optional-federate-to-an-upstream-idp) — without changing any
`psService.auth.*` value at all.

If neither this configuration nor the local-test bypass
(`PS_SERVICE_LOCAL_TEST_BYPASS=true`, evaluation only — never for a
network-reachable deployment) is present, PS Service refuses to start. The
evaluator profile does **not** use the bypass (it cannot start in a container);
it runs the same real OIDC login as production.

## The four values

| Value | Env var | Helm value | What it is |
| --- | --- | --- | --- |
| Issuer | `PS_AUTH_ISSUER` | `psService.auth.issuer` | The OIDC authorization server's base URL. PS Service fetches `<issuer>/.well-known/openid-configuration` from it at startup to discover the JWKS endpoint and the signing algorithms it trusts, and every presented token's `iss` claim must match this value exactly (character for character). |
| Audience | `PS_AUTH_AUDIENCE` | `psService.auth.audience` | The identifier of PS Service itself as an OIDC *resource server* — for the bundled Authentik, the fixed OAuth2 Provider's own `client_id`; Every presented token's `aud` claim must match this value exactly. |
| CLI client id | `PS_AUTH_CLI_CLIENT_ID` | `psService.auth.cliClientId` | The **public client** Provider that PS-Cli authenticates as (device-authorization flow, issue #57). Optional: only needed so PS Service can advertise it in the `/.well-known/oauth-protected-resource` metadata as `ps_cli_client_id`, letting a client discover which client id to use without being told out of band. |
| Scopes | `PS_AUTH_SCOPES` | `psService.auth.scopes` | The OAuth scope(s) (space- or comma-separated) that a client should request when logging in — e.g. Authentik's own `openid profile email offline_access` scope mappings. **Not used for token validation** (PS Service checks `aud`, not `scope`), but **required in practice**: PS-Cli's device-authorization login (issue #57) sources its OAuth `scope` request parameter directly from this value, via `/.well-known/oauth-protected-resource`'s `scopes_supported`. Leaving it unset makes PS Service advertise an empty scope list, which most IdPs reject outright, so `ps-cli auth login` fails against any deployment that omits it. |

Only `issuer` and `audience` are required for PS Service to validate tokens at
all; `cliClientId` is a convenience for client discovery. `scopes` is likewise
optional from PS Service's own validation standpoint, but omitting it breaks
PS-Cli login end-to-end — treat it as required for any deployment a CLI or
Claude Desktop client will actually log into.

## Worked example: the bundled Authentik instance (default)

As of issue #129, `scripts/deploy-ps-prod.sh`'s production profile bundles
Authentik as PS Service's fixed IdP and computes all four
`psService.auth.*` values itself; since issue #165 `scripts/deploy-ps-eval.sh` does
the same for the evaluator profile — **an operator running either default flow
performs none of the manual app-registration steps this section used to
require.** This section documents what those four values resolve to and why,
both so a reader understands what the deploy scripts are doing on their behalf and
so someone pointing PS Service at an independently-run Authentik instance
(outside the scripts) knows what to configure.

### The four values, resolved

Authentik's blueprint (`charts/policy-system/files/authentik-blueprint.yaml`)
defines exactly **one** OAuth2 Provider and one Application, both with the
fixed literal name `ps-cli` — there is no separate API-app/CLI-app
split, because Authentik's `aud` claim is always the bare OAuth2 Provider
`client_id` (confirmed live, #128's spike, AC-BI-002 row):

| Value | Resolves to | Where it comes from |
| --- | --- | --- |
| `psService.auth.issuer` | Production: `https://<hostname>/auth/application/o/ps-cli/`. Evaluator: `https://<hostname>:30443/application/o/ps-cli/` (default hostname `authentik.local`) | Production: `<hostname>` is PS Service's own resolved public DNS-label hostname (the same one its own Ingress uses); `/auth` is the path prefix Authentik's Ingress is mounted at (not a second hostname — see `docs/architecture/customer-azure-deployment.md`'s [Authentik](../architecture/customer-azure-deployment.md#authentik) section for why). Evaluator: the name given to `deploy-ps-eval.sh --hostname` and Authentik's HTTPS NodePort `30443` (the issuer includes the port), no path prefix. `/application/o/ps-cli/` is Authentik's own fixed issuer-path shape, filled by the blueprint's Application `slug`. |
| `psService.auth.audience` | `ps-cli` | The blueprint's fixed OAuth2 Provider `client_id` literal. |
| `psService.auth.cliClientId` | `ps-cli` | The same literal — Authentik has no separate public-client registration to distinguish. |
| `psService.auth.scopes` | `openid profile email offline_access` | The blueprint's own explicit `property_mappings` on the Provider. `offline_access` is included deliberately: a blueprint-created Provider gets **no** scope mappings by default, and PS-Cli's device flow always requests `offline_access` for its refresh token — omitting it here would silently drop that scope from every login. |

`scripts/deploy-ps-prod.sh` sets all four via `ensure_release`'s `--set`
overrides, as fixed script constants computed once the hostname resolves —
never fetched from a live API call. `scripts/deploy-ps-eval.sh` sets the same four
from the hostname and the fixed HTTPS NodePort. See `docs/architecture/customer-azure-deployment.md`'s [Helm
release and chart hardening
profile](../architecture/customer-azure-deployment.md#helm-release-and-chart-hardening-profile)
section for the exact `--set` list.

### The `sub` claim and the bootstrap owner

The blueprint sets the `ps-cli` Provider's `sub_mode` to `user_username`, so the ID token's
`sub` is the user's **username**, not Authentik's default hashed user id. The deploy scripts
create the owner with username and email both equal to the address they were given, which makes
the owner's `sub` computable before that person ever logs in. The scripts therefore set
`psService.authzBootstrapOwner.subject` to the email and `.issuer` to the issuer above in the
**same deploy** that installs everything else — a bootstrap owner is matched on `(sub, iss)`,
character for character. An invitee chooses a username at enrolment, and that becomes their
`sub`. The owner is both the Authentik administrator and the PS `SystemOwner`; PS Service
grants the role on the owner's first role-gated call (see the [Installation Guide's SystemOwner
bootstrap](./installation-guide.md#systemowner-bootstrap)).

### Transport: HTTPS, and trust for `ps-cli`

`ps-cli` (and the MCP bridge that reuses its login) refuses to send credentials to an issuer,
device-authorization endpoint or token endpoint that is neither `https://` nor a loopback address
(`_assert_secure_or_loopback` in `oidc_discovery.py`); this rule is unchanged. The evaluator
profile therefore serves Authentik over HTTPS, with a certificate from a local certificate
authority created by `scripts/deploy-ps-eval.sh`. PS Service's pod trusts that CA through
`SSL_CERT_FILE` (a bundle of the system CAs plus the local CA, mounted by the chart); nothing
disables TLS verification. `ps-cli` and `ps-cli-mcp-bridge` do **not** read the operating
system trust store, only `SSL_CERT_FILE`/`SSL_CERT_DIR`, so on the evaluator they need
`SSL_CERT_FILE` pointing at the local CA certificate (which replaces their default bundle
for that process). Browsers need the CA imported into the OS or browser store instead, and
passkeys only work over such a trusted HTTPS origin. Production uses a public Let's Encrypt
certificate, so none of this applies there. See the [Installation
Guide](./installation-guide.md#8-trust-the-local-ca-log-in-and-install-the-policy-system-plugin).

### Passkey-only enrolment and recovery

For every user of the bundled Authentik, enrolment and recovery ask for a passkey and never a
password:

- **Invitation enrolment** (`ps-invite-enrollment`): Invitation stage → Prompt
  (username/name/email) → User Write → WebAuthn passkey setup → User Login. The prompt has no
  password field, so an invitee's account has **no password set**.
- **Recovery** (`ps-passkey-recovery`, set as the brand's `flow_recovery`): WebAuthn setup →
  User Login, with no password stage. The deploy scripts issue the owner's single-use,
  time-limited link from it. Authentik cannot issue a recovery link unless the brand has a
  recovery flow, which is why the blueprint sets one.
- **Login**: the identification stage accepts a passkey directly. The shipped authentication
  flow still contains a password stage and an administrator can set a user's password, so
  "passkey-only" is enforced at enrolment and recovery, not by removing that stage (removing it
  is a documented lockout risk).

### Configuring PS Service manually (non-script Authentik instances)

If you are pointing PS Service at an Authentik instance not provisioned by
`deploy-ps-prod.sh` or `deploy-ps-eval.sh` (e.g. this chart's own subchart installed standalone), set the
equivalent values directly from that instance's own Application/Provider
configuration:

```bash
helm upgrade --install policy-system oci://ghcr.io/mindovermachine-dev/charts/policy-system \
  -f values-prod.yaml \
  --set psService.auth.issuer=https://<authentik-hostname>/application/o/<application-slug>/ \
  --set psService.auth.audience=<provider-client-id> \
  --set psService.auth.cliClientId=<provider-client-id> \
  --set psService.auth.scopes="openid profile email offline_access"
```

(Or set the equivalent `PS_AUTH_ISSUER`/`PS_AUTH_AUDIENCE`/
`PS_AUTH_CLI_CLIENT_ID`/`PS_AUTH_SCOPES` environment variables directly, if
not deploying via the Helm chart.) PS Service's own startup then performs
OIDC discovery against the issuer above, confirming it can reach
`<issuer>/.well-known/openid-configuration` and that the response advertises
a usable JWKS endpoint and at least one trusted (asymmetric) signing
algorithm — Authentik's discovery document advertises `RS256` by default,
which PS Service trusts.

### Inviting a new user

As of issue #140, inviting a new user no longer requires an admin to
hand-craft the curl call in step 1 of the [Verify](#verify) example below.
Call the `invite-user` MCP tool directly, or use the `ps-invite-user` skill
from Claude Desktop, with the target email — PS Service calls Authentik's
invitation-stage API itself, using its own `PS_AUTHENTIK_API_TOKEN`/
`PS_AUTHENTIK_BASE_URL` service credentials (never a caller-supplied token),
gated to callers holding `SystemAdmin` or above (`SystemOwner` counts). The
tool returns the created invite's `itoken` and redemption `invite_url`;
delivering that URL to the invitee is still the admin's own responsibility — no
email is sent by PS Service or the skill.

The API token is the same random value the chart generates once and hands to
Authentik as its `AUTHENTIK_BOOTSTRAP_TOKEN` (Secret
`policy-system-authentik-api-token`), so Authentik accepts it from the first
start. Authentik applies a bootstrap token only once per tenant and never
rotates it. PS Service calls the API at `PS_AUTHENTIK_BASE_URL` and builds the
invitee's link on the optional `PS_AUTHENTIK_PUBLIC_URL` when set, else on the
base URL. The evaluator's base URL is already user-reachable, so it sets no
public URL; production's base URL is the in-cluster Service (`http://policy-system-authentik-server/auth`,
because the invitation API is not on the public Ingress) and its public URL is
`https://<host>/auth`. `PS_AUTHENTIK_PUBLIC_URL` is not in a published release yet — see the
[Installation Guide's Verification status](./installation-guide.md#verification-status).

The curl-based workflow below is still valid as a manual fallback or
verification method — for example when diagnosing a deployment directly
against Authentik's API, outside of PS Service.

### Verify

The following is a representative excerpt of a real, live end-to-end run made
under issue #129 (the prompt then still had a password field; it no longer does, see
[Passkey-only enrolment and recovery](#passkey-only-enrolment-and-recovery)) —
generate an invite, redeem it with a passkey enrollment, log in via device
flow, and present the resulting token to a protected route:

```bash
# 1. Generate a single-use invite code (admin-only -- AC-BI-007)
$ curl -X POST http://localhost:9080/api/v3/stages/invitation/invitations/ \
    -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
    -d '{"name":"example-invite","single_use":true}'
201 {"pk":"266f9e11-...", "single_use":true, ...}

# 2. Redeem it at /if/flow/ps-invite-enrollment/?itoken=<pk> -- Prompt (username/
#    name/email; no password field since #165) -> User Write -> WebAuthn passkey
#    enrollment -> logged in.
#    (server-side confirmation a real credential was persisted:)
$ ak shell -c "WebAuthnDevice.objects.filter(user__username='combined-user-1')"
device 'WebAuthn Device' created 2026-09-26 12:08:...

# 3. ps-cli auth login (device flow) -- passkey satisfies login with NO password
#    entered in this session at all (AC-BI-004: replaces password, not just MFA).
$ uv run ps-cli auth login
http://localhost:9080/device?code=097724529
logged in to s11 (http://localhost:9080/application/o/ps-cli/)

# 4. Present the resulting real token to a protected PS Service route.
$ curl -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8001/catalog                       # no token
401
$ curl -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer garbage.token.value" ...   # garbage token
401
$ curl -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer <real token>" ...           # real token
200
```

PS Service's existing, unmodified OIDC validator (issue #58) accepted a
real, passkey-login-derived Authentik token — no PS Service code or
configuration beyond the four values above was involved.

### Common pitfalls

- **`ps-cli auth login` device-flow page 404s**: almost always means
  Authentik's Brand has no `flow_device_code` configured — the bundled
  blueprint sets this, but a hand-run/standalone Authentik instance built
  without it will 404 silently (the exact gap #128's spike found).
- **Every login redirects to a password prompt, passkey never offered
  standalone**: means the shipped `default-authentication-identification`
  stage's `webauthn_stage` FK is unset — by default, a fresh Authentik
  install only accepts a passkey as an MFA *second* factor, not as a
  password replacement. The bundled blueprint patches this explicitly (see
  `docs/architecture/customer-azure-deployment.md`'s Authentik section).
- **`ps-cli auth login` reports `Could not reach ...` against the evaluator's
  `https://` issuer**: the local CA is not trusted by `ps-cli`, which reads only
  `SSL_CERT_FILE`/`SSL_CERT_DIR` — see [Transport](#transport-https-and-trust-for-ps-cli).
- **Opening an owner link says no recovery flow is set**: the blueprint has not applied
  yet (a fresh install takes about a minute after Authentik is Ready). The deploy scripts
  wait for it; a hand-run instance needs the bundled blueprint applied.
- **`ps-cli auth login` fails with an empty-scope rejection**: `scopes` was
  left unset or empty — see [The four values](#the-four-values) above;
  Authentik rejects an empty scope request the same way most IdPs do.
- **An invite link stops working before the intended recipient uses it**:
  Authentik's `Invitation` stage consumes a single-use code on the *first*
  request that resolves it, including a stray `curl`/prefetch, not only a
  completed enrollment — see `docs/architecture/customer-azure-deployment.md`'s
  Open Risks for the full note on distribution-channel link-prefetching.

## Optional: federate to an upstream IdP

The bundled Authentik can federate to an upstream identity provider such as
Microsoft Entra ID, by adding an OAuth/OIDC Source in Authentik's own Admin UI
(see Authentik's [Sources documentation](https://docs.goauthentik.io/users-sources/sources/)).
**This requires no `psService.auth.*` value change:** PS Service never trusts
Entra directly — it keeps validating tokens issued by the same fixed Authentik
issuer/OAuth2 Provider, and only Authentik's login page gains a "Sign in with
Microsoft" option. Federation is additive; local invite-registered accounts keep
working. This repo does not script or blueprint the Source.
