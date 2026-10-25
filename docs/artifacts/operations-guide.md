# Policy System Operations Guide

## Table of Contents

- [Evaluator (local-test) operations](#evaluator-local-test-operations)
  - [Updating to the latest version](#updating-to-the-latest-version)
  - [Resetting the evaluator data](#resetting-the-evaluator-data)
  - [Granting an access role from the command line](#granting-an-access-role-from-the-command-line)
  - [Updating or reinstalling the Policy System Plugin](#updating-or-reinstalling-the-policy-system-plugin)
  - [Owner recovery, certificate renewal and re-running the script](#owner-recovery-certificate-renewal-and-re-running-the-script)
  - [Upgrading an existing evaluator](#upgrading-an-existing-evaluator)
  - [Moving an existing evaluator off a .local hostname](#moving-an-existing-evaluator-off-a-local-hostname)
  - [Recording verification evidence](#recording-verification-evidence)
  - [Evaluator verification history](#evaluator-verification-history)
  - [Reaching the Authentik admin UI (evaluator)](#reaching-the-authentik-admin-ui-evaluator)
  - [Measuring the ingress controller footprint (evaluator)](#measuring-the-ingress-controller-footprint-evaluator)
  - [Allowed approval origins](#allowed-approval-origins)
  - [Recreating the kind cluster](#recreating-the-kind-cluster)
  - [Cleanup / teardown](#cleanup--teardown)
  - [Rotating the Azure LLM API key](#rotating-the-azure-llm-api-key)
- [Production operations](#production-operations)
  - [Updating to the latest version](#updating-to-the-latest-version-1)
  - [Upgrading to the graph mutation log](#upgrading-to-the-graph-mutation-log)
  - [Audit log integrity](#audit-log-integrity)
  - [Graph replay at startup and a gated graph](#graph-replay-at-startup-and-a-gated-graph)
  - [FalkorDB graph name](#falkordb-graph-name)
  - [What `/ready` reveals](#what-ready-reveals)
  - [Metrics](#metrics)
  - [Request body limit and startup discovery retry](#request-body-limit-and-startup-discovery-retry)
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
- [Authentik Backup](#authentik-backup)
  - [Take an Authentik backup](#take-an-authentik-backup)
  - [Check the Authentik backup](#check-the-authentik-backup)
  - [Authentik secrets to retain and protection](#authentik-secrets-to-retain-and-protection)
- [Restore](#restore)
  - [Before you start](#before-you-start)
  - [Restore with the script](#restore-with-the-script)
  - [Restore by hand](#restore-by-hand)
  - [If a credentials Secret is lost](#if-a-credentials-secret-is-lost)
- [Authentik Restore](#authentik-restore)
  - [Restore Authentik by hand](#restore-authentik-by-hand)
  - [Restoring Authentik and PS together](#restoring-authentik-and-ps-together)
- [Troubleshooting / FAQ](#troubleshooting--faq)
  - [Reading a failed ingestion](#reading-a-failed-ingestion)
- [Teardown](#teardown)

This guide is for administrators **operating an already-deployed** Policy System
instance — rotating credentials, upgrading, backing up/restoring, tearing down, or
diagnosing an issue. For first-time deployment, see the [Evaluator Installation
Guide](./evaluator-installation-guide.md) or the [Installation Guide](./installation-guide.md)
(production). For day-to-day usage (`ps-cli`, Claude Desktop), see
the [User Guide](./user-guide.md).

Run the `scripts/` commands in this guide from the folder you unpacked the deploy bundle into
(see [Evaluator Installation Guide: Install the tools and download the bundle](./evaluator-installation-guide.md#step-1-install-the-tools-and-download-the-bundle)),
or from the repo root if you cloned it. Download the bundle again before an upgrade: the scripts
in it install the chart of their own release. The bundle holds `ps-upgrade.sh`,
`ps-reset-data.sh`, `grant-access-role.sh` and `ps-aks.sh`. It does not hold
`verify-backup-restore.sh` (its `counts` and `compare` steps import PS Service code) or
`ps-service.sh` (developers only); both need a clone.

---

## Evaluator (local-test) operations

These apply to an instance deployed via the [Evaluator Installation
Guide](./evaluator-installation-guide.md).

### Updating to the latest version

If an unmerge approval is pending, read
[Releasing while an unmerge approval is pending](#releasing-while-an-unmerge-approval-is-pending)
before you update.

Download the deploy bundle of the new release first. The scripts in it install the chart of
their own release, so an old bundle reinstalls the old version. Go to the folder where you want
the bundle. Download and unpack it, then go to the bundle folder:

```bash
curl -fsSL https://github.com/mindovermachine-dev/policy-system/releases/latest/download/ps-deploy-bundle.tgz | tar xz
cd policy-system-deploy
```

Then run [`scripts/ps-upgrade.sh`](../../scripts/ps-upgrade.sh). It upgrades the release with
`--reset-then-reuse-values`.

```bash
scripts/ps-upgrade.sh
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

Your graph data is kept: it is rebuilt from the graph mutation log (see [Backup](#backup)).

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

`--role` is required and is `SystemOwner`, `SystemAdmin`, `PolicyManager` or `ComplianceOfficer`. The caller must hold a role that may grant the requested one (`SystemOwner` for any role; `SystemAdmin` for `PolicyManager` and `ComplianceOfficer`). The tool is never available under the local-test bypass. The `ps-manage-access-roles` skill grants and revokes roles from a Claude session.

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
  see [Moving an existing evaluator off a .local
  hostname](#moving-an-existing-evaluator-off-a-local-hostname).
- **The Authentik bootstrap token does not rotate.** Authentik applies `AUTHENTIK_BOOTSTRAP_TOKEN`
  once per tenant, so changing the chart-generated Secret later does not change the token Authentik
  holds; the script then stops with "Authentik rejected the shared API token" before creating
  anything. See [Installation Guide: Upgrading an existing
  install](./installation-guide.md#upgrading-an-existing-install) for the fix.

### Upgrading an existing evaluator

Re-run `scripts/deploy-ps-eval.sh` with the new bundle. The upgrade changes what a cluster
created earlier exposes:

- Authentik and PS Service are `ClusterIP` Services and the ingress controller holds port `30443`. Re-running
  is the recovery if Helm reports the port as allocated while the Services switch over.
- The script creates a second Secret, `policy-system-local-ca`, that holds only the CA
  certificate. `policy-system-local-tls` holds the certificate and key, and only the Ingress
  uses it.
- Authentik no longer serves the local certificate. If its keypair `ps-local-tls` is still
  registered, the script prints how to remove it from the admin UI (System > Brands, then
  System > Certificates).
- A cluster created earlier keeps its `127.0.0.1:8000` host mapping. Nothing listens behind it
  any more, and the evaluator is no longer reached on `http://127.0.0.1:8000`: use
  `https://<hostname>:30443`, also on the evaluator machine.
- Signing credentials that were enrolled on the relying party `localhost` do not work on the new
  origin. Enrol them again after the upgrade.

### Moving an existing evaluator off a .local hostname

**Applies to:** evaluators installed with a hostname ending in `.local` (the former default).

Claude Code cannot resolve `.local` names. It fails with `ENOTFOUND`,
even when `/etc/hosts` maps the name. The evaluator name is `ps.test`, and
`deploy-ps-eval.sh` rejects any `--hostname` that ends in `.local`. The hostname is also the
issuer and the WebAuthn relying-party ID, so moving an install changes all three. The steps:

1. Download the bundle of the release that contains this change ([Install the tools and download the bundle](./evaluator-installation-guide.md#step-1-install-the-tools-and-download-the-bundle)), or pull it if you
   cloned the repo.
2. Run `scripts/deploy-ps-eval.sh --owner-email <same email>`. Before it touches the cluster, it
   prints what this machine still needs and the reset command in step 5 below. Read it first.
3. Edit the hosts file: remove the `<old-name>.local` lines and add `<LAN address> ps.test`. The
   script prints the exact line. Its `--apply-host-setup` adds it with `sudo` and never edits or
   removes an existing line. The local CA is replaced by one that permits only `ps.test`. Remove
   the old CA and trust the new one with the commands the script prints (see
   [Moving an existing evaluator to ps.test](#moving-an-existing-evaluator-from-authentiktest-to-pstest)
   for the removal commands).
4. The script then does the rest in one pass:
   - It regenerates the leaf certificate for the new name.
   - It runs one `helm upgrade` that moves the issuer, the bootstrap owner's issuer, the Authentik
     base URL and the `hostAliases` entry for PS Service.
   - It restarts the Authentik pods, because the certificate checksum changed.
   - The `ps-cli` redirect URIs and the audience (`ps-cli`) do not change. The redirect URIs hold
     no hostname.
5. Clear the role assignments recorded under the old issuer. Run this after the script finishes
   (PS Service is up on the new issuer) and **before the owner signs in**. The script prints the
   same block:

   ```bash
   POD=$(kubectl get pod -l app.kubernetes.io/component=ps-postgres -o jsonpath='{.items[0].metadata.name}')
   STATE_PW=$(kubectl get secret policy-system-ps-postgres-state-credentials \
     -o jsonpath='{.data.PS_STATE_POSTGRES_PASSWORD}' | base64 -d)
   kubectl exec "$POD" -- env PGPASSWORD="$STATE_PW" psql -h 127.0.0.1 -U ps_state -d ps_state \
     -c "DELETE FROM access_role_assignments;"
   ```

   - The bootstrap grants SystemOwner only while the table is empty. A grant recorded under the
     old issuer would otherwise block it.
   - After the reset, the first role-gated call by the configured owner grants SystemOwner under
     the new issuer.
   - The reset also deletes invitees' grants. Their issuer changed too. They re-enrol and are
     granted again.
   - Do not open the enrolment link before this step.
   - If someone other than the owner calls first, the bootstrap grants nothing and the table stays
     empty, so the step is safe to retry.
   - Evaluator only. Never run it against a production install.
6. Register a new passkey with the link the script prints. Passkeys are bound to the hostname, so
   the old one no longer works. After a hostname change the script issues a fresh enrolment link
   even though the old passkey is still listed. After signing in, the owner may delete the dead
   passkey from the user settings page.
7. Each LAN colleague changes their hosts line to `<LAN address> ps.test`, trusts the new
   `ca.pem`, and registers a new passkey (invitation or recovery link).
8. Everyone using `ps-cli` runs `ps-cli auth login` again. The stored tokens belong to the old
   issuer.

This procedure has not been verified on a live cluster. Treat the steps as unverified.

### Moving an existing evaluator from authentik.test to ps.test

**Applies to:** evaluators installed under the name `authentik.test`, whose Authentik issuer sits at the
root path (`https://authentik.test:30443/application/o/ps-cli/`). This is the only place in the guide
that names the old name.

The evaluator now has one name, `ps.test`, with Authentik under `/auth/` as in production. The
issuer is `https://ps.test:30443/auth/application/o/ps-cli/`. The local CA is replaced by one that
permits only `ps.test`. **All passkeys must be registered again**: the owner and every invitee,
because a passkey is bound to the name it was registered on.

`scripts/deploy-ps-eval.sh` finds the old name and issuer in the recorded Helm values and
announces the move before it changes the cluster. The steps:

1. Run `scripts/deploy-ps-eval.sh --owner-email <same email>` from the release that contains this
   change. Read what it prints first.
2. Remove the old CA on every machine that trusted it. The script names the old CA by subject, SHA-1
   and SHA-256 fingerprint (colon-free hex) and keeps a copy next to the new one in its state
   directory as `ca.replaced-<first 8 hex of the SHA-1>.pem`. Use the commands it prints:
   - macOS: `sudo security remove-trusted-cert -d <state dir>/ca.replaced-<first 8>.pem` clears the
     admin trust settings the old trust command set. Then
     `sudo security delete-certificate -Z <SHA-1> -t /Library/Keychains/System.keychain` deletes
     the certificate.
   - Debian/Ubuntu: `sudo rm /usr/local/share/ca-certificates/ps-local-ca.crt && sudo update-ca-certificates --fresh`
   - Fedora/RHEL: `sudo rm /etc/pki/ca-trust/source/anchors/ps-local-ca.pem && sudo update-ca-trust`

   Then trust the new `ca.pem` for TLS server authentication with the commands in
   [Evaluator Installation Guide: Trust the CA](./evaluator-installation-guide.md#step-2-trust-the-ca).
3. Fix the hosts file. The script lists the lines it found for the old name, and you remove the
   lines that map `authentik.test`. Add `<LAN address> ps.test` (the script prints the exact line). The
   script never edits an existing hosts line. Its `--apply-host-setup` only appends the new line
   when no line maps `ps.test`.
4. The script then does the rest in one pass: it regenerates the CA and the leaf for `ps.test`,
   runs one `helm upgrade` that moves the issuer, the bootstrap owner's issuer, the Authentik base
   URL and the `hostAliases` entry, and waits for the rollouts. A re-run changes nothing: no second
   upgrade, no second hosts line, no second CA and no duplicate user.
5. Clear the role assignments recorded under the old issuer, **before the owner signs in**. The
   script prints the same block as step 5 of
   [Moving an existing evaluator off a .local hostname](#moving-an-existing-evaluator-off-a-local-hostname)
   (`DELETE FROM access_role_assignments;`). Evaluator only. Never run it against a production
   install.
6. Register a new passkey with the fresh single-use enrolment link the script prints. It issues
   the link even though the owner already has a passkey on the old name. Every invitee registers
   a new passkey too, with a new invitation or recovery link. The owner may delete the dead
   passkeys in the user settings.
7. Each LAN colleague changes their hosts line to `<LAN address> ps.test`, removes the old CA
   and trusts the new `ca.pem` (never `ca.key`), then runs `ps-cli auth login` again. The stored
   tokens belong to the old issuer.
8. Reserve the server's address in your router's DHCP settings (a reserved DHCP address). Every
   colleague's hosts line points at it.

**The same host with a different issuer.** When the recorded hostname already is `ps.test` and
only the issuer differs (for example the root-path issuer), the script announces the role
assignment reset in step 5 but does not issue a new enrolment link: the passkeys are bound to the
host name, which did not change, so they stay valid.

**The name is permanent.** Once anyone registers a signing passkey, the WebAuthn `rp_id` is the
name, so the evaluator name `ps.test` cannot change again without losing every signing passkey.
Choose before the first signing passkey is registered.

This procedure is unverified: it has not been run on a live evaluator moved from the old name.

### Recording verification evidence

The evaluator on `ps.test` has not yet been run from Claude Code, Claude Desktop or
`ps-cli`. A person must run the checks below on a machine with those tools and record the result.
Nothing in this guide may claim a verified result for this change until that is done.

1. Deploy a fresh evaluator with the default options (`scripts/deploy-ps-eval.sh`).
2. Claude Code CLI: add the plugin, run `/mcp` and complete the OAuth login for `ps-mcp`. Read the
   `mcp-logs` output for `plugin:ps-plugin:ps-mcp` and copy the "Successfully connected" line. Run
   `claude --version`.
3. Claude Desktop, Code tab: do the same login. Record the Desktop app version and the bundled
   Claude Code version.
4. Run `ps-cli auth login` and record the result.
5. Do a migration rehearsal. Take an evaluator installed under the former name and issuer. Follow
   [Moving an existing evaluator to ps.test](#moving-an-existing-evaluator-from-authentiktest-to-pstest).
   Record whether the owner got an enrolment link, registered a new passkey, and still holds
   `SystemOwner`. Prove `SystemOwner` with an owner-only call. Record the command and result.
6. Client install (Part 2 of the [Evaluator Installation Guide](./evaluator-installation-guide.md#part-2-install-the-client)): follow only Part 2, on each of the four machines below. Record the result in
   the matrix. Use a clean machine for each cell.
7. Fill in the template below and keep it with your deployment records.

```text
Evaluator verification evidence
Date / operator:
Evaluator hostname:               ps.test
1. Claude Code CLI version (claude --version):
2. mcp-logs line "Successfully connected" for plugin:ps-plugin:ps-mcp:
3. Claude Desktop version / bundled Claude Code version:
4. Desktop Code tab: ps-mcp OAuth result:
5. ps-cli auth login result:
6. Migration rehearsal: owner enrolment link issued (yes/no), new passkey registered (yes/no),
   SystemOwner confirmed by an owner-only call (command and result):
7. Client install matrix (Part 2). For each cell, record the OS and version. Then record one
   result per check: `ps-cli get health` after login; Claude Code (CLI) connected to the plugin
   server; Desktop Code tab connected to the plugin server:
   macOS, same machine as the server:
   macOS, separate machine:
   Linux, same machine as the server:
   Linux, separate machine (headless: dbus session and keyring steps used, yes/no):
```

**After evidence is recorded.** Update the [surface table](./installation-guide.md#which-claude-surfaces-reach-your-deployment). In the CLI row and in the Desktop row, replace the pending sentence with the verified result and the recorded versions. For the CLI row, give the Claude Code version. For the Desktop row, give the Desktop app version and the bundled Claude Code version. Change the Desktop non-public status from "Expected" to "Verified" only with recorded evidence.

### Evaluator verification history

This is the record of what was verified for the evaluator before the [Evaluator Installation
Guide](./evaluator-installation-guide.md) and its scenario table. Read it as history: the scenario
table is the current status.

- **Evaluator path: verified live** on a clean `kind` cluster (Podman) with the real script.
  The test covered:
  - One deployment.
  - Owner passkey enrolment in a browser.
  - `ps-cli auth login` with `SSL_CERT_FILE`.
  - `SystemOwner` granted on the first role-gated call.
  - A second user invited and enrolled.
  - Owner re-run recovery.
  - Certificate refresh without a manual restart.
  - This run used an earlier hostname.
- **Evaluator on `ps.test`, macOS, same machine as the server: partly verified (2026-10-20).**
  - An existing evaluator on the former name (chart 5.2.4) was moved to `ps.test` with
    `scripts/deploy-ps-eval.sh` and the checkout chart. The script detected the former name, built
    the name-constrained CA and printed the hosts and trust commands, which were run by hand.
  - Through `https://ps.test:30443` with the CA trusted: `/health` returned 200, an unauthenticated
    `/ready` returned only the status, `/mcp/` and a REST path returned 401 without a token,
    `/auth/application/o/ps-cli/.well-known/openid-configuration` returned 200, and
    `/auth/if/admin/` and `/auth/api/v3/core/users/` returned 403. TLS 1.1 failed to connect.
    Plain HTTP to port 8000 on the LAN address returned nothing.
  - The owner registered a new passkey on `ps.test`. `ps-cli auth login` then logged in and
    `ps-cli get health` returned `alive` and `ready` (ps-cli 7.0.1, installed with `install.sh`).
  - A `ps-cli` older than the service (5.2.5 against 7.0.1) logged in but `ps-cli get health`
    failed with "unexpected response shape". Run `install.sh` again to upgrade the client.
  - Claude Code CLI 2.1.285 with `PS_MCP_URL` set to `https://ps.test:30443/mcp/` in
    `~/.claude/settings.json`: the OAuth login for `ps-mcp` completed (reported by the operator)
    and `claude mcp list` then showed `plugin:ps-plugin:ps-mcp ... Connected`. The `mcp-logs` line
    was not captured.
  - After the owner's first role-gated call, `access_role_assignments` held `AuthenticatedUser`
    and `SystemOwner` for the owner under the new issuer. The old rows had been deleted first, as
    the script instructs. An owner-only call was not made: `ps-cli export instrument` answered
    `access_denied`, and the role it requires was not checked.
  - Claude Desktop 2.42304.2, Code tab, same `PS_MCP_URL` setting: `ps-mcp` worked (reported by
    the operator). The bundled Claude Code version and the log line were not captured; the
    Desktop install holds Claude Code 2.1.295 and 2.1.299.
  - Not verified: an owner-only call after the migration.
  - Two problems surfaced in this migration and are open: the deploy script drops
    `psPostgres.*.existingSecret` values from the release, so an install with retained database
    credentials loses PS Service until they are set again; and a migration that stopped before it
    issued the owner link cannot issue it on a re-run, because the script then reports the owner as
    already enrolled.
- **A second machine on the LAN: simulated, not tried on physical hardware.**
  - A separate container with the CA in its trust store reached the ingress controller over HTTPS through
    the `0.0.0.0:30443` mapping of the host.
  - A second browser profile completed a passkey device login. The hostname was mapped to the
    LAN address.
  - Browser trust in that run used a test flag. It did not use an imported CA. The macOS
    keychain was untouched.
- **Client install (Part B of the [Installation Guide](./installation-guide.md#part-b-client-install)): not yet verified.** Part B restructured the client steps into one
  place. It was not followed on a clean machine yet. Nothing below is claimed as verified until the
  matrix in [Recording verification evidence](#recording-verification-evidence) is filled in:
  - macOS, same machine as the server: `ps-cli`, Claude Code CLI and the Desktop Code tab
    verified (see above).
  - macOS, separate machine: pending.
  - Linux, same machine as the server: pending.
  - Linux, separate machine: pending.
  - The Linux prerequisites in [B2](./installation-guide.md#b2-install-ps-cli) are the recipe to test. A real user hit
    the failures on a headless Ubuntu machine, and the commands in B2 have not been re-run end to
    end. The package names for a recent Ubuntu release (`libgirepository-2.0-dev`) and the
    `gnome-keyring-daemon --unlock` step are the parts most likely to need a correction.

### Reaching the Authentik admin UI (evaluator)

The Ingress does not route the admin UI. You reach it with a port-forward that runs on the
**Server**, the machine with the kind cluster (the backend machine of the [Evaluator Installation
Guide](./evaluator-installation-guide.md#supported-scenarios)). A **Client** is any other machine
you use as the owner. In the single-laptop scenarios (1 and 2) the Server and the Client are the
same machine.

> [!WARNING]
> **Unverified.** Login is passkey-only, and the owner's passkey is registered for the evaluator
> hostname, not for `localhost`. Whether the passkey prompt works at `http://localhost:9000` has
> not been confirmed. If the prompt does not appear or fails, stop and report it.

1. **On the Server**, in a terminal you leave open:

   ```bash
   kubectl port-forward svc/policy-system-authentik-server 9000:80
   ```

2. Open the admin UI.

   - **Browser on the Server:** open the login URL below on the Server.
   - **Browser on a Client:** **on the Client**, in a second terminal you leave open, forward the
     port from the Server:

     ```bash
     ssh -N -L 9000:127.0.0.1:9000 <user>@<server address>
     ```

     Then open the login URL below **on the Client**. Windows 10 and 11 include `ssh`.

3. Sign in as the owner with your passkey. Open the login flow directly, so the admin UI opens
   after you sign in:

   ```text
   http://localhost:9000/auth/if/flow/ps-login/?next=%2Fauth%2Fif%2Fadmin%2F
   ```

   Do not open `http://localhost:9000/auth/if/admin/` while signed out. The admin page then
   redirects to `http://localhost:9000/flows/-/default/authentication/`, which is missing the
   `/auth` prefix, and the browser shows a 404.

When you are done, stop both terminals with Ctrl+C. See [Evaluator Installation Guide: What is
exposed](./evaluator-installation-guide.md#what-is-exposed).

### Measuring the ingress controller footprint (evaluator)

The ingress controller's measured memory and CPU use, and the minimum host size, are in
[Evaluator Installation Guide](./evaluator-installation-guide.md#step-1-install-the-tools-and-download-the-bundle).
The controller in `deploy/kind/ingress-nginx.yaml` declares a request of 100m CPU and 90Mi
memory and no limits; those are declared values, not measurements. Run this procedure on a
running evaluator to reproduce or update the measurement. It only reads the cluster.

1. Install the Kubernetes metrics API if `kubectl top pod` fails with "Metrics API not
   available". kind does not ship metrics-server; it needs the `--kubelet-insecure-tls` argument
   on kind. Without it, use the cgroup files in step 3.
2. Let the evaluator settle for 120 seconds, then read the controller pod:

   ```bash
   kubectl -n ingress-nginx top pod --selector=app.kubernetes.io/component=controller
   ```

3. Without metrics-server, read the controller's cgroup instead. Memory is `memory.current`.
   CPU is the difference of `usage_usec` between two reads, divided by the seconds between them:

   ```bash
   pod=$(kubectl -n ingress-nginx get pod --selector=app.kubernetes.io/component=controller -o name)
   kubectl -n ingress-nginx exec "$pod" -- sh -c 'cat /sys/fs/cgroup/memory.current; grep usage_usec /sys/fs/cgroup/cpu.stat'
   ```

4. Repeat the read while 8 clients request `https://<hostname>:30443/health` in a loop (use
   `curl --cacert <ca.pem>`) for 60 seconds.
5. Keep the figures with the date, the host size and the controller image tag. Record a new
   measurement in the installation guide's sizing note when the controller image or its load
   changes.

### Allowed approval origins

`psService.passkeySigning.allowedOrigins` (env `PS_PASSKEYSIGNING_ALLOWED_ORIGINS`, a
comma-separated list of `scheme://host[:port]` origins) names the places the approval page may be
served from. The first entry is the WebAuthn relying party: its host is the `rp_id` and its origin
is what the browser must present. A POST to an approval route (`/approvals/<id>/summary`,
`enroll/options`, `enroll/verify`, `sign/options`, `sign/verify`) whose `Host` matches no entry
is answered `421`, before the approval is read and without issuing a challenge. `Host` and the
entries are compared case-insensitively, a missing port counts as the scheme's default, and
`X-Forwarded-Host` is ignored. The evaluator script sets it to `https://<hostname>:30443`, the ingress address of the configured name.

- With no entry set, the relying party is derived from each request's own `Host`, and a startup
  warning is logged unless the local-test bypass is active. Set it on every deployment that is
  reachable under more than one name.
- A passkey is bound to its `rp_id`. Changing the first entry makes every passkey enrolled under
  the old one unusable until it is enrolled again.
- The approval link is built from the `Host` of the request that created it. A client that reached
  PS Service under an unlisted name receives a link that answers `421`; open the approval from a
  listed name.

See [Helm Chart Values Reference](./helm-chart-values-reference.md).

### Recreating the kind cluster

`deploy/kind/cluster.yaml`'s port mappings are fixed when a cluster is created. A `policy-system`
cluster created before Authentik was served over HTTPS lacks the `30443` mapping (it had a
plain-HTTP `30080` one), so it must be deleted and recreated — `kind delete cluster --name
policy-system`, then step 3 of the installation guide again: first `scripts/deploy-ps-eval.sh
--check-exposure` (add `--allow-public-host` only if you accept a public address on this machine),
then the `kind create cluster` command. This destroys
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

If an unmerge approval is pending, read
[Releasing while an unmerge approval is pending](#releasing-while-an-unmerge-approval-is-pending)
before you update.

Download the deploy bundle of the new release first. The scripts in it install the chart of
their own release, so an old bundle reinstalls the old version. Go to the folder where you want
the bundle. Download and unpack it, then go to the bundle folder:

```bash
curl -fsSL https://github.com/mindovermachine-dev/policy-system/releases/latest/download/ps-deploy-bundle.tgz | tar xz
cd policy-system-deploy
```

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
not been run against a live AKS upgrade.

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
   The Job applies the privileged migrations in a fixed order: `graph_gateway` first, then
   `audit_integrity` (`0001_audit_log_insert_only_hash_chain.sql`). The order matters:
   `graph_gateway/0001` creates a foreign key from `graph_log.groups` to `audit_events`, so the
   table must still exist under its old name when that file runs; `audit_integrity` then moves it
   into the `audit_log` schema and the reference follows the table. `audit_integrity` runs in a
   single transaction: it moves `audit_events` into `audit_log`, adds the hash-chain columns,
   chains every row written before this release (in `occurred_at, id` order), installs the
   insert-only triggers and the integrity functions, and sets the final privileges. If any
   statement fails, none of it takes effect. It holds an `ACCESS EXCLUSIVE` lock on `audit_events`
   until it commits, so audit reads and writes wait for that time (a 100,000-row history took
   about 5 seconds in testing; a long history takes proportionally longer). It gives up after 30
   seconds if it cannot get the lock, fails with a named error and leaves the audit table where it
   was; the Job then retries.
2. [`scripts/ps-upgrade.sh`](../../scripts/ps-upgrade.sh) and `scripts/deploy-ps-prod.sh` pass
   `--wait --wait-for-jobs`, so a Job that exhausts its retries fails the upgrade with a Helm
   error naming the Job; it does not hang. An upgrade run by hand must pass the same two flags.
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

**What this does not protect.** The `ps_state` role still owns the `ps_state` database and the
`public` schema. It can therefore `DROP DATABASE` from another connection or drop `public`
objects; the `graph_log` and `audit_log` tables are protected from alteration by that role, not
the database containing them. `audit_events` now lives in the `audit_log` schema, owned by the
same non-login owner role as `graph_log`; `ps_state` can only `SELECT` and `INSERT` there and
cannot update, delete or truncate a row, so those statements fail with a permission error. The
audit trail is tamper-evident, not tamper-proof: a database superuser (the `postgres_admin`
account, or anyone who can reach the owner role or the database owner) can alter rows, and a
consistent rewrite of the whole chain and its head is not detected, because there is no key and no
external anchor. Moving database ownership is outside this change. Do not describe the log as
undroppable, and do not describe the audit trail as tamper-proof. See
[Audit log integrity](#audit-log-integrity).

**Rollback and rolling updates.** Moving `audit_events` is not reversible by the previous image.
That image reads and writes `public.audit_events` and has no knowledge of `audit_log`; there is no
compatibility view and no `search_path` bridge (both were considered and rejected, because a
view or a role-level `search_path` that lets the application role resolve the old name would
weaken the very protection this change adds). After the Job has run, an old `ps-service` pod
(during a rolling update, or after `helm rollback`) fails every audit read and write with
`relation "audit_events" does not exist`, and the actions that record an audit row fail with it.
Roll forward instead. If you must roll back the image, restore the pre-upgrade [Backup](#backup)
(see [Restore](#restore)). Take a backup before upgrading.

**Future audit migrations.** Any ordinary audit migration added later must schema-qualify the
table as `audit_log.audit_events`; an unqualified name no longer finds it.

### Audit log integrity

**Applies to:** every release that includes the `audit_integrity` migration.

Each row in `audit_log.audit_events` carries a sequence number (`chain_seq`), the hash of the
previous row (`prev_row_hash`) and its own hash (`row_hash`), computed by the database in the same
transaction as the insert; the newest link is also recorded in a one-row head table
(`audit_log.audit_chain_head`). Triggers, enabled even in replica mode, reject `UPDATE`, `DELETE`
and `TRUNCATE` for the owner role as well. Any writer, including a raw `INSERT` as `ps_state`, is
chained.

**What it proves.** Recomputing the chain shows whether any recorded row was changed, deleted,
reordered or truncated since it was written. It is tamper-evident only. It does not stop a
database superuser (`postgres_admin`, or anyone with superuser or owner-role access, or full
compromise of the database) from altering the data, and a superuser who rewrites the entire
chain and the head consistently is not detected. The hash has no secret key and there is no
external anchor. Superuser and full-database compromise are out of scope.

**Run the check.** Call the MCP tool `verify-audit-log` (it takes no arguments), or ask the
`ps-verify-audit-log` skill in the Policy System Plugin. The caller must be a `SystemOwner` or
`SystemAdmin` with a real authenticated session; the local-test bypass is refused. The check is
read-only, runs in one consistent snapshot and is safe while the service is writing. It reads
every row, so run it off-peak on a very large trail.

**Read the result.**

| Result | Meaning |
| --- | --- |
| `intact: true`, `first_break: null`, with `rows_checked` and `head_chain_seq` | Every row recomputes to its stored hash, the sequence has no gap, and the last row matches the recorded head. |
| `intact: false`, `first_break.kind = row_hash_mismatch` | The content or time of the row at `chain_seq` (with its `id` and `occurred_at`) was altered. `occurred_at` is null when the time was rewritten to a value with no timestamp. |
| `prev_hash_mismatch` | The link to the previous row at `chain_seq` was rewritten. |
| `sequence_gap` | A row is missing; `chain_seq` is the first row after the gap (or the trail does not start at 1). |
| `head_mismatch` | Every present row verifies but the head names a different last row: the newest rows were removed. There is no row id or time. |

Only the lowest-numbered break is reported; later rows may also be affected. A failed check
(`authorization_store_unavailable`, `access_denied` or an unreachable service) is not a verdict
on the data.

**If the check reports a break.** Treat it as a security incident. Preserve the database and its
logs as they are and do not run anything that writes to the audit table. Compare the row counts
with the last [Backup](#backup) using `scripts/verify-backup-restore.sh counts`
(the audit trail is counted as `ps_state.audit_log.audit_events`), and restore an earlier backup to
compare the rows around `chain_seq`.

**Backup and restore.** The physical [Backup](#backup) includes the `audit_log` schema, the owner
role and the chain, so a [Restore](#restore) brings the trail back with its hashes. Run
`verify-audit-log` after a restore.

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

- `/health` stays `200`. `/ready` answers `503` `not_ready`, so the pod is out of the Service
  rotation; a caller with a valid access token also sees `graph_replay` in
  `unhealthy_dependencies` (see [What `/ready` reveals](#what-ready-reveals)). Any other path
  except `/.well-known/` answers `503` with the status-only body `{"status": "not_ready"}` and
  `Retry-After: 5`, so no caller reads a graph that is empty or half rebuilt. A rebuild of a graph the size of CRA times ten (about
  160,000 log entries) took 45 seconds on a laptop; do not restart the pod just because it takes
  a minute.
- If Postgres or FalkorDB is down, the replay is tried again with a growing wait, capped at
  `startup_replay_max_backoff_seconds`, until it can finish; the entries `startup_replay` /
  `retry` show the attempt and the error class.

**A gated graph.** `/ready` lists `gated_graphs` for a caller with a valid access token (an
anonymous call sees the status only, see [What `/ready` reveals](#what-ready-reveals)). A graph appears there when its rebuild
failed (the rebuilt graph did not match its recorded digest, the log has a gap or an entry that
does not decode, or the rebuild went past its checkpoint without being compared) or when
FalkorDB refuses one of its logged entries. The other graphs are served and `/ready` can be `200`
while one is gated; writes to the gated graph are refused with an error naming the graph and the
log position. Nothing repairs it silently. After you have understood the cause (the log entries
name the graph, the position and the error class, never content), rebuild it from the log:

```bash
POD=$(kubectl get pod -l app.kubernetes.io/component=falkordb -o jsonpath='{.items[0].metadata.name}')
kubectl exec "$POD" -c falkordb -- redis-cli DEL <graph>      # the name from gated_graphs (authenticated /ready)
kubectl rollout restart deploy/<ps-service deployment>        # the empty graph is rebuilt at start
```

Deleting the key removes the graph and the progress record kept inside it; the log is not
touched. If the rebuild fails again at the same position, the log itself or a checkpoint is
wrong: keep the pod as it is and escalate with the logged position.

### Dependency connection and socket timeouts

PS Service bounds every wait on its three network dependencies, so a dependency that accepts a
connection and never answers fails fast and visibly instead of hanging a request or the pod.
Six variables set the bounds. All are optional; an invalid value makes the service refuse to
start with a `ServiceConfigurationError` that names the variable.

| Variable | Default | Applies to | Constraint |
| --- | --- | --- | --- |
| `PS_FALKORDB_CONNECT_TIMEOUT_SECONDS` | 5 | Establishing a FalkorDB connection | Positive number of seconds |
| `PS_FALKORDB_SOCKET_TIMEOUT_SECONDS` | 30 | Waiting for a FalkorDB reply on an open connection | Positive number of seconds, greater than `PS_QUERY_TIMEOUT_MS` (converted to seconds); otherwise refused at start naming both variables |
| `PS_STATE_POSTGRES_CONNECT_TIMEOUT_SECONDS` | 5 | Establishing a connection to the state Postgres (`ps_state`), at startup and per request | Positive number of seconds; libpq enforces a 2 second minimum |
| `PS_STATE_POSTGRES_STATEMENT_TIMEOUT_MS` | 30000 | One statement on a request-path state Postgres connection (not startup migrations) | Positive integer number of milliseconds |
| `PS_PASSKEYSIGNING_POSTGRES_CONNECT_TIMEOUT_SECONDS` | 5 | Establishing a connection to the passkey signing Postgres (`ps_signing`) | Positive number of seconds; libpq enforces a 2 second minimum |
| `PS_PASSKEYSIGNING_POSTGRES_STATEMENT_TIMEOUT_MS` | 30000 | One statement on a request-path passkey signing Postgres connection | Positive integer number of milliseconds |
| `PS_SERVICE_READY_REPROBE_MIN_INTERVAL_SECONDS` | 5 | Shortest time between the starts of two `/ready` re-probes of the same dependency | Finite number of seconds, 0 or more (0 removes the limit) |
| `PS_SERVICE_READY_REPROBE_DEADLINE_SECONDS` | 3 | How long one `/ready` poll waits for re-probes and for the caller's token check | Finite number of seconds, above 0 |

The Helm chart does not expose these variables yet, so a deployment that needs a value other than
the default sets it through the pod environment. The metrics scrape keeps its own fixed bounds
(2 seconds to connect, 2000 ms per statement) and is not affected.

What an operator sees when a bound is hit:

| Dependency | What happens | Where to look |
| --- | --- | --- |
| FalkorDB does not answer | The call fails after the bound, the gateway retries (4 attempts), then the caller gets `GraphUnavailableError`, or the group is reported as committed but not yet applied if it was already logged | `falkordb` in `unhealthy_dependencies`, `/ready` answers `503`; the next `/ready` poll re-probes FalkorDB, so it answers `200` again on its own once FalkorDB answers |
| State Postgres does not answer at startup | The state migration step fails within the connect timeout, which is fatal: the pod exits and restarts | Pod logs and restarts |
| State Postgres does not answer on a request | The store raises its fixed unavailable error within the connect timeout | `state_postgres` in `unhealthy_dependencies`, `/ready` answers `503`; the next `/ready` poll re-probes it, so it answers `200` again on its own once the database answers |
| A state Postgres statement runs past the limit | The server cancels it and the store raises its fixed error, which carries no SQL, host or driver text | `state_postgres` goes unhealthy and heals through the `/ready` re-probe or the next successful Postgres call (see the note below) |
| Passkey signing Postgres does not answer | Startup logs a warning and continues; approvals fail with the stores' fixed errors | `passkey_signing_postgres` in `unhealthy_dependencies` |

**Tuning.** Keep `PS_FALKORDB_SOCKET_TIMEOUT_SECONDS` above `PS_QUERY_TIMEOUT_MS`: raise both
together when a legitimately long Cypher query is needed. `PS_QUERY_TIMEOUT_MS` is set from the
chart value `psService.query.timeoutMs`; the socket timeout (30 s by default) is set only through
the pod environment. Raise the socket timeout alone only for a very large graph whose copy, digest
scan or replay page takes longer than the default (a CRA-sized graph takes well under a second to
copy, under a second to scan and a few seconds a replay page).
Raise `PS_STATE_POSTGRES_STATEMENT_TIMEOUT_MS` if the audit chain check (`verify_chain`, run by the
`verify-audit-log` tool) or another large scan is cancelled on a large audit table; one bound
covers all request-path statements, so there is no per-call override.

**Readiness re-probe.** While FalkorDB or the state Postgres reads unhealthy, each `/ready` poll
runs that dependency's connectivity check, at most once per
`PS_SERVICE_READY_REPROBE_MIN_INTERVAL_SECONDS` and off the event loop, and waits at most
`PS_SERVICE_READY_REPROBE_DEADLINE_SECONDS` for it. The default interval is below the chart's
readiness `periodSeconds`, so every kubelet poll re-probes. Keep the deadline below the readiness
probe `timeoutSeconds` (5 s in the chart): a larger deadline lets a hung dependency turn the answer
into a probe timeout instead of a `503`. The chart does not set either variable. A healthy
dependency is never probed. The re-probe writes one `ready_reprobe` log entry with outcome `failed`
and the exception class name when an outage starts, and one with outcome `succeeded` when it ends.

**A cancelled statement marks the dependency unhealthy.** Dependency health records whether the
most recent call succeeded. A statement cancelled by the statement timeout therefore turns
`state_postgres` unhealthy and `/ready` to `503` until a successful call to the database clears it.
The `/ready` re-probe is such a call, so a pod that has left rotation recovers on its own once the
database answers; no restart is needed.

### FalkorDB graph name

`PS_FALKORDB_GRAPH` names the single-tenant graph that PS Service reads and writes. When it is
unset the name is `policy_system`. The service reads the variable once at startup into its
configuration, and every component uses that name, so changing it takes effect only after a restart.
A value that is set but empty or whitespace-only makes the service refuse to start, with a
`ServiceConfigurationError` that names `PS_FALKORDB_GRAPH`. The per-instrument
`{short}_baseline` and `{short}_native` graphs are not affected by this setting.

### What `/ready` reveals

`/ready` needs no token, so a probe never has to log in, and the HTTP status (`200` ready, `503`
not ready) is the whole answer for the probe. An anonymous call, a call whose token does not
verify, and every call while the local-test bypass is active receive only
`{"status": "ready"}` or `{"status": "not_ready"}`. A bad token is not an error here; it is
treated as no token. A call with a valid `Authorization: Bearer <access token>` (any verified
user, no role needed) also receives `unhealthy_dependencies` and `gated_graphs`. The
replay-gate `503` on every other path carries the status only. `ps-cli get health` sends your
login automatically. A `/ready` poll can start a bounded re-probe of an unhealthy FalkorDB or
state Postgres (see Readiness re-probe under the dependency timeouts); the answer reveals nothing
beyond the fields above. To read the detail by hand:

```bash
curl -s -H "Authorization: Bearer $ACCESS_TOKEN" https://ps.test:30443/ready
```

The local-test bypass has no verifier, so it never returns the detail; read the startup log
entries instead.

The anonymous [Metrics](#metrics) endpoint withholds the same names: it reports only an
aggregate count of unhealthy dependencies, never which ones.

### Metrics

PS Service serves operational metrics on its service port at `GET /metrics`, in the Prometheus
text exposition format, built with the OpenTelemetry Metrics API. A Prometheus server, a Grafana
Agent or an OpenTelemetry Collector's `prometheus` receiver can scrape it, so an alert on a
degraded dependency or a stuck ingestion needs no log scraping.

**Exposure rules**

- `/metrics` is anonymous: it is exempt from authentication and from the startup replay gate,
  like `/health` and `/ready`, so it answers `200` while a graph replay runs.
- It is in-cluster only. Any pod that can reach the service port can read it. The chart-owned PS Service
  Ingress renders only an allowlist of paths without `/metrics`, so the chart ingress has no route
  for it and the controller answers `404`.
- Keep it that way: deny `/metrics` on any Ingress you write yourself in front of PS Service, and
  do not expose the service with NodePort or LoadBalancer, because either one publishes an
  anonymous endpoint. The chart's default service type is `ClusterIP`, and `ingress.enabled=true`
  fails the render for any other type.
- It exposes nothing that anonymous `/ready` withholds: no dependency names, no graph names, no
  error text and no user, document or run identifiers. Labels are fixed values, and route labels
  are route templates, never raw paths. Per-dependency and per-graph detail stays behind
  authenticated `/ready`.
- A scrape adds no probes. It reads state the service already keeps, and a metric whose data
  source fails or times out is left out of that scrape while the others are still served.

**Registered metrics**

| OpenTelemetry name | Prometheus name | Type | Labels | Meaning |
| ------------------ | --------------- | ---- | ------ | ------- |
| `http.server.request.duration` | `http_server_request_duration_seconds` (`_bucket`, `_count`, `_sum`) | Histogram, seconds | `http.request.method` (as `http_request_method`), `http.route` (as `http_route`), `http.response.status_code` (as `http_response_status_code`) | Request count and latency per route. The route is the route template, such as `/ingestions/{run_id}`, or `__unmatched__` when no route matches; a method outside the standard set is `_OTHER`. Requests answered by the replay gate (503) or by authentication (401) are counted too. |
| `ps_service.dependencies.unhealthy` | `ps_service_dependencies_unhealthy` | Gauge, dependencies | `gating` (`true` or `false`) | Aggregate count of unhealthy dependencies, always two series: `gating="true"` for the dependencies that gate `/ready` (FalkorDB, PS state Postgres, graph replay) and `gating="false"` for the others (LLM Interface, Cellar/ELI, Passkey Signing Postgres). Each is `0` when healthy. It reads the dependency-health registry that `/ready` reads, with no probe, and carries no dependency name; the names stay behind authenticated `/ready`. |
| `ps_service.ingestion.runs` | `ps_service_ingestion_runs_total` | Counter, runs | `outcome` (`succeeded` or `failed`) | Ingestion runs that reached a terminal state, counted once per run by the call that wrote the terminal row. A run completed by a status poll because its worker was gone (interrupted) counts as `failed`; a run whose terminal write failed or lost the race to another writer counts nothing. The label never carries a run id or instrument id. Counts restart at zero when the service restarts. |
| `ps_service.ingestion.run.duration` | `ps_service_ingestion_run_duration_seconds` (`_bucket`, `_count`, `_sum`) | Histogram, seconds | `outcome` (`succeeded` or `failed`) | Run duration for the same runs as the counter above: from the start of the background worker to its terminal write, or, for an interrupted run, from submission to the status poll that completed it. Buckets run from 1 second to 1 hour. |
| `ps_service.graph_log.pending_entries` | `ps_service_graph_log_pending_entries` | Gauge, entries | none | One total of graph mutation log entries that are written but not yet applied, summed over all graphs; no graph name is exposed (per-graph detail stays behind authenticated `/ready`). It is read from PS state Postgres with a read-only query on its own short-lived connection, never through the graph log store, so a scrape does not change the Postgres health `/ready` reports. The value is cached for 15 seconds, and a failure is cached too, so a scrape loop does not load a degraded Postgres. The series is omitted while Postgres is unreachable, unconfigured, or slower than the 5 second deadline; the other metrics are still served. |

**Example scrape configuration**

Prometheus, scraping the in-cluster service (replace `<release>` with the Helm release name):

```yaml
scrape_configs:
  - job_name: ps-service
    metrics_path: /metrics
    static_configs:
      - targets: ["<release>-ps-service:8000"]
```

An OpenTelemetry Collector, using its Prometheus receiver:

```yaml
receivers:
  prometheus:
    config:
      scrape_configs:
        - job_name: ps-service
          metrics_path: /metrics
          static_configs:
            - targets: ["<release>-ps-service:8000"]
```

### Graph drift alert

**Applies to:** every PS Service that has a graph mutation log, from the release that adds the
drift check.

PS Service writes its graphs only through the Graph Write Gateway. After the last write of a flow
(a restore, an ingestion, a merge, a re-ingest, a cleanup) the gateway records a digest of the
graph, a short hash of everything in it. Once an hour (`PS_GRAPHGATEWAY_DRIFT_CHECK_INTERVAL_SECONDS`,
Helm `psService.graphGateway.driftCheckIntervalSeconds`; `0` turns the check off) it compares each
graph with the digest at the head of its log. The check exposes nothing of its own: it has no
endpoint, no tool, no `/ready` field and no metric, and a failing check never changes readiness.

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

### Request body limit and startup discovery retry

`psService.maxRequestBodyBytes` (default 104857600, 100 MiB) is the largest request body PS
Service accepts. A request whose `Content-Length` is larger is answered `413` before its body is
read. The PS Service Ingress limit (`nginx.ingress.kubernetes.io/proxy-body-size`) is derived
from it as 1.5 times the value, so the application limit is the one a client meets first and an
oversized upload gets PS Service's own `413`. To raise the limit, change this one value; setting
the annotation through `ingress.annotations` fails the render. The evaluator uses the same Ingress, so the same limit applies.

At startup PS Service fetches the OIDC discovery document from the issuer. On a first install the
issuer's certificate can still be pending, so a failed fetch is retried for
`psService.auth.discoveryRetryBudgetSeconds` (default 300, `0` for a single attempt) before the
process exits. Each retry logs a line with `action=oidc_discovery outcome=retrying`, the attempt
number and the error class. Nothing listens while it retries; the startup probe holds liveness
and readiness back, and its window is derived from the budget, so the container is not restarted
during the retries. When the budget is spent PS Service exits with an error naming the issuer.
A malformed discovery document is not retried.

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

### Releasing while an unmerge approval is pending

The `unmerge-entities` tool replaces the tool that earlier releases registered for unmerging
under a shorter name. A passkey approval created by an earlier release names the shorter tool,
which the new release does not know. Signing such an approval is refused, and it expires on its
own at its `expires_at`, 15 minutes after it was created. Nothing is migrated.

Release when no unmerge approval is in flight. If one is, wait 15 minutes (`expires_at`) plus
5 minutes (`RECONCILE_GRACE`) after the last one was created. An approval that was signed before
the release and has no recorded outcome is never reconciled afterwards, because the new release
has no verifier for it. `check-cleanup-approval` does not find it.

Audit rows written before the release keep the earlier tool name. The audit log is insert-only
and hash-chained, so those rows are not rewritten, and
[`verify-audit-log`](#audit-log-integrity) still passes.

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
  similar but different title becomes a separate draft; there is no semantic comparison,
  so a Policy never raises a near miss.
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
| `ps_state` | `ps_state` | `access_role_assignments`, `runtime_config` (runtime-mutable settings, including the curated-catalog source override), `ingestion_runs` (status and result of each MCP-submitted ingestion run), the `graph_log` schema (the insert-only graph mutation log: `groups`, `entries`, `payloads`, `checkpoints`, `applied_markers`, owned by the non-login role `ps_state_graph_owner`), the `audit_log` schema (`audit_events`, the permanent insert-only audit trail with its hash chain, and `audit_chain_head`; also owned by `ps_state_graph_owner`, with `ps_state` limited to `SELECT` and `INSERT`), and the `ps_schema_migrations` tracking table (the `graph_gateway` and `audit_integrity` components are tracked there too, but applied by the admin-credential provisioning Job, not by PS Service) | `policy-system-ps-postgres-state-credentials` (`PS_STATE_POSTGRES_PASSWORD`) |
| `ps_signing` | `ps_signing` | Passkey Signing data (`pending_approvals`, `signing_credentials`) and its own `schema_migrations` tracking table | `policy-system-ps-postgres-signing-credentials` (`PS_PASSKEYSIGNING_POSTGRES_PASSWORD`) |

The provisioning Job creates the public `ps_state` tables, executing as `ps_state` (`SET ROLE`),
so `ps_state` owns them; PS Service at startup applies only migrations newer than the Job's
image. The `graph_log` and `audit_log` tables are owned by `ps_state_graph_owner` instead.

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
and logs the exact row count of every base table of the `ps_state` and `ps_signing` databases
before and after, as `event=counts` entries keyed `<database>.<schema>.<table>` (for example
`ps_state.audit_log.audit_events=7`). The tables are discovered from the Postgres catalog, so a
table added by a migration is counted with no change to the script; the bookkeeping tables
`ps_state.public.ps_schema_migrations` and `ps_signing.public.schema_migrations` are among
them. The same keys appear in `scripts/verify-backup-restore.sh counts`, `restore` and `e2e`.
The counts cover PS Postgres only: Authentik's database is a separate deployment and is not
covered by this script (see [Authentik Backup](#authentik-backup)). The script refuses a
context that is not `kind-*` unless `--allow-context <ctx>` names it.

### What the verifier seeds

`scripts/verify-backup-restore.sh seed` (also the first step of `e2e`) fills a running `kind-*`
deployment so that every table the counts cover holds data worth restoring. It restores curated
instruments through the restore path, grants access roles through the access-role store, and
writes one synthetic row to each of these tables through the service's own stores:

| Table | Seeded row |
| --- | --- |
| `ps_state.public.runtime_config` | the `curated_source.base_url` override, set to the service's configured catalog URL (kept if an override already exists) |
| `ps_state.public.ingestion_runs` | one finished run with a fixed identifier |
| `ps_signing.public.pending_approvals` | one pending approval, added on every `seed` run |
| `ps_signing.public.signing_credentials` | one credential with fixed synthetic bytes |

The values are synthetic and the base URL is only syntax-checked, so run `seed` against a
throwaway `kind-*` cluster: its curated-source override names a placeholder host, not a real
catalog. After seeding, the script logs the counts (`event=counts phase=seeded`) and exits 1,
naming every table that still holds no row. `e2e` applies the same check to its counts before the backup and stops there.
A table may be empty only as a listed exception: the `SEED_EMPTY_EXCEPTIONS` list at the top of
the script names it with a reason. The exception list is empty: every table of `ps_state` and `ps_signing` is expected to hold
at least one row after seeding.

### What the verifier compares

`scripts/verify-backup-restore.sh e2e` seeds the cluster, takes one backup, wipes both volumes,
restores, and then compares the cluster with itself before the backup:

- The canonical graph digest of every graph that `graph_log` has entries for. A differing graph
  is named in the output.
- The row count of every table the catalog lists in `ps_state` and `ps_signing`, including the
  bookkeeping tables `ps_state.public.ps_schema_migrations` and
  `ps_signing.public.schema_migrations`. Every table that differs is reported on its own line as
  `Count differs for <database>.<schema>.<table>: before <n>, after <n>`; a table that exists on
  one side only shows `absent` for the other.

`e2e` exits 0 and logs `event=e2e_verified` only if every digest and every count is equal;
otherwise it reports every difference it found, logs `event=e2e_failed` and exits 1. A table named
in the `VOLATILE_COUNT_TABLES` list at the top of the script, with a reason, is not compared and
is logged as `event=counts_volatile_skipped table=<database>.<schema>.<table>`. The list is empty:
no table is known to change across a backup and restore. With `--skip-restore` the wiped volumes
stay unrestored, so the comparison must fail and name the emptied tables.

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
in its own Postgres, a separate server that `pg_basebackup` of PS Postgres does not read. Back it
up with the [Authentik Backup](#authentik-backup) procedure and restore it with
[Authentik Restore](#authentik-restore), which also covers restoring Authentik and PS together.

### Operational notes

- The init step that creates the two databases and roles runs only when the PVC is empty.
  Rotating a password in its Secret does not change the existing database role; run
  `ALTER ROLE` by hand as well.
- A pre-existing PVC from a chart version that predates this layout does not contain the
  `ps_state` database. Upgrade onto a fresh PVC (nothing is migrated).

## Authentik Backup

Authentik keeps its users, enrolled passkeys, the `ps-cli` OIDC application and its flows in the
`authentik` database of its own Postgres (`policy-system-authentik-postgres`, role `authentik`),
separate from PS Postgres. The [PS backup](#backup) does not read it. Back it up with one logical
dump (`pg_dump`, custom format): one file, taken from the running database.

### Take an Authentik backup

Run `pg_dump` inside the Authentik Postgres pod over the local socket and stream the archive to a
local file:

```bash
kubectl exec deploy/policy-system-authentik-postgres -- pg_dump -U authentik -d authentik -Fc \
  > "authentik-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

`-Fc` writes one compressed archive that `pg_restore` can read. `pg_dump` takes a consistent
snapshot of the live database, so Authentik can keep running.

### Check the Authentik backup

A backup is usable only if all three hold:

1. The `kubectl exec` exit code is `0`.
2. The file is not empty.
3. `pg_restore -l` can read the archive:

   ```bash
   kubectl exec -i deploy/policy-system-authentik-postgres -- sh -c \
     'cat > /tmp/check.dump && pg_restore -l /tmp/check.dump > /dev/null; rc=$?; rm -f /tmp/check.dump; exit $rc' \
     < authentik-<UTC timestamp>.dump
   ```

   Copy the file into the pod before reading it, as above. Piping the file straight into
   `pg_restore -l` leaves `kubectl exec` hanging after the command has finished.

Delete a file that fails any check and run the backup again.

### Authentik secrets to retain and protection

The dump contains every user and their enrolled passkey credentials. It is not encrypted by
Postgres or by the command. Keep it out of any repository, and store it encrypted at rest (an
encrypted volume or bucket, or an encrypting archiver) with access limited to the operators who
may restore the deployment. Treat it as you treat the PS backup, which holds the signing
credentials.

Keep these two Secrets with the backup, outside the cluster and outside the dump file:

| Secret | Why |
| --- | --- |
| `policy-system-authentik-credentials` (key `AUTHENTIK_SECRET_KEY`; the same Secret also holds `AUTHENTIK_POSTGRESQL__PASSWORD`) | Authentik's secret key, which it uses for signing and for protecting stored values. A restored database is meant to be served with the key it was written under. Operator-managed: set `authentik.credentialsExistingSecret` and `authentik.authentik.existingSecret.secretName` to the same Secret name. |
| `policy-system-authentik-api-token` (key `PS_AUTHENTIK_API_TOKEN`) | The API token PS Service sends to Authentik for invitations. Authentik stores the token in the database and applies the bootstrap token only once, so after a restore the original token works and a regenerated one does not. Operator-managed: `psService.authentik.existingSecret`. |

What the kind verification showed: after a restore, the original API token was accepted by
Authentik, and a passkey login still worked when Authentik ran with a different
`AUTHENTIK_SECRET_KEY`. Keep the key anyway, because the test did not exercise every value
Authentik protects with it. The dump holds no role entries, so it carries no database role or
password. The Authentik database password is not needed to read the dump.

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

   On a `kind-*` evaluator cluster, `scripts/verify-backup-restore.sh retain-secrets
   [--chart <reference>] [--chart-version <version>]` runs the copy, the stop and the upgrade
   above and does nothing when the release already retains the three Secrets. It keeps the copies
   in the same cluster, so it is not a substitute for copies outside the cluster on a real one.
2. **Use the same chart version as the backup, or a newer one.** The provisioning Job applies any
   migration the newer version adds to the restored databases.
3. **Know the target.** The restore overwrites all PS Postgres data and empties FalkorDB in the
   current kubectl context. Authentik is not touched; restore it separately
   (see [Authentik Restore](#authentik-restore), which also gives the order for restoring both).

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
keep the version the release runs. The final line logs the row count of every table of
`ps_state` and `ps_signing` (`event=counts phase=restored`), keyed
`<database>.<schema>.<table>` as in [Backup](#backup).

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

   While PS Service rebuilds the graphs from the log, `/ready` can answer `503` (an authenticated
   call names `graph_replay` in `unhealthy_dependencies`). The restore is done when it answers
   `200`; an authenticated call must also show an empty `gated_graphs`. The script runs
   in-pod over loopback without a login: set `PS_READY_TOKEN` to a valid access token to make
   `wait-ready` (and the `restore` and `e2e` steps that wait for readiness) also require an empty
   `gated_graphs`; without it the script waits for `200` and `"status": "ready"` only, logs
   `gated_graphs=unchecked`, and skips the gated check. The `e2e` run still proves the graphs
   through its digest and count comparison. A gated graph is handled as described in
   [Graph replay at startup and a gated graph](#graph-replay-at-startup-and-a-gated-graph).
7. Compare the counts with the system the backup was taken from:
   `scripts/verify-backup-restore.sh counts` prints the row count of every table of `ps_state`
   and `ps_signing`, keyed `<database>.<schema>.<table>`. Every entry equals the count logged
   when the backup was taken; anything recorded after the backup is gone, including audit
   events, role grants, signing credentials and logged graph changes.

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

## Authentik Restore

This puts one [Authentik backup file](#take-an-authentik-backup) back into the Authentik Postgres
of a running release. The restore overwrites every user, passkey and flow in Authentik with the
contents of the file. Users created, and passkeys enrolled, after the backup are gone, and users
deleted after the backup reappear.

### Restore Authentik by hand

1. Stop the Authentik server and worker so nothing holds the database open:

   ```bash
   kubectl scale deploy/policy-system-authentik-server deploy/policy-system-authentik-worker --replicas=0
   for component in server worker; do
     kubectl wait --for=delete pod -l app.kubernetes.io/name=authentik,app.kubernetes.io/component=$component --timeout=180s
   done
   ```

2. Replace the database. `authentik` is the Postgres superuser of this server, so it can drop and
   create databases from the `postgres` maintenance database:

   ```bash
   kubectl exec deploy/policy-system-authentik-postgres -- psql -U authentik -d postgres \
     -v ON_ERROR_STOP=1 -c 'DROP DATABASE authentik WITH (FORCE)' -c 'CREATE DATABASE authentik OWNER authentik'
   kubectl exec -i deploy/policy-system-authentik-postgres -- \
     pg_restore -U authentik -d authentik --exit-on-error --no-owner < authentik-<UTC timestamp>.dump
   ```

3. Start Authentik again and wait for it:

   ```bash
   kubectl scale deploy/policy-system-authentik-server deploy/policy-system-authentik-worker --replicas=1
   kubectl rollout status deploy/policy-system-authentik-server --timeout=300s
   kubectl rollout status deploy/policy-system-authentik-worker --timeout=300s
   ```

4. Check it: `pg_restore` exited `0`, and a user created after the backup is gone. Log in with
   a passkey that was enrolled before the backup. PS Service resolves the
   caller from the ID token `sub`, which for the `ps-cli` application is the Authentik username
   (`sub_mode: user_username`), so a restored user presents the same `principal_subject` as before
   and their PS access-role grants apply unchanged.

### Restoring Authentik and PS together

PS access-role grants are keyed by the Authentik username (`principal_subject`) and the issuer, so
the two backups describe one identity state and should be taken at the same time. Restore in this
order:

1. Restore PS Postgres first ([Restore](#restore)). It stops PS Service and leaves it stopped.
2. Restore Authentik ([Restore Authentik by hand](#restore-authentik-by-hand)) and wait until
   the Authentik server is ready.
3. Start PS Service last. It reads Authentik's OIDC discovery document when it starts and exits
   with `OIDC discovery failed` while Authentik is unreachable, so it crash-loops if it starts
   first.

Restoring the two to different points in time has these consequences:

- **Authentik older than PS.** A username created after the Authentik backup no longer exists in
  Authentik but keeps its grants in PS. If a different person is later invited under that same
  username, they inherit those grants. Review `list-access-roles` against the Authentik user
  list and revoke grants for usernames that no longer exist before inviting anyone.
- **Authentik newer than PS.** Users can exist that have no grants in PS, and a grant revoked
  after the PS backup returns with the restore. Check the assignments after the restore.
- **Users deleted after an older Authentik backup** reappear with working passkeys. Delete them
  again in Authentik.

The username-reuse risk is closed only by an immutable subject identifier, which is a separate
decision and not part of this procedure.

---

## Troubleshooting / FAQ

| Symptom | Check |
| --- | --- |
| `ps-cli` reports "Could not reach PS Service" | Is the target URL right (`ps-cli config get-contexts` / `echo $PS_CLI_SERVICE_URL`)? Is PS Service actually running there? |
| A command fails right after connecting | `ps-cli get health` reports the process state and readiness and, when you are logged in (`ps-cli auth login`), names every unhealthy dependency (`unhealthy_dependencies`: `falkordb`, `state_postgres`, `graph_replay`, `llm_interface`, `cellar_eli`, `passkey_signing_postgres`). Readiness (`/ready`, HTTP 200 or 503) depends only on `falkordb`, `state_postgres` and `graph_replay`, and on the ingestion configuration being complete: `health: alive` with `ready: not_ready` means one of these is down, the startup graph replay has not finished, or required ingestion config is incomplete. `llm_interface`, `cellar_eli` and `passkey_signing_postgres` are named in `unhealthy_dependencies` without flipping `ready` to `not_ready`. Without a login the report shows the readiness status only and suggests logging in to see the dependencies. |
| `ingest document`, or an `ingest-regulation` / `start-ingestion` tool call, fails immediately with a config-related error | PS Service's ingestion-required config (`PS_LLMINTERFACE_MODEL`, `PS_LLMINTERFACE_EMBED_MODEL`, `PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD`, `PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD`, `PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD`) is likely missing — this is a PS Service operator/deployer concern, see [Helm Chart Values Reference](./helm-chart-values-reference.md#core-values). |
| `ingest document` (or an `ingest-regulation` / `start-ingestion` tool call) fails with HTTP 503 "graph writes are unavailable: the graph write gateway is not running" | Ingestion, the Domain Mapper (extraction and derivation), Company Merge (the merge stage and near-miss resolution) Policy Lifecycle (every tool that creates, edits, adds to or transitions a Policy, and `get-policy` when a legacy Policy needs its `status`/`version` backfilled) Graph Cleanup (the edit that runs when a passkey approval is signed) and Restore (`POST /restorations`, `POST /restorations/from-catalog` and the `restore-instrument` MCP tool: a restore is one gateway group), including a re-ingest that resumes at any of those stages, write the graph only through the Graph Write Gateway, which needs the PS state database (the graph mutation log). The state database is not configured, or the gateway could not be built at startup; configure it (see [Upgrading to the graph mutation log](#upgrading-to-the-graph-mutation-log)) and restart. Ingestion never writes to FalkorDB directly as a fallback, and neither does the Domain Mapper, Company Merge, Policy Lifecycle, Graph Cleanup or Restore, and nothing was written. A restore answers the same 503 before it reads any graph; it needs the PS state database as well as FalkorDB, and the `restore-instrument` MCP tool answers `error: ...` with the same text. The Policy Lifecycle MCP tools do not answer HTTP 503: without a gateway they answer `error: ...` with the same text, record no audit row and write nothing. The same applies to resolving a near-miss and to signing a near-miss merge approval: without the gateway the request is answered 503 and the approval stays pending, so it can be signed again after the restart. Graph Cleanup answers no 503 either: the signed approval is executed, but without a gateway the edit is not made, the audit trail holds an `applied` row followed by a `failed` row for the approval, and the Compliance Officer is told the edit could not be completed; read the outcome with `check-cleanup-approval`, and preview again after the restart. An approval signed while the graph is behind its log is settled only once the graph has caught up (the check answers with no outcome until then). The same 503 answers the Regulatory Change Monitor's amendment check (the `check-regulations` tool and `POST /change-checks`, including a `finalize` or link-only re-ingest that runs no pipeline stage but still writes the succession; the sweep looks for the gateway first, before any graph is opened, and the MCP tool answers `error: ...`) and Export (`POST /exports`): the Regulatory Change Monitor records the instrument succession and Export its embedding backfill only through the Graph Write Gateway, with no direct-write fallback. Export therefore also needs the PS state database, and so does the maintainer CLI shim `tools/curated-export/export_instrument.py` (`curated-export`), which builds its own gateway. A link-only or `finalize` re-ingest now leaves an `ingestion_run.submit` / `ingestion_run.complete` audit pair, and every Export run one `export.instrument` system audit row. A 503 from the audit trail instead means the run's audit row could not be recorded; nothing was written either, and the request can be retried. |
| `ps-cli export instrument`, or `POST /exports`, fails with HTTP 409 `export_unresolved_near_misses`, or `tools/curated-export/export_instrument.py` exits 1 and prints the same message to stderr | The instrument still has unresolved near-miss reviews: the message gives the count and the review ids. Review decisions live in the `policy_system` graph, which an export never serializes, so exporting now would publish Capabilities or Roles that were never reviewed. Nothing was done: no `export.instrument` audit row, no embedding backfill and no file or `catalog.json` was written. Resolve the reviews (the `list-near-misses` and `resolve-near-miss` tools, or the `ps-near-miss-review` skill) and export again; there is no override. A stored review whose kind has no review spec refuses the export too, with HTTP 409 `review_kind_unsupported` naming the kind; listing and resolving refuse it the same way. If `policy_system` cannot be read the export fails closed with the usual graph error (HTTP 500 from the route, exit 1 from the shim) and writes nothing. The check reads the graph named by `PS_FALKORDB_GRAPH` (default `policy_system`, see [FalkorDB graph name](#falkordb-graph-name)). |
| The near-miss review list is flooded with obviously unrelated pairs, or a known duplicate never appears in it | A review is raised only for a pair whose best similarity score is at or above `PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD` (chart `psService.companyMerge.capabilityReviewThreshold`) and below the merge threshold; it must not exceed the merge threshold or startup fails. Raise it to cut noise, lower it to surface more candidates. A pair that scored below it recorded nothing, so lowering the value later does not bring it back. Reviews already stored are not removed when you raise it. Role reviews (two Roles of one instrument) use `PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD` (chart `psService.companyMerge.roleReviewThreshold`, default 0.80, provisional until calibrated on a live CRA run) and follow the same rules; a Role review is never merged automatically. The shipped default, 0.43, sits at or below the lowest labelled true duplicate (0.4359; the labelled set has only 10 true pairs, so lower it if a real duplicate is missing); choose the value with `tools/company-merge/company_merge_similarity_sweep.py` and its `--floor` check. |
| A restore is reported as committed but not yet applied (`committed_apply_pending` in the audit trail, `RestoreApplyPendingError`), or writes to `policy_system` or to the instrument's `_native`/`_baseline` graph are refused as blocked | The restore is recorded in the graph log, but the swap that makes it visible did not finish (FalkorDB was unreachable, or refused the swap). Until it finishes nothing of the instrument is visible, and the three graphs of the restore refuse further writes with nothing logged. Restore FalkorDB, then restart `ps-service`: startup recovery retries the swap, and finishes all three graphs together as one unit. Restoring the same instrument again is not needed. Last resort, only if the FalkorDB engine cannot perform the swap: rebuild each of the three graphs from its log in ascending graph-name order (the gateway's `replay_graph`); while they are rebuilt one by one a reader can see the three graphs in different states, so do it in a quiet window. The block lifts once every graph is at the head of its log. |
| `/ready` answers 503 after FalkorDB or PS Postgres failed while the pod was running | Each `/ready` poll re-probes a failed FalkorDB or state Postgres, so the pod returns to `200` on its own within about `PS_SERVICE_READY_REPROBE_MIN_INTERVAL_SECONDS` of the dependency answering again; no restart is needed. If it stays `503`, the dependency is still unreachable: read `unhealthy_dependencies` (see [What `/ready` reveals](#what-ready-reveals)), the `ready_reprobe` entries in the pod log (exception class only) and the dependency itself. |
| The pod was restarted (or received SIGTERM) while an ingestion started with `start-ingestion` was running, and `get-ingestion-status` still shows `running` | The run record stays `running` until the next `get-ingestion-status` call for it, which marks it `failed` with the message that the run was interrupted before it finished and its outcome is unknown. Start the ingestion again. |
| Calls fail with `GraphUnavailableError`, or `falkordb` stays in `unhealthy_dependencies` and `/ready` answers 503, while FalkorDB accepts connections but does not answer | A FalkorDB that hangs fails each call after `PS_FALKORDB_SOCKET_TIMEOUT_SECONDS` (30 s by default) and the gateway makes four attempts before it reports the graph unavailable. Check the FalkorDB pod (`kubectl logs`, CPU, memory). A graph that is simply large needs a longer socket timeout, which must stay above `PS_QUERY_TIMEOUT_MS`. The service refuses to start if it is not, naming both variables. See [Dependency connection and socket timeouts](#dependency-connection-and-socket-timeouts). |
| The `ps-service` pod fails to start and restarts, and its logs show the state migration step failing | The state Postgres did not accept a connection within `PS_STATE_POSTGRES_CONNECT_TIMEOUT_SECONDS` (5 s by default, 2 s minimum). Startup state migration is fatal by design. Check the `ps-postgres` pod and the Service and NetworkPolicy in front of it. |
| A request fails with a store's fixed "unavailable" error and `state_postgres` is in `unhealthy_dependencies` | The state Postgres did not answer within the connect timeout, or a statement ran past `PS_STATE_POSTGRES_STATEMENT_TIMEOUT_MS` (30 s by default) and was cancelled. The error text carries no SQL or host by design; the pod log has the error class. A cancelled statement heals through the `/ready` re-probe or the next successful call. If a legitimate long statement is cancelled, for example `verify-audit-log` on a large audit table, raise `PS_STATE_POSTGRES_STATEMENT_TIMEOUT_MS`. |
| Passkey approvals fail and `passkey_signing_postgres` is in `unhealthy_dependencies`, with only a warning at startup | The passkey signing Postgres did not answer within `PS_PASSKEYSIGNING_POSTGRES_CONNECT_TIMEOUT_SECONDS` (5 s by default), or a statement ran past `PS_PASSKEYSIGNING_POSTGRES_STATEMENT_TIMEOUT_MS`. Startup continues by design. Check the `ps_signing` database. |
| The service refuses to start with an error naming `PS_FALKORDB_SOCKET_TIMEOUT_SECONDS` and `PS_QUERY_TIMEOUT_MS` | The socket timeout must be greater than the query timeout (in seconds), or a healthy slow query would be cut off on the socket. Raise the socket timeout or lower `PS_QUERY_TIMEOUT_MS`. |
| Approving a graph cleanup edit (a near-miss merge batch included) fails or never completes (passkey signing) | Passkey signing uses its own database (`ps_signing`). When it is unreachable the signing ceremony cannot run, `passkey_signing_postgres` appears in `unhealthy_dependencies`, readiness is unaffected, and the rest of the system keeps working. Restore the database and request a new approval. |
| Every request returns 401 although the token is fresh | The identity provider or its key set (JWKS) is unreachable from PS Service, or the token's issuer or audience does not match `PS_AUTH_ISSUER` / `PS_AUTH_AUDIENCE`. Readiness is unaffected. Check `kubectl logs` for the unauthenticated entries and their reason category. |
| Ingestion fails at the first stage with a Cellar/ELI error | The EU publication service is unreachable or returned an unparseable document. `cellar_eli` can appear in `unhealthy_dependencies`; readiness is unaffected. Retry later. |
| A pod restarts repeatedly with `OOMKilled` (`ps-service`, FalkorDB, PS Postgres, authentik Postgres) or the `ps-state-provision` Job fails with it | The container exceeded its memory limit. Raise the limit of the affected workload in the chart values: `psService.resources`, `falkordb.resources`, `psPostgres.resources`, `authentik.postgres.resources`, or `psPostgres.provisioning.resources` for the Job (see [Helm Chart Values Reference](./helm-chart-values-reference.md)). A FalkorDB restart loses no data: the graph is rebuilt from the mutation log in PS Postgres, but a large graph needs a higher `falkordb.resources` memory limit. |
| `ingest document`, or an `ingest-regulation` / `start-ingestion` / `check-regulations` tool call, fails immediately with "LLM Interface is unavailable" | PS Service's LLM Interface is unreachable — `ps-cli`'s pre-flight check caught it before any pipeline call; check PS Service's `/ready` endpoint with a valid access token (`ps-cli get health` while logged in) and its LLM provider configuration. |
| `ps-cli auth login` against the evaluator reports "Could not verify the TLS certificate of ..." | The issuer is HTTPS with a locally issued certificate and the CA is not in the OS trust store, which `ps-cli` verifies against. Trust the CA there (`scripts/deploy-ps-eval.sh --apply-host-setup` does this on the evaluator's own machine). A plain "Could not reach ..." means the host is unreachable instead. See [Evaluator Installation Guide, Part 2](./evaluator-installation-guide.md#part-2-install-the-client). |
| A browser shows a certificate warning, or passkey registration is refused, on the evaluator | The browser does not trust the local CA, or the hostname is not mapped in `/etc/hosts` on that machine. Passkeys need a trusted HTTPS origin. Trust `ca.pem` and map the hostname (see [Evaluator Installation Guide, Part 2](./evaluator-installation-guide.md#part-2-install-the-client)). |
| The deploy script stops with "Authentik rejected the shared API token" | Authentik's bootstrap token never rotates and the Secret no longer matches it. See [Installation Guide: Upgrading an existing install](./installation-guide.md#upgrading-an-existing-install). |
| An owner or invite link says "No recovery flow set" or the script waited for the blueprint | The bundled Authentik blueprint had not applied yet (about a minute on a fresh install). The scripts wait for it; re-run once Authentik's worker is Ready. |
| Referencing a context that doesn't exist | `ps-cli` exits non-zero and lists valid context names — see [User Guide: ps-cli Troubleshooting](./user-guide.md#troubleshooting). |
| PS Service does not start and the log reports a `ServiceConfigurationError` naming `PS_FALKORDB_GRAPH` | The variable is set but empty or whitespace-only. Set it to the graph name, or remove it to use the default `policy_system`, then restart. See [FalkorDB graph name](#falkordb-graph-name). |

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
  (for example `ingest-regulation` or `start-ingestion`), `outcome` `failed`, `run_id`,
  `exception_type` and a scrubbed `detail`. These entries do not use `unhandled_request_error`.
  The tool's `started` and `failed` entries share one `run_id`; find them with
  `grep <run_id> ps-service.jsonl`, or list a tool's failures with
  `grep '"action": "<tool name>"' ps-service.jsonl`.
- `get-ingestion-status` for a run started with `start-ingestion` returns the run's
  `status`, `stage`, `result` and `error`; for the cause behind a generic `error`, use the
  log as above. A failure inside that background run does not reach the HTTP handler: it
  logs as `component` `mcp_interface`, `outcome` `failed`, with `exception_type` and a scrubbed
  `detail`, under the run id `start-ingestion` returned. The action name is
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
