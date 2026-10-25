# Helm Chart Values Reference

Every operator-facing key in `charts/policy-system/values.yaml` (local-test default) and
`charts/policy-system/values-prod.yaml` (production override file, passed via
`-f values-prod.yaml`). See the [Evaluator Installation Guide](./evaluator-installation-guide.md) for
how to deploy the chart in the first place — this page is the values reference for
that walkthrough, the deploy script step onward. The evaluator and production scripts
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
| `isProductionProfile` | `false` (`true` in `values-prod.yaml`) | Marks the production profile. Production-only refusals key on it, such as refusing `psService.curatedSource.allowInsecureHttp=true`. `true` or `false`; any other value fails the render. Do not override it: setting it to `false` on a production install turns those refusals off. |
| `psService.image.repository` | `ghcr.io/mindovermachine-dev/ps-service` | PS Service image. |
| `psService.image.tag` | `""` (falls back to `Chart.appVersion`) | PS Service image tag. Empty by default — the chart's own `appVersion` (kept in lockstep with the image by the release job) is used unless you pin an explicit tag with `--set`/`-f`. |
| `psService.service.type` | `ClusterIP` | PS Service Service type. Clients reach PS Service through the chart-owned Ingress (evaluator and production), so it is `ClusterIP` in every profile, and `ingress.enabled=true` fails the render for any other type. `/metrics` is anonymous and served on this port; the chart Ingress never routes it, but a `NodePort` Service or a hand-written Ingress that forwards to this Service publishes it. |
| `psService.service.nodePort` | `null` | Rendered only when `type` is `NodePort` and this is set. Unused by the shipped profiles. |
| `psService.companyMerge.capabilityMergeThreshold` | `0.59` | `PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD` — fuzzy-match threshold for company entity merging. Empirically recommended by a labeled precision/recall/F1 sweep, not an undocumented judgment call. |
| `psService.companyMerge.capabilityReviewThreshold` | `0.43` | `PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD` — a Capability near miss is raised for review only when its best similarity score is at or above this value; `0.0` raises every one. Must not exceed `capabilityMergeThreshold`. Calibrated with `tools/company-merge/company_merge_similarity_sweep.py` (`--floor 0.43` passes): the lowest labelled true duplicate scores 0.4359, so the default is at or below it, rounded down to 0.01 (GH #349). |
| `psService.companyMerge.roleReviewThreshold` | `0.80` | `PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD` — a Role near miss (two Roles of one instrument) is raised for review only when its best similarity score is at or above this value; `0.0` raises every pair. Roles are review-only and are never merged automatically, so there is no Role merge threshold. The default is provisional until it is calibrated against a real instrument. |
| `psService.graphGateway.driftCheckIntervalSeconds` | `3600` | `PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS` — seconds between two passes of the Graph Write Gateway's drift check, which compares each graph with the digest checkpointed after its last gateway write and logs an alert (`drift_check`, outcome `failed`, `error_class` `GraphDriftDetected`) if they differ. Internal only: no endpoint, no `/status` field. `0` turns the check off. |
| `psService.resources.requests.cpu` / `psService.resources.requests.memory` / `psService.resources.limits.cpu` / `psService.resources.limits.memory` | `500m` / `512Mi` / `2` / `2Gi` | CPU and memory requests and limits of the `ps-service` container. An unmeasured starting point; unlike the other workloads it carries a CPU limit, because it predates them (the others are memory-limited only). A container that exceeds its memory limit is `OOMKilled` and restarts. Override the keys individually; there is no opt-out, and setting the whole block to `null` is not supported. |
| `psService.probes.readiness.timeoutSeconds` / `psService.probes.readiness.periodSeconds` / `psService.probes.readiness.failureThreshold` | `5` / `15` / `5` | Readiness probe of the `ps-service` container (`GET /readyz`, in-cluster only). A pod that fails it `failureThreshold` times in a row leaves the Service rotation. The same poll drives the `/readyz` re-probe of an unhealthy FalkorDB or state Postgres; its deadline (`PS_SERVICE_READY_REPROBE_DEADLINE_SECONDS`, 3 s) must stay below `timeoutSeconds`, and the chart does not expose the two re-probe variables. |
| `psService.probes.liveness.timeoutSeconds` / `psService.probes.liveness.periodSeconds` / `psService.probes.liveness.failureThreshold` | `5` / `15` / `5` | Liveness probe of the `ps-service` container (`GET /livez`, in-cluster only). A pod that fails it `failureThreshold` times in a row is restarted. |
| `psService.probes.startup.timeoutSeconds` / `psService.probes.startup.periodSeconds` | `5` / `10` | Startup probe of the `ps-service` container (`GET /livez`); liveness and readiness probes begin once it succeeds. Its failure threshold is derived as `ceil((psService.auth.discoveryRetryBudgetSeconds + 90) / periodSeconds)`, so the window (390 s at the defaults) covers the discovery retry budget plus process boot and one last discovery attempt. |
| `psService.localTestBypass.enabled` | `false` | `PS_SERVICE_LOCAL_TEST_BYPASS` — opt-in auth bypass. Off by default and **not used by the evaluator install**: the bypass refuses to bind a non-loopback host and every container image binds `0.0.0.0`, so it cannot start in a container. The evaluator profile runs real OIDC login against the bundled Authentik instead. |
| `psService.publicUrl` | `""` | `PS_PUBLIC_URL` — the public origin of PS Service, as `https://host[:port]` with no path, query or fragment. PS Service publishes it as `resource` in its `/.well-known/oauth-protected-resource` metadata and in the `resource_metadata` URL of every `401` `WWW-Authenticate` challenge; no request header can change it. Required (render fails naming the value) unless `psService.localTestBypass.enabled=true`. An `http://` origin is accepted only with the bypass on. **Script:** production `https://<hostname>`; evaluator `https://<hostname>:30443`. |
| `psService.maxRequestBodyBytes` | `104857600` | `PS_SERVICE_MAX_REQUEST_BODY_BYTES` — the largest accepted request body, a positive integer number of bytes (default 100 MiB). A request whose `Content-Length` is larger is answered `413` before its body is read; a non-positive value fails the render. The PS Service Ingress limit (`nginx.ingress.kubernetes.io/proxy-body-size`) is derived from it as 1.5 times the value, rounded up. |
| `psService.gracefulShutdownSeconds` | `10` | `PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS` — seconds PS Service waits for in-flight requests to finish after `SIGTERM` before it stops. A whole number, 0 or more. `0` stops without waiting. A negative value, a value that is not a number, or a quoted number such as `"10"` fails the render. The chart derives the pod's `terminationGracePeriodSeconds` from it: `gracefulShutdownSeconds + 15 + 5`, where 15 is PS Service's default stop budget for the graph gateway's drift monitor, startup replay and reconciler (`PS_SERVICE_SHUTDOWN_STOP_BUDGET_SECONDS`, not a chart value) and 5 is a margin for the process to exit; 30 s with the default. |
| `psService.query.timeoutMs` | `5000` | `PS_QUERY_TIMEOUT_MS` — server-side time limit for one Cypher query, in milliseconds; FalkorDB aborts a query that runs longer. A whole number, 1 or more. Zero, a negative value, a value that is not a number, or a quoted number such as `"5000"` fails the render. It must stay below `PS_FALKORDB_SOCKET_TIMEOUT_SECONDS` (30 s by default, not a chart value), or PS Service refuses to start, so a value of 30000 or more renders but the pod does not start. |
| `psService.query.rowCap` | `1000` | `PS_QUERY_ROW_CAP` — the most rows one Cypher query returns; extra rows are dropped and the result is marked truncated. A whole number, 1 or more. Zero, a negative value, a value that is not a number, or a quoted number such as `"5000"` fails the render. |
| `psService.ingestionRuns.maxInFlight` | `1` | `PS_INGESTIONRUNS_MAX_IN_FLIGHT` — the most ingestion runs executing at once; a submission beyond it is refused until a run finishes. The limit is per process, because the run registry is process-local, so it assumes `replicas: 1`. A whole number, 1 or more. Zero, a negative value, a value that is not a number, or a quoted number such as `"5000"` fails the render. |
| `psService.curatedSource.url` | `""` | `PS_CURATEDSOURCE_URL` — the base URL of the curated-content source that the catalog listing and instrument restore read from. Empty means PS Service uses the public curated-content tree, and the chart renders no `PS_CURATEDSOURCE_URL` entry; when set, the entry carries the value as given. PS Service validates it at startup and refuses to start on an invalid URL: it must be `https://` unless `psService.curatedSource.allowInsecureHttp=true`. Do not embed credentials in the curated source URL (user info, or tokens in the query): it is stored in plain text in the Deployment spec. |
| `psService.curatedSource.allowInsecureHttp` | `false` | `PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP` — whether PS Service accepts a plain `http://` curated source URL. Insecure HTTP is for non-production use only, for example a local curated-content mirror; it is refused in the prod profile: the render fails naming this key when `isProductionProfile=true`, which `values-prod.yaml` sets. `true` or `false`; any other value fails the render. |
| `psService.auth.issuer` | `""` | `PS_AUTH_ISSUER` — the OIDC authorization-server URL (for the bundled Authentik: production `https://<hostname>/auth/application/o/ps-cli/`; evaluator `https://<hostname>:30443/auth/application/o/ps-cli/`). Required unless `psService.localTestBypass.enabled=true`; see the [IdP configuration contract](./idp-configuration-contract.md). |
| `psService.auth.audience` | `""` | `PS_AUTH_AUDIENCE` — the resource-server audience (for the bundled Authentik, the fixed OAuth2 Provider's `client_id`, `ps-cli`). Required unless `psService.localTestBypass.enabled=true`. |
| `psService.auth.cliClientId` | `""` | `PS_AUTH_CLI_CLIENT_ID` — the public CLI client's id (for the bundled Authentik, `ps-cli`). Optional; when set it is advertised in the `/.well-known/oauth-protected-resource` metadata as `ps_cli_client_id`. |
| `psService.auth.scopes` | `""` | `PS_AUTH_SCOPES` — space/comma-separated OAuth scope(s) a client should request at login (for the bundled Authentik, `openid profile email offline_access`). Not used for token validation, but required in practice: PS-Cli's device-flow login sources its OAuth `scope` request directly from this value via `scopes_supported`, and most IdPs reject an empty scope. See the [IdP configuration contract](./idp-configuration-contract.md). |
| `psService.auth.discoveryRetryBudgetSeconds` | `300` | `PS_AUTH_DISCOVERY_RETRY_BUDGET_SECONDS` — seconds PS Service keeps retrying a failed OIDC discovery fetch at startup (doubling pause from 1 s, capped at 30 s) before it exits; `0` makes a single attempt. About five minutes lets a first install wait for the issuer's certificate and stays below the deploy script's helm wait. A malformed `jwks_uri` or algorithm list is not retried. Nothing listens during the retries, so the container is not restarted until the startup window (see `psService.probes.startup.*`) has passed. |
| `psService.passkeySigning.allowedOrigins` | `[]` | `PS_PASSKEYSIGNING_ALLOWED_ORIGINS` — list of origins (`scheme://host[:port]`, no path) the approval page may be served from, rendered comma-joined and only when non-empty. The first entry is the WebAuthn relying party: its host is the `rp_id` and passkeys enrolled under it stay valid only while it is unchanged. A POST to an approval route whose `Host` matches none of the entries is answered `421`. Empty derives the relying party from each request's `Host`, so set it on every deployment that is reachable by more than one name. **Script (evaluator):** `["https://<hostname>:30443"]`. |

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
| `falkordb.resources` | requests `cpu: 100m`, `memory: 256Mi`; limits `memory: 1Gi` | Resource requests and limits of the `falkordb` container. An unmeasured starting point: the memory-only limit bounds a runaway in-memory store without CPU throttling. A graph larger than the limit is `OOMKilled` and rebuilt from the mutation log in PS Postgres (no data loss), so a large graph needs a higher `limits.memory`. Set to `null` to omit; `{}` does not opt out because Helm merges it with the default. |
| `falkordb.probes.startup.timeoutSeconds` / `falkordb.probes.startup.periodSeconds` / `falkordb.probes.startup.failureThreshold` | `5` / `10` / `90` | **** Startup probe of the `falkordb` container: `redis-cli ping` must reply `PONG` on port 6379, so the pod is held back while FalkorDB loads its RDB. The defaults allow 15 minutes (`90` x `10` s) before the container is restarted; raise `failureThreshold` for large datasets. No credentials are used. |
| `falkordb.probes.readiness.timeoutSeconds` / `falkordb.probes.readiness.periodSeconds` / `falkordb.probes.readiness.failureThreshold` | `5` / `10` / `3` | **** Readiness probe of the `falkordb` container: the same `PONG` check as startup, so the pod leaves the Service endpoints while it replies `LOADING` or does not answer. No credentials are used. |
| `falkordb.probes.liveness.timeoutSeconds` / `falkordb.probes.liveness.periodSeconds` / `falkordb.probes.liveness.failureThreshold` | `5` / `15` / `5` | **** Liveness probe of the `falkordb` container: the same `PONG` check, which fails on any non-`PONG` reply or a hang and then restarts the container after about 75 s. It never runs before the startup probe has passed, so a slow RDB load is not restarted. A restart loses no data, because the graph is rebuilt from the mutation log in PS Postgres. No credentials are used. |

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

Without `psService.ollamaHostGatewayIP` set, PS Service cannot reach Ollama. `/status` still
reports `"status": "ready"` — readiness depends on FalkorDB, PS state Postgres and the startup graph replay, not on the LLM provider — but an authenticated call lists
`llm_interface` under `unhealthy_dependencies`, and `ps-cli ingest document` and the
`ingest-regulation`, `start-ingestion` and `check-regulations` tools stop at their pre-flight check with
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

Verify what reached the pod, then check readiness. The `status` field of `/status` reflects FalkorDB,
PS state Postgres and the startup graph replay only, so `helm install --wait` succeeding and `"status": "ready"` do **not**
prove the Azure credentials or deployment names work. The LLM signal is the
`unhealthy_dependencies` list in the response to a call with a valid bearer token: at startup PS Service makes one real
completion call with `PS_LLMINTERFACE_MODEL` and one real embedding call with
`PS_LLMINTERFACE_EMBED_MODEL`, and if either fails `llm_interface` is listed there.

```bash
kubectl exec deploy/policy-system-ps-service -- env | grep '^PS_LLMINTERFACE_'
```

```bash
curl -s https://<hostname>:30443/status
# anonymous: {"status":"ready","version":"<version>"} (status and version, no dependency names)
curl -s -H "Authorization: Bearer $ACCESS_TOKEN" https://<hostname>:30443/status
# healthy: {"status":"ready","version":"<version>","unhealthy_dependencies":[],"gated_graphs":[]}
# "llm_interface" listed: wrong key, base URL or deployment name — fix the --set
# values and `helm upgrade`; the restarted pod re-probes at startup.
```

`ps-cli ingest document` and the `ingest-regulation`, `start-ingestion` and `check-regulations`
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
| `psPostgres.provisioning.resources` | requests `cpu: 100m`, `memory: 128Mi`; limits `memory: 256Mi` | Resource requests and limits of the `ps-state-provision` Job container. An unmeasured starting point: the Job runs a short migration CLI, and the memory-only limit bounds a runaway run without CPU throttling. Set to `null` to omit; `{}` does not opt out because Helm merges it with the default. |
| `psService.waitForPostgres.timeoutSeconds` | `300` | **** How long the `wait-for-postgres` init container of the `ps-service` Pod waits for `<release>-ps-postgres:5432` to accept TCP connections before exiting non-zero. Helm creates `ps-service` and `ps-postgres` together, and `ps-service` exits on its first failed state-database connection; the init container removes the resulting crash restarts on a fresh PVC (first-start initdb). It uses the `ps-service` image and no credentials, and runs after `build-ca-bundle` when `psService.localCa.secretName` is set. |
| `psPostgres.probes.readiness.timeoutSeconds` / `psPostgres.probes.readiness.periodSeconds` / `psPostgres.probes.readiness.failureThreshold` | `5` / `5` / `6` | **** Readiness probe of the `ps-postgres` container: `pg_isready` over TCP (`-h 127.0.0.1`) against `psPostgres.state.database`, so the pod is Ready only once the final server accepts connections, not during first-start initdb (when `ps-service` would otherwise crash-restart once). No credentials are used. There is deliberately neither a startup nor a liveness probe: a kill during initdb would leave PGDATA half-initialised, and a kill during crash recovery would restart the recovery in a loop. |
| `psPostgres.resources` | requests `cpu: 100m`, `memory: 256Mi`; limits `memory: 512Mi` | Resource requests and limits of the `ps-postgres` container. An unmeasured starting point: the memory-only limit bounds a runaway server without CPU throttling. Set to `null` to omit; `{}` does not opt out because Helm merges it with the default. |
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
| `psService.authentik.baseUrl` | `https://authentik.example.com` | `PS_AUTHENTIK_BASE_URL` — where PS Service calls Authentik's API for `invite_user`. **Script:** evaluator and production both `http://policy-system-authentik-server/auth` (the in-cluster Service; the admin and core APIs are not reachable through the Ingress). |
| `psService.authentik.publicUrl` | `""` | `PS_AUTHENTIK_PUBLIC_URL` — optional user-reachable Authentik address used **only** to build the invitee's enrolment link. Not rendered when empty. **Script:** evaluator `https://<hostname>:30443/auth`; production `https://<hostname>/auth`. Needs a PS Service image that supports it (see the [Installation Guide's Verification status](./installation-guide.md#verification-status)); older images ignore the variable. |
| `psService.authentikHostname` / `psService.authentikHostAliasIP` | `""` / `""` | Both set: PS Service's pod resolves that hostname to that IP (a `hostAliases` entry), so its token validation reaches the same Authentik a browser reaches on the kind node's port. **Script (evaluator):** the `--hostname` (default `ps.test`; the chart has no default and the value stays empty) and the kind node container's IP. The script rejects names ending in `.local` because Claude Code cannot resolve them. Unused in production. |
| `psService.localCa.secretName` | `""` | Name of a Secret holding `ca.crt` only (created by `scripts/deploy-ps-eval.sh`, never by the chart). Empty = off. When set, PS Service gets a trust bundle (system CAs plus `ca.crt`) through an init container and `SSL_CERT_FILE`. The init container uses the PS Service image itself. |
| `ingress.enabled` | `false` | **Script (evaluator and production):** `true`. Renders the chart-owned Ingresses: one for PS Service and, when `authentik.enabled`, one for Authentik. Both use the host of `psService.publicUrl`; rendering fails naming `psService.publicUrl` when it is unset. The PS Service Ingress also requires `psService.service.type: ClusterIP`. |
| `ingress.className` | `""` | `ingressClassName` of both Ingresses. Empty leaves it unset, so the cluster default class applies. **Script (evaluator):** `nginx`, the class of the ingress controller in `deploy/kind/ingress-nginx.yaml`. |
| `ingress.annotations` | `{}` | Annotations of the PS Service Ingress only, for example `cert-manager.io/cluster-issuer`. The Authentik Ingress gets none. Setting `nginx.ingress.kubernetes.io/proxy-body-size` here fails the render; set `psService.maxRequestBodyBytes` instead. |
| `ingress.tls.secretName` | `""` | Secret for the certificate of the PS Service Ingress host. Empty renders no `tls` block. The Authentik Ingress never has a `tls` block because it shares the host. **Script (evaluator):** `policy-system-local-tls`, created by the script. |
| `authentik.server.service.type` | `ClusterIP` | Service type of the Authentik server, in every profile. |
| `authentik.postgres.image.repository` / `authentik.postgres.image.tag` | `postgres` / `16-alpine` | Image of the chart-owned Postgres that backs Authentik. |
| `authentik.postgres.database` / `authentik.postgres.user` | `authentik` / `authentik` | Database and role names in that Postgres. The password is held in the Authentik credentials Secret (see [Authentik credentials values](#authentik-credentials-values)). |
| `authentik.postgres.resources` | requests `cpu: 50m`, `memory: 128Mi`; limits `memory: 256Mi` | Resource requests and limits of the `authentik-postgres` container. An unmeasured starting point: the memory-only limit bounds a runaway server without CPU throttling. Set to `null` to omit; `{}` does not opt out because Helm merges it with the default. |
| `authentik.postgres.persistence.size` / `authentik.postgres.persistence.storageClassName` | `10Gi` / `""` | Data volume size and StorageClass of that Postgres; an empty class selects the cluster default. |
| `authentik.postgres.probes.readiness.timeoutSeconds` / `authentik.postgres.probes.readiness.periodSeconds` / `authentik.postgres.probes.readiness.failureThreshold` | `5` / `5` / `6` | **** Readiness probe of the `authentik-postgres` container: `pg_isready` over TCP (`-h 127.0.0.1`) against `authentik.postgres.database` and `authentik.postgres.user`, so first-start initdb (whose temporary server is socket-only) cannot pass it and the pod is Ready only once the final server accepts connections. No credentials are used. There is deliberately neither a startup nor a liveness probe: a kill during initdb would leave PGDATA half-initialised, and a kill during crash recovery would restart the recovery in a loop. A start that hangs shows as an unready pod and a failed `helm upgrade --wait`. |
| `authentik.postgresql.enabled` | `false` | Keeps the upstream Authentik chart's bundled Postgres off; the chart-owned Postgres above is used. |
| `authentik.authentik.postgresql.host` / `.port` / `.name` / `.user` | `""` / `5432` / `""` / `""` | Non-secret connection values that the upstream Authentik chart expects; the connection is supplied through the credentials Secret, so these stay empty in the default profiles. |
| `authentik.server.env` / `authentik.worker.env` | `AUTHENTIK_BOOTSTRAP_TOKEN` from Secret `policy-system-authentik-api-token`, key `PS_AUTHENTIK_API_TOKEN` | Makes Authentik create an API token equal to PS Service's on first start. Authentik applies it **once per tenant** and never rotates it. The Secret name is a literal because Helm never templates values files (the release name is fixed to `policy-system`). The evaluator profile and production both set `authentik.global.env` (a list, which Helm replaces rather than merges) with `AUTHENTIK_WEB__PATH=/auth/`, so Authentik serves under `/auth/` in both and these are not overridden. The evaluator profile also sets `AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS=10.244.0.0/16`, the kind pod network where the evaluator proxy runs, so Authentik honours forwarding headers from that network only; production leaves Authentik's built-in default. |
| `authentik.server.resources` | requests `cpu: 100m`, `memory: 512Mi`; limits `memory: 512Mi` | Resource requests and limits of the Authentik server container. An unmeasured starting point: the upstream chart's own suggested values, with a memory-only limit and no CPU limit to avoid throttling. The upstream chart ships an empty map; setting `null` renders an empty resources field (no requests or limits), and the key itself is not removed. |
| `authentik.worker.resources` | requests `cpu: 100m`, `memory: 512Mi`; limits `memory: 512Mi` | Resource requests and limits of the Authentik worker container. An unmeasured starting point: the upstream chart's own suggested values, with a memory-only limit and no CPU limit to avoid throttling. The upstream chart ships an empty map; setting `null` renders an empty resources field (no requests or limits), and the key itself is not removed. |
| `authentik.blueprints.configMaps` | the chart's blueprint ConfigMap | Set in `values.yaml`, so **every** profile, evaluator included, gets the bundled blueprint (the `ps-cli` OIDC provider, passkey-only enrolment and recovery flows, `sub_mode: user_username`). |

The chart-owned Ingresses expose only allowlists: the PS Service paths in `files/ps-service-public-paths.txt` and the Authentik paths in `files/authentik-public-paths.txt`, both in the chart — see [Installation Guide: What is exposed
(production)](./installation-guide.md#what-is-exposed-production).
