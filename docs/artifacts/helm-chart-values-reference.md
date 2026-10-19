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
| `psService.service.type` | `ClusterIP` | PS Service Service type. Clients reach PS Service through the proxy (evaluator) or the Ingress (production), so it is `ClusterIP` in every profile. |
| `psService.service.nodePort` | `null` | Rendered only when `type` is `NodePort` and this is set. Unused by the shipped profiles. |
| `psService.companyMerge.capabilityMergeThreshold` | `0.59` | `PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD` — fuzzy-match threshold for company entity merging. Empirically recommended by a labeled precision/recall/F1 sweep, not an undocumented judgment call. |
| `psService.companyMerge.capabilityReviewThreshold` | `0.43` | `PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD` — a Capability near miss is raised for review only when its best similarity score is at or above this value; `0.0` raises every one. Must not exceed `capabilityMergeThreshold`. Calibrated with `tools/company-merge/company_merge_similarity_sweep.py` (`--floor 0.43` passes): the lowest labelled true duplicate scores 0.4359, so the default is at or below it, rounded down to 0.01 (GH #349). |
| `psService.graphGateway.driftCheckIntervalSeconds` | `3600` | `PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS` — seconds between two passes of the Graph Write Gateway's drift check, which compares each graph with the digest checkpointed after its last gateway write and logs an alert (`drift_check`, outcome `failed`, `error_class` `GraphDriftDetected`) if they differ. Internal only: no endpoint, no `/ready` field. `0` turns the check off. |
| `psService.resources.requests.cpu` / `psService.resources.requests.memory` / `psService.resources.limits.cpu` / `psService.resources.limits.memory` | `500m` / `512Mi` / `2` / `2Gi` | CPU and memory requests and limits of the `ps-service` container. A container that exceeds its memory limit is `OOMKilled` and restarts. |
| `psService.probes.readiness.timeoutSeconds` / `psService.probes.readiness.periodSeconds` / `psService.probes.readiness.failureThreshold` | `5` / `15` / `5` | Readiness probe of the `ps-service` container (`GET /ready`). A pod that fails it `failureThreshold` times in a row leaves the Service rotation. |
| `psService.probes.liveness.timeoutSeconds` / `psService.probes.liveness.periodSeconds` / `psService.probes.liveness.failureThreshold` | `5` / `15` / `5` | Liveness probe of the `ps-service` container (`GET /health`). A pod that fails it `failureThreshold` times in a row is restarted. |
| `psService.localTestBypass.enabled` | `false` | `PS_SERVICE_LOCAL_TEST_BYPASS` — opt-in auth bypass. Off by default and **not used by the evaluator install**: the bypass refuses to bind a non-loopback host and every container image binds `0.0.0.0`, so it cannot start in a container. The evaluator profile runs real OIDC login against the bundled Authentik instead. |
| `psService.auth.issuer` | `""` | `PS_AUTH_ISSUER` — the OIDC authorization-server URL (for the bundled Authentik: production `https://<hostname>/auth/application/o/ps-cli/`; evaluator `https://<hostname>:30443/auth/application/o/ps-cli/`). Required unless `psService.localTestBypass.enabled=true`; see the [IdP configuration contract](./idp-configuration-contract.md). |
| `psService.auth.audience` | `""` | `PS_AUTH_AUDIENCE` — the resource-server audience (for the bundled Authentik, the fixed OAuth2 Provider's `client_id`, `ps-cli`). Required unless `psService.localTestBypass.enabled=true`. |
| `psService.auth.cliClientId` | `""` | `PS_AUTH_CLI_CLIENT_ID` — the public CLI client's id (for the bundled Authentik, `ps-cli`). Optional; when set it is advertised in the `/.well-known/oauth-protected-resource` metadata as `ps_cli_client_id`. |
| `psService.auth.scopes` | `""` | `PS_AUTH_SCOPES` — space/comma-separated OAuth scope(s) a client should request at login (for the bundled Authentik, `openid profile email offline_access`). Not used for token validation, but required in practice: PS-Cli's device-flow login sources its OAuth `scope` request directly from this value via `scopes_supported`, and most IdPs reject an empty scope. See the [IdP configuration contract](./idp-configuration-contract.md). |
| `psService.passkeySigning.allowedOrigins` | `[]` | `PS_PASSKEYSIGNING_ALLOWED_ORIGINS` — list of origins (`scheme://host[:port]`, no path) the approval page may be served from, rendered comma-joined and only when non-empty. The first entry is the WebAuthn relying party: its host is the `rp_id` and passkeys enrolled under it stay valid only while it is unchanged. A POST to an approval route whose `Host` matches none of the entries is answered `421`. Empty derives the relying party from each request's `Host`, so set it on every deployment that is reachable by more than one name. **Script (evaluator):** `["https://<hostname>:30443"]`. |

PS Service reads some settings that the chart does not expose as values. `PS_SERVICE_MAX_REQUEST_BODY_BYTES` is the largest accepted request body: a positive integer number of bytes, default `104857600` (100 MiB). A request whose `Content-Length` is larger is answered `413` before its body is read. The default applies to every deployment; to change it, add the variable to the `ps-service` container's environment yourself.

When `psService.localTestBypass.enabled=false` (the chart's default) and either `psService.auth.issuer` or `psService.auth.audience` is unset, `helm template`/`helm lint` fail with a message naming both values and the bypass alternative — this is the chart's fail-closed render guard, not a bug. See the [IdP configuration contract](./idp-configuration-contract.md) for what each value means and a worked Authentik example.
| **`llm.provider`** | `azure` (`ollama` still available via `--set llm.provider=ollama`) | **** Selects the LLM backend: `ollama` or `azure`. Drives `PS_LLMINTERFACE_MODEL`/`PS_LLMINTERFACE_EMBED_MODEL` and whether a Secret renders. |
| **`llm.model`** | `""` → `gpt-5.4-mini` (`azure`) / `phi3:mini` (`ollama`) | Chat model as a **bare name** — an Azure deployment name or an Ollama model tag — with **no `<provider>/` prefix**; the chart prepends `llm.provider/` itself to build `PS_LLMINTERFACE_MODEL`. Leave empty for the provider default shown. A name YAML would type as a number or boolean (e.g. `0`, `false`) must be quoted in a values file or passed with `--set-string`, or the chart treats it as unset. |
| **`llm.embedModel`** | `""` → `text-embedding-3-large` (`azure`) / `nomic-embed-text` (`ollama`) | Embedding model, same bare-name / no-prefix rule as `llm.model`; becomes `PS_LLMINTERFACE_EMBED_MODEL`. |
| **`llm.existingSecret`** | `""` | **** Set to reuse an operator-managed Secret name instead of `llm.azure.*` below. |
| **`llm.azure.apiKey`** | `""` | **** Azure API key. Never set a real value here in a committed file — pass via `--set` or use `llm.existingSecret`. Rendered into a Kubernetes `Secret` (`templates/secret.yaml`), never a ConfigMap or plaintext env var. |
| **`llm.azure.apiBase`** | `""` | **** Azure API base URL. Same secret-backed handling as `apiKey`. |
| `llm.azure.apiVersion` | `"preview"` | `AZURE_API_VERSION` — fixed literal matching the reference deployment (see [Customer-Managed Azure LLM Bootstrap](../architecture/customer-azure-llm-bootstrap.md)); not derived from the deployment, not treated as a secret. |
| **`falkordb.persistence.enabled`** | `true` (both profiles) | **** Toggles FalkorDB storage between a `PersistentVolumeClaim` (default) and an `emptyDir` (ephemeral). Either way the graph content is rebuilt from the mutation log in PS Postgres; the volume only avoids a replay at startup. No manual manifest edits needed — flip via `--set`/`-f` and `helm upgrade`. |
| `falkordb.persistence.storageClassName` | `""` | Empty string = let the cluster pick its own default StorageClass. Never hardcoded to kind's default StorageClass name (both profiles) — override explicitly for a real cluster if needed. |
| `falkordb.browser.enabled` | `true` (`false` in prod) | **** FalkorDB Browser UI Service. On by default for local-test convenience, off in prod. |
| `falkordb.browser.nodePort` | `30300` | Fixed NodePort behind host port `3001` (via `extraPortMappings`). Only applies when `falkordb.browser.enabled=true`. |
| `falkordb.image.repository` / `falkordb.image.tag` | `falkordb/falkordb` / `v4.22.1` | FalkorDB image. |

Immediately after installing, `falkordb.persistence.enabled` (on by default; the
volume only avoids a replay of the graph log when the pod restarts) and the `llm.*` keys (which provider, and how its credentials
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
reports `"status": "ready"` — readiness depends on FalkorDB, PS state Postgres and the startup graph replay, not on the LLM provider — but an authenticated call lists
`llm_interface` under `unhealthy_dependencies`, and `ps-cli ingest document` and the
`ingest_regulation`, `start_ingestion` and `check_regulations` tools stop at their pre-flight check with
`LLM Interface is unavailable.`. See
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
with `text-embedding-3-large`; re-embedding for another provider is out of scope).

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

Verify what reached the pod, then check readiness. `/ready`'s `status` reflects FalkorDB,
PS state Postgres and the startup graph replay only, so `helm install --wait` succeeding and `"status": "ready"` do **not**
prove the Azure credentials or deployment names work. The LLM signal is the
`unhealthy_dependencies` list in the response to a call with a valid bearer token: at startup PS Service makes one real
completion call with `PS_LLMINTERFACE_MODEL` and one real embedding call with
`PS_LLMINTERFACE_EMBED_MODEL`, and if either fails `llm_interface` is listed there.

```bash
kubectl exec deploy/policy-system-ps-service -- env | grep '^PS_LLMINTERFACE_'
```

```bash
curl -s https://<hostname>:30443/ready
# anonymous: {"status":"ready"} (status only, no dependency names)
curl -s -H "Authorization: Bearer $ACCESS_TOKEN" https://<hostname>:30443/ready
# healthy: {"status":"ready","unhealthy_dependencies":[],"gated_graphs":[]}
# "llm_interface" listed: wrong key, base URL or deployment name — fix the --set
# values and `helm upgrade`; the restarted pod re-probes at startup.
```

`ps-cli ingest document` and the `ingest_regulation`, `start_ingestion` and `check_regulations`
MCP tools run the same check as a pre-flight and stop with `LLM Interface is unavailable.`
while `llm_interface` is listed.

## PS Postgres values

PS Service keeps its durable state in one PostgreSQL server holding two
databases, each with its own least-privilege role: `ps_state` (audit events, authz,
runtime configuration, ingestion run status) and `ps_signing` (Passkey Signing: pending merge approvals and
enrolled WebAuthn credentials). It is **distinct from FalkorDB and from Authentik's own
bundled Postgres**: its own Deployment/Service/PVC/NetworkPolicy/Secrets,
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
| `psPostgres.state.graphOwnerRole` | `ps_state_graph_owner` | Name of the non-login role that owns the insert-only `graph_log` tables and the `audit_log` schema (the audit trail, `audit_log.audit_events`). Created by the init script on first start and by the provisioning Job on an existing cluster; `ps_state` is never a member of it. Passed to the Job as `PS_STATE_GRAPH_OWNER_ROLE`. Never reuse the state role's name. |
| `psPostgres.provisioning.enabled` | `true` | Renders the `ps-state-provision` Job that applies the ordinary `ps_state` migrations (as `ps_state`, through `SET ROLE`) and then creates and migrates the `graph_log` tables with the admin credential, on fresh installs and on every `helm upgrade`, and admits it through the Postgres NetworkPolicy. It succeeds from an empty database in one pass. Set `false` only if you run `python -m ps_service.graph_gateway.provision` yourself: PS Service fails closed at startup until the tables exist. Upgrades must pass `--wait --wait-for-jobs` so a failed Job fails the upgrade. |
| `psPostgres.provisioning.backoffLimit` / `psPostgres.provisioning.connectTimeoutSeconds` / `psPostgres.provisioning.activeDeadlineSeconds` | `3` / `120` / `900` | Retry budget, how long one attempt waits for a starting Postgres server (passed as `PS_STATE_PROVISION_CONNECT_TIMEOUT_SECONDS`), and the overall deadline of that Job. A failure is a real error, not an expected ordering wait, so retries are few. The chart refuses to render when `(backoffLimit + 1) * connectTimeoutSeconds` exceeds `activeDeadlineSeconds`. |
| `psService.waitForPostgres.timeoutSeconds` | `300` | **** How long the `wait-for-postgres` init container of the `ps-service` Pod waits for `<release>-ps-postgres:5432` to accept TCP connections before exiting non-zero. Helm creates `ps-service` and `ps-postgres` together, and `ps-service` exits on its first failed state-database connection; the init container removes the resulting crash restarts on a fresh PVC (first-start initdb). It uses the `ps-service` image and no credentials, and runs after `build-ca-bundle` when `psService.localCa.secretName` is set. |
| `psPostgres.probes.readiness.timeoutSeconds` / `psPostgres.probes.readiness.periodSeconds` / `psPostgres.probes.readiness.failureThreshold` | `5` / `5` / `6` | **** Readiness probe of the `ps-postgres` container: `pg_isready` over TCP (`-h 127.0.0.1`) against `psPostgres.state.database`, so the pod is Ready only once the final server accepts connections, not during first-start initdb (when `ps-service` would otherwise crash-restart once). No credentials are used. There is deliberately no liveness probe, so a slow initdb can never cause a restart loop. |
| **`psPostgres.admin.existingSecret`** / **`psPostgres.state.existingSecret`** / **`psPostgres.signing.existingSecret`** | `""` | When a value is unset (the default in both profiles), the chart generates and persists that credential itself via a `lookup`+`randAlphaNum` idiom (`templates/ps-postgres-secret.yaml`; Secrets `<fullname>-ps-postgres-{admin,state,signing}-credentials`, e.g. `policy-system-ps-postgres-{admin,state,signing}-credentials` for release `policy-system`; `<fullname>` is the release name when it already contains `policy-system`, else `<release>-policy-system`), reused verbatim — no Key Vault or other external call — on every subsequent `helm upgrade`. Set to reuse an operator-managed Secret name instead; each value suppresses only its own Secret. Required keys: `POSTGRES_PASSWORD` (admin), `PS_STATE_POSTGRES_PASSWORD` (state), `PS_PASSKEYSIGNING_POSTGRES_PASSWORD` (signing). **Restore:** the role password hashes inside a PS Postgres backup match these three passwords, so retain all three Secrets outside the cluster and the backup file, and set all three values before a restore (the restore procedure and `scripts/verify-backup-restore.sh restore` refuse otherwise). Give each Secret a different name from the chart-generated one: once the value is set the chart does not render the generated Secret and Helm removes it. See [Restore](./operations-guide.md#restore). |
| `psPostgres.persistence.size` | `10Gi` | PVC storage request for the data volume. |
| `psPostgres.persistence.storageClassName` | `""` | Empty string = let the cluster pick its own default StorageClass. Only consulted when `durableStorageClass.enabled` below is `false`. |
| `psPostgres.persistence.durableStorageClass.enabled` | `false` (`true` in prod) | Toggles a dedicated Premium SSD, Retain-reclaim StorageClass for this PVC (mirrors `falkordb.persistence.durableStorageClass` / `authentik.postgres.persistence.durableStorageClass` exactly). |

The provisioning Job (never the `ps-service` pod) receives `PS_STATE_POSTGRES_HOST`, `_PORT`,
`_DATABASE`, `_USER` (target and application role), `PS_STATE_GRAPH_OWNER_ROLE` (from
`psPostgres.state.graphOwnerRole`), and the admin credential as `PS_STATE_ADMIN_POSTGRES_USER`
(from `psPostgres.admin.user`) and `PS_STATE_ADMIN_POSTGRES_PASSWORD` (from the admin Secret's
`POSTGRES_PASSWORD`). `PS_STATE_PROVISION_CONNECT_TIMEOUT_SECONDS` (default `120`) bounds how
long the command waits for the server to accept connections. `PS_STATE_PROVISION_LOCK_TIMEOUT_SECONDS`
(default `60`, not set by the chart) bounds how long it waits for the migration lock another
runner holds. These variables are read only by
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
| **`authentik.credentialsExistingSecret`** | `""` | When unset (the default in both profiles), the chart generates and persists Authentik's `AUTHENTIK_SECRET_KEY`/`AUTHENTIK_POSTGRESQL__PASSWORD` itself via a `lookup`+`randAlphaNum` idiom (`templates/authentik-credentials-secret.yaml`), reused verbatim — no Key Vault or other external call — on every subsequent `helm upgrade`. Set to reuse an operator-managed Secret name instead. This is a **separate** value from `authentik.authentik.existingSecret.secretName` below — the upstream `authentik` sub-chart's own routing switch — and when set, it must be given the **same** Secret name as that value, or the render fails with a `fail()` guard naming both values. |
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
locally trusted HTTPS certificate. Both deploy scripts set the ones marked
"script" in one Helm release; see the [IdP configuration
contract](./idp-configuration-contract.md) for what they mean.

| Key | Default | Purpose |
| --- | --- | --- |
| `psService.authzBootstrapOwner.subject` / `psService.authzBootstrapOwner.issuer` | `""` / `""` | `PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT` / `_ISSUER` — the `(sub, iss)` allowed to claim `SystemOwner`. **Script:** the owner's email (the bundled provider's `sub` is the username, which the scripts make equal to the email) and the Authentik issuer, in the same deploy as everything else. Required (render fails) whenever the bypass is off. |
| `psService.authentik.existingSecret` | `""` | Reuse an operator-managed Secret (key `PS_AUTHENTIK_API_TOKEN`) as PS Service's Authentik API token instead of the chart-generated one (`<release>-authentik-api-token`, 64 random characters, generated once and kept across upgrades). When `authentik.enabled`, also set the `secretKeyRef` name of `AUTHENTIK_BOOTSTRAP_TOKEN` in `authentik.server.env` and `authentik.worker.env` to the same name, or the render fails. There is **no** `psService.authentik.apiToken` value any more (the old fixed placeholder is gone), and a Secret still holding it is replaced by a generated token. |
| `psService.authentik.baseUrl` | `https://authentik.example.com` | `PS_AUTHENTIK_BASE_URL` — where PS Service calls Authentik's API for `invite_user`. **Script:** evaluator and production both `http://policy-system-authentik-server/auth` (the in-cluster Service; the admin and core APIs are not reachable through the proxy or the Ingress). |
| `psService.authentik.publicUrl` | `""` | `PS_AUTHENTIK_PUBLIC_URL` — optional user-reachable Authentik address used **only** to build the invitee's enrolment link. Not rendered when empty. **Script:** evaluator `https://<hostname>:30443/auth`; production `https://<hostname>/auth`. Needs a PS Service image that supports it (see the [Installation Guide's Verification status](./installation-guide.md#verification-status)); older images ignore the variable. |
| `psService.authentikHostname` / `psService.authentikHostAliasIP` | `""` / `""` | Both set: PS Service's pod resolves that hostname to that IP (a `hostAliases` entry), so its token validation reaches the same Authentik a browser reaches on the kind node's port. **Script (evaluator):** the `--hostname` (default `ps.test`; the chart has no default and the value stays empty) and the kind node container's IP. The script rejects names ending in `.local` because Claude Code cannot resolve them. Unused in production. |
| `psService.localCa.secretName` | `""` | Name of a Secret holding `ca.crt` only (created by `scripts/deploy-ps-eval.sh`, never by the chart). Empty = off. When set, PS Service gets a trust bundle (system CAs plus `ca.crt`) through an init container and `SSL_CERT_FILE`. The init container uses the PS Service image itself. |
| `proxy.enabled` | `false` | Renders the evaluator reverse proxy (Deployment, ConfigMap, NodePort Service). The evaluator script sets it; production uses its Ingress. Requires `proxy.tls.secretName` and `psService.authentikHostname`. |
| `proxy.image.repository` / `proxy.image.tag` | `nginx` / `1.31.7-alpine` | The official nginx image the proxy runs. |
| `proxy.service.nodePort` | `30443` | NodePort of the proxy, the host port `deploy/kind/cluster.yaml` maps, the port in the OIDC issuer URL and the `X-Forwarded-Port` the proxy sends. Changing it on an existing kind cluster needs a cluster recreate. |
| `proxy.tls.secretName` | `""` | Name of a Secret with keys `tls.crt` and `tls.key` (created by `scripts/deploy-ps-eval.sh`, never by the chart). Only the proxy mounts it. |
| `proxy.lanExposure.enabled` | `false` | Declares that the proxy port is published to the local network. The evaluator script sets it. Rendering fails, naming this setting and `psService.localTestBypass.enabled`, when both are `true`: a machine reachable from the LAN must not serve an unauthenticated PS Service. |
| `proxy.podAnnotations` | `{}` | Extra proxy pod annotations; the script sets a certificate checksum so a rotated leaf restarts the proxy. |
| `proxy.tunables.clientMaxBodySize` / `proxy.tunables.proxyTimeoutSeconds` | `150m` / `3600` | Largest request body (restores and imports) and the read/send timeout (long-lived MCP streams). |
| `proxy.resources.requests.cpu` / `proxy.resources.requests.memory` | `50m` / `32Mi` | Provisional proxy requests; configuration, not a measurement. |
| `proxy.resources.limits.cpu` / `proxy.resources.limits.memory` | `500m` / `128Mi` | Provisional proxy limits; configuration, not a measurement. |
| `proxy.probes.readiness.timeoutSeconds` / `proxy.probes.readiness.periodSeconds` / `proxy.probes.readiness.failureThreshold` | `3` / `10` / `3` | Timings of the readiness TCP probe on the TLS listener. |
| `proxy.probes.liveness.timeoutSeconds` / `proxy.probes.liveness.periodSeconds` / `proxy.probes.liveness.failureThreshold` | `3` / `15` / `5` | Timings of the liveness TCP probe on the TLS listener. |
| `authentik.server.service.type` | `ClusterIP` | Service type of the Authentik server, in every profile. |
| `authentik.postgres.image.repository` / `authentik.postgres.image.tag` | `postgres` / `16-alpine` | Image of the chart-owned Postgres that backs Authentik. |
| `authentik.postgres.database` / `authentik.postgres.user` | `authentik` / `authentik` | Database and role names in that Postgres. The password is held in the Authentik credentials Secret (see [Authentik credentials values](#authentik-credentials-values)). |
| `authentik.postgres.persistence.size` / `authentik.postgres.persistence.storageClassName` | `10Gi` / `""` | Data volume size and StorageClass of that Postgres; an empty class selects the cluster default. |
| `authentik.postgresql.enabled` | `false` | Keeps the upstream Authentik chart's bundled Postgres off; the chart-owned Postgres above is used. |
| `authentik.authentik.postgresql.host` / `.port` / `.name` / `.user` | `""` / `5432` / `""` / `""` | Non-secret connection values that the upstream Authentik chart expects; the connection is supplied through the credentials Secret, so these stay empty in the default profiles. |
| `authentik.server.env` / `authentik.worker.env` | `AUTHENTIK_BOOTSTRAP_TOKEN` from Secret `policy-system-authentik-api-token`, key `PS_AUTHENTIK_API_TOKEN` | Makes Authentik create an API token equal to PS Service's on first start. Authentik applies it **once per tenant** and never rotates it. The Secret name is a literal because Helm never templates values files (the release name is fixed to `policy-system`). The evaluator profile and production both set `authentik.global.env` (a list, which Helm replaces rather than merges) with `AUTHENTIK_WEB__PATH=/auth/`, so Authentik serves under `/auth/` in both and these are not overridden. The evaluator profile also sets `AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS=10.244.0.0/16`, the kind pod network where the evaluator proxy runs, so Authentik honours forwarding headers from that network only; production leaves Authentik's built-in default. |
| `authentik.blueprints.configMaps` | the chart's blueprint ConfigMap | Set in `values.yaml`, so **every** profile, evaluator included, gets the bundled blueprint (the `ps-cli` OIDC provider, passkey-only enrolment and recovery flows, `sub_mode: user_username`). |

The production Ingress exposes only an allowlist of Authentik paths; that list lives in
`scripts/deploy-ps-prod.sh`, not in the chart — see [Installation Guide: What is exposed
(production)](./installation-guide.md#what-is-exposed-production).
