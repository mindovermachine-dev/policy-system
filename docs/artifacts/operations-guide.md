# Policy System Operations Guide

## Table of Contents

- [Evaluator (local-test) operations](#evaluator-local-test-operations)
  - [Updating to the latest version](#updating-to-the-latest-version)
  - [Cleanup / teardown](#cleanup--teardown)
  - [Rotating the Azure LLM API key](#rotating-the-azure-llm-api-key)
- [Production operations](#production-operations)
  - [Updating to the latest version](#updating-to-the-latest-version-1)
  - [Rotate the API key later](#rotate-the-api-key-later)
  - [Start and stop the AKS cluster](#start-and-stop-the-aks-cluster)
  - [Manual steps and operational notes](#manual-steps-and-operational-notes)
- [Backup](#backup)
  - [FalkorDB](#falkordb)
  - [PS Postgres (`ps_state` and `ps_signing`)](#ps-postgres-ps_state-and-ps_signing)
- [Restore](#restore)
  - [FalkorDB](#falkordb-1)
  - [PS Postgres (`ps_state` and `ps_signing`)](#ps-postgres-ps_state-and-ps_signing-1)
- [Troubleshooting / FAQ](#troubleshooting--faq)
- [Teardown](#teardown)

This guide is for administrators **operating an already-deployed** Policy System
instance — rotating credentials, upgrading, backing up/restoring, tearing down, or
diagnosing an issue. For first-time deployment, see the [Installation
Guide](./installation-guide.md). For day-to-day usage (`ps-cli`, Claude Desktop), see
the [User Guide](./user-guide.md).

---

## Evaluator (local-test) operations

These apply to an instance deployed via [Installation Guide: Evaluator
installation](./installation-guide.md#evaluator-installation).

### Updating to the latest version

Run [`scripts/ps-upgrade.sh`](../../scripts/ps-upgrade.sh) from a repo checkout — it re-runs
[`ps-cli/install.sh`](../../ps-cli/install.sh) to pick up the new client version, upgrades the
Helm release (no `--version` pin, so it always installs the latest chart, matching the client
version install.sh just picked up), and verifies pod status:

```bash
scripts/ps-upgrade.sh
```

Equivalently, run the steps it automates by hand:

```bash
helm upgrade --install policy-system oci://ghcr.io/mindovermachine-dev/charts/policy-system \
  --set llm.existingSecret=policy-system-llm-credentials --wait
```

```bash
kubectl get pods -l app.kubernetes.io/component=ps-service \
  -o custom-columns='NAME:.metadata.name,IMAGE:.spec.containers[0].image,STATUS:.status.phase'
```

The chart's `psService.image.tag` defaults to empty and falls back to `Chart.appVersion`
(see the [Helm Chart Values Reference](./helm-chart-values-reference.md)), so the chart
version and the image version can never disagree — there is no tag to hand-pin and no
flag needed to reset one. Your graph data is kept — FalkorDB persists to a
`PersistentVolumeClaim` (see [Backup](#backup)), so previously loaded regulations do
not need to be re-seeded.

### Cleanup / teardown

Nothing here is torn down automatically:

```bash
az group delete --name rg-policy-system --yes
```

```bash
az keyvault list-deleted --query "[].name" -o tsv
```

Find the vault pending purge.

```bash
az keyvault purge --name <vault-name>
```

Clears soft-delete retention.

### Rotating the Azure LLM API key

```bash
scripts/deploy-llm.sh --rotate-key
```

Regenerates whichever API key slot isn't currently active in Key Vault; re-run
`sync-llm-secrets-to-kind.sh` afterward to push the new value into the cluster.

```bash
scripts/sync-llm-secrets-to-kind.sh
```

---

## Production operations

These apply to an instance deployed via [Installation Guide: Production
installation](./installation-guide.md#production-installation).

### Updating to the latest version

`scripts/deploy-ps.sh`'s Helm reconciliation (`ensure_release`) only calls `helm upgrade
--install` when one of the 5 auth-related fields (`llm.existingSecret`,
`psService.auth.{issuer,audience,cliClientId,scopes}`) differs from what's already
deployed. If none of those changed, re-running
`scripts/deploy-ps.sh` is a no-op and will **not** pick up a new chart version.

Run [`scripts/ps-upgrade.sh`](../../scripts/ps-upgrade.sh) from a repo checkout instead — it
re-runs [`ps-cli/install.sh`](../../ps-cli/install.sh) to pick up the new client version, reads
back the currently-deployed auth values and re-supplies them explicitly so a plain `helm
upgrade` doesn't reset them to chart defaults, upgrades the Helm release (like Evaluator's
chart, `CHART_REF` carries no `--version` pin, so this always pulls whatever is latest at that
OCI reference), and verifies pod status:

```bash
scripts/ps-upgrade.sh
```

Equivalently, run the steps it automates by hand:

```bash
helm get values policy-system -o json
```

```bash
helm upgrade --install policy-system oci://ghcr.io/mindovermachine-dev/charts/policy-system \
  -f charts/policy-system/values-prod.yaml \
  --set llm.existingSecret=policy-system-llm-credentials \
  --set psService.auth.issuer=<issuer from helm get values> \
  --set psService.auth.audience=<audience from helm get values> \
  --set psService.auth.cliClientId=<cliClientId from helm get values> \
  --set psService.auth.scopes=<scopes from helm get values> \
  --wait
```

```bash
kubectl get pods -l app.kubernetes.io/component=ps-service \
  -o custom-columns='NAME:.metadata.name,IMAGE:.spec.containers[0].image,STATUS:.status.phase'
```

### Rotate the API key later

```bash
scripts/deploy-ps.sh --rotate-key
```

Regenerates whichever Azure Cognitive Services API key slot isn't currently active
in Key Vault and writes the new value back. Fails clearly if run before a first
successful deploy.

### Start and stop the AKS cluster

Stopping the cluster pauses node compute billing without deleting anything — the
FalkorDB PVC (and its data) and Key Vault are both untouched. PS Service and `ps-cli` are unreachable for the whole time the cluster
is stopped, so avoid stopping while an ingestion job is running.

```bash
scripts/ps-aks.sh stop
```

```bash
scripts/ps-aks.sh start
```

[`scripts/ps-aks.sh`](../../scripts/ps-aks.sh) resolves the cluster name the same
way `deploy-ps.sh` does (a subscription-derived hash, not a fixed string) so there's
nothing to look up by hand. Give it a minute or two after starting before checking
pod status — see the `kubectl get pods` command in [Updating to the latest
version](#updating-to-the-latest-version-1) above.

### Manual steps and operational notes

Manual steps and known gaps in `scripts/deploy-ps.sh`'s automation, current as of
this guide:

1. **AOAI SKU/quota discovery — partly automated.** `deploy-ps.sh` validates
   whatever SKU/capacity you configure against that region's live-reported range
   and quota, and fails with the exact numbers if insufficient — but discovering
   which SKU has real default quota for your subscription/region in the first
   place remains a manual step before you fill in `scripts/ps-defaults.conf`:
   ```bash
   az cognitiveservices model list --location <region> \
     --query "[?model.name=='gpt-5.4-mini'].model.skus[].name"
   ```
   ```bash
   az cognitiveservices usage list --location <region>
   ```
2. **AKS node VM-size allowlist + vCPU quota — automated.** `scripts/deploy-ps.sh`
   checks both the subscription allowlist and vCPU family quota for the fixed
   `Standard_D4as_v7` node size before ever calling `az aks create`, failing with
   the actual restriction reason or vCPU shortfall rather than a generic error. No
   operator action needed here.
3. **`kubectl rollout restart` FalkorDB startup-race workaround — remains
   manual.** If PS Service's pod isn't `Ready` shortly after first install, once
   FalkorDB is confirmed `Running`:
   ```bash
   kubectl rollout restart deployment/policy-system-ps-service
   ```
   The underlying FalkorDB startup race is out of scope for this deployment
   script; only the workaround is documented here, not a fix.

## Backup

### FalkorDB

FalkorDB persists to a `PersistentVolumeClaim` (`policy-system-falkordb-data`) when
`falkordb.persistence.enabled=true` (the default in both the local-test and production
profiles — see the [Helm Chart Values Reference](./helm-chart-values-reference.md)).
Back up the graph — every ingested regulation, internal policy, and company-graph merge
state — by capturing FalkorDB's own RDB snapshot file. No Policy System-specific backup
feature exists or is planned; this uses standard, unmodified Redis/`kubectl` tooling.

1. Find the running FalkorDB pod:
   ```bash
   POD=$(kubectl get pod -l app.kubernetes.io/component=falkordb -o jsonpath='{.items[0].metadata.name}')
   ```
2. Trigger a snapshot and wait for it to finish:
   ```bash
   kubectl exec "$POD" -c falkordb -- redis-cli BGSAVE
   ```
   ```bash
   kubectl exec "$POD" -c falkordb -- redis-cli INFO persistence | grep rdb_bgsave_in_progress
   ```
   Re-run the second command until it reports `rdb_bgsave_in_progress:0`.
3. Copy the resulting file out of the pod:
   ```bash
   kubectl cp "$POD":/var/lib/falkordb/data/dump.rdb "./falkordb-backup-$(date +%Y%m%d).rdb" -c falkordb
   ```

Store the resulting `.rdb` file wherever your backup retention policy requires — it is
a complete, standalone copy of the graph.

> **Tip:** `/var/lib/falkordb/data/dump.rdb` is the chart's default data-volume path, not
> a Policy System-configured one. If you've changed FalkorDB's persistence settings, confirm
> the real path first: `kubectl exec "$POD" -c falkordb -- redis-cli CONFIG GET dir`.

This is a different concern from
[#66](https://github.com/mindovermachine-dev/policy-system/issues/66)'s curated-catalog
restore, which seeds public reference content into any deployment (fresh or established)
without needing this backup/restore machinery at all.

### PS Postgres (`ps_state` and `ps_signing`)

The chart renders one PS Postgres server (`policy-system-ps-postgres` Deployment, Service and
PVC) hosting two databases, each with its own role and its own credentials Secret:

| Database | Role | Holds | Password Secret (key) |
| --- | --- | --- | --- |
| `ps_state` | `ps_state` | `access_role_assignments`, `audit_events` (the permanent, insert-only audit trail), `runtime_config` (runtime-mutable settings, including the curated-catalog source override), and the `ps_schema_migrations` tracking table | `policy-system-ps-postgres-state-credentials` (`PS_STATE_POSTGRES_PASSWORD`) |
| `ps_signing` | `ps_signing` | Passkey Signing data (`pending_approvals`, `signing_credentials`) and its own `schema_migrations` tracking table | `policy-system-ps-postgres-signing-credentials` (`PS_PASSKEYSIGNING_POSTGRES_PASSWORD`) |

PS Service reads its connection settings from `PS_STATE_POSTGRES_*` and
`PS_PASSKEYSIGNING_POSTGRES_*`, which the chart wires into the pod. Each role can connect only
to its own database. Authentik keeps its own separate Postgres (when enabled); it is not
covered here.

The PS state Postgres is a hard dependency: PS Service does not start, and `/ready` reports
`not_ready`, while it is unreachable. Reads of the catalog-source override fail closed — while
the database cannot be read, `GET /catalog`, curated-instrument restores and `get-catalog-source`
return an error rather than serving the env-var/default source. `set-catalog-source` and
`reset-catalog-source` also need this database, including under the local-test bypass.

Back up both databases with standard, unmodified `pg_dump` — no Policy System-specific backup
feature exists or is planned. Because the catalog override lives in `ps_state`, restoring
`ps_state` restores the override with it.

1. Find the running PS Postgres pod and read each role's password from its Secret:
   ```bash
   POD=$(kubectl get pod -l app.kubernetes.io/component=ps-postgres -o jsonpath='{.items[0].metadata.name}')
   STATE_PW=$(kubectl get secret policy-system-ps-postgres-state-credentials \
     -o jsonpath='{.data.PS_STATE_POSTGRES_PASSWORD}' | base64 -d)
   SIGNING_PW=$(kubectl get secret policy-system-ps-postgres-signing-credentials \
     -o jsonpath='{.data.PS_PASSKEYSIGNING_POSTGRES_PASSWORD}' | base64 -d)
   ```
   If you set `psPostgres.state.existingSecret` / `psPostgres.signing.existingSecret`, read
   the password from that Secret instead (same key names).
2. Dump each database in `pg_dump`'s custom format (compressed, and it supports selective
   `pg_restore`), inside the pod:
   ```bash
   kubectl exec "$POD" -- env PGPASSWORD="$STATE_PW" \
     pg_dump -h 127.0.0.1 -U ps_state -d ps_state -Fc -f /tmp/ps_state.dump
   kubectl exec "$POD" -- env PGPASSWORD="$SIGNING_PW" \
     pg_dump -h 127.0.0.1 -U ps_signing -d ps_signing -Fc -f /tmp/ps_signing.dump
   ```
3. Copy both files out of the pod, then remove the copies left in it:
   ```bash
   kubectl cp "$POD":/tmp/ps_state.dump "./ps_state-backup-$(date +%Y%m%d).dump"
   kubectl cp "$POD":/tmp/ps_signing.dump "./ps_signing-backup-$(date +%Y%m%d).dump"
   kubectl exec "$POD" -- rm /tmp/ps_state.dump /tmp/ps_signing.dump
   ```

Store the two `.dump` files wherever your backup retention policy requires — together they
are a complete, standalone copy of everything PS Service keeps in Postgres. Both files
contain sensitive data (the audit trail and enrolled signing credentials); protect them
accordingly.

Two operational notes:

- The init step that creates the two databases and roles runs only when the PVC is empty.
  Rotating a password in its Secret does not change the existing database role; run
  `ALTER ROLE` by hand as well.
- A pre-existing PVC from a chart version that predates this layout does not contain the
  `ps_state` database. Upgrade onto a fresh PVC (nothing is migrated).

## Restore

### FalkorDB

Restoring loads a previously captured `dump.rdb` snapshot back into FalkorDB so it starts
from that data. Run this against a deployment whose FalkorDB pod is already up — a fresh
install already has one running, even before any data has been ingested.

1. Find the running FalkorDB pod:
   ```bash
   POD=$(kubectl get pod -l app.kubernetes.io/component=falkordb -o jsonpath='{.items[0].metadata.name}')
   ```
2. Copy the snapshot into the pod's data volume, overwriting whatever is there:
   ```bash
   kubectl cp ./falkordb-backup-YYYYMMDD.rdb "$POD":/var/lib/falkordb/data/dump.rdb -c falkordb
   ```
3. Restart FalkorDB so it loads the new file on startup — Redis only reads `dump.rdb` when
   the process starts, not while it's already running:
   ```bash
   kubectl rollout restart deployment/policy-system-falkordb
   ```

The graph now contains exactly what was in the backup — anything ingested after that
snapshot was taken is gone.

### PS Postgres (`ps_state` and `ps_signing`)

Restoring loads previously captured `pg_dump` custom-format files back into the PS Postgres
server, replacing the current contents of each database with the backup's. Run this against
a deployment whose PS Postgres pod is already up — a fresh install already has one running,
with both databases and roles created, even before PS Service has run its migrations. Restore
`ps_state`, `ps_signing`, or both; each is independent. Restoring `ps_state` also restores the
catalog-source override recorded in its `runtime_config` table.

1. Find the pod and read the passwords as in the Backup section above (`POD`, `STATE_PW`,
   `SIGNING_PW`).
2. Copy the dump files into the pod:
   ```bash
   kubectl cp ./ps_state-backup-YYYYMMDD.dump "$POD":/tmp/ps_state.dump
   kubectl cp ./ps_signing-backup-YYYYMMDD.dump "$POD":/tmp/ps_signing.dump
   ```
3. Restore each database, dropping and recreating every object the dump contains before
   loading it (`--clean --if-exists` — safe on a database that already has some or all of the
   same tables, and on an empty one):
   ```bash
   kubectl exec "$POD" -- env PGPASSWORD="$STATE_PW" \
     pg_restore -h 127.0.0.1 -U ps_state -d ps_state --clean --if-exists /tmp/ps_state.dump
   kubectl exec "$POD" -- env PGPASSWORD="$SIGNING_PW" \
     pg_restore -h 127.0.0.1 -U ps_signing -d ps_signing --clean --if-exists /tmp/ps_signing.dump
   kubectl exec "$POD" -- rm /tmp/ps_state.dump /tmp/ps_signing.dump
   ```
4. Restart `ps-service` so it starts from a consistent view of the restored databases and its
   migration runners re-check the restored schemas against their tracking tables:
   ```bash
   kubectl rollout restart deployment/policy-system-ps-service
   ```
5. Verify the restore landed before relying on it — compare the counts against the system the
   backup was taken from:
   ```bash
   kubectl exec "$POD" -- env PGPASSWORD="$STATE_PW" psql -h 127.0.0.1 -U ps_state -d ps_state \
     -c "SELECT count(*) FROM access_role_assignments;" \
     -c "SELECT count(*) FROM runtime_config;" \
     -c "SELECT count(*), max(occurred_at) FROM audit_events;"
   kubectl exec "$POD" -- env PGPASSWORD="$SIGNING_PW" psql -h 127.0.0.1 -U ps_signing -d ps_signing \
     -c "SELECT count(*) FROM pending_approvals;" \
     -c "SELECT count(*) FROM signing_credentials;"
   ```

The role roster, audit trail, runtime settings and enrolled signing credentials now contain
exactly what was in the backups — anything recorded after the snapshots were taken is gone.

---

## Troubleshooting / FAQ

| Symptom | Check |
| --- | --- |
| `ps-cli` reports "Could not reach PS Service" | Is the target URL right (`ps-cli config get-contexts` / `echo $PS_CLI_SERVICE_URL`)? Is PS Service actually running there? |
| A command fails right after connecting | `ps-cli get health` — reports whether FalkorDB, the LLM Interface, and Cellar/ELI are all reachable, and which is not if any aren't. `health: alive` with `ready: not_ready` means the process is up but FalkorDB is unreachable, or required ingestion config is incomplete — LLM Interface/Cellar-ELI issues are named in `unhealthy_dependencies` without flipping `ready` to `not_ready`. |
| `ingest regulation` / `ingest document` fails immediately with a config-related error | PS Service's ingestion-required config (`PS_LLMINTERFACE_MODEL`, `PS_LLMINTERFACE_EMBED_MODEL`, `PS_COMPANYMERGE_SIMILARITY_THRESHOLD`) is likely missing — this is a PS Service operator/deployer concern, see [Helm Chart Values Reference](./helm-chart-values-reference.md#core-values). |
| `ingest regulation` / `ingest document` / `check regulations` fails immediately with "LLM Interface is unavailable" | PS Service's LLM Interface is unreachable — `ps-cli`'s pre-flight check caught it before any pipeline call; check PS Service's `/ready` endpoint and its LLM provider configuration. |
| Referencing a context that doesn't exist | `ps-cli` exits non-zero and lists valid context names — see [User Guide: ps-cli Troubleshooting](./user-guide.md#troubleshooting). |

## Teardown

Nothing is torn down automatically:

```bash
az group delete --name rg-policy-system --yes
```

The resource group delete covers everything RG-scoped (AKS, the AIServices
account, Key Vault, networking); `deploy-ps.sh` creates nothing tenant-level, so
there is nothing to clean up outside the resource group.
