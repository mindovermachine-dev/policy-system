# IdP Configuration Contract

PS Service is a generic OIDC resource server (issue #58): it validates bearer
tokens against **any** OIDC-compliant identity provider via standard OIDC
discovery — there is no provider-specific code. A deployment supplies up to
four configuration values (as `PS_AUTH_*` environment variables, or as the
corresponding `psService.auth.*` Helm values — see the
[Helm Chart Values Reference](./helm-chart-values-reference.md)). This page
documents what each value is, what IdP-side artifact it corresponds to, and a
worked example for Microsoft Entra ID — the reference IdP for Azure-hosted
customers.

If neither this configuration nor the local-test bypass
(`PS_SERVICE_LOCAL_TEST_BYPASS=true`, evaluation only — never for a
network-reachable deployment) is present, PS Service refuses to start.

## The four values

| Value | Env var | Helm value | What it is |
| --- | --- | --- | --- |
| Issuer | `PS_AUTH_ISSUER` | `psService.auth.issuer` | The OIDC authorization server's base URL. PS Service fetches `<issuer>/.well-known/openid-configuration` from it at startup to discover the JWKS endpoint and the signing algorithms it trusts, and every presented token's `iss` claim must match this value exactly (character for character). |
| Audience | `PS_AUTH_AUDIENCE` | `psService.auth.audience` | The identifier of PS Service itself as an OIDC *resource server* — i.e. the API app registration's own identifier, never the CLI client's. Every presented token's `aud` claim must match this value exactly. |
| CLI client id | `PS_AUTH_CLI_CLIENT_ID` | `psService.auth.cliClientId` | The **public client** app registration that PS-Cli authenticates as (device-authorization flow, issue #57). Optional: only needed so PS Service can advertise it in the `/.well-known/oauth-protected-resource` metadata as `ps_cli_client_id`, letting a client discover which client id to use without being told out of band. |
| Scopes | `PS_AUTH_SCOPES` | `psService.auth.scopes` | The OAuth scope(s) (space- or comma-separated) that a client should request when logging in — e.g. the `access_as_user` delegated scope on the API app registration. **Not used for token validation** (PS Service checks `aud`, not `scope`), but **required in practice**: PS-Cli's device-authorization login (issue #57) sources its OAuth `scope` request parameter directly from this value, via `/.well-known/oauth-protected-resource`'s `scopes_supported`. Leaving it unset makes PS Service advertise an empty scope list, which most IdPs — Entra included (`AADSTS900144`) — reject outright, so `ps-cli auth login` fails against any deployment that omits it. |

Only `issuer` and `audience` are required for PS Service to validate tokens at
all; `cliClientId` is a convenience for client discovery. `scopes` is likewise
optional from PS Service's own validation standpoint, but omitting it breaks
PS-Cli login end-to-end — treat it as required for any deployment a CLI or
Claude Desktop client will actually log into.

## Worked example: Microsoft Entra ID

This walkthrough creates the two Entra app registrations PS Service's OIDC
contract needs — an **API app registration** (the resource server PS Service
represents) and a **public client app registration** (what PS-Cli
authenticates as) — and shows exactly which value from each becomes which
`psService.auth.*` setting. It assumes an Azure AD / Entra ID tenant and a
user with the **Application Administrator** (or **Cloud Application
Administrator**, or Global Administrator) directory role — subscription-level
Contributor alone is not sufficient to create app registrations.

Substitute your own tenant's values wherever `<...>` appears; nothing below
depends on a specific tenant.

### Step 1 — Find your tenant ID

1. In the Azure Portal, go to **Microsoft Entra ID** → **Overview**.
2. Copy the **Tenant ID** shown there. This is `<tenant-id>` for every step
   below.

### Step 2 — Create the API app registration (the resource server)

1. **Microsoft Entra ID** → **App registrations** → **New registration**.
2. Name: e.g. `Policy System API`.
3. Supported account types: **Accounts in this organizational directory
   only** (single tenant) — the typical choice for a customer-tenant
   deployment.
4. Redirect URI: leave blank. This app registration never signs a user in
   interactively; it only represents PS Service as a protected resource.
5. Click **Register**.
6. Open the new registration's **Expose an API** blade.
7. Next to **Application ID URI**, click **Add** (or **Set**). Accept the
   offered default (`api://<api-app-client-id>`) or set a custom identifier
   URI — either is fine, and this URI form (`<Application ID URI>/access_as_user`)
   is what a client requests as its OAuth *scope*. It is **not** what becomes
   `psService.auth.audience` — see Step 10.
8. Still on **Expose an API**, click **Add a scope** and fill in:
   - **Scope name**: `access_as_user`
   - **Who can consent**: Admins and users
   - **Admin consent display name**: e.g. `Access Policy System as the signed-in user`
   - **Admin consent description**: e.g. `Allows PS-Cli to call Policy System on behalf of the signed-in user`
   - **State**: **Enabled**
9. Click **Add scope**.
10. Note the **Application (client) ID** from the **Overview** blade (the bare
    GUID, *not* the Application ID URI from step 7) → this becomes
    `psService.auth.audience`. Confirmed against a real tenant: for the
    self-referencing `api://<own-client-id>` URI pattern, Entra normalizes a
    device-flow-issued token's `aud` claim to the bare client ID GUID, not the
    URI — configuring `psService.auth.audience` as the URI causes every real
    token to be rejected (`aud` mismatch) even though login itself succeeds.
    A custom (non-self-referencing) Application ID URI may not have this
    quirk; verify against your own tenant before assuming otherwise. Also
    note the tenant ID from Step 1. The OIDC discovery issuer for an Entra v2
    tenant is:
    ```
    https://login.microsoftonline.com/<tenant-id>/v2.0
    ```
    This full URL, exactly as written (including the `/v2.0` suffix — Entra
    v2 tokens' `iss` claim is this exact string), becomes
    `psService.auth.issuer`.

### Step 3 — Create the public client app registration (PS-Cli)

1. **Microsoft Entra ID** → **App registrations** → **New registration**.
2. Name: e.g. `Policy System CLI`.
3. Supported account types: same tenant as Step 2.
4. Redirect URI: leave blank for now (added in the next step).
5. Click **Register**.
6. Open the new registration's **Authentication** blade.
7. Click **Add a platform** → **Mobile and desktop applications**.
8. Check the suggested redirect URI
   `https://login.microsoftonline.com/common/oauth2/nativeclient` (or add it
   manually if not offered), then **Configure**.
9. Still on **Authentication**, under **Advanced settings**, set **Allow
   public client flows** to **Yes**, then **Save**. This is what permits the
   OAuth 2.0 Device Authorization Grant (device-code flow) for this
   registration — without it, device-code login is rejected by Entra
   regardless of anything PS-Cli does.
10. Open **API permissions** → **Add a permission** → **My APIs** → select
    `Policy System API` (Step 2's registration) → **Delegated permissions**
    → check `access_as_user` → **Add permissions**.
11. Still on **API permissions**, click **Grant admin consent for
    `<tenant-name>`** and confirm. This is the "pre-consent" step: without it,
    every device-code login would stop on an interactive consent prompt the
    first time, which is not itself a failure of PS Service's contract but
    does turn PS-Cli login into a two-step, admin-gated process instead of a
    one-step device-code flow — grant it here so the flow works end to end.
12. Open the **Overview** blade and note the **Application (client) ID** —
    this becomes `psService.auth.cliClientId`.

### Step 4 — Configure PS Service with the four values

```bash
helm upgrade --install policy-system oci://ghcr.io/mindovermachine-dev/charts/policy-system \
  -f values-prod.yaml \
  --set psService.auth.issuer=https://login.microsoftonline.com/<tenant-id>/v2.0 \
  --set psService.auth.audience=<api-app-client-id> \
  --set psService.auth.cliClientId=<cli-app-client-id> \
  --set psService.auth.scopes=api://<api-app-client-id>/access_as_user
```

(Or set the equivalent `PS_AUTH_ISSUER` / `PS_AUTH_AUDIENCE` /
`PS_AUTH_CLI_CLIENT_ID` environment variables directly, if not deploying via
the Helm chart.)

On the next `helm template`/`helm lint`/`helm upgrade`, the chart's
fail-closed render guard (only relevant when
`psService.localTestBypass.enabled=false`, the chart's default) is satisfied
by these two values being set, and PS Service's own startup performs OIDC
discovery against the issuer above — confirming it can reach
`https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration`
and that the response advertises a usable JWKS endpoint and at least one
trusted (asymmetric) signing algorithm. Entra's discovery document advertises
`RS256` by default, which PS Service trusts.

### Step 5 — Verify

With PS-Cli's own device-code login (issue #57) obtaining a token for the
`access_as_user` scope against the CLI client registration from Step 3, a
request presenting that token to a protected PS Service route should return
`200`; the same request with no `Authorization` header should return `401`.
Recording this pair of curl runs against a real tenant is the manual
verification step this contract exists to make possible (tracked on issue
#58 as AC-BI-018/AC-BI-019) — this document's job is only to make every step
up to that point reproducible without guesswork.

### Common pitfalls

- **Audience mismatch**: the token's `aud` claim must equal
  `psService.auth.audience` exactly. For the self-referencing
  `api://<own-client-id>` Application ID URI pattern this walkthrough uses,
  Entra sets `aud` to the **bare client ID GUID**, not the `api://...` URI —
  confirmed against a real tenant (Step 10). Decode a real token
  (`jwt.io` or any base64-JSON decode of its payload segment) and compare
  its `aud` to `psService.auth.audience` verbatim if login succeeds but every
  API call still 401s.
- **Issuer mismatch**: Entra issues both v1 (`https://sts.windows.net/<tenant-id>/`)
  and v2 (`https://login.microsoftonline.com/<tenant-id>/v2.0`) tokens
  depending on how the client requests them. `psService.auth.issuer` must
  match the `iss` claim the CLI's token actually carries — use the v2
  endpoint above unless the CLI client registration is explicitly configured
  for v1 tokens.
- **Device-code flow rejected outright**: almost always means Step 3.9
  (**Allow public client flows: Yes**) was skipped.
- **Consent prompt blocks non-interactive login**: means Step 3.11 (admin
  consent) was skipped or granted against the wrong app registration.
- **Scripting Steps 2–3 via Graph API/`az ad app create` instead of the
  Portal**: two Portal behaviors are easy to miss when scripting this
  worked example instead of clicking through it:
  - **`AADSTS650052` ("lacks a service principal")** on first login:
    `POST /applications` (what `az ad app create` calls) creates only the
    **application** object, not its tenant **service principal** — the
    Portal's "New registration" wizard creates both. Run
    `az ad sp create --id <app-id>` for each app registration (API,
    CLI, and any connector app) after creating it via the API/CLI.
  - **v1 tokens issued despite configuring `psService.auth.issuer` for
    v2**: a Graph-API-created app registration's
    `api.requestedAccessTokenVersion` defaults to unset (v1); the Portal's
    "Expose an API" flow sets it to v2 automatically. Set it explicitly —
    `az rest --method PATCH --uri https://graph.microsoft.com/v1.0/applications/<object-id> --body '{"api":{"requestedAccessTokenVersion":2}}'`
    — and expect roughly a minute of propagation delay before it takes
    effect. Once on v2, the token's `aud` claim is the bare app ID
    (`<api-app-client-id>`), **not** the `api://<api-app-client-id>` form
    — set `psService.auth.audience` to match whichever form the token
    actually carries, not whichever form was set as the identifier URI.
