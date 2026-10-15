# Policy System Operations Guide

## Table of Contents

- [Evaluator (local-test) operations](#evaluator-local-test-operations)
  - [Updating to the latest version](#updating-to-the-latest-version)
  - [Resetting the evaluator data](#resetting-the-evaluator-data)
  - [Granting an access role from the command line](#granting-an-access-role-from-the-command-line)
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
- [Imported policy trees (restore and internal ingest)](#imported-policy-trees-restore-and-internal-ingest)
- [Backup](#backup)
  - [PS Postgres (`ps_state` and `ps_signing`)](#ps-postgres-ps_state-and-ps_signing)
  - [Take a backup](#take-a-backup)
  - [Check the backup](#check-the-backup)
  - [Contents and protection](#contents-and-protection)
  - [Authentik is not covered](#authentik-is-not-covered)
  - [Operational notes](#operational-notes)
- [Restore](#restore)
  - [Before you start](#before-you-start)
  - [Restore with the script](#restore-with-the-script)
  - [Restore by hand](#restore-by-hand)
  - [If a credentials Secret is lost](#if-a-credentials-secret-is-lost)
- [Troubleshooting / FAQ](#troubleshooting--faq)
  - [Reading a failed ingestion](#reading-a-failed-ingestion)
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
flag needed to reset one. Your graph data is kept: the FalkorDB
`PersistentVolumeClaim` only spares PS Service a replay at startup. The graph content itself is
rebuilt from the graph mutation log in PS Postgres (see [Backup](#backup)), so previously loaded
regulations do not need to be re-seeded.

### Resetting the evaluator data

Run [`scripts/ps-reset-data.sh`](../../scripts/ps-reset-data.sh) to start the evaluator from an empty store, then `scripts/ps-upgrade.sh` to recreate what it removed:

```bash
scripts/ps-reset-data.sh && scripts/ps-upgrade.sh
```

The script deletes the PS Service, FalkorDB and PS Postgres deployments and the FalkorDB and PS Postgres data volumes. That removes the graph, the graph mutation log, ingestion runs, audit events, access-role grants, the catalog source setting and the passkey signing data. Authentik (users, passkeys and the OIDC application) is kept, so you do not log in again. The upgrade recreates the deleted resources and the provisioning Job reapplies the migrations. The script asks for confirmation; `--yes` skips the prompt. It refuses to run unless the active `kubectl` context is a `kind-*` context, so it cannot be used on a production cluster.

### Granting an access role from the command line

[`scripts/grant-access-role.sh`](../../scripts/grant-access-role.sh) calls the `grant-access-role` tool of the current `ps-cli` context with the token that `ps-cli auth login` stored:

```bash
scripts/grant-access-role.sh --principal <subject> --role ComplianceOfficer
```

`--role` is `SystemOwner`, `SystemAdmin`, `PolicyManager` or `ComplianceOfficer` (default `ComplianceOfficer`). The caller must hold a role that may grant the requested one (`SystemOwner` for any role; `SystemAdmin` for `PolicyManager` and `ComplianceOfficer`). The tool is never available under the local-test bypass. The `ps-manage-access-roles` skill grants and revokes roles from a Claude session.

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
- **Hostname change** (for example away from a `.local` name, which Claude Code cannot resolve):
  see [Installation Guide: Moving an existing evaluator off a .local
  hostname](./installation-guide.md#moving-an-existing-evaluator-off-a-local-hostname).
- **The Authentik bootstrap token does not rotate.** Authentik applies `AUTHENTIK_BOOTSTRAP_TOKEN`
  once per tenant, so changing the chart-generated Secret later does not change the token Authentik
  holds; the script then stops with "Authentik rejected the shared API token" before creating
  anything. See [Installation Guide: Upgrading an existing
  install](./installation-guide.md#upgrading-an-existing-install) for the fix.

### Reaching the Authentik admin UI (evaluator)

In the evaluator profile the admin UI is on the same HTTPS listener as the login pages, reachable
from any machine that can reach this laptop's port `30443` — by design. Open
`https://authentik.test:30443/if/admin/` (your `--hostname`) in a browser that trusts the local
CA and sign in as the owner. To keep it to your own machine, block port `30443` from the LAN in the
host firewall. See [Installation Guide: What is exposed
(evaluator)](./installation-guide.md#what-is-exposed-evaluator).

### Recreating the kind cluster

`deploy/kind/cluster.yaml`'s port mappings are fixed when a cluster is created. A `policy-system`
cluster created before Authentik was served over HTTPS lacks the `30443` mapping (it had a
plain-HTTP `30080` one), so it must be deleted and recreated — `kind delete cluster --name
policy-system`, then the `kind create cluster` command from the installation guide. This destroys
the cluster's PVC data (FalkorDB graph, PS Postgres, Authentik's Postgres), so load curated
content again afterwards, or restore a PS Postgres backup (see [Restore](#restore)). The local CA
and certificate live outside the cluster (default
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
   The Job also applies `0002_group_swap_set.sql`, which adds a nullable `swap_set_id` column and
   a partial index to `graph_log.groups` (it links the groups of a restore that are swapped in
   together). The column is additive: the new `ps-service` pod refuses to start until the Job has
   recorded the file, and rolling back to the previous image leaves the column in place and
   harmless.
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

**Backup and restore.** The physical [Backup](#backup) includes `graph_log`, its owner role
`ps_state_graph_owner` and the grants. The [Restore](#restore) brings them back as they were, and
the provisioning Job runs again on the restored data with the admin credential.

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

### Graph drift alert

**Applies to:** every PS Service that has a graph mutation log, from the release that adds the
drift check.

PS Service writes its graphs only through the Graph Write Gateway. After the last write of a flow
(a restore, an ingestion, a merge, a re-ingest, a cleanup) the gateway records a digest of the
graph, a short hash of everything in it. Once an hour (`PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS`,
Helm `psService.graphGateway.driftCheckIntervalSeconds`; `0` turns the check off) it compares each
graph with the digest at the head of its log. Nothing is exposed: there is no endpoint, no tool
and no `/ready` field, and a failing check never changes readiness.

A graph that changed behind the gateway's back (someone wrote to FalkorDB directly) shows up as
one log entry per graph per hour, and it is also written to the container's stderr so
`kubectl logs` shows it:

```json
{
  "component": "graph_gateway",
  "action": "drift_check",
  "outcome": "failed",
  "error_class": "GraphDriftDetected",
  "graph": "policy_system",
  "last_position": 1204,
  "checkpoint_digest": "sha256:...",
  "live_digest": "sha256:..."
}
```

`last_position` is the head of the graph's log, the position the checkpoint was taken at. The two
digests are hashes; no content is logged. The graph is never repaired by PS Service: it keeps serving, and the
alert repeats every interval until it is dealt with. Find who wrote to FalkorDB outside PS Service
(the server's own client list and the `kubectl logs` of other pods) and stop them. To bring the
graph back to what the log says, rebuild it from the log as for a gated graph (delete the key,
restart the service):

```bash
kubectl exec "$POD" -c falkordb -- redis-cli DEL <graph>      # the name from the alert
kubectl rollout restart deploy/<ps-service deployment>        # the empty graph is rebuilt at start
```

Other `drift_check` entries are normal and need no action:

| `outcome` | Meaning |
| --- | --- |
| `ok` | The graph matches its checkpoint. |
| `no_baseline` | A write after the last checkpoint moved the head, so there is nothing to compare with yet. The next flow that checkpoints the graph ends it. |
| `skipped` | The check stood aside; `reason` is `busy` (a write was being applied), `unapplied` (the log is ahead of the graph), `in_flight` (a write landed while the graph was read) or `gated` (a replay has the graph closed). Never an alert. |
| `failed` without `GraphDriftDetected` | The check could not run; `error_class` names the exception (for example `ConnectionError` when FalkorDB or Postgres was down). The next pass tries again. |

Each pass also logs `drift_pass` with `graph_count` and `drift_count`, and the monitor logs
`drift_monitor` (`started`, `stopped`, `disabled`) with `interval_seconds`.

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

## Imported policy trees (restore and internal ingest)

An internal instrument carries a Policy/Standard/Control tree. Restoring a curated
internal instrument and ingesting an internal document (`ps-cli ingest document`, or
`POST /ingestions` with `source: "internal"`; each requires `ComplianceOfficer`) import that
tree the same way, through one shared step:

- **Always `draft`.** Every Policy, Standard and Control is written `draft` with its
  authored properties, whatever `status` the artifact or document carried. Only the Policy
  Lifecycle (propose, approve) moves it on. The authored `status` is informational.
- **Owned by the importer.** Each Policy is owned by the verified identity of the caller who
  restored or ingested it. The caller must hold `ComplianceOfficer` and a verified identity:
  without them `POST /ingestions` answers 403 and nothing is written. A document with no
  Policy (or an external regulation) is owned by no one but still needs the role.
- **Never merged into existing policies.** Imported nodes are created only if absent
  (their ids derive from their content). A node that already exists, including one since
  approved, is left untouched when the same content is imported again. A Policy with a
  similar but different title becomes a separate draft; there is no semantic comparison
  and no near-miss review for Policies.
- **Counts.** The merge stage of the ingestion response, and the `succeeded` entry of the
  run log, report `governance_policies`, `governance_standards`, `governance_controls`
  (nodes imported) and `governance_status_overridden` (authored statuses replaced by
  `draft`). A restore's `succeeded` log entry carries the same counts.

Importing is not approval: review the drafts and take them through the Policy Lifecycle.

**What a restore costs.** A restore is recorded in the graph mutation log, so the log grows by about
the size of the instrument (about 35,000 entries for the largest, CRA). While the restore's swap
runs, writers to `policy_system` and to the instrument's graphs wait for it; the wait grows with
the total size of `policy_system`, not with the instrument, and each swap logs it as
`hold_seconds` (action `swap_set`). The target is under 60 seconds for CRA over a `policy_system`
that holds all ten curated instruments; it has been checked on test stores only, not yet on a
live deployment, so watch `hold_seconds` after the first large restore.

## Backup

One backup operation covers everything Policy System keeps: a physical backup (`pg_basebackup`)
of the PS Postgres server, taken from the running pod. There is no separate FalkorDB step. The
graphs are not backed up; PS Service rebuilds and verifies them from the graph mutation log
(`graph_log`), which is part of the backup, when it starts after a [Restore](#restore).

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
to its own database.

The PS state Postgres is a hard dependency: PS Service does not start, and `/ready` reports
`not_ready`, while it is unreachable. Reads of the catalog-source override fail closed — while
the database cannot be read, `GET /catalog`, curated-instrument restores and `get-catalog-source`
return an error rather than serving the env-var/default source. `set-catalog-source` and
`reset-catalog-source` also need this database, including under the local-test bypass.

The backup is physical, so it captures the whole server: both databases, the roles and the
role password hashes. Because the catalog override lives in `ps_state`, a restore brings the
override back with it.

### Take a backup

Run one `pg_basebackup` inside the PS Postgres pod over the local socket, as the admin role
(`psPostgres.admin.user`, default `postgres_admin`), and stream the tar to a local file. The
local socket needs no password, and the default `pg_hba` already admits it.

```bash
kubectl exec deploy/policy-system-ps-postgres -- pg_basebackup -U postgres_admin -D - -Ft -X fetch -c fast \
  > "ps-postgres-$(date -u +%Y%m%dT%H%M%SZ).tar"
```

`-Ft -D -` writes one tar of the data directory to standard output. `-X fetch` includes the
write-ahead log needed to make the copy consistent, so the file is self-contained. `-c fast`
starts the backup at once instead of waiting for the next scheduled checkpoint. The backup runs
against the live database.

`scripts/verify-backup-restore.sh backup [--out-dir DIR]` runs the same command, writes
`ps-postgres-<UTC timestamp>.tar` into `DIR`, performs the checks below, prints the file path
and logs the row counts of `audit_events`, `access_role_assignments` and `graph_log.entries`
before and after. It refuses a context that is not `kind-*` unless `--allow-context <ctx>`
names it.

### Check the backup

A backup is usable only if all three hold:

1. The `kubectl exec` exit code is `0`.
2. The file is not empty.
3. `tar tf <file> > /dev/null` exits `0`, which proves the tar is complete and readable.

Delete a file that fails any check and run the backup again. The script does this for you.

### Contents and protection

The file is a copy of the whole PS Postgres data directory. It contains the role password
hashes for `postgres_admin`, `ps_state` and `ps_signing`, the audit trail, the enrolled passkey
signing credentials and the graph mutation log. It is not encrypted by Postgres or by the
command. Keep it out of any repository, and store it encrypted at rest (an encrypted volume or
bucket, or an encrypting archiver) with access limited to the operators who may restore the
deployment.

A restore also needs the three PS Postgres credentials Secrets (`psPostgres.admin`,
`psPostgres.state` and `psPostgres.signing`, each via `existingSecret`), because the password
hashes in the backup match those passwords. Keep those Secrets separately from the backup file.

### Authentik is not covered

This backup does not include Authentik. Authentik keeps its users, passkeys and OIDC application
in its own Postgres (when enabled), a separate server that `pg_basebackup` of PS Postgres does
not read. A tested Authentik backup and restore procedure is tracked in
[#235](https://github.com/mindovermachine-dev/policy-system/issues/235). Until then, follow the
upstream procedure: <https://docs.goauthentik.io/sys-mgmt/ops/backup-restore>.

### Operational notes

- The init step that creates the two databases and roles runs only when the PVC is empty.
  Rotating a password in its Secret does not change the existing database role; run
  `ALTER ROLE` by hand as well.
- A pre-existing PVC from a chart version that predates this layout does not contain the
  `ps_state` database. Upgrade onto a fresh PVC (nothing is migrated).

## Restore

A restore puts one [backup file](#take-a-backup) (taken with `pg_basebackup -X fetch -c fast`)
back into the PS Postgres server of a running release and empties FalkorDB. PS Service is
stopped for the whole restore. When it starts again it rebuilds and verifies every graph from the
restored graph mutation log (see [Graph replay at startup and a gated graph](#graph-replay-at-startup-and-a-gated-graph)),
so the graphs come back without a separate FalkorDB step. Only graphs recorded in the log come
back; a graph that was never written to the log is not rebuilt.

The backup file holds the whole data directory, including the roles (among them the non-login
`ps_state_graph_owner` that owns the log tables, so no role is created by hand), the role
password hashes, the audit trail and the graph mutation log. Decrypt it first if you stored it encrypted, and keep the
decrypted copy only for the duration of the restore.

### Before you start

1. **Retain the three credentials Secrets.** The role password hashes in the backup match the
   passwords of `psPostgres.admin`, `psPostgres.state` and `psPostgres.signing`, so the
   restored server accepts only those passwords. Keep operator-managed copies of the three
   Secrets outside the cluster and outside the backup file, and point the release at them with
   `psPostgres.admin.existingSecret`, `psPostgres.state.existingSecret` and
   `psPostgres.signing.existingSecret` (keys `POSTGRES_PASSWORD`, `PS_STATE_POSTGRES_PASSWORD`,
   `PS_PASSKEYSIGNING_POSTGRES_PASSWORD`). Use names other than the chart-generated
   `<release>-ps-postgres-{admin,state,signing}-credentials`: once `existingSecret` is set the
   chart does not render the generated Secret and Helm removes it. To retain the current
   passwords of a running release, copy each generated Secret under a new name, stop PS Postgres,
   then upgrade. The upgrade changes the PS Postgres pod template (new Secret names), and on a
   cluster whose volumes are single-node disks (AKS) the replacement pod cannot attach the disk
   while the old pod holds it (`Multi-Attach error`), so the rollout never finishes unless PS
   Postgres is stopped first; the upgrade starts it again:

   ```bash
   for role in admin state signing; do
     kubectl get secret "policy-system-ps-postgres-${role}-credentials" -o json \
       | jq --arg name "ps-${role}-retained" '{apiVersion, kind, type, metadata: {name: $name}, data}' \
       | kubectl apply -f -
   done
   kubectl scale deploy/policy-system-ps-service deploy/policy-system-ps-postgres --replicas=0
   kubectl wait --for=delete pod -l app.kubernetes.io/component=ps-postgres --timeout=180s
   helm upgrade policy-system <chart> --reset-then-reuse-values \
     --set psPostgres.admin.existingSecret=ps-admin-retained \
     --set psPostgres.state.existingSecret=ps-state-retained \
     --set psPostgres.signing.existingSecret=ps-signing-retained \
     --wait --wait-for-jobs
   ```

   On a new cluster, create the Secrets first and install with the same values, so the first
   start of PS Postgres uses the retained passwords.
2. **Use the same chart version as the backup, or a newer one.** The provisioning Job applies any
   migration the newer version adds to the restored databases.
3. **Know the target.** The restore overwrites all PS Postgres data and empties FalkorDB in the
   current kubectl context. Authentik is not touched; restore it separately
   (see [Authentik is not covered](#authentik-is-not-covered), [#235](https://github.com/mindovermachine-dev/policy-system/issues/235)).

### Restore with the script

```bash
scripts/verify-backup-restore.sh restore --artifact ps-postgres-<UTC timestamp>.tar \
  [--chart <chart reference or path>] [--chart-version <version>]
```

The script refuses unless the context is `kind-*` (or named with `--allow-context <ctx>`), the
three `existingSecret` values are set on the release and those Secrets exist, the file is a
readable tar, and you type `yes` (or pass `--yes`). It then runs the steps below in order and
stops at the first failure with `ps-service` still stopped. `--chart` defaults to the published
OCI chart `oci://ghcr.io/mindovermachine-dev/charts/policy-system`; pass `--chart-version` to
keep the version the release runs. The final lines log the row counts of `audit_events`,
`access_role_assignments` and `graph_log.entries` (`event=counts phase=restored`).

### Restore by hand

1. Stop PS Service, FalkorDB and PS Postgres, and wait until their pods are gone:

   ```bash
   kubectl scale deploy/policy-system-ps-service deploy/policy-system-falkordb \
     deploy/policy-system-ps-postgres --replicas=0
   for component in ps-service falkordb ps-postgres; do
     kubectl wait --for=delete pod -l app.kubernetes.io/component=$component --timeout=180s
   done
   ```

2. Replace the data directory and empty FalkorDB's volume from one helper pod that mounts both
   volumes and runs as root (the extracted files keep the owner recorded in the backup, so no
   `chown` is needed):

   ```bash
   kubectl apply -f - <<'EOF'
   apiVersion: v1
   kind: Pod
   metadata:
     name: policy-system-restore-helper
   spec:
     restartPolicy: Never
     containers:
       - name: helper
         image: postgres:16-alpine   # the image of the ps-postgres Deployment
         command: ["sleep", "3600"]
         securityContext:
           runAsUser: 0
         volumeMounts:
           - {name: pg, mountPath: /pg}
           - {name: fdb, mountPath: /fdb}
     volumes:
       - name: pg
         persistentVolumeClaim: {claimName: policy-system-ps-postgres-data}
       - name: fdb
         persistentVolumeClaim: {claimName: policy-system-falkordb-data}
   EOF
   kubectl wait --for=condition=Ready pod/policy-system-restore-helper --timeout=180s
   kubectl exec -i policy-system-restore-helper -- \
     sh -c 'set -e; rm -rf /pg/pgdata; mkdir -p /pg/pgdata; chmod 700 /pg/pgdata; tar -xf - -C /pg/pgdata' \
     < ps-postgres-<UTC timestamp>.tar
   kubectl exec policy-system-restore-helper -- sh -c 'rm -rf /fdb/* /fdb/.[!.]* /fdb/..?*'
   kubectl delete pod policy-system-restore-helper --wait=true
   ```

   FalkorDB is emptied because a FalkorDB that keeps data newer than the restored log is not
   repaired: PS Service starts, `/ready` answers `200`, and the extra data stays in the graph.
3. Start PS Postgres, then FalkorDB, and wait for each:

   ```bash
   kubectl scale deploy/policy-system-ps-postgres --replicas=1
   kubectl rollout status deploy/policy-system-ps-postgres --timeout=300s
   kubectl scale deploy/policy-system-falkordb --replicas=1
   kubectl rollout status deploy/policy-system-falkordb --timeout=300s
   ```

   PS Postgres finds a data directory and skips its init script; the log reads
   `PostgreSQL Database directory appears to contain a database; Skipping initialization`, then
   `completed backup recovery` and `database system is ready to accept connections`.
4. Run the provisioning Job with a Helm upgrade. The Job is named by release revision, so every
   `helm upgrade` runs it, and it applies any migration the restored databases still need.
   Keep `--wait-for-jobs` so a failed Job fails the upgrade:

   ```bash
   helm upgrade policy-system <chart> --reset-then-reuse-values --wait --wait-for-jobs
   ```

   Helm does not change the replica count of a Deployment whose manifest did not change, so
   PS Service stays stopped until step 6.
5. Check the provisioning. The Job of the new revision must be `Complete`, and the PS Postgres
   log must not contain `ps-postgres-init: created roles` (that line means the init script ran
   on an empty data directory, so the restored data is not what the server is using):

   ```bash
   REVISION=$(helm status policy-system -o json | jq -r .version)
   kubectl get job "policy-system-ps-state-provision-r${REVISION}"
   kubectl logs deploy/policy-system-ps-postgres | grep -c 'ps-postgres-init: created roles'   # 0
   ```

   `scripts/verify-backup-restore.sh verify-provision` runs both checks.
6. Start PS Service and wait for `/ready`:

   ```bash
   kubectl scale deploy/policy-system-ps-service --replicas=1
   scripts/verify-backup-restore.sh wait-ready
   ```

   While PS Service rebuilds the graphs from the log, `/ready` can answer `503` with
   `graph_replay` in `unhealthy_dependencies`. The restore is done when it answers `200` with an
   empty `gated_graphs`. A gated graph is handled as described in
   [Graph replay at startup and a gated graph](#graph-replay-at-startup-and-a-gated-graph).
7. Compare the counts with the system the backup was taken from:
   `scripts/verify-backup-restore.sh counts` prints `audit_events`, `access_role_assignments` and
   `graph_log_entries`. They equal the counts logged when the backup was taken; anything
   recorded after the backup is gone, including audit events, role grants and logged graph
   changes.

### If a credentials Secret is lost

Without the retained Secrets, the new passwords do not match the hashes in the backup. The
provisioning Job then cannot authenticate: it retries, and `helm upgrade --wait --wait-for-jobs`
fails when its timeout runs out, while PS Service crash-loops on the state password. Set the roles'
passwords to the new Secrets' values, through the local socket as the admin role (no password is
needed there), and run the upgrade again:

```bash
ADMIN_PW=$(kubectl get secret <admin secret> -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)
STATE_PW=$(kubectl get secret <state secret> -o jsonpath='{.data.PS_STATE_POSTGRES_PASSWORD}' | base64 -d)
SIGNING_PW=$(kubectl get secret <signing secret> -o jsonpath='{.data.PS_PASSKEYSIGNING_POSTGRES_PASSWORD}' | base64 -d)
kubectl exec -i deploy/policy-system-ps-postgres -- psql -U postgres_admin -d postgres \
  -v ON_ERROR_STOP=1 -v admin_pw="$ADMIN_PW" -v state_pw="$STATE_PW" -v signing_pw="$SIGNING_PW" <<'SQL'
ALTER ROLE postgres_admin PASSWORD :'admin_pw';
ALTER ROLE ps_state PASSWORD :'state_pw';
ALTER ROLE ps_signing PASSWORD :'signing_pw';
SQL
helm upgrade policy-system <chart> --reset-then-reuse-values \
  --set psPostgres.admin.existingSecret=<admin secret> \
  --set psPostgres.state.existingSecret=<state secret> \
  --set psPostgres.signing.existingSecret=<signing secret> \
  --wait --wait-for-jobs
```

Use the role names from `psPostgres.admin.user`, `psPostgres.state.user` and
`psPostgres.signing.user` if you changed them. A failed `helm upgrade` leaves the release on its
last deployed revision, and `--reset-then-reuse-values` reuses that revision's values, so pass the
`--set` flags again on the retry. Delete the leftover provisioning Job of the failed revision
(`kubectl delete job policy-system-ps-state-provision-r<N>`).

---

## Troubleshooting / FAQ

| Symptom | Check |
| --- | --- |
| `ps-cli` reports "Could not reach PS Service" | Is the target URL right (`ps-cli config get-contexts` / `echo $PS_CLI_SERVICE_URL`)? Is PS Service actually running there? |
| A command fails right after connecting | `ps-cli get health` reports the process state and readiness and names every unhealthy dependency (`unhealthy_dependencies`: `falkordb`, `state_postgres`, `graph_replay`, `llm_interface`, `cellar_eli`, `passkey_signing_postgres`). Readiness (`/ready`, HTTP 200 or 503) depends only on `falkordb`, `state_postgres` and `graph_replay`, and on the ingestion configuration being complete: `health: alive` with `ready: not_ready` means one of these is down, the startup graph replay has not finished, or required ingestion config is incomplete. `llm_interface`, `cellar_eli` and `passkey_signing_postgres` are named in `unhealthy_dependencies` without flipping `ready` to `not_ready`. |
| `ingest document`, or an `ingest_regulation` / `start_ingestion` tool call, fails immediately with a config-related error | PS Service's ingestion-required config (`PS_LLMINTERFACE_MODEL`, `PS_LLMINTERFACE_EMBED_MODEL`, `PS_COMPANYMERGE_SIMILARITY_THRESHOLD`) is likely missing — this is a PS Service operator/deployer concern, see [Helm Chart Values Reference](./helm-chart-values-reference.md#core-values). |
| `ingest document` (or an `ingest_regulation` / `start_ingestion` tool call) fails with HTTP 503 "graph writes are unavailable: the graph write gateway is not running" | Ingestion, the Domain Mapper (extraction and derivation), Company Merge (the merge stage and near-miss resolution) Policy Lifecycle (every tool that creates, edits, adds to or transitions a Policy, and `get-policy` when a legacy Policy needs its `status`/`version` backfilled) Graph Cleanup (the edit that runs when a passkey approval is signed) and Restore (`POST /restorations`, `POST /restorations/from-catalog` and the `restore_instrument` MCP tool: a restore is one gateway group), including a re-ingest that resumes at any of those stages, write the graph only through the Graph Write Gateway, which needs the PS state database (the graph mutation log). The state database is not configured, or the gateway could not be built at startup; configure it (see [Upgrading to the graph mutation log](#upgrading-to-the-graph-mutation-log)) and restart. Ingestion never writes to FalkorDB directly as a fallback, and neither does the Domain Mapper, Company Merge, Policy Lifecycle, Graph Cleanup or Restore, and nothing was written. A restore answers the same 503 before it reads any graph; it needs the PS state database as well as FalkorDB, and the `restore_instrument` MCP tool answers `error: ...` with the same text. The Policy Lifecycle MCP tools do not answer HTTP 503: without a gateway they answer `error: ...` with the same text, record no audit row and write nothing. The same applies to resolving a near-miss and to signing a near-miss merge approval: without the gateway the request is answered 503 and the approval stays pending, so it can be signed again after the restart. Graph Cleanup answers no 503 either: the signed approval is executed, but without a gateway the edit is not made, the audit trail holds an `applied` row followed by a `failed` row for the approval, and the Compliance Officer is told the edit could not be completed; read the outcome with `check-cleanup-approval`, and preview again after the restart. An approval signed while the graph is behind its log is settled only once the graph has caught up (the check answers with no outcome until then). The same 503 answers the Regulatory Change Monitor's amendment check (the `check_regulations` tool and `POST /change-checks`, including a `finalize` or link-only re-ingest that runs no pipeline stage but still writes the succession; the sweep looks for the gateway first, before any graph is opened, and the MCP tool answers `error: ...`) and Export (`POST /exports`): the Regulatory Change Monitor records the instrument succession and Export its embedding backfill only through the Graph Write Gateway, with no direct-write fallback. Export therefore also needs the PS state database, and so does the maintainer CLI shim `tools/curated-export/export_instrument.py` (`curated-export`), which builds its own gateway. A link-only or `finalize` re-ingest now leaves an `ingestion_run.submit` / `ingestion_run.complete` audit pair, and every Export run one `export.instrument` system audit row. A 503 from the audit trail instead means the run's audit row could not be recorded; nothing was written either, and the request can be retried. |
| A restore is reported as committed but not yet applied (`committed_apply_pending` in the audit trail, `RestoreApplyPendingError`), or writes to `policy_system` or to the instrument's `_native`/`_baseline` graph are refused as blocked | The restore is recorded in the graph log, but the swap that makes it visible did not finish (FalkorDB was unreachable, or refused the swap). Until it finishes nothing of the instrument is visible, and the three graphs of the restore refuse further writes with nothing logged. Restore FalkorDB, then restart `ps-service`: startup recovery retries the swap, and finishes all three graphs together as one unit. Restoring the same instrument again is not needed. Last resort, only if the FalkorDB engine cannot perform the swap: rebuild each of the three graphs from its log in ascending graph-name order (the gateway's `replay_graph`); while they are rebuilt one by one a reader can see the three graphs in different states, so do it in a quiet window. The block lifts once every graph is at the head of its log. |
| `/ready` stays 503 after FalkorDB or PS Postgres failed while the pod was running and then came back | After startup, readiness follows the live dependency health, which only real calls update; a pod that left rotation receives none, so it does not flip back by itself. Restart the `ps-service` pod. |
| The pod was restarted (or received SIGTERM) while an ingestion started with `start_ingestion` was running, and `get_ingestion_status` still shows `running` | The run record stays `running` until the next `get_ingestion_status` call for it, which marks it `failed` with the message that the run was interrupted before it finished and its outcome is unknown. Start the ingestion again. |
| Approving a near-miss merge or a graph cleanup edit fails or never completes (passkey signing) | Passkey signing uses its own database (`ps_signing`). When it is unreachable the signing ceremony cannot run, `passkey_signing_postgres` appears in `unhealthy_dependencies`, readiness is unaffected, and the rest of the system keeps working. Restore the database and request a new approval. |
| Every request returns 401 although the token is fresh | The identity provider or its key set (JWKS) is unreachable from PS Service, or the token's issuer or audience does not match `PS_AUTH_ISSUER` / `PS_AUTH_AUDIENCE`. Readiness is unaffected. Check `kubectl logs` for the unauthenticated entries and their reason category. |
| Ingestion fails at the first stage with a Cellar/ELI error | The EU publication service is unreachable or returned an unparseable document. `cellar_eli` can appear in `unhealthy_dependencies`; readiness is unaffected. Retry later. |
| The `ps-service` pod restarts repeatedly with `OOMKilled` | The container exceeded its memory limit. Raise `psService.resources` in the chart values (see [Helm Chart Values Reference](./helm-chart-values-reference.md)). |
| `ingest document`, or an `ingest_regulation` / `start_ingestion` / `check_regulations` tool call, fails immediately with "LLM Interface is unavailable" | PS Service's LLM Interface is unreachable — `ps-cli`'s pre-flight check caught it before any pipeline call; check PS Service's `/ready` endpoint and its LLM provider configuration. |
| `ps-cli auth login` against the evaluator reports "Could not verify the TLS certificate of ..." | The issuer is HTTPS with a locally issued certificate and the CA is not in the OS trust store, which `ps-cli` verifies against. Trust the CA there (`scripts/deploy-ps-eval.sh --apply-host-setup` does this on the evaluator's own machine). A plain "Could not reach ..." means the host is unreachable instead. See [Installation Guide, step 8](./installation-guide.md#8-register-your-passkey-and-log-in-with-ps-cli). |
| A browser shows a certificate warning, or passkey registration is refused, on the evaluator | The browser does not trust the local CA, or the hostname is not mapped in `/etc/hosts` on that machine. Passkeys need a trusted HTTPS origin. Trust `ca.pem` and map the hostname (see step 8). |
| The deploy script stops with "Authentik rejected the shared API token" | Authentik's bootstrap token never rotates and the Secret no longer matches it. See [Installation Guide: Upgrading an existing install](./installation-guide.md#upgrading-an-existing-install). |
| An owner or invite link says "No recovery flow set" or the script waited for the blueprint | The bundled Authentik blueprint had not applied yet (about a minute on a fresh install). The scripts wait for it; re-run once Authentik's worker is Ready. |
| Referencing a context that doesn't exist | `ps-cli` exits non-zero and lists valid context names — see [User Guide: ps-cli Troubleshooting](./user-guide.md#troubleshooting). |

### Reading a failed ingestion

A failed stage returns only a generic message to the caller (for example `merge failed`),
and an unexpected failure returns `internal_error` ("An internal error occurred."), by design.
The cause is in the server-side log:

- **`kubectl logs <ps-service pod>`** shows every failed log entry, because failures are
  mirrored to stderr as well as to the log file.
- **The JSONL log file** `ps-service.jsonl`, in the directory set by `PS_LOGGING_DIR`,
  holds all entries. Find a failing stage with
  `grep failing_stage ps-service.jsonl` (inside the pod, or from the file you copied out).
  That finds only stage failures; for `internal_error` see the next bullet.
- **An `internal_error` response** means an exception no handler names, for example an
  unreachable graph store before any stage ran, or a failure on a route that is not an
  ingestion. It logs one entry with `component` `api`, `action` `unhandled_request_error`,
  `outcome` `failed`, `run_id`, `exception_type` and `detail`. Take the `run_id` from the
  response body and run `grep <run_id> ps-service.jsonl`, or list every such failure with
  `grep unhandled_request_error ps-service.jsonl`. A request that bound no run id has no
  `run_id` on its entry. `detail` is scrubbed as described below. The uvicorn
  `Exception in ASGI application` traceback in `kubectl logs` is unchanged and carries no
  `run_id`, so match on the `unhandled_request_error` line next to it.
- A stage failure entry has `outcome` `failed`, `failing_stage` (for example `merge`),
  `exception_type` and `detail`; an `unhandled_request_error` entry, and an MCP-action or
  background-run entry, has the same `exception_type` and `detail` without a stage. `detail` is the exception message with bearer and basic
  tokens, `key`/`token`/`secret`/`password` values, URLs, host:port pairs and file paths
  removed, cut to 300 characters. It is meant to name the cause without leaking secrets;
  if it is too thin, reproduce the call against a test instance.
- Failures the service names itself (such as `access_denied`, a 403 for a caller lacking
  the required role) are returned in the response and do not produce a
  detail entry.
- **An MCP tool that fails unexpectedly** (the caller sees `error: an unexpected error
  occurred`) logs one entry with `component` `mcp_interface`, `action` set to the MCP tool name
  (for example `ingest_regulation` or `start_ingestion`), `outcome` `failed`, `run_id`,
  `exception_type` and a scrubbed `detail`. These entries do not use `unhandled_request_error`.
  The tool's `started` and `failed` entries share one `run_id`; find them with
  `grep <run_id> ps-service.jsonl`, or list a tool's failures with
  `grep '"action": "<tool name>"' ps-service.jsonl`.
- `get_ingestion_status` for a run started with `start_ingestion` returns the run's
  `status`, `stage`, `result` and `error`; for the cause behind a generic `error`, use the
  log as above. A failure inside that background run does not reach the HTTP handler: it
  logs as `component` `mcp_interface`, `outcome` `failed`, with `exception_type` and a scrubbed
  `detail`, under the run id `start_ingestion` returned. The action name is
  `background_ingestion_run`, so `grep <run_id> ps-service.jsonl` or
  `grep background_ingestion_run ps-service.jsonl` finds it.

## Teardown

Nothing is torn down automatically:

```bash
az group delete --name rg-policy-system --yes
```

The resource group delete covers everything RG-scoped (AKS, the AIServices
account, Key Vault, networking); `deploy-ps-prod.sh` creates nothing tenant-level, so
there is nothing to clean up outside the resource group.
