# Helm Chart Values Reference

Every operator-facing key in `charts/policy-system/values.yaml` (local-test default) and
`charts/policy-system/values-prod.yaml` (production override file, passed via
`-f values-prod.yaml`). See the [User Guide](./user-guide.md)'s
[Local Test](./user-guide.md#local-test) walkthrough for how to deploy the chart in the
first place — this page is the values reference for that walkthrough, step 5 onward.

## Table of Contents

- [Core values](#core-values)
- [Example: `helm upgrade`](#example-helm-upgrade)
- [Ollama values](#ollama-values)

## Core values

| Key | Default (local-test) | Purpose |
| --- | --- | --- |
| `psService.image.repository` | `ghcr.io/mindovermachine-dev/ps-service` | PS Service image. |
| `psService.image.tag` | `""` (falls back to `Chart.appVersion`) | PS Service image tag. Empty by default — the chart's own `appVersion` (kept in lockstep with the image by the release job) is used unless you pin an explicit tag with `--set`/`-f`. |
| `psService.service.type` | `NodePort` (`ClusterIP` in prod) | PS Service Service type. `NodePort` is what `deploy/kind/cluster.yaml`'s `extraPortMappings` targets locally; prod has no kind-specific reachability mechanism, so it's `ClusterIP`-only there. |
| `psService.service.nodePort` | `30800` | Fixed NodePort behind host port `8000` (via `extraPortMappings`). Not set in prod (no `nodePort` field when `type: ClusterIP`). |
| `psService.companyMerge.similarityThreshold` | `0.85` | `PS_COMPANYMERGE_SIMILARITY_THRESHOLD` — fuzzy-match threshold for company entity merging. |
| `psService.localTestBypass.enabled` | `false` | `PS_SERVICE_LOCAL_TEST_BYPASS` — opt-in auth bypass for local evaluation. Off by default even under the local-test profile; an evaluator flips it explicitly to use the plugin path (step 7 of the User Guide) without OIDC. |
| **`llm.provider`** | `ollama` (`azure` in prod) | **(AC-BI-003)** Selects the LLM backend: `ollama` or `azure`. Drives `PS_LLMINTERFACE_MODEL`/`PS_LLMINTERFACE_EMBED_MODEL` and whether a Secret renders. |
| **`llm.existingSecret`** | `""` | **(AC-BI-003)** Set to reuse an operator-managed Secret name instead of `llm.azure.*` below. |
| **`llm.azure.apiKey`** | `""` | **(AC-BI-003)** Azure API key. Never set a real value here in a committed file — pass via `--set` or use `llm.existingSecret`. Rendered into a Kubernetes `Secret` (`templates/secret.yaml`), never a ConfigMap or plaintext env var. |
| **`llm.azure.apiBase`** | `""` | **(AC-BI-003)** Azure API base URL. Same secret-backed handling as `apiKey`. |
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

Ollama is the **local-test** profile's default LLM provider (`llm.provider=ollama`) — no
API key needed, and it runs entirely on your machine. The production profile
(`values-prod.yaml`) defaults to `llm.provider=azure` instead — see
[Core values](#core-values) above for switching providers.

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

Without `psService.ollamaHostGatewayIP` set, `/ready` will likely never turn healthy under
the Ollama provider, since PS Service can't reach the LLM.

| Key | Default (local-test) | Purpose |
| --- | --- | --- |
| `psService.ollamaHostGatewayIP` | `""` | IP of the Podman network gateway, used to render a `hostAliases` entry so pods can resolve `host.containers.internal` when `llm.provider=ollama`. Empty by default — the chart can't know this statically. See [Podman host networking](#ollama-values) above. |
| `llm.ollama.apiBase` | `"http://host.containers.internal:11434"` | `OLLAMA_API_BASE` — set only when `llm.provider=ollama` and non-empty. |
