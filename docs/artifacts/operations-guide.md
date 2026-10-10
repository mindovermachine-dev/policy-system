# Policy System Operations Guide

## Table of Contents

- [Evaluator (local-test) operations](#evaluator-local-test-operations)
  - [Updating to the latest version](#updating-to-the-latest-version)
  - [Updating or reinstalling the Policy System Plugin](#updating-or-reinstalling-the-policy-system-plugin)
  - [Owner recovery, certificate renewal and re-running the script](#owner-recovery-certificate-renewal-and-re-running-the-script)
  - [Reaching the Authentik admin UI (evaluator)](#reaching-the-authentik-admin-ui-evaluator)
  - [Recreating the kind cluster](#recreating-the-kind-cluster)
  - [Cleanup / teardown](#cleanup--teardown)
  - [Rotating the Azure LLM API key](#rotating-the-azure-llm-api-key)
- [Production operations](#production-operations)
  - [Updating to the latest version](#updating-to-the-latest-version-1)
  - [Upgrading to the graph mutation log](#upgrading-to-the-graph-mutation-log)
  - [Graph replay at startup and a gated graph](#graph-replay-at-startup-and-a-gated-graph)
  - [Owner recovery and the Authentik admin UI](#owner-recovery-and-the-authentik-admin-ui)
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

Run [`scripts/ps-upgrade.sh`](../../scripts/ps-upgrade.sh). It upgrades the release with
`--reset-then-reuse-values`: the new chart's defaults apply, everything you (or
`scripts/deploy-ps-eval.sh`) supplied at install — the local certificate Secret, Authentik, the
bootstrap owner and issuer — is kept, and only `llm.existingSecret` is set again. It also
updates the client with [`ps-cli/install.sh`](../../ps-cli/install.sh). The reuse behaviour is
covered by a test against a fake `helm`; it has not been run against a live upgrade.

To do the same by hand (Helm 3.14 or newer):

```bash
helm upgrade --install policy-system oci://ghcr.io/mindovermachine-dev/charts/policy-system \
  --reset-then-reuse-values --set llm.existingSecret=policy-system-llm-credentials --wait --wait-for-jobs
```

`scripts/deploy-ps-eval.sh` re-applies its values only when they changed, so it does not pick
up a newer chart version by itself.

The upgrade also runs a provisioning Job for the graph mutation log; see
[Upgrading to the graph mutation log](#upgrading-to-the-graph-mutation-log).

Then check the PS Service pod:

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

### Updating or reinstalling the Policy System Plugin

The Policy System Plugin (`ps-plugin`) comes from the Policy System Marketplace (`ps-marketplace`)
and provides the Policy System MCP connector (`ps-mcp`). Updating the backend does not update the
plugin; refresh it separately:

```bash
claude plugin marketplace update ps-marketplace
claude plugin update ps-plugin@ps-marketplace
```

In Claude Desktop, use **Update marketplace** under **Customize** → **Plugins** instead (verify
the wording on screen). To reinstall from scratch, run `/plugin install ps-plugin@ps-marketplace`.

### Owner recovery, certificate renewal and re-running the script

Re-running [`scripts/deploy-ps-eval.sh`](../../scripts/deploy-ps-eval.sh) is safe and is the
recovery path for the owner; it is gated by access to the cluster (the script needs `kubectl`
against it and reads the shared Authentik token from its Secret). With the same
email as before:

- **Owner with no passkey registered** (the enrolment link expired or was never used): a fresh
  single-use link is printed.
- **Owner who already has a passkey**: unchanged, no link is issued. If the only device was lost,
  remove it from the owner's user page in the Authentik admin UI (next section) and re-run.
- **Certificate renewal**: the script regenerates the local certificate when it is within 30 days
  of expiry or the hostname changed (the certificate lasts 397 days; the local CA 10 years). It
  then makes Authentik serve the new certificate by re-applying its certificate blueprint, with no
  manual restart, and restarts PS Service only if it had to. Browsers already trust the same CA,
  so nothing changes for users; a CA that expires is not renewed automatically.
- **The Authentik bootstrap token does not rotate.** Authentik applies `AUTHENTIK_BOOTSTRAP_TOKEN`
  once per tenant, so changing the chart-generated Secret later does not change the token Authentik
  holds; the script then stops with "Authentik rejected the shared API token" before creating
  anything. See [Installation Guide: Upgrading an existing
  install](./installation-guide.md#upgrading-an-existing-install) for the fix.

### Reaching the Authentik admin UI (evaluator)

In the evaluator profile the admin UI is on the same HTTPS listener as the login pages, reachable
from any machine that can reach this laptop's port `30443` — by design. Open
`https://authentik.local:30443/if/admin/` (your `--hostname`) in a browser that trusts the local
CA and sign in as the owner. To keep it to your own machine, block port `30443` from the LAN in the
host firewall. See [Installation Guide: What is exposed
(evaluator)](./installation-guide.md#what-is-exposed-evaluator).

### Recreating the kind cluster

`deploy/kind/cluster.yaml`'s port mappings are fixed when a cluster is created. A `policy-system`
cluster created before Authentik was served over HTTPS lacks the `30443` mapping (it had a
plain-HTTP `30080` one), so it must be deleted and recreated — `kind delete cluster --name
policy-system`, then the `kind create cluster` command from the installation guide. This destroys
the cluster's PVC data (FalkorDB graph, PS Postgres, Authentik's Postgres), so load curated
content again afterwards. The local CA and certificate live outside the cluster (default
`~/.config/policy-system/eval-tls`) and are reused by the next `scripts/deploy-ps-eval.sh` run,
which also creates the owner again (a new cluster has a new Authentik).

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

`scripts/deploy-ps-prod.sh`'s Helm reconciliation (`ensure_release`) only calls `helm upgrade
--install` when one of the 9 script-managed fields (`llm.existingSecret`,
`psService.auth.{issuer,audience,cliClientId,scopes}`,
`psService.authzBootstrapOwner.{subject,issuer}`, `psService.authentik.{baseUrl,publicUrl}`)
differs from what's already deployed. If none of those changed, re-running
`scripts/deploy-ps-prod.sh` is a no-op and will **not** pick up a new chart version.

Use [`scripts/ps-upgrade.sh`](../../scripts/ps-upgrade.sh): it upgrades with
`--reset-then-reuse-values`, so the values the release already has (`psService.auth.*`, the
bootstrap owner, `psService.authentik.{baseUrl,publicUrl}`) are kept, and `values-prod.yaml` and
the new chart's defaults still apply. It refuses to run if the release has no stored
`psService.auth.issuer`. The reuse behaviour is covered by a test against a fake `helm`; it has
not been run against a live AKS upgrade. By hand (Helm 3.14 or newer):

```bash
helm upgrade --install policy-system oci://ghcr.io/mindovermachine-dev/charts/policy-system \
  -f charts/policy-system/values-prod.yaml --reset-then-reuse-values \
  --set llm.existingSecret=policy-system-llm-credentials --wait --wait-for-jobs
```

`ps-cli/install.sh` updates the client (the script runs it for you).

```bash
kubectl get pods -l app.kubernetes.io/component=ps-service \
  -o custom-columns='NAME:.metadata.name,IMAGE:.spec.containers[0].image,STATUS:.status.phase'
```

The upgrade also runs a provisioning Job for the graph mutation log. See
[Upgrading to the graph mutation log](#upgrading-to-the-graph-mutation-log).

### Upgrading to the graph mutation log

**Applies to:** every `helm upgrade` of a release installed before the graph mutation log
existed (the evaluator and production profiles alike), and to fresh installs, which take the same
path.

PS Postgres gains a `graph_log` schema in the `ps_state` database. It holds five tables: the
insert-only mutation log (`groups`, `entries`, `payloads`), the digest `checkpoints`, and the
`applied_markers` (the only table that is updated). A dedicated non-login owner role
(`ps_state_graph_owner` by default, `psPostgres.state.graphOwnerRole`) owns them. The `ps_state`
role that PS Service connects as is not a member of that role and holds only `SELECT` and
`INSERT` on the log tables (plus `UPDATE` on the markers), so PS Service cannot alter, truncate
or drop the log. Existing `ps_state` tables and their rows are not touched.

The Postgres init script that creates the owner role runs **only on an empty `PGDATA`**, so it
can never reach an existing cluster. Existing deployments are covered by the `ps-state-provision`
Job, not by the script:

1. `helm upgrade` renders the Job `<release>-ps-state-provision-r<revision>`. It runs
   `python -m ps_service.graph_gateway.provision` from the PS Service image with the admin
   credential (the `psPostgres.admin` Secret, which only this Job and the Postgres pod reference;
   the `ps-service` pod never does). On one admin connection it first applies the pending
   ordinary `ps_state` migrations (audit, authorization, runtime config, ingestion runs),
   executing as `ps_state` through `SET ROLE`, so `ps_state` owns every public table exactly as
   when PS Service applied them itself. It then creates the owner role if absent and the
   `graph_log` schema, tables, ownership and grants. From an empty database that is the only step
   needed, so the Job does not wait for PS Service. The command is idempotent: a repeat run
   applies nothing. The admin credential must be allowed to `SET ROLE` to `ps_state` (the chart's
   bootstrap superuser is); otherwise the Job fails with `admin connection cannot SET ROLE
   ps_state`.
2. [`scripts/ps-upgrade.sh`](../../scripts/ps-upgrade.sh) and `scripts/deploy-ps-prod.sh` pass
   `--wait --wait-for-jobs`, so a Job that exhausts its retries fails the upgrade with a Helm
   error naming the Job; it does not hang. An upgrade run by hand must pass the same two flags
   (see the command in [Updating to the latest version](#updating-to-the-latest-version-1)).
3. A failing migration exits non-zero naming the component and file (never SQL, host or
   password) and creates no `graph_log` object; migrations already applied stay and the next run
   resumes. The Job retries in place up to `psPostgres.provisioning.backoffLimit` (default `3`).
   While Postgres is still starting, one attempt waits up to
   `psPostgres.provisioning.connectTimeoutSeconds` (default `120`);
   `psPostgres.provisioning.activeDeadlineSeconds` (default `900`) bounds the whole Job, and the
   chart refuses to render when `(backoffLimit + 1) * connectTimeoutSeconds` exceeds it.
4. The Job and a starting PS Service serialize on a Postgres advisory lock (the migration lock),
   so they can run at the same time. A runner that cannot get the lock within 60 s (the
   Job: `PS_STATE_PROVISION_LOCK_TIMEOUT_SECONDS`) fails with `could not acquire the migration
   lock`. It means another session is holding the lock: a migration is still running, or a stuck
   session was left behind. Check `pg_locks` for an advisory lock and
   `pg_stat_activity` for the holder, end a stuck session, then restart PS Service (the Job
   retries by itself).

Confirm it ran:

```bash
kubectl get job -l app.kubernetes.io/component=ps-state-provision
kubectl logs job/<job-name>     # the name printed above
POD=$(kubectl get pod -l app.kubernetes.io/component=ps-postgres -o jsonpath='{.items[0].metadata.name}')
STATE_PW=$(kubectl get secret policy-system-ps-postgres-state-credentials \
  -o jsonpath='{.data.PS_STATE_POSTGRES_PASSWORD}' | base64 -d)
kubectl exec "$POD" -- env PGPASSWORD="$STATE_PW" psql -h 127.0.0.1 -U ps_state -d ps_state \
  -c "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'graph_log';"
```

Every table is owned by `ps_state_graph_owner`, not by `ps_state`.

**If it has not run** (for example `psPostgres.provisioning.enabled=false`, or the Job failed):
PS Service fails closed at startup and exits, and `/ready` reports `not_ready`. The log names the
missing migration and gives the remedy; the message reads `privileged migration
graph_gateway/0001_graph_mutation_log.sql is not in place (<reason>); run
python -m ps_service.graph_gateway.provision with the admin credential`. PS Service never
creates these tables itself. To run the step by hand, use the same CLI with the admin Secret
(`PS_STATE_ADMIN_POSTGRES_USER`, `PS_STATE_ADMIN_POSTGRES_PASSWORD`, the target
`PS_STATE_POSTGRES_HOST`, `_PORT`, `_DATABASE`, `_USER`, and optionally `PS_STATE_GRAPH_OWNER_ROLE`):

```bash
kubectl port-forward svc/policy-system-ps-postgres 5432:5432 &
ADMIN_PW=$(kubectl get secret policy-system-ps-postgres-admin-credentials \
  -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)
PS_STATE_POSTGRES_HOST=127.0.0.1 PS_STATE_POSTGRES_PORT=5432 \
  PS_STATE_POSTGRES_DATABASE=ps_state PS_STATE_POSTGRES_USER=ps_state \
  PS_STATE_ADMIN_POSTGRES_USER=postgres_admin PS_STATE_ADMIN_POSTGRES_PASSWORD="$ADMIN_PW" \
  uv run python -m ps_service.graph_gateway.provision
```

Use `psPostgres.admin.existingSecret`'s `POSTGRES_PASSWORD` instead if you set one. The command
runs from a checkout (or any environment with the PS Service package) and goes through the
port-forward, because the NetworkPolicy admits Postgres ingress for the provisioning Job pod only
while `psPostgres.provisioning.enabled` is true. Developers running `scripts/ps-service.sh start`
against their own state Postgres run the same command once with admin credentials; until then PS
Service refuses to start.

**Backup and restore.** `pg_dump` as `ps_state` is unchanged and includes `graph_log`. Restoring
`ps_state` now needs the admin credential and the owner role; see
[Restore](#ps-postgres-ps_state-and-ps_signing-1). The single-backup design is tracked in #216.

**What this does not protect.** The `ps_state` role still owns the `ps_state` database (and the
`public` schema, including `audit_events`). It can therefore `DROP DATABASE` from another
connection or drop `public` objects; the log tables are protected from alteration by that role,
not the database containing them. Making `audit_events` insert-only is #151, and moving database
ownership is outside this change. Do not describe the log as undroppable.

### Graph replay at startup and a gated graph

**Applies to:** every start of PS Service once the graph mutation log is in place.

On every start PS Service looks at each logged graph and, before it takes traffic, rebuilds from
the log any graph that is empty or half rebuilt (for example a FalkorDB that lost its data, or a
rebuild that was interrupted), resumes an interrupted rebuild where it stopped, and applies any
entries a graph is owed. It judges a graph by looking at its content, not by the applied marker,
which can still say everything is applied after FalkorDB lost the graph. Nothing is configured
for this. The log entries carry the action `startup_replay` (`replayed`, `resumed`, `caught_up`,
`untouched`, `gated`, `retry`) and `replay_graph` for each rebuilt graph.

What you see while it runs:

- `/health` stays `200`. `/ready` answers `503` `not_ready` with `graph_replay` in
  `unhealthy_dependencies`, so the pod is out of the Service rotation. Any other path except
  `/.well-known/` answers `503` with the same body and `Retry-After: 5`, so no caller reads a
  graph that is empty or half rebuilt. A rebuild of a graph the size of CRA times ten (about
  160,000 log entries) took 45 seconds on a laptop; do not restart the pod just because it takes
  a minute.
- If Postgres or FalkorDB is down, the replay is tried again with a growing wait, capped at
  `startup_replay_max_backoff_seconds`, until it can finish; the entries `startup_replay` /
  `retry` show the attempt and the error class.

**A gated graph.** `/ready` always lists `gated_graphs`. A graph appears there when its rebuild
failed (the rebuilt graph did not match its recorded digest, the log has a gap or an entry that
does not decode, or the rebuild went past its checkpoint without being compared) or when
FalkorDB refuses one of its logged entries. The other graphs are served and `/ready` can be `200`
while one is gated; writes to the gated graph are refused with an error naming the graph and the
log position. Nothing repairs it silently. After you have understood the cause (the log entries
name the graph, the position and the error class, never content), rebuild it from the log:

```bash
POD=$(kubectl get pod -l app.kubernetes.io/component=falkordb -o jsonpath='{.items[0].metadata.name}')
kubectl exec "$POD" -c falkordb -- redis-cli DEL <graph>      # the name from gated_graphs
kubectl rollout restart deploy/<ps-service deployment>        # the empty graph is rebuilt at start
```

Deleting the key removes the graph and the progress record kept inside it; the log is not
touched. If the rebuild fails again at the same position, the log itself or a checkpoint is
wrong: keep the pod as it is and escalate with the logged position.

### Owner recovery and the Authentik admin UI

Re-running `scripts/deploy-ps-prod.sh` with the same owner email (`--owner-email` or
`AUTHZ_OWNER_EMAIL`) is the owner-recovery path, gated by cluster access: an owner with no passkey
registered gets a fresh single-use link (printed on `https://<host>/auth/if/flow/...`); an owner
with one is left unchanged and no link is issued. If the only device was lost, remove it from
the owner's user page in the admin UI and re-run.

The Authentik admin UI is **not** on the public Ingress; reach it from a machine with cluster
access (see [Installation Guide, step 4](./installation-guide.md#4-access-the-cluster-with-kubelogin))
through a port-forward, and leave it running while you work:

```bash
kubectl port-forward svc/policy-system-authentik-server 9000:80
```

Then open `http://localhost:9000/auth/if/admin/` and sign in as the owner. Note the `/auth`
prefix. The public Ingress routes only eight user-facing path prefixes; see [Installation Guide:
What is exposed (production)](./installation-guide.md#what-is-exposed-production). The Authentik
API token is the Authentik bootstrap token, stored in the Secret
`policy-system-authentik-api-token`; it does not rotate, so do not change the Secret alone (see
[Installation Guide: Upgrading an existing
install](./installation-guide.md#upgrading-an-existing-install)).

### Rotate the API key later

```bash
scripts/deploy-ps-prod.sh --rotate-key
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
way `deploy-ps-prod.sh` does (a subscription-derived hash, not a fixed string) so there's
nothing to look up by hand. Give it a minute or two after starting before checking
pod status — see the `kubectl get pods` command in [Updating to the latest
version](#updating-to-the-latest-version-1) above.

### Manual steps and operational notes

Manual steps and known gaps in `scripts/deploy-ps-prod.sh`'s automation, current as of
this guide:

1. **AOAI SKU/quota discovery — partly automated.** `deploy-ps-prod.sh` validates
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
2. **AKS node VM-size allowlist + vCPU quota — automated.** `scripts/deploy-ps-prod.sh`
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
| `ps_state` | `ps_state` | `access_role_assignments`, `audit_events` (the permanent, insert-only audit trail), `runtime_config` (runtime-mutable settings, including the curated-catalog source override), `ingestion_runs` (status and result of each MCP-submitted ingestion run), the `graph_log` schema (the insert-only graph mutation log: `groups`, `entries`, `payloads`, `checkpoints`, `applied_markers`, owned by the non-login role `ps_state_graph_owner`), and the `ps_schema_migrations` tracking table (the `graph_gateway` component is tracked there too, but applied by the admin-credential provisioning Job, not by PS Service) | `policy-system-ps-postgres-state-credentials` (`PS_STATE_POSTGRES_PASSWORD`) |
| `ps_signing` | `ps_signing` | Passkey Signing data (`pending_approvals`, `signing_credentials`) and its own `schema_migrations` tracking table | `policy-system-ps-postgres-signing-credentials` (`PS_PASSKEYSIGNING_POSTGRES_PASSWORD`) |

The provisioning Job creates the public `ps_state` tables, executing as `ps_state` (`SET ROLE`),
so `ps_state` owns them; PS Service at startup applies only migrations newer than the Job's
image. The `graph_log` tables are owned by `ps_state_graph_owner` instead.

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

`pg_dump` as `ps_state` still captures the `graph_log` tables (the role holds `SELECT` on them);
the dump records their owner as `ps_state_graph_owner` and their grants. Restoring them needs more
than `ps_state` can do; see Restore below.

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
   same tables, and on an empty one). **`ps_state` must be restored with the admin credential**,
   not as `ps_state`: the `graph_log` tables are owned by `ps_state_graph_owner`, and `--clean`
   drops and recreates them, which `ps_state` is deliberately unable to do (it would fail on the
   first `graph_log` table). The owner role must exist first (the provisioning Job and the init
   script create it; a restore into a brand-new server needs one of them to have run, because
   `pg_dump` does not carry roles). `ps_signing` is unaffected:
   ```bash
   ADMIN_PW=$(kubectl get secret policy-system-ps-postgres-admin-credentials \
     -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)
   kubectl exec "$POD" -- env PGPASSWORD="$ADMIN_PW" \
     pg_restore -h 127.0.0.1 -U postgres_admin -d ps_state --clean --if-exists /tmp/ps_state.dump
   kubectl exec "$POD" -- env PGPASSWORD="$SIGNING_PW" \
     pg_restore -h 127.0.0.1 -U ps_signing -d ps_signing --clean --if-exists /tmp/ps_signing.dump
   kubectl exec "$POD" -- rm /tmp/ps_state.dump /tmp/ps_signing.dump
   ```
   Use `psPostgres.admin.existingSecret`'s `POSTGRES_PASSWORD` if you set one, and
   `psPostgres.admin.user` if you changed it from `postgres_admin`. The dump is restored with its
   original owners and grants, so the log tables stay owned by `ps_state_graph_owner`. A single
   backup that covers PS Postgres end to end is tracked in #216.
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
| `ps-cli auth login` (or the plugin's MCP bridge) against the evaluator reports "Could not verify the TLS certificate of ..." | The issuer is HTTPS with a locally issued certificate and the CA is not in the OS trust store, which `ps-cli` and the bridge verify against. Trust the CA there (`scripts/deploy-ps-eval.sh --apply-host-setup` does this on the evaluator's own machine). A plain "Could not reach ..." means the host is unreachable instead. See [Installation Guide, step 8](./installation-guide.md#8-register-your-passkey-and-log-in-with-ps-cli). |
| A browser shows a certificate warning, or passkey registration is refused, on the evaluator | The browser does not trust the local CA, or the hostname is not mapped in `/etc/hosts` on that machine. Passkeys need a trusted HTTPS origin. Trust `ca.pem` and map the hostname (see step 8). |
| The deploy script stops with "Authentik rejected the shared API token" | Authentik's bootstrap token never rotates and the Secret no longer matches it. See [Installation Guide: Upgrading an existing install](./installation-guide.md#upgrading-an-existing-install). |
| An owner or invite link says "No recovery flow set" or the script waited for the blueprint | The bundled Authentik blueprint had not applied yet (about a minute on a fresh install). The scripts wait for it; re-run once Authentik's worker is Ready. |
| Referencing a context that doesn't exist | `ps-cli` exits non-zero and lists valid context names — see [User Guide: ps-cli Troubleshooting](./user-guide.md#troubleshooting). |

## Teardown

Nothing is torn down automatically:

```bash
az group delete --name rg-policy-system --yes
```

The resource group delete covers everything RG-scoped (AKS, the AIServices
account, Key Vault, networking); `deploy-ps-prod.sh` creates nothing tenant-level, so
there is nothing to clean up outside the resource group.
