# IdP Configuration Contract

PS Service is a generic OIDC resource server (issue #58): it validates bearer
tokens against **any** OIDC-compliant identity provider via standard OIDC
discovery — there is no provider-specific code. A deployment supplies up to
four configuration values (as `PS_AUTH_*` environment variables, or as the
corresponding `psService.auth.*` Helm values — see the
[Helm Chart Values Reference](./helm-chart-values-reference.md)). This page
documents what each value is, what IdP-side artifact it corresponds to, and a
worked example for the bundled [Authentik](https://goauthentik.io) instance —
PS Service's default, fixed production IdP as of issue #129 (`scripts/
deploy-ps.sh`'s production profile bundles Authentik and points PS Service at
it always; zero Entra app registrations are created by default). A customer
wanting their own Entra tenant's identities to log in can additionally
federate the bundled Authentik to Entra — see [Optional: federate to
Microsoft Entra ID](#optional-federate-to-microsoft-entra-id) — without
changing any `psService.auth.*` value at all.

If neither this configuration nor the local-test bypass
(`PS_SERVICE_LOCAL_TEST_BYPASS=true`, evaluation only — never for a
network-reachable deployment) is present, PS Service refuses to start.

## The four values

| Value | Env var | Helm value | What it is |
| --- | --- | --- | --- |
| Issuer | `PS_AUTH_ISSUER` | `psService.auth.issuer` | The OIDC authorization server's base URL. PS Service fetches `<issuer>/.well-known/openid-configuration` from it at startup to discover the JWKS endpoint and the signing algorithms it trusts, and every presented token's `iss` claim must match this value exactly (character for character). |
| Audience | `PS_AUTH_AUDIENCE` | `psService.auth.audience` | The identifier of PS Service itself as an OIDC *resource server* — for the bundled Authentik, the fixed OAuth2 Provider's own `client_id`; for Entra, the API app registration's own identifier, never the CLI client's. Every presented token's `aud` claim must match this value exactly. |
| CLI client id | `PS_AUTH_CLI_CLIENT_ID` | `psService.auth.cliClientId` | The **public client** app registration/Provider that PS-Cli authenticates as (device-authorization flow, issue #57). Optional: only needed so PS Service can advertise it in the `/.well-known/oauth-protected-resource` metadata as `ps_cli_client_id`, letting a client discover which client id to use without being told out of band. |
| Scopes | `PS_AUTH_SCOPES` | `psService.auth.scopes` | The OAuth scope(s) (space- or comma-separated) that a client should request when logging in — e.g. Authentik's own `openid profile email offline_access` scope mappings, or Entra's `access_as_user` delegated scope. **Not used for token validation** (PS Service checks `aud`, not `scope`), but **required in practice**: PS-Cli's device-authorization login (issue #57) sources its OAuth `scope` request parameter directly from this value, via `/.well-known/oauth-protected-resource`'s `scopes_supported`. Leaving it unset makes PS Service advertise an empty scope list, which most IdPs — Entra included (`AADSTS900144`) — reject outright, so `ps-cli auth login` fails against any deployment that omits it. |

Only `issuer` and `audience` are required for PS Service to validate tokens at
all; `cliClientId` is a convenience for client discovery. `scopes` is likewise
optional from PS Service's own validation standpoint, but omitting it breaks
PS-Cli login end-to-end — treat it as required for any deployment a CLI or
Claude Desktop client will actually log into.

## Worked example: the bundled Authentik instance (default)

As of issue #129, `scripts/deploy-ps.sh`'s production profile bundles
Authentik as PS Service's fixed IdP and computes all four
`psService.auth.*` values itself — **an operator running the default flow
performs none of the manual app-registration steps this section used to
require.** This section documents what those four values resolve to and why,
both so a reader understands what `deploy-ps.sh` is doing on their behalf and
so someone pointing PS Service at an independently-run Authentik instance
(outside `deploy-ps.sh`) knows what to configure.

### The four values, resolved

Authentik's blueprint (`charts/policy-system/files/authentik-blueprint.yaml`)
defines exactly **one** OAuth2 Provider and one Application, both with the
fixed literal name `ps-cli` — there is no Entra-style API-app-vs-CLI-app
split, because Authentik's `aud` claim is always the bare OAuth2 Provider
`client_id` (confirmed live, #128's spike, AC-BI-002 row):

| Value | Resolves to | Where it comes from |
| --- | --- | --- |
| `psService.auth.issuer` | `https://<hostname>/auth/application/o/ps-cli/` | `<hostname>` is PS Service's own resolved public DNS-label hostname (the same one its own Ingress uses); `/auth` is the path prefix Authentik's Ingress is mounted at (not a second hostname — see `docs/architecture/customer-azure-deployment.md`'s [Authentik](../architecture/customer-azure-deployment.md#authentik) section for why); `/application/o/ps-cli/` is Authentik's own fixed issuer-path shape, filled by the blueprint's Application `slug`. |
| `psService.auth.audience` | `ps-cli` | The blueprint's fixed OAuth2 Provider `client_id` literal. |
| `psService.auth.cliClientId` | `ps-cli` | The same literal — Authentik has no separate public-client registration to distinguish. |
| `psService.auth.scopes` | `openid profile email offline_access` | The blueprint's own explicit `property_mappings` on the Provider. `offline_access` is included deliberately: a blueprint-created Provider gets **no** scope mappings by default, and PS-Cli's device flow always requests `offline_access` for its refresh token — omitting it here would silently drop that scope from every login. |

`scripts/deploy-ps.sh` sets all four via `ensure_release`'s `--set`
overrides, as fixed script constants computed once the hostname resolves —
never fetched from a live API call the way the old Entra flow's audience/
scopes were. See `docs/architecture/customer-azure-deployment.md`'s [Helm
release and chart hardening
profile](../architecture/customer-azure-deployment.md#helm-release-and-chart-hardening-profile)
section for the exact `--set` list.

### Configuring PS Service manually (non-`deploy-ps.sh` Authentik instances)

If you are pointing PS Service at an Authentik instance not provisioned by
`deploy-ps.sh` (e.g. this chart's own subchart installed standalone), set the
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

### Verify

The following is a representative excerpt of a real, live end-to-end run
(full transcript: `.orchestrator/tracker/issue-129/IMPL_SLICE_11.md`) —
generate an invite, redeem it with a passkey enrollment, log in via device
flow, and present the resulting token to a protected route:

```bash
# 1. Generate a single-use invite code (admin-only -- AC-BI-007)
$ curl -X POST http://localhost:9080/api/v3/stages/invitation/invitations/ \
    -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
    -d '{"name":"example-invite","single_use":true}'
201 {"pk":"266f9e11-...", "single_use":true, ...}

# 2. Redeem it at /if/flow/ps-invite-enrollment/?itoken=<pk> -- Prompt (username/
#    name/email/password) -> User Write -> WebAuthn passkey enrollment -> logged in.
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
- **`ps-cli auth login` fails with an empty-scope rejection**: `scopes` was
  left unset or empty — see [The four values](#the-four-values) above;
  Authentik rejects an empty scope request the same way most IdPs do.
- **An invite link stops working before the intended recipient uses it**:
  Authentik's `Invitation` stage consumes a single-use code on the *first*
  request that resolves it, including a stray `curl`/prefetch, not only a
  completed enrollment — see `docs/architecture/customer-azure-deployment.md`'s
  Open Risks for the full note on distribution-channel link-prefetching.

## Optional: federate to Microsoft Entra ID

`#129 AC-BI-006` (AC-BI-\* numbering is per-issue, not global — unrelated to
any other `AC-BI-006` used elsewhere in this repo's docs): a customer who
wants their own Entra tenant's identities to log in, rather than (or in
addition to) Authentik's own local invite-registered accounts, can federate
the bundled Authentik to Entra. **This requires no `psService.auth.*` value
change at all** — confirmed live during #128's spike (AC-BI-003 row): PS
Service keeps validating tokens issued by the same fixed Authentik
issuer/OAuth2 Provider throughout; only Authentik's own login page gains a
"Sign in with Microsoft" option.

This is architecturally different from this document's old Entra worked
example (pre-#129): PS Service never trusts Entra directly, and there is no
API-app/CLI-app split to create for PS Service's own sake. The only Entra-side
artifact needed is a single app registration that Authentik itself uses as an
inbound **Source** (Authentik's own term for an external identity provider it
delegates to) — conceptually similar to registering any other application
that supports "Sign in with Microsoft," not to this doc's old
PS-Service-specific app registrations.

### Step 1 — Create an Entra app registration for Authentik's Source

1. **Microsoft Entra ID** → **App registrations** → **New registration**.
2. Name: e.g. `PS Service Authentik Federation`.
3. Supported account types: **Accounts in this organizational directory
   only** (single tenant), the typical choice for a customer-tenant
   deployment.
4. Redirect URI: Authentik's own OAuth Source callback URL for the Source
   you are about to create in Step 2 (Authentik displays this URL on the
   Source's own creation/edit page — its exact path is an Authentik-side
   detail, confirm it there rather than guessing it here).
5. **Certificates & secrets** → **New client secret** — note the secret
   value immediately; it is not retrievable again later.
6. Note the **Application (client) ID**, the client secret from step 5, and
   the tenant ID (**Microsoft Entra ID** → **Overview**).

### Step 2 — Add an OIDC Source in Authentik

In Authentik's own Admin UI (or API): **Directory** → **Federation & Social
login** → **Create** → an Entra/Azure-AD-flavored OAuth Source, supplying the
client ID/secret/tenant ID from Step 1. Confirmed live (#128's spike): adding
this Source is a single, self-contained change — the OAuth2 Provider/
Application PS-Cli and PS Service already point at is never touched, verified
by re-reading it unchanged immediately after. The exact field names/flavor
of Source Authentik offers for Entra are an Authentik-side configuration
surface this repo does not script or blueprint (deliberately out of scope —
see the issue's own "documented manual path only" framing); confirm them
against Authentik's own [Sources
documentation](https://docs.goauthentik.io/users-sources/sources/) at
configuration time rather than assuming this doc's paraphrase is exact.

### Step 3 — Verify

Reload Authentik's login page — a "Sign in with Microsoft" (or the Source's
configured display name) button should appear alongside the existing
username/password/passkey form, with no Authentik restart and no PS Service
redeploy. Confirmed live (#128's spike, AC-BI-004 row): a pre-existing local
(invite-registered) account continued logging in via password/passkey with
no disruption after the Source was added — adding Entra federation is
additive, not a replacement for local accounts, unless separately configured
to be.

### Notes carried over from the pre-#129 Entra flow

- **`AADSTS650052` ("lacks a service principal")**: if you script Step 1 via
  Graph API/`az ad app create` instead of the Portal, `POST /applications`
  creates only the **application** object, not its tenant **service
  principal** — run `az ad sp create --id <app-id>` afterward, same as any
  other Entra app registration created this way.
- This appendix does **not** carry over the old worked example's
  `aud`/`api://` audience-format pitfalls — those were specific to PS
  Service trusting Entra tokens directly, which no longer happens by
  default. Federation only ever produces Authentik-issued tokens for PS
  Service to validate.
