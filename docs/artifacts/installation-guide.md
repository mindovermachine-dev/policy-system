# Policy System Installation Guide

## Table of Contents

- [Evaluator installation](#evaluator-installation)
  - [Prerequisites](#prerequisites)
  - [1. Install Claude Desktop](#1-install-claude-desktop)
  - [2. Install Podman and start its machine](#2-install-podman-and-start-its-machine)
  - [3. Clone the repo](#3-clone-the-repo)
  - [4. Create the local cluster](#4-create-the-local-cluster)
  - [5. Provision the Azure LLM backend](#5-provision-the-azure-llm-backend)
  - [6. Install ps-cli](#6-install-ps-cli)
  - [7. Deploy Policy System Backend](#7-deploy-policy-system-backend)
  - [8. Install the Policy System plugin](#8-install-the-policy-system-plugin)
- [Production installation](#production-installation)
  - [Prerequisites (Production)](#prerequisites-production)
  - [1. Sign in to Azure](#1-sign-in-to-azure)
  - [2. Review scripts/ps-defaults.conf](#2-review-scriptsps-defaultsconf)
  - [3. Run scripts/deploy-ps.sh](#3-run-scriptsdeploy-pssh)
  - [4. Access the cluster with kubelogin](#4-access-the-cluster-with-kubelogin)
  - [5. Set up each user's computer](#5-set-up-each-users-computer)
- [SystemOwner bootstrap](#systemowner-bootstrap)

This guide covers **deploying** Policy System — either an evaluator local-test
instance on your own laptop, or a production customer-tenant rollout to Azure. If
you want an overview of the project see [README.md](../../README.md). If you want
to build, test, or release the project, see [CONTRIBUTING.md](../../CONTRIBUTING.md).
Once your instance is deployed, see the [User Guide](./user-guide.md) for asking
questions and using `ps-cli`, and the [Operations Guide](./operations-guide.md) for
rotating credentials, upgrading, backing up, and tearing down an instance.

---

## Evaluator installation

> [!NOTE]
> **This path is for evaluators** trying Policy System on their own laptop via a
> Helm chart on a local `kind` cluster.

The same Helm chart also serves **production administrators** deploying to a real
Azure subscription, with a different values profile. This walkthrough covers the
local-test profile only.

### Prerequisites

| Tool                                                                 | Why                                         |
| --------------------------------------------------------------------- | -------------------------------------------- |
| [Claude Desktop](https://claude.com/download)                        | Hosts the Policy System plugin              |
| [git](https://git-scm.com/downloads)                                 | Clones this repo                            |
| [Podman](https://podman.io/docs/installation)                        | Container runtime backing the local cluster |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | Runs a Kubernetes cluster on Podman         |
| [kubectl](https://kubernetes.io/docs/tasks/tools/)                   | Talks to the cluster                        |
| [Helm](https://helm.sh/docs/intro/install/)                          | Installs the Policy System chart            |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Provisions the Azure LLM backend (step 5)   |
| [jq](https://jqlang.org/download/)                                   | Used by the Azure LLM bootstrap scripts (step 5) |


### 1. Install Claude Desktop

Download from [claude.com/download](https://claude.com/download) (macOS or Windows) and
sign in.

### 2. Install Podman and start its machine


```bash
brew install podman
```

```bash
podman machine init --cpus 4 --memory 8192
```

```bash
podman machine start
```

```bash
podman info
```

The last command confirms the Podman machine is running.

kind under Podman needs a machine with enough headroom to run a control plane plus both
Policy System containers. 4 CPUs / 8 GB is the tested floor.

### 3. Clone the repo

Navigate to the folder where you want the Policy System repo to reside.
```bash
git clone https://github.com/mindovermachine-dev/policy-system
```

Navigate to the repo root folder.

```bash
cd policy-system
```

The remaining steps reference repo-relative paths (`deploy/kind/cluster.yaml`,
`./charts/policy-system`) and assume you're running commands from inside the repo root folder.

### 4. Create the local cluster

```bash
brew install kind kubectl
```

```bash
export KIND_EXPERIMENTAL_PROVIDER=podman
```

```bash
kind create cluster --config deploy/kind/cluster.yaml --name policy-system
```

```bash
kubectl cluster-info --context kind-policy-system
```

This last command will print the cluster information, confirming that the local kind cluster is up and running.

> [!WARNING]
> **Already have a `policy-system` kind cluster from before this guide added Authentik
> support?** `kind`'s `extraPortMappings` are fixed at cluster creation and cannot be
> changed on an existing cluster. To pick up the new Authentik NodePort mapping (used by
> [step 7's optional Authentik setup](#7-deploy-policy-system-backend)) you must delete
> and recreate the cluster:
> ```bash
> kind delete cluster --name policy-system
> ```
> **This destroys all existing kind PVC data** — FalkorDB's graph and (if enabled)
> Authentik's Postgres database both live on PVCs backed by this cluster's local
> storage. Re-run the `kind create cluster` command above, then re-run step 5 onward;
> see [User Guide: Load curated content](./user-guide.md#load-curated-content) to
> reload FalkorDB's data afterward.

### 5. Provision the Azure LLM backend

This provisions a real Azure Cognitive Services (`AIServices`) account, two model
deployments, and a Key Vault in your own Azure subscription, then syncs the
resulting credentials into the kind cluster created in step 4. It's a one-time
setup per subscription — both scripts are safe to re-run and no-op once nothing
has changed.

You'll need an Azure subscription where your signed-in identity has `Owner` or
`Contributor` at subscription scope:

```bash
az login
```

```bash
scripts/deploy-llm.sh
```

This prints a confirmation table — region candidates, resource group, account,
both model deployments, Key Vault, all resolved from `scripts/llm-defaults.conf`
(edit that file first if you want different model names/capacities) — and
prompts `Proceed with these values? [Y/n]`. 

It then verifies your subscription
permissions, checks the configured region and quota, and provisions everything,
printing a summary line only — never a secret value. If the deterministic
AIServices account is soft-deleted, the script detects it and asks before purging
it permanently so its model capacity is released. Pass `--yes` to accept both
the deployment and purge confirmations non-interactively.

```bash
scripts/sync-llm-secrets-to-kind.sh
```

This reads the three credentials back out of Key Vault and writes them into the
active kind cluster as a `policy-system-llm-credentials` Secret. It refuses to run
unless your current `kubectl` context is a `kind-*` context, so it can't land
Azure credentials in the wrong cluster.

Once your instance is running, see the [Operations Guide](./operations-guide.md#rotating-the-azure-llm-api-key)
for rotating this key later.

### 6. Install ps-cli

`ps-cli` is a command-line client for PS Service's REST API: select and ingest EU
regulations from Cellar/ELI, ingest internal policies, and check service health and
readiness. It's a distributed client, installable independently of this repo like
`gh`/`az` — no clone/checkout needed. Installing it requires
[uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
curl -fsSL https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/ps-cli/install.sh | bash
```

This runs [`ps-cli/install.sh`](../../ps-cli/install.sh), which resolves the **latest
non-prerelease GitHub Release** of `ps-cli`, downloads its wheel, verifies the wheel's
SHA-256 against the release's `SHA256SUMS` asset, then installs it via `uv tool install`
and puts it on `PATH` through `uv`'s tool-install shims. Verify:

```
ps-cli --version
```

PS Service isn't deployed yet at this point, so the second line reports
`unavailable (...)` — that's expected here, and `ps-cli --version` still exits 0.

On Linux, `ps-cli auth login`/`auth status`/`auth logout` additionally require
`gir1.2-secret-1` (Debian/Ubuntu: `sudo apt install python3-gi gir1.2-secret-1`) to
access the OS Secret Service for encrypted credential storage. If it's missing,
`ps-cli` reports an actionable error naming the exact install command rather than
crashing.

There is no separate upgrade command — re-run `install.sh` whenever a newer release is available; re-running is the documented upgrade path and installs over whatever version is currently on `PATH`.

### 7. Deploy Policy System Backend

The evaluator profile runs with real OIDC login against the bundled Authentik IdP —
`psService.localTestBypass.enabled=true` cannot start in a container (the bypass refuses to
bind a non-loopback host, and every container image binds `0.0.0.0`), so this step deploys
with the bypass **off**. This requires the third `deploy/kind/cluster.yaml` port mapping
added in step 4, so make sure step 4's cluster-recreate warning doesn't apply to you.

```bash
brew install helm
```

Find the kind node container's own IP address:

```bash
podman inspect policy-system-control-plane --format '{{ .NetworkSettings.IPAddress }}'
```

(`docker inspect policy-system-control-plane --format '{{ .NetworkSettings.IPAddress }}'`
if you're running kind under Docker instead of Podman.) PS Service's pod must resolve your
chosen Authentik hostname to this address, so its OIDC issuer validation reaches the exact
same Authentik instance a browser reaches via the NodePort mapping in
`deploy/kind/cluster.yaml`.

`authentik.local` is just this guide's chosen hostname — pick any name you like, as long as
it matches what you add to `/etc/hosts`:

- **On the evaluator's own machine** (the one running the kind cluster), map it to
  loopback:
  ```
  127.0.0.1 authentik.local
  ```
- **On a colleague's machine on the same LAN**, map it to the evaluator laptop's own
  LAN IP instead (e.g. `ipconfig getifaddr en0` on macOS):
  ```
  192.168.1.42 authentik.local
  ```

Deploy. The `psService.auth.*` values are this chart's bundled-Authentik values (fixed by
`charts/policy-system/files/authentik-blueprint.yaml`, see
[idp-configuration-contract.md](./idp-configuration-contract.md#the-four-values-resolved)),
adapted for this guide's `authentik.local:30080` NodePort address instead of a shared
production Ingress hostname. `psService.authzBootstrapOwner.*` is the identity allowed to
claim `SystemOwner`; you don't know your own yet, so this first pass uses the
[placeholder identity](#step-1-deploy-with-a-placeholder-identity):

```bash
helm upgrade --install policy-system oci://ghcr.io/mindovermachine-dev/charts/policy-system \
  --set llm.existingSecret=policy-system-llm-credentials \
  --set psService.localTestBypass.enabled=false \
  --set authentik.enabled=true \
  --set psService.authentikHostname=authentik.local \
  --set psService.authentikHostAliasIP=<kind-node-container-IP-from-above> \
  --set psService.auth.issuer=http://authentik.local:30080/application/o/ps-cli/ \
  --set psService.auth.audience=ps-cli \
  --set psService.auth.cliClientId=ps-cli \
  --set psService.auth.scopes="openid profile email offline_access" \
  --set psService.authzBootstrapOwner.subject=unclaimed-placeholder \
  --set psService.authzBootstrapOwner.issuer=https://placeholder.invalid \
  --wait
```

Run the command below to check that ps-service, falkordb and authentik are in "Running" state

```bash
kubectl get pods
```

```bash
curl http://127.0.0.1:8000/health
```

```bash
curl http://127.0.0.1:8000/ready
```

```bash
open http://localhost:3001/login
```

`localhost:3001/login` opens to FalkorDB web ui used to explore the graph database.
Authentik's login page is reachable at `http://authentik.local:30080/`.

> [!WARNING]
> This traffic is **plaintext HTTP — no TLS**. It is accepted only for local/LAN
> evaluator use on `kind`, and must **never** be used in production. Production exposes
> Authentik behind a TLS-terminating Ingress instead (see
> [customer-azure-deployment.md](../architecture/customer-azure-deployment.md)).

Nobody holds `SystemOwner` yet. Follow [SystemOwner bootstrap](#systemowner-bootstrap) to
claim it: read your own `sub`/`iss`, then redeploy this same command with those two values
in place of the placeholder.

Once deployed, see the [Operations Guide](./operations-guide.md#updating-to-the-latest-version)
for how to upgrade to a newer release later — your graph data is kept across upgrades.

A freshly deployed system has an empty graph and can answer nothing — see the [User
Guide: Load curated content](./user-guide.md#load-curated-content) for seeding it.

### 8. Install the Policy System plugin

In Claude Desktop: **Customize** → **Plugins** → **Add** → **Add marketplace** → **Add from a repository**, then add
this repo:

```text
URL:  `https://github.com/mindovermachine-dev/policy-system`
```

This installs the full set of Policy System skills (`ps-qna`, `ps-author-policy`,
`ps-check-regulations`, `ps-ingest-regulation`, `ps-assess-instrument-applicability`,
`ps-invite-user`, `ps-manage-access-roles`, `ps-list-audit-events`,
`ps-near-miss-review`, `ps-restore-instrument`, `ps-policy-lifecycle`, and
`ps-get-catalog-listing`) and its shared `policy-system-graph` MCP connector. Unlike a
typical remote connector, this one runs **locally** — the plugin declares it as a
`stdio` server backed by `ps-cli-mcp-bridge` (installed alongside `ps-cli` in step 6), which reaches whichever PS Service instance `ps-cli`'s current
context points at ([Configuring which PS Service instance ps-cli
targets](./user-guide.md#configuring-which-ps-service-instance-ps-cli-targets)).
That's why it works against the local `127.0.0.1:8000` instance from step 7 with no
extra setup: local-test PS Service runs with no auth required at all, and the bridge
sends no `Authorization` header when nothing is stored — the same shape as every other
unauthenticated `ps-cli` call.

Quit Claude Desktop fully (⌘Q) and relaunch after installing, then open a **new** chat
— tools bind when a conversation starts. `policy-system-graph` exposes the tools
backing each of the skills above — including `domain_concepts` and `cypher` for direct
graph queries — plus the catalog-source, access-role, and audit-event tools used by the
[Role System](./user-guide.md#role-system). If nothing binds, check
`~/Library/Logs/Claude/mcp*.log` and, separately, the bridge's own log at
`~/.config/ps-cli/mcp-bridge.log` (`$PS_CLI_CONFIG_DIR/mcp-bridge.log` if that's set) —
written independently of whatever the host does with the bridge's stderr.

Outside local-test, several of these tools — ingesting/restoring/exporting curated
content and running change-checks — require the caller to hold the `ComplianceOfficer`
[access role](./user-guide.md#role-system) first; a fresh production instance has no
roles granted until its first caller bootstraps as `SystemOwner` (see [Manage access
roles](./user-guide.md#manage-access-roles)).

Once installed, see the [User Guide](./user-guide.md#using-claude-desktop) for how to
ask a question.

---

## Production installation

> [!NOTE]
> **This path is for production/customer-tenant deployments** to a real Azure
> subscription, using [`scripts/deploy-ps.sh`](../../scripts/deploy-ps.sh). It is a
> separate, self-contained script from `deploy-llm.sh` (used by [Evaluator
> installation step 5](#5-provision-the-azure-llm-backend)) — the two are not the same
> code path and both keep working independently. `deploy-ps.sh` provisions the LLM
> backend, the Entra app registrations, an AKS cluster, the Helm release, and public
> HTTPS exposure, all in one run.

### Prerequisites (Production)

| Tool | Why | Verify |
| --- | --- | --- |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Everything `deploy-ps.sh` provisions | `az version` |
| [jq](https://jqlang.org/download/) | Used by `deploy-ps.sh` to parse Azure CLI JSON output | `jq --version` |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | Talks to the AKS cluster `deploy-ps.sh` creates | `kubectl version --client` |
| [Helm](https://helm.sh/docs/intro/install/) | Installs the Policy System chart | `helm version` |
| [kubelogin](https://azure.github.io/kubelogin/install.html) | Required to authenticate `kubectl`/`helm` against the AAD-enabled AKS cluster — see [step 4](#4-access-the-cluster-with-kubelogin) | `kubelogin --version` |

You'll need an Azure subscription where your signed-in identity has `Owner` or
`Contributor` at subscription scope (checked by the script before it touches
anything).

### 1. Sign in to Azure

```bash
az login
```

```bash
az account set --subscription <subscription-id>
```

### 2. Review scripts/ps-defaults.conf

`scripts/ps-defaults.conf` holds the evaluator-tunable defaults: region candidates,
chat/embedding model names and SKUs, capacities, and `TLS_CONTACT_EMAIL` (used for
Let's Encrypt expiry/revocation notices — leave blank to be prompted interactively),
`AUTHZ_BOOTSTRAP_OWNER_SUBJECT` and `AUTHZ_BOOTSTRAP_OWNER_ISSUER` (the identity allowed to
claim `SystemOwner` — also prompted for when blank; on a first deploy enter the
[placeholder identity](#step-1-deploy-with-a-placeholder-identity), see
[SystemOwner bootstrap](#systemowner-bootstrap)).
The default SKUs are proven to have quota on a fresh subscription; if your
subscription/region differs, see the [Operations Guide](./operations-guide.md#manual-steps-and-operational-notes)
item 1 for how to discover the right values before your first run.

### 3. Run scripts/deploy-ps.sh

```bash
scripts/deploy-ps.sh
```

This prints a confirmation table — region candidates, resource group, AIServices
account, both model deployments, Key Vault, AKS cluster name, and public DNS label,
all deterministically derived from your subscription id — and prompts
`Proceed with these values? [Y/n]`. Pass `--yes` to skip the prompt.

> [!NOTE]
> **Global Admin admin-consent fallback.**
>
> If the signed-in identity lacks Global
> Administrator / Privileged Role Administrator, `deploy-ps.sh` prints the exact
> command for a colleague with that role to run:
> ```bash
> az ad app permission admin-consent --id <cli-app-id>
> ```
> (the real `<cli-app-id>` is printed inline). Re-run `scripts/deploy-ps.sh`
> afterward — it detects the grant and continues past this step. See [Operations
> Guide](./operations-guide.md#manual-steps-and-operational-notes) item 3 for
> more detail.

Each phase prints a `==> <step>` progress line as it starts. The whole run is
idempotent — re-running with nothing changed does no work and reports so. It ends
with a summary naming which secrets were written (never their values) and the
resulting URL:

```
Policy System provisioned. Wrote secrets: AZURE-API-BASE, AZURE-API-KEY, AZURE-API-VERSION.
PS Service: https://<label>.<region>.cloudapp.azure.com
```

### 4. Access the cluster with kubelogin

`deploy-ps.sh` creates the AKS cluster with `--enable-aad --enable-azure-rbac
--disable-local-accounts`, so a plain `kubeconfig` from `az aks get-credentials`
(which the script already ran for you) cannot authenticate on its own —
`kubectl`/`helm` need `kubelogin` to complete the Azure AD sign-in:

```bash
kubelogin convert-kubeconfig -l azurecli
```

```bash
kubectl get pods
```

ps-service and falkordb should both be in "Running" state. This is a manual,
per-operator, per-machine prerequisite `deploy-ps.sh` does not automate.

Nobody holds `SystemOwner` after this first run. Follow [SystemOwner
bootstrap](#systemowner-bootstrap) below, then rerun `scripts/deploy-ps.sh` with your real
`sub`/`iss` — an unchanged identity on a later rerun is a no-op, so this is safe to repeat.

Once deployed, see the [Operations Guide](./operations-guide.md#production-operations)
for rotating the API key, manual operational notes, and teardown.

### 5. Set up each user's computer

The steps above provision the shared backend once. Every person who wants to query
this instance — via `ps-cli` directly or the Claude Desktop plugin — separately needs
the following on their own machine; none of it is done by `scripts/deploy-ps.sh`.

**Install Claude Desktop.** Download from [claude.com/download](https://claude.com/download)
(macOS or Windows) and sign in.

**Install `ps-cli`.** Requires [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
curl -fsSL https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/ps-cli/install.sh | bash
```

**Point `ps-cli` at this instance and log in.** See [User Guide: Point ps-cli at your
instance](./user-guide.md#point-ps-cli-at-your-instance) — use the URL [step
3](#3-run-scriptsdeploy-pssh) printed (`https://<label>.<region>.cloudapp.azure.com`).
Unlike the Evaluator's local-test instance, a production instance is deployed with
Entra auth wired in (`deploy-ps.sh` sets this up — see [step 3](#3-run-scriptsdeploy-pssh)),
so logging in is required here.

**Install the Policy System plugin.** Same as [Evaluator installation, step
8](#8-install-the-policy-system-plugin): in Claude Desktop, **Customize** → **Plugins**
→ **Add** → **Add marketplace** → **Add from a repository**, then add this repo
(`https://github.com/mindovermachine-dev/policy-system`). The plugin's local
`ps-cli-mcp-bridge` reuses whichever `ps-cli` context is current, so once it's set to
`prod` above, the plugin talks to this instance with no separate configuration — and
sends the stored `prod` credential as an `Authorization` header, same as any other
authenticated `ps-cli` call.

---

For `ps-cli` configuration reference (context/credential storage, env vars, config
files), see [User Guide: Appendix — ps-cli reference](./user-guide.md#appendix-ps-cli-reference).
For PS Service / chart-level configuration, see the [Helm Chart Values
Reference](./helm-chart-values-reference.md).

---

## SystemOwner bootstrap

The first authenticated caller whose `(sub, iss)` matches
`psService.authzBootstrapOwner.subject`/`.issuer` is granted `SystemOwner`, exactly once.
Anyone else who reaches an empty instance first gets nothing (the attempt is audited as
`access_role.bootstrap_rejected`). The catch: your Authentik `sub` isn't known until you've
logged in once, so claiming ownership takes two deploys. This applies to both the
[Evaluator](#7-deploy-policy-system-backend) and [Production](#3-run-scriptsdeploy-pssh)
installs — only the deploy command and the Authentik base URL differ.

### Step 1: deploy with a placeholder identity

Deploy with `subject=unclaimed-placeholder` and `issuer=https://placeholder.invalid`
(evaluator: the `--set` flags in step 7; production: enter them when `scripts/deploy-ps.sh`
prompts, or set them in `scripts/ps-defaults.conf`). `.invalid` is a top-level domain
reserved by RFC 2606 that can never be a real issuer, so no real principal's `(sub, iss)`
matches it: nobody is granted `SystemOwner` during this pass, and the role table stays
empty, so your real identity can still claim it later.

### Step 2: log in once and read your own `sub`/`iss`

`ps-cli` never prints or stores an access token, so request one directly with Authentik's
device flow. `<issuer>` is the value of `psService.auth.issuer` — evaluator:
`http://authentik.local:30080/application/o/ps-cli/`; production:
`https://<label>.<region>.cloudapp.azure.com/auth/application/o/ps-cli/`.

```bash
ISSUER=<issuer>
DEVICE_ENDPOINT=$(curl -s "${ISSUER}.well-known/openid-configuration" | jq -r .device_authorization_endpoint)
TOKEN_ENDPOINT=$(curl -s "${ISSUER}.well-known/openid-configuration" | jq -r .token_endpoint)
curl -s -d client_id=ps-cli -d "scope=openid profile email" "$DEVICE_ENDPOINT"
```

Open the returned `verification_uri_complete` in a browser and sign in, then exchange the
returned `device_code` (it expires after 60 seconds; rerun the previous command if it does):

```bash
curl -s -d client_id=ps-cli \
  -d grant_type=urn:ietf:params:oauth:grant-type:device_code \
  -d device_code=<device_code> "$TOKEN_ENDPOINT" | jq -r .access_token
```

Decode the access token's payload (the middle, dot-separated part) — no signature check is
needed here, you're only reading your own claims:

```bash
echo '<access_token>' | jq -R 'split(".")[1] | gsub("-";"+") | gsub("_";"/") | . + ("="*((4 - length % 4) % 4)) | @base64d | fromjson | {sub, iss}'
```

Note both values exactly, character for character — a single differing character means no
match.

### Step 3: redeploy with the real identity

Evaluator: rerun step 7's `helm upgrade` with
`--set psService.authzBootstrapOwner.subject=<sub>` and
`--set psService.authzBootstrapOwner.issuer=<iss>` in place of the placeholder. Production:
edit `AUTHZ_BOOTSTRAP_OWNER_SUBJECT`/`AUTHZ_BOOTSTRAP_OWNER_ISSUER` in
`scripts/ps-defaults.conf` and rerun `scripts/deploy-ps.sh` (or enter them at its prompts).
Rerunning with the same pair again changes nothing.

### Step 4: log in again to claim ownership

The next role-gated call you make while logged in as that same principal — for example
listing access roles through the `ps-manage-access-roles` skill — is the one that wins the
bootstrap and makes you `SystemOwner`. See the [User Guide's Role
System](./user-guide.md#role-system) for what to do with it.

