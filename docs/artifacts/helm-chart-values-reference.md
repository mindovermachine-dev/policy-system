# Helm Chart Values Reference

Every operator-facing key in `charts/policy-system/values.yaml` (local-test default) and
`charts/policy-system/values-prod.yaml` (production override file, passed via
`-f values-prod.yaml`). See the [User Guide](./user-guide.md)'s
[Local Test](./user-guide.md#local-test) walkthrough for how to deploy the chart in the
first place — this page is the values reference for that walkthrough, step 6 onward.

## Table of Contents

- [Core values](#core-values)
- [Example: `helm upgrade`](#example-helm-upgrade)
- [Ollama values](#ollama-values)
- [Azure values](#azure-values)

## Core values

| Key | Default (local-test) | Purpose |
| --- | --- | --- |
| `nameOverride` / `fullnameOverride` | `""` / `""` | Standard Helm naming knobs (`templates/_helpers.tpl`): `nameOverride` replaces the chart name inside `<release>-<chart>` resource names; `fullnameOverride` replaces that whole prefix. Both truncated to 63 chars. Leave empty unless two releases must coexist in one namespace. |
| `psService.image.repository` | `ghcr.io/mindovermachine-dev/ps-service` | PS Service image. |
| `psService.image.tag` | `""` (falls back to `Chart.appVersion`) | PS Service image tag. Empty by default — the chart's own `appVersion` (kept in lockstep with the image by the release job) is used unless you pin an explicit tag with `--set`/`-f`. |
| `psService.service.type` | `NodePort` (`ClusterIP` in prod) | PS Service Service type. `NodePort` is what `deploy/kind/cluster.yaml`'s `extraPortMappings` targets locally; prod has no kind-specific reachability mechanism, so it's `ClusterIP`-only there. |
| `psService.service.nodePort` | `30800` | Fixed NodePort behind host port `8000` (via `extraPortMappings`). Not set in prod (no `nodePort` field when `type: ClusterIP`). |
| `psService.companyMerge.similarityThreshold` | `0.59` | `PS_COMPANYMERGE_SIMILARITY_THRESHOLD` — fuzzy-match threshold for company entity merging. Empirically recommended by issue #29's labeled precision/recall/F1 sweep, not an undocumented judgment call. |
| `psService.localTestBypass.enabled` | `false` | `PS_SERVICE_LOCAL_TEST_BYPASS` — opt-in auth bypass for local evaluation. Off by default even under the local-test profile; an evaluator flips it explicitly to use the plugin path (step 7 of the User Guide) without OIDC. |
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
| `falkordb.browser.nodePort` | `30300` | Fixed NodePort behind host port `3000` (via `extraPortMappings`). Only applies when `falkordb.browser.enabled=true`. |
| `falkordb.image.repository` / `falkordb.image.tag` | `falkordb/falkordb` / `latest` | FalkorDB image. |

Immediately after installing, `falkordb.persistence.enabled` (on by default — data
survives a pod restart) and the `llm.*` keys (which provider, and how its credentials
reach the pod) are the two settings worth double-checking against your intended setup.
Persistent storage means the PVC needs a StorageClass available in your cluster; a
default `kind` cluster provisions one automatically, so this works out of the box locally
too. See the User Guide's
[Operations: Backup & Restore](./user-guide.md#operations-backup--restore) for backing up
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
