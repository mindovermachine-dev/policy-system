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
  - [Authz Postgres (including audit_events)](#authz-postgres-including-audit_events)
- [Restore](#restore)
  - [FalkorDB](#falkordb-1)
  - [Authz Postgres (including audit_events)](#authz-postgres-including-audit_events-1)
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
FalkorDB PVC (and its data), Key Vault, and the Entra app registrations are all
untouched. PS Service and `ps-cli` are unreachable for the whole time the cluster
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
3. **Global Admin admin-consent fallback — remains manual.** If the signed-in
   identity lacks Global Administrator / Privileged Role Administrator,
   `deploy-ps.sh` prints the exact command for a colleague with that role to run:
   ```bash
   az ad app permission admin-consent --id <cli-app-id>
   ```
   (the real `<cli-app-id>` is printed inline). Re-run `scripts/deploy-ps.sh`
   afterward — it detects the grant and continues past this step.
4. **`kubectl rollout restart` FalkorDB startup-race workaround — remains
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

### Authz Postgres (including audit_events)

`ps.service.authz` (issue #133) and the shared `ps.service.audit` trail it writes through
(issue #147) persist to their own PostgreSQL database — configured via `PS_AUTHZ_POSTGRES_HOST`/
`_PORT`/`_DATABASE`/`_USER`/`_PASSWORD` — holding `access_role_assignments` (who currently
holds `SystemOwner`/`SystemAdmin`/`PolicyManager`) and `audit_events` (the permanent,
insert-only record of every bootstrap/grant/revoke, applied or denied, and every future
audited action that adopts the shared `AuditStore`). This is a **distinct** PostgreSQL
instance from FalkorDB, from Passkey Signing's own Postgres (`psServiceSigning.postgres`,
`docs/artifacts/helm-chart-values-reference.md#passkey-signing-postgres-values`), and from
Authentik's own bundled Postgres — even if an operator points more than one of these at the
same physical server, per `ServiceConfig.authz_postgres_host`'s own documented allowance
(`ps-service/src/ps_service/config.py`).

Unlike Passkey Signing (`<release>-signing-postgres` Deployment/Service, chart-rendered),
this chart does not yet render a dedicated Kubernetes resource for the authz Postgres, nor
does `ps-service-deployment.yaml` yet wire `PS_AUTHZ_POSTGRES_*` into the PS Service pod's
own environment the way it wires the Signing Postgres equivalents — an operator provisions
one independently (in-cluster or managed) and supplies `PS_AUTHZ_POSTGRES_*` to PS Service
out of band (until that chart wiring exists, a values override or a separately-applied
Secret/env-injection mechanism of the operator's own choosing). Because it's plain
PostgreSQL regardless of how it's provisioned, back it up with standard, unmodified
`pg_dump` tooling — no Policy System-specific backup feature exists or is planned here
either, mirroring the FalkorDB section above.

1. Confirm the connection you configured for PS Service (`PS_AUTHZ_POSTGRES_HOST`/`_PORT`/
   `_DATABASE`/`_USER`/`_PASSWORD`) — wherever you set it, since the chart does not render
   these today. If you did wire them into the PS Service pod's own environment yourself, you
   can confirm what's actually live there:
   ```bash
   kubectl exec deployment/policy-system-ps-service -- env | grep PS_AUTHZ_POSTGRES_
   ```
2. Take a full logical backup of that database, in `pg_dump`'s custom format (supports
   selective `pg_restore` later, and is compressed by default). If the instance is reachable
   directly (e.g. a managed cloud Postgres, or one exposed outside the cluster):
   ```bash
   PGPASSWORD="$PS_AUTHZ_POSTGRES_PASSWORD" pg_dump \
     -h "$PS_AUTHZ_POSTGRES_HOST" -p "$PS_AUTHZ_POSTGRES_PORT" \
     -U "$PS_AUTHZ_POSTGRES_USER" -d "$PS_AUTHZ_POSTGRES_DATABASE" \
     -Fc -f "authz-postgres-backup-$(date +%Y%m%d).dump"
   ```
   If it's only reachable in-cluster (e.g. co-located with Passkey Signing's own pod, or a
   self-hosted `authz-postgres` pod you've added), run the same command inside that pod and
   copy the result out, mirroring the FalkorDB pod-exec-then-`kubectl cp` pattern above:
   ```bash
   POD=<the authz Postgres pod's name>
   kubectl exec "$POD" -- env PGPASSWORD="$PS_AUTHZ_POSTGRES_PASSWORD" pg_dump \
     -U "$PS_AUTHZ_POSTGRES_USER" -d "$PS_AUTHZ_POSTGRES_DATABASE" -Fc \
     -f "/tmp/authz-postgres-backup-$(date +%Y%m%d).dump"
   kubectl cp "$POD":/tmp/authz-postgres-backup-$(date +%Y%m%d).dump \
     "./authz-postgres-backup-$(date +%Y%m%d).dump"
   ```

Store the resulting `.dump` file wherever your backup retention policy requires — it is a
complete, standalone copy of `access_role_assignments` and `audit_events` (and any future
table `ps.service.authz`/`ps.service.audit` add to this same database).

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

### Authz Postgres (including audit_events)

Restoring loads a previously captured `pg_dump` custom-format file back into the authz
Postgres database, replacing its current `access_role_assignments`/`audit_events` (and any
other table in that database) with the backup's contents. Run this against a database that
already exists and is reachable at the configured `PS_AUTHZ_POSTGRES_*` connection — a fresh
install already has one (whatever an operator provisioned per the Backup section above),
even before `ps-service` has run its own migrations against it.

1. Restore into the target database, dropping and recreating each object the dump contains
   before loading it (`--clean --if-exists` — safe even on a database that already has some
   or all of the same tables/rows):
   ```bash
   PGPASSWORD="$PS_AUTHZ_POSTGRES_PASSWORD" pg_restore \
     -h "$PS_AUTHZ_POSTGRES_HOST" -p "$PS_AUTHZ_POSTGRES_PORT" \
     -U "$PS_AUTHZ_POSTGRES_USER" -d "$PS_AUTHZ_POSTGRES_DATABASE" \
     --clean --if-exists "authz-postgres-backup-YYYYMMDD.dump"
   ```
   (In-cluster equivalent: `kubectl cp` the `.dump` file into the target pod, then run the
   same `pg_restore` command via `kubectl exec`, mirroring the Backup section's own
   pod-exec pattern.)
2. Restart `ps-service` so any in-process connection pool picks up a consistent view of the
   now-restored database, and so its migration runner (`ps_service.authz.migration_runner`)
   re-checks the restored schema against its own tracking table on next start:
   ```bash
   kubectl rollout restart deployment/policy-system-ps-service
   ```
3. Verify the restore landed before relying on it — compare row counts against what the
   backup was taken from (or, at minimum, confirm both tables are non-empty and the most
   recent `occurred_at` in `audit_events` matches what you expect from the backup's own
   timing):
   ```bash
   PGPASSWORD="$PS_AUTHZ_POSTGRES_PASSWORD" psql \
     -h "$PS_AUTHZ_POSTGRES_HOST" -p "$PS_AUTHZ_POSTGRES_PORT" \
     -U "$PS_AUTHZ_POSTGRES_USER" -d "$PS_AUTHZ_POSTGRES_DATABASE" \
     -c "SELECT count(*) FROM access_role_assignments;" \
     -c "SELECT count(*), max(occurred_at) FROM audit_events;"
   ```

The role roster and audit trail now contain exactly what was in the backup — any grant,
revoke, or audited action recorded after that snapshot was taken is gone.

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

```bash
az ad app delete --id $(az ad app list --display-name "Policy System API" --query "[0].appId" -o tsv)
```

```bash
az ad app delete --id $(az ad app list --display-name "Policy System CLI" --query "[0].appId" -o tsv)
```

The resource group delete covers everything RG-scoped (AKS, the AIServices
account, Key Vault, networking). The two Entra app registrations are tenant-level
and survive an RG delete, so they need their own delete calls.
