# Helm Chart Values Reference

Every operator-facing key in `charts/policy-system/values.yaml` (local-test default) and
`charts/policy-system/values-prod.yaml` (production override file, passed via
`-f values-prod.yaml`). See the [Installation Guide](./installation-guide.md)'s
[Evaluator installation](./installation-guide.md#evaluator-installation) walkthrough for
how to deploy the chart in the first place — this page is the values reference for
that walkthrough, step 6 onward. The evaluator and production scripts
(`scripts/deploy-ps-eval.sh`, `scripts/deploy-ps-prod.sh`) set most of the Authentik-related
keys below for you; you only set them by hand when deploying the chart without a script.

## Table of Contents

- [Core values](#core-values)
- [Example: `helm upgrade`](#example-helm-upgrade)
- [Ollama values](#ollama-values)
- [Azure values](#azure-values)
- [PS Postgres values](#ps-postgres-values)
- [Authentik credentials values](#authentik-credentials-values)
- [Authentik and local TLS values](#authentik-and-local-tls-values)

## Core values

| Key | Default (local-test) | Purpose |
| --- | --- | --- |
| `nameOverride` / `fullnameOverride` | `""` / `""` | Standard Helm naming knobs (`templates/_helpers.tpl`): `nameOverride` replaces the chart name inside `<release>-<chart>` resource names; `fullnameOverride` replaces that whole prefix. Both truncated to 63 chars. Leave empty unless two releases must coexist in one namespace. |
| `psService.image.repository` | `ghcr.io/mindovermachine-dev/ps-service` | PS Service image. |
| `psService.image.tag` | `""` (falls back to `Chart.appVersion`) | PS Service image tag. Empty by default — the chart's own `appVersion` (kept in lockstep with the image by the release job) is used unless you pin an explicit tag with `--set`/`-f`. |
| `psService.service.type` | `NodePort` (`ClusterIP` in prod) | PS Service Service type. `NodePort` is what `deploy/kind/cluster.yaml`'s `extraPortMappings` targets locally; prod has no kind-specific reachability mechanism, so it's `ClusterIP`-only there. |
| `psService.service.nodePort` | `30800` | Fixed NodePort behind host port `8000` (via `extraPortMappings`). Not set in prod (no `nodePort` field when `type: ClusterIP`). |
| `psService.companyMerge.similarityThreshold` | `0.59` | `PS_COMPANYMERGE_SIMILARITY_THRESHOLD` — fuzzy-match threshold for company entity merging. Empirically recommended by issue #29's labeled precision/recall/F1 sweep, not an undocumented judgment call. |
| `psService.localTestBypass.enabled` | `false` | `PS_SERVICE_LOCAL_TEST_BYPASS` — opt-in auth bypass. Off by default and **not used by the evaluator install**: the bypass refuses to bind a non-loopback host and every container image binds `0.0.0.0`, so it cannot start in a container. The evaluator profile runs real OIDC login against the bundled Authentik instead. |
| `psService.auth.issuer` | `""` | `PS_AUTH_ISSUER` — the OIDC authorization-server URL (for the bundled Authentik: production `https://<hostname>/auth/application/o/ps-cli/`; evaluator `https://<hostname>:30443/application/o/ps-cli/`). Required unless `psService.localTestBypass.enabled=true`; see the [IdP configuration contract](./idp-configuration-contract.md). |
| `psService.auth.audience` | `""` | `PS_AUTH_AUDIENCE` — the resource-server audience (for the bundled Authentik, the fixed OAuth2 Provider's `client_id`, `ps-cli`). Required unless `psService.localTestBypass.enabled=true`. |
| `psService.auth.cliClientId` | `""` | `PS_AUTH_CLI_CLIENT_ID` — the public CLI client's id (for the bundled Authentik, `ps-cli`). Optional; when set it is advertised in the `/.well-known/oauth-protected-resource` metadata as `ps_cli_client_id`. |
| `psService.auth.scopes` | `""` | `PS_AUTH_SCOPES` — space/comma-separated OAuth scope(s) a client should request at login (for the bundled Authentik, `openid profile email offline_access`). Not used for token validation, but required in practice: PS-Cli's device-flow login sources its OAuth `scope` request directly from this value via `scopes_supported`, and most IdPs reject an empty scope. See the [IdP configuration contract](./idp-configuration-contract.md). |

When `psService.localTestBypass.enabled=false` (the chart's default) and either `psService.auth.issuer` or `psService.auth.audience` is unset, `helm template`/`helm lint` fail with a message naming both values and the bypass alternative — this is the chart's fail-closed render guard, not a bug. See the [IdP configuration contract](./idp-configuration-contract.md) for what each value means and a worked Authentik example.
| **`llm.provider`** | `azure` (`ollama` still available via `--set llm.provider=ollama`) | **(AC-BI-003)** Selects the LLM backend: `ollama` or `azure`. Drives `PS_LLMINTERFACE_MODEL`/`PS_LLMINTERFACE_EMBED_MODEL` and whether a Secret renders. |
| **`llm.model`** | `""` → `gpt-5.4-mini` (`azure`) / `phi3:mini` (`ollama`) | Chat model as a **bare name** — an Azure deployment name or an Ollama model tag — with **no `<provider>/` prefix**; the chart prepends `llm.provider/` itself to build `PS_LLMINTERFACE_MODEL`. Leave empty for the provider default shown. A name YAML would type as a number or boolean (e.g. `0`, `false`) must be quoted in a values file or passed with `--set-string`, or the chart treats it as unset. |
| **`llm.embedModel`** | `""` → `text-embedding-3-large` (`azure`) / `nomic-embed-text` (`ollama`) | Embedding model, same bare-name / no-prefix rule as `llm.model`; becomes `PS_LLMINTERFACE_EMBED_MODEL`. |
| **`llm.existingSecret`** | `""` | **(AC-BI-003)** Set to reuse an operator-managed Secret name instead of `llm.azure.*` below. |
| **`llm.azure.apiKey`** | `""` | **(AC-BI-003)** Azure API key. Never set a real value here in a committed file — pass via `--set` or use `llm.existingSecret`. Rendered into a Kubernetes `Secret` (`templates/secret.yaml`), never a ConfigMap or plaintext env var. |
| **`llm.azure.apiBase`** | `""` | **(AC-BI-003)** Azure API base URL. Same secret-backed handling as `apiKey`. |
| `llm.azure.apiVersion` | `"preview"` | `AZURE_API_VERSION` — fixed literal matching the reference deployment (see [Customer-Managed Azure LLM Bootstrap](../architecture/customer-azure-llm-bootstrap.md)); not derived from the deployment, not treated as a secret. |
| **`falkordb.persistence.enabled`** | `true` (both profiles) | **(AC-BI-008)** Toggles FalkorDB storage between a `PersistentVolumeClaim` (default) and an `emptyDir` (ephemeral — data lost on pod restart). No manual manifest edits needed — flip via `--set`/`-f` and `helm upgrade`. |
| `falkordb.persistence.storageClassName` | `""` | Empty string = let the cluster pick its own default StorageClass. Never hardcoded to kind's default StorageClass name (both profiles) — override explicitly for a real cluster if needed. |
| `falkordb.browser.enabled` | `true` (`false` in prod) | **(AC-BI-009)** FalkorDB Browser UI Service. On by default for local-test convenience, off in prod. |
| `falkordb.browser.nodePort` | `30300` | Fixed NodePort behind host port `3001` (via `extraPortMappings`). Only applies when `falkordb.browser.enabled=true`. |
| `falkordb.image.repository` / `falkordb.image.tag` | `falkordb/falkordb` / `latest` | FalkorDB image. |

Immediately after installing, `falkordb.persistence.enabled` (on by default — data
survives a pod restart) and the `llm.*` keys (which provider, and how its credentials
reach the pod) are the two settings worth double-checking against your intended setup.
Persistent storage means the PVC needs a StorageClass available in your cluster; a
default `kind` cluster provisions one automatically, so this works out of the box locally
too. See the Operations Guide's [Backup](./operations-guide.md#backup) section for backing up
that volume once persistence is on.

## Example: `helm upgrade`

```bash
# Example: disable persistent storage for a disposable evaluation run
helm upgrade policy-system ./charts/policy-system \
  --set falkordb.persistence.enabled=false \
  --wait
```

PS Service's Deployment is untouched by this upgrade — only FalkorDB's
Deployment/PVC change — so an in-flight PS Service pod is not restarted just because
you changed a FalkorDB-only value.

## Ollama values

Azure is the default LLM provider for both the **local-test** profile and the production
profile (`values-prod.yaml`) — see [Azure values](#azure-values) below. Ollama is
**opt-in**: pass `--set llm.provider=ollama` to run entirely on your own machine, no
API key needed — see [Core values](#core-values) above for switching providers.

If Ollama runs on your Podman host (not in-cluster), PS Service's pods cannot resolve
`host.containers.internal` on their own — Podman only injects that hostname into the kind
node container's own `/etc/hosts`, not into a Pod's separate network namespace. Look up
your Podman network's gateway IP and pass it along:

```bash
podman network inspect podman --format '{{(index .Subnets 0).Gateway}}'
# commonly 10.88.0.1 on a default rootful install
```

```bash
helm install policy-system ./charts/policy-system \
  --set llm.provider=ollama \
  --set psService.ollamaHostGatewayIP=10.88.0.1 \
  --wait
```

Without `psService.ollamaHostGatewayIP` set, PS Service cannot reach Ollama. `/ready` still
reports `"status": "ready"` — readiness reflects FalkorDB only since issue #75 — but lists
`llm_interface` under `unhealthy_dependencies`, and `ps-cli ingest …` / `check regulations`
stop at their pre-flight check with `LLM Interface is unavailable.`. See
[Azure values](#azure-values) for the same verification step.

| Key | Default (local-test) | Purpose |
| --- | --- | --- |
| `psService.ollamaHostGatewayIP` | `""` | IP of the Podman network gateway, used to render a `hostAliases` entry so pods can resolve `host.containers.internal` when `llm.provider=ollama`. Empty by default — the chart can't know this statically. See the Podman host-networking note at the top of this section. |
| `llm.ollama.apiBase` | `"http://host.containers.internal:11434"` | `OLLAMA_API_BASE` — set only when `llm.provider=ollama` and non-empty. |

## Azure values

**Local test: kind cluster against Azure.** Azure is now the local-test profile's
default LLM provider (see [Ollama values](#ollama-values) for the Ollama opt-in). To
run the same kind cluster against Azure — for example to use the provider and models
production runs on — supply credentials, and (optionally) name your own deployments with
the bare-name keys from [Core values](#core-values). Azure is also the only provider
compatible with the curated content shipped in the repo (its embeddings were produced
with `text-embedding-3-large`; re-embedding for another provider is out of scope for
issue #103).

**Recommended: provision real credentials with the bootstrap scripts.** Rather than
hand-copying keys from the Azure Portal, run `scripts/deploy-llm.sh` against your own
Azure subscription — it creates the resource group, `AIServices` account, both model
deployments, and a Key Vault, and stores the resulting credentials there. Then run
`scripts/sync-llm-secrets-to-kind.sh` to read them back out of Key Vault and write them
into your active `kind` cluster as the `policy-system-llm-credentials` Secret, ready for
`--set llm.existingSecret=policy-system-llm-credentials` below. See
[Customer-Managed Azure LLM Bootstrap](../architecture/customer-azure-llm-bootstrap.md)
for the full design. The rest of this section covers bringing your own credentials
instead.

Credentials never live in a committed file. The chart renders `AZURE_API_KEY` /
`AZURE_API_BASE` into a Kubernetes `Secret` (`templates/secret.yaml`) from
`llm.azure.apiKey` / `llm.azure.apiBase` and mounts it with `envFrom`, so pass them
with `--set` from your shell:

```bash
helm install policy-system ./charts/policy-system \
  --set llm.provider=azure \
  --set llm.azure.apiKey="$AZURE_API_KEY" \
  --set llm.azure.apiBase="$AZURE_API_BASE" \
  --set llm.model=my-chat-deployment \
  --set llm.embedModel=my-embedding-deployment \
  --wait
```

`llm.model` / `llm.embedModel` are the Azure **deployment names** with no `azure/`
prefix — the chart renders `PS_LLMINTERFACE_MODEL=azure/my-chat-deployment` and
`PS_LLMINTERFACE_EMBED_MODEL=azure/my-embedding-deployment`. Omit both to get the
defaults (`gpt-5.4-mini` / `text-embedding-3-large`).

If you would rather manage the Secret yourself, create it first and point the chart at
it with `llm.existingSecret` (the chart then renders no Secret of its own):

```bash
kubectl create secret generic my-llm-credentials \
  --from-literal=AZURE_API_KEY="$AZURE_API_KEY" \
  --from-literal=AZURE_API_BASE="$AZURE_API_BASE"
```

```bash
helm install policy-system ./charts/policy-system \
  --set llm.provider=azure \
  --set llm.existingSecret=my-llm-credentials \
  --set llm.model=my-chat-deployment \
  --set llm.embedModel=my-embedding-deployment \
  --wait
```

`psService.ollamaHostGatewayIP` is not needed under Azure — no `hostAliases` entry is
rendered.

Verify what reached the pod, then check readiness. `/ready`'s `status` reflects FalkorDB
only (issue #75), so `helm install --wait` succeeding and `"status": "ready"` do **not**
prove the Azure credentials or deployment names work. The LLM signal is the
`unhealthy_dependencies` list in the same response: at startup PS Service makes one real
completion call with `PS_LLMINTERFACE_MODEL` and one real embedding call with
`PS_LLMINTERFACE_EMBED_MODEL`, and if either fails `llm_interface` is listed there.

```bash
kubectl exec deploy/policy-system-ps-service -- env | grep '^PS_LLMINTERFACE_'
```

```bash
curl -s http://127.0.0.1:8000/ready
# healthy: {"status":"ready","unhealthy_dependencies":[]}
# "llm_interface" listed: wrong key, base URL or deployment name — fix the --set
# values and `helm upgrade`; the restarted pod re-probes at startup.
```

`ps-cli ingest regulation` / `ingest document` / `check regulations` run the same check
as a pre-flight and stop with `LLM Interface is unavailable.` while `llm_interface` is
listed.

## PS Postgres values

PS Service keeps its durable state in one PostgreSQL server (issue #130) holding two
databases, each with its own least-privilege role: `ps_state` (audit events, authz,
runtime configuration, ingestion run status) and `ps_signing` (Passkey Signing: pending merge approvals and
enrolled WebAuthn credentials). It is **distinct from FalkorDB and from Authentik's own
bundled Postgres** (AC-BI-006): its own Deployment/Service/PVC/NetworkPolicy/Secrets,
never a shared PVC, NetworkPolicy selector, or credential Secret with either. Unlike
`authentik.*` (opt-in via `authentik.enabled`), it is always deployed, the same
"always-on" posture as `falkordb.*`.

On first start (empty data directory only) `files/ps-postgres-init.sh` creates both roles
and databases, revokes `CONNECT` on both databases from `PUBLIC`, grants it only to each
database's own role, and revokes `CREATE` on schema `public` from `PUBLIC` in each. The
Postgres image does not re-run it against an existing volume, and changing a Secret later
does not change an existing role's password (run `ALTER ROLE` by hand).

| Key | Default (local-test) | Purpose |
| --- | --- | --- |
| `psPostgres.image.repository` / `psPostgres.image.tag` | `postgres` / `16-alpine` | Image — same version family as `.devcontainer/docker-compose.yml`'s local dev `postgres` service. |
| `psPostgres.admin.user` | `postgres_admin` | Superuser that runs the init script. Its Secret is consumed only by the Postgres container; the `ps-service` pod never references it. |
| `psPostgres.state.database` / `psPostgres.state.user` | `ps_state` / `ps_state` | `PS_STATE_POSTGRES_DATABASE` / `PS_STATE_POSTGRES_USER` — plain (non-secret) env vars on `ps-service`, and the names the init script creates. |
| `psPostgres.signing.database` / `psPostgres.signing.user` | `ps_signing` / `ps_signing` | `PS_PASSKEYSIGNING_POSTGRES_DATABASE` / `PS_PASSKEYSIGNING_POSTGRES_USER` — same wiring for Passkey Signing. |
| `psPostgres.state.graphOwnerRole` | `ps_state_graph_owner` | Name of the non-login role that owns the insert-only `graph_log` tables (issue #205). Created by the init script on first start and by the provisioning Job on an existing cluster; `ps_state` is never a member of it. Passed to the Job as `PS_STATE_GRAPH_OWNER_ROLE`. Never reuse the state role's name. |
| `psPostgres.provisioning.enabled` | `true` | Renders the `ps-state-provision` Job that creates and migrates the `graph_log` tables with the admin credential, on fresh installs and on every `helm upgrade`, and admits it through the Postgres NetworkPolicy. Set `false` only if you run `python -m ps_service.graph_gateway.provision` yourself: PS Service fails closed at startup until the tables exist. Upgrades must pass `--wait --wait-for-jobs` so a failed Job fails the upgrade. |
| `psPostgres.provisioning.backoffLimit` / `psPostgres.provisioning.activeDeadlineSeconds` | `10` / `900` | Retry budget and overall deadline of that Job. Retries are generous because the first attempts can precede the audit tables PS Service's own startup migrations create. |
| **`psPostgres.admin.existingSecret`** / **`psPostgres.state.existingSecret`** / **`psPostgres.signing.existingSecret`** | `""` | **(AC-BI-002/AC-BI-007, issue #159)** When a value is unset (the default in both profiles), the chart generates and persists that credential itself via a `lookup`+`randAlphaNum` idiom (`templates/ps-postgres-secret.yaml`; Secrets `<fullname>-ps-postgres-{admin,state,signing}-credentials`, e.g. `policy-system-ps-postgres-{admin,state,signing}-credentials` for release `policy-system`; `<fullname>` is the release name when it already contains `policy-system`, else `<release>-policy-system`), reused verbatim — no Key Vault or other external call — on every subsequent `helm upgrade`. Set to reuse an operator-managed Secret name instead; each value suppresses only its own Secret. Required keys: `POSTGRES_PASSWORD` (admin), `PS_STATE_POSTGRES_PASSWORD` (state), `PS_PASSKEYSIGNING_POSTGRES_PASSWORD` (signing). |
| `psPostgres.persistence.size` | `10Gi` | PVC storage request for the data volume. |
| `psPostgres.persistence.storageClassName` | `""` | Empty string = let the cluster pick its own default StorageClass. Only consulted when `durableStorageClass.enabled` below is `false`. |
| `psPostgres.persistence.durableStorageClass.enabled` | `false` (`true` in prod) | Toggles a dedicated Premium SSD, Retain-reclaim StorageClass for this PVC (mirrors `falkordb.persistence.durableStorageClass` / `authentik.postgres.persistence.durableStorageClass` exactly). |

The provisioning Job (never the `ps-service` pod) receives `PS_STATE_POSTGRES_HOST`, `_PORT`,
`_DATABASE`, `_USER` (target and application role), `PS_STATE_GRAPH_OWNER_ROLE` (from
`psPostgres.state.graphOwnerRole`), and the admin credential as `PS_STATE_ADMIN_POSTGRES_USER`
(from `psPostgres.admin.user`) and `PS_STATE_ADMIN_POSTGRES_PASSWORD` (from the admin Secret's
`POSTGRES_PASSWORD`). `PS_STATE_PROVISION_CONNECT_TIMEOUT_SECONDS` (default `120`) bounds how
long the command waits for the server to accept connections. These variables are read only by
`python -m ps_service.graph_gateway.provision`; `ps-service` ignores them and never holds the admin
credential.

`ps-service`'s Deployment consumes the server via ten env vars, five per database:
`PS_STATE_POSTGRES_*` and `PS_PASSKEYSIGNING_POSTGRES_*` — `_HOST` (the chart's own
rendered Service name, identical for both, never a pod IP), `_PORT` (fixed `5432`),
`_DATABASE`, `_USER` (plain values above), and `_PASSWORD` (always from that role's own
Secret, never a plain env value). These map onto `ServiceConfig`'s `state_postgres_*` and
`passkey_signing_postgres_*` fields.

```bash
# Example: point the chart at operator-managed Secrets instead of the local-test default
kubectl create secret generic my-ps-state-secret \
  --from-literal=PS_STATE_POSTGRES_PASSWORD="$STATE_POSTGRES_PASSWORD"
kubectl create secret generic my-ps-signing-secret \
  --from-literal=PS_PASSKEYSIGNING_POSTGRES_PASSWORD="$SIGNING_POSTGRES_PASSWORD"
helm upgrade policy-system ./charts/policy-system \
  --set psPostgres.state.existingSecret=my-ps-state-secret \
  --set psPostgres.signing.existingSecret=my-ps-signing-secret \
  --wait
```

## Authentik credentials values

| Key | Default (local-test) | Purpose |
| --- | --- | --- |
| **`authentik.credentialsExistingSecret`** | `""` | **(AC-BI-001/AC-BI-005/AC-BI-007, issue #159)** When unset (the default in both profiles), the chart generates and persists Authentik's `AUTHENTIK_SECRET_KEY`/`AUTHENTIK_POSTGRESQL__PASSWORD` itself via a `lookup`+`randAlphaNum` idiom (`templates/authentik-credentials-secret.yaml`), reused verbatim — no Key Vault or other external call — on every subsequent `helm upgrade`. Set to reuse an operator-managed Secret name instead. This is a **separate** value from `authentik.authentik.existingSecret.secretName` below — the upstream `authentik` sub-chart's own routing switch — and when set, it must be given the **same** Secret name as that value, or the render fails with a `fail()` guard naming both values. |
| `authentik.authentik.existingSecret.secretName` | `"policy-system-authentik-credentials"` (never empty) | The upstream `authentik` dependency's own switch for which Secret its server/worker Deployments consume via `envFrom`. Always carries this deterministic literal (`policy-system.authentikCredentialsSecretName`, `templates/_helpers.tpl`) unless overridden together with `authentik.credentialsExistingSecret` above — an empty value here makes the upstream chart fall back to a *different*, sub-chart-owned Secret name this chart never populates. |

```bash
# Example: point the chart at an operator-managed Secret instead of letting it
# generate its own Authentik credentials Secret. Both values below must match.
kubectl create secret generic my-authentik-secret \
  --from-literal=AUTHENTIK_SECRET_KEY="$AUTHENTIK_SECRET_KEY" \
  --from-literal=AUTHENTIK_POSTGRESQL__PASSWORD="$AUTHENTIK_POSTGRES_PASSWORD" \
  --from-literal=AUTHENTIK_POSTGRESQL__HOST=policy-system-authentik-postgres \
  --from-literal=AUTHENTIK_POSTGRESQL__PORT=5432 \
  --from-literal=AUTHENTIK_POSTGRESQL__USER=authentik \
  --from-literal=AUTHENTIK_POSTGRESQL__NAME=authentik
helm upgrade policy-system ./charts/policy-system \
  --set authentik.enabled=true \
  --set authentik.credentialsExistingSecret=my-authentik-secret \
  --set authentik.authentik.existingSecret.secretName=my-authentik-secret \
  --wait
```

## Authentik and local TLS values

Keys that wire PS Service to the bundled Authentik and, in the evaluator profile, give it a
locally trusted HTTPS certificate (issue #165). Both deploy scripts set the ones marked
"script" in one Helm release; see the [IdP configuration
contract](./idp-configuration-contract.md) for what they mean.

| Key | Default | Purpose |
| --- | --- | --- |
| `psService.authzBootstrapOwner.subject` / `.issuer` | `""` / `""` | `PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT` / `_ISSUER` — the `(sub, iss)` allowed to claim `SystemOwner`. **Script:** the owner's email (the bundled provider's `sub` is the username, which the scripts make equal to the email) and the Authentik issuer, in the same deploy as everything else. Required (render fails) whenever the bypass is off. |
| `psService.authentik.existingSecret` | `""` | Reuse an operator-managed Secret (key `PS_AUTHENTIK_API_TOKEN`) as PS Service's Authentik API token instead of the chart-generated one (`<release>-authentik-api-token`, 64 random characters, generated once and kept across upgrades). When `authentik.enabled`, also set the `secretKeyRef` name of `AUTHENTIK_BOOTSTRAP_TOKEN` in `authentik.server.env` and `authentik.worker.env` to the same name, or the render fails. There is **no** `psService.authentik.apiToken` value any more (the old fixed placeholder is gone), and a Secret still holding it is replaced by a generated token. |
| `psService.authentik.baseUrl` | `https://authentik.example.com` | `PS_AUTHENTIK_BASE_URL` — where PS Service calls Authentik's API for `invite_user`. **Script:** evaluator `https://<hostname>:30443` (trusted through `SSL_CERT_FILE`); production `http://policy-system-authentik-server/auth`, the in-cluster Service, because the invitation API is not on the public Ingress. |
| `psService.authentik.publicUrl` | `""` | `PS_AUTHENTIK_PUBLIC_URL` — optional user-reachable Authentik address used **only** to build the invitee's enrolment link. Not rendered when empty. **Script:** unset for the evaluator; production `https://<hostname>/auth`. Needs a PS Service image that supports it (see the [Installation Guide's Verification status](./installation-guide.md#verification-status)); older images ignore the variable. |
| `psService.authentikHostname` / `psService.authentikHostAliasIP` | `""` / `""` | Both set: PS Service's pod resolves that hostname to that IP (a `hostAliases` entry), so its token validation reaches the same Authentik a browser reaches on the kind node's port. **Script (evaluator):** the `--hostname` and the kind node container's IP. Unused in production. |
| `localTls.secretName` | `""` | Name of a Secret with keys `tls.crt`, `tls.key` and `ca.crt` (created by `scripts/deploy-ps-eval.sh`, never by the chart). Empty = off and nothing below renders. When set, the chart adds a blueprint registering the certificate as Authentik's web certificate and gives PS Service a trust bundle (system CAs plus `ca.crt`) through an init container and `SSL_CERT_FILE`. The init container uses the PS Service image itself. The Authentik pods' own mount of the Secret at `/ps-tls` is supplied by the installer through `authentik.global.volumes` / `authentik.global.volumeMounts` (a values file cannot be conditional). |
| `authentik.server.service.nodePortHttp` | `30080` | Authentik's plain-HTTP NodePort. **Not** mapped to the host by `deploy/kind/cluster.yaml`; reachable only inside the kind node. Unused in production (`ClusterIP`). |
| `authentik.server.service.nodePortHttps` | `30443` | Authentik's HTTPS NodePort, and the one Authentik port `deploy/kind/cluster.yaml` maps to the host (`0.0.0.0:30443`, so it is reachable from the LAN by design). Changing the port on an existing kind cluster needs a cluster recreate (port mappings are fixed at creation). |
| `authentik.server.env` / `authentik.worker.env` | `AUTHENTIK_BOOTSTRAP_TOKEN` from Secret `policy-system-authentik-api-token`, key `PS_AUTHENTIK_API_TOKEN` | Makes Authentik create an API token equal to PS Service's on first start. Authentik applies it **once per tenant** and never rotates it. The Secret name is a literal because Helm never templates values files (the release name is fixed to `policy-system`). Production sets `authentik.global.env` (a list, which Helm replaces rather than merges) for `AUTHENTIK_WEB__PATH=/auth/` only, so these are not overridden there. |
| `authentik.blueprints.configMaps` | the chart's blueprint ConfigMap | Set in `values.yaml`, so **every** profile, evaluator included, gets the bundled blueprint (the `ps-cli` OIDC provider, passkey-only enrolment and recovery flows, `sub_mode: user_username`). |

The production Ingress exposes only an allowlist of Authentik paths; that list lives in
`scripts/deploy-ps-prod.sh`, not in the chart — see [Installation Guide: What is exposed
(production)](./installation-guide.md#what-is-exposed-production).
