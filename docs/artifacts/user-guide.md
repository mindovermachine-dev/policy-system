# Policy System User Guide

## Table of Contents

- [Local Test](#local-test)
  - [Prerequisites](#prerequisites)
  - [1. Install Claude Desktop](#1-install-claude-desktop)
  - [2. Install Podman and start its machine](#2-install-podman-and-start-its-machine)
  - [3. Clone the repo](#3-clone-the-repo)
  - [4. Create the local cluster](#4-create-the-local-cluster)
  - [5. Provision the Azure LLM backend](#5-provision-the-azure-llm-backend)
  - [6. Install ps-cli](#6-install-ps-cli)
  - [7. Deploy Policy System](#7-deploy-policy-system)
  - [8. Load regulations into the graph](#8-load-regulations-into-the-graph)
  - [9. Install the Policy System plugin](#9-install-the-policy-system-plugin)
  - [10. Ask a question](#10-ask-a-question)
- [Production](#production)
  - [Prerequisites (Production)](#prerequisites-production)
  - [1. Sign in to Azure](#1-sign-in-to-azure)
  - [2. Review scripts/ps-defaults.conf](#2-review-scriptsps-defaultsconf)
  - [3. Run scripts/deploy-ps.sh](#3-run-scriptsdeploy-pssh)
  - [4. Access the cluster with kubelogin](#4-access-the-cluster-with-kubelogin)
  - [5. Rotate the API key later](#5-rotate-the-api-key-later)
  - [Manual steps and operational notes](#manual-steps-and-operational-notes)
  - [Teardown](#teardown)
- [ps-cli](#ps-cli)
  - [Configuring which PS Service instance ps-cli targets](#configuring-which-ps-service-instance-ps-cli-targets)
    - [Single target (default)](#single-target-default)
    - [Multiple named targets (contexts)](#multiple-named-targets-contexts)
    - [Credential storage](#credential-storage)
  - [Command reference](#command-reference)
  - [Running commands](#running-commands)
  - [Troubleshooting](#troubleshooting)
  - [Uninstalling](#uninstalling)
- [Policy Editor](#policy-editor)
- [Configuration reference](#configuration-reference)
- [Operations: Backup & Restore](#operations-backup--restore)
- [Troubleshooting / FAQ](#troubleshooting--faq)
- [Glossary](#glossary)

This guide is for people **using** Policy System — deploying a local-test instance,
asking compliance questions, ingesting regulations or internal policies, or
administering an instance. If you want an overview of the project see [README.md](../../README.md). If you want to build, test, or
release the project, see [CONTRIBUTING.md](../../CONTRIBUTING.md).

Policy System has three clients. Which section you need depends on what you're doing — see README's
[Target audiences](../../README.md#target-audiences) table if you want the
role-oriented view.

| I want to... | Use | Status |
| --- | --- | --- |
| Try Policy System on my own laptop | [Local Test](#local-test) | ✅ Available |
| Ask a compliance question in natural language | [Policy System plugin](#9-install-the-policy-system-plugin) | ✅ Available |
| Ingest a regulation or internal policy, check service health, administer an instance | [ps-cli](#ps-cli) | ✅ Available |
| Author Policies, Standards, and Controls | Policy Editor | ❌ Not yet designed |

---

## Local Test

> [!NOTE]
> **This path is for evaluators** trying Policy System on their own laptop via a
> Helm chart on a local `kind` cluster.

The same Helm chart also serves **production administrators** deploying to a real
Azure subscription, with a different values profile. This walkthrough covers the
local-test profile only; see [Production](#production) below for the
customer-tenant Azure rollout via `scripts/deploy-ps.sh`.

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
brew install podman    # macOS

podman machine init --cpus 4 --memory 8192

podman machine start

podman info    # confirm the machine is running
```

kind under Podman needs a machine with enough headroom to run a control plane plus both
Policy System containers. 4 CPUs / 8 GB is the tested floor.

### 3. Clone the repo

```bash
git clone https://github.com/mindovermachine-dev/policy-system

cd policy-system
```

Navigate to the repo root folder

The remaining steps reference repo-relative paths (`deploy/kind/cluster.yaml`,
`./charts/policy-system`) and assume you're running commands from inside this checkout.

### 4. Create the local cluster

```bash
brew install kind kubectl

export KIND_EXPERIMENTAL_PROVIDER=podman
kind create cluster --config deploy/kind/cluster.yaml --name policy-system

kubectl cluster-info --context kind-policy-system
```

The cluster config binds PS Service's REST and MCP ports to fixed host ports via
`extraPortMappings`, so clients reach a stable URL. This must be set at cluster creation
— it cannot be added to a running cluster — and it is what keeps the system reachable
without a `kubectl port-forward` held open in a terminal.

`deploy/kind/cluster.yaml` already names the cluster `policy-system`; the explicit
`--name policy-system` flag is a defensive guard in case your shell already has
`KIND_CLUSTER_NAME` set from another project, which would otherwise silently override
the config file's name.

### 5. Provision the Azure LLM backend

> [!NOTE]
> **Azure is the chart's default LLM provider**, for both the local-test and
> production profiles. Step 7 (Deploy Policy System) fails to render its Secret
> unless credentials exist first — that's why this step comes before it.

This provisions a real Azure Cognitive Services (`AIServices`) account, two model
deployments, and a Key Vault in your own Azure subscription, then syncs the
resulting credentials into the kind cluster created in step 4. It's a one-time
setup per subscription — both scripts are safe to re-run and no-op once nothing
has changed. See [Customer-Managed Azure LLM
Bootstrap](../architecture/customer-azure-llm-bootstrap.md) for the full design.

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
prompts `Proceed with these values? [Y/n]`. It then verifies your subscription
permissions, picks the first candidate region where both models are available at
the required SKU, checks quota covers the configured capacities, and provisions
everything, printing a summary line only — never a secret value. Pass `--yes` to
skip the confirmation prompt.

```bash
scripts/sync-llm-secrets-to-kind.sh
```

This reads the three credentials back out of Key Vault and writes them into the
active kind cluster as a `policy-system-llm-credentials` Secret. It refuses to run
unless your current `kubectl` context is a `kind-*` context, so it can't land
Azure credentials in the wrong cluster.

> [!TIP]
> **Rotating the key.** `scripts/deploy-llm.sh --rotate-key` regenerates whichever
> API key slot isn't currently active in Key Vault; re-run
> `sync-llm-secrets-to-kind.sh` afterward to push the new value into the cluster.
>
> **Cleanup.** Nothing here is torn down automatically:
> ```bash
> az group delete --name rg-policy-system --yes
> az keyvault list-deleted --query "[].name" -o tsv   # find the vault pending purge
> az keyvault purge --name <vault-name>                # clears soft-delete retention
> ```
>
> **Don't want Azure?** Pass `--set llm.provider=ollama` in step 7 instead of
> `llm.existingSecret` — see [Ollama
> values](./helm-chart-values-reference.md#ollama-values). The curated catalog
> used in step 8 only works against Azure's `text-embedding-3-large` embeddings,
> though.

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
$ ps-cli --version
PS-CLI Client Version: 1.4.0
PS-Service Version: unavailable (...)
```

PS Service isn't deployed yet at this point, so the second line reports
`unavailable (...)` — that's expected here, and `ps-cli --version` still exits 0.

**The client version is the version to deploy next.** `ps-cli`, PS Service, and the
Helm chart are released in lockstep by the same automated job, so the
`PS-CLI Client Version` this just printed (`1.4.0` above) is also the chart version to
pass as `--version` in the next step.

To install a specific version instead of the latest, set `PS_CLI_VERSION`:

```bash
curl -fsSL https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/ps-cli/install.sh | PS_CLI_VERSION=1.2.3 bash
```

There is no separate upgrade command — re-run `install.sh` (with or without
`PS_CLI_VERSION`) whenever a newer release is available; re-running is the documented
upgrade path and installs over whatever version is currently on `PATH`.

### 7. Deploy Policy System

```bash
brew install helm
```

```bash
helm upgrade --install policy-system oci://ghcr.io/mindovermachine-dev/charts/policy-system \
  --version <X> --set llm.existingSecret=policy-system-llm-credentials --wait 
```
  
X = the "PS-CLI Client Version" ps-cli --version printed in step 6, e.g. 1.4.0. `llm.existingSecret`
points the chart at the credentials step 5 synced into this cluster — Azure is the chart's default
provider, so this flag is required unless you opted into `llm.provider=ollama` instead (see step 5's
tip). This step can take a few minutes to complete as the container images are downloaded.

```bash
kubectl get pods
```

ps-service and falkordb should both be in "Running" state

```bash
curl http://127.0.0.1:8000/health
```

```bash
curl http://127.0.0.1:8000/ready
```

```bash
open http://localhost:3001/login

```

`localhost:3001/login` opens to FalkorDB web ui used to explore the graph database

> [!TIP]
> **Updating to the latest version.** The chart is installed straight from GHCR as an
> OCI artifact — no repo checkout or repo sync needed to upgrade. Re-run
> [`ps-cli/install.sh`](../../ps-cli/install.sh) (step 6) to pick up the new client
> version, then re-run the deploy command with that version:
>
> ```bash
> helm upgrade --install policy-system oci://ghcr.io/mindovermachine-dev/charts/policy-system \
>   --version <new-X> --set llm.existingSecret=policy-system-llm-credentials --wait
>
> kubectl get pods -l app.kubernetes.io/component=ps-service \
>   -o custom-columns='NAME:.metadata.name,IMAGE:.spec.containers[0].image,STATUS:.status.phase'
> ```
>
> The chart's `psService.image.tag` defaults to empty and falls back to `Chart.appVersion`
> (see the [Helm Chart Values Reference](./helm-chart-values-reference.md)), so the chart
> version and the image version can never disagree — there is no tag to hand-pin and no
> flag needed to reset one. Your graph data is kept — FalkorDB persists to a
> `PersistentVolumeClaim` (see [Operations: Backup & Restore](#operations-backup--restore)),
> so regulations loaded in step 8 do not need to be re-seeded.

### 8. Load regulations into the graph

A freshly deployed system has an empty graph and can answer nothing. Seed it:

```bash
ps-cli get catalog              # curated instruments available to restore

ps-cli restore instrument <id>  # e.g. `ps-cli restore instrument CRA-1.0` just printed
```

Until something is seeded, the system answers questions with an explicit "graph is unseeded" error rather than an empty result.

### 9. Install the Policy System plugin

In Claude Desktop: **Customize** → **Plugins** → **Add** → **Add marketplace** → **Add from a repository**, then add
this repo:

```text
URL:  `https://github.com/mindovermachine-dev/policy-system`
```

This installs the `ps-qna` skill and its `policy-system-graph` MCP connector. Unlike a
typical remote connector, this one runs **locally** — the plugin declares it as a
`stdio` server backed by `ps-cli-mcp-bridge` (installed alongside `ps-cli` in step 6,
never run by hand), which reaches whichever PS Service instance `ps-cli`'s current
context points at ([Configuring which PS Service instance ps-cli targets](#configuring-which-ps-service-instance-ps-cli-targets)).
That's why it works against the local `127.0.0.1:8000` instance from step 7 with no
extra setup: local-test PS Service runs with no auth required at all, and the bridge
sends no `Authorization` header when nothing is stored — the same shape as every other
unauthenticated `ps-cli` call.

Against a non-local, auth-required PS Service instance, run `ps-cli auth login` once
first (see [Credential storage](#credential-storage)); the bridge then attaches
whatever token is stored, refreshing it as needed.

Quit Claude Desktop fully (⌘Q) and relaunch after installing, then open a **new** chat
— tools bind when a conversation starts, so an already-open chat won't pick up a
plugin installed mid-session. `policy-system-graph` should expose two tools,
`domain_concepts` and `cypher`. If it doesn't, check `~/Library/Logs/Claude/mcp*.log`
and, separately, the bridge's own diagnostics on stderr (surfaced in the same log).

### 10. Ask a question

```text
What obligations does the Cyber Resilience Act place on manufacturers,
and which of our policies cover them?
```

The skill grounds itself against the domain model, writes read-only Cypher, retrieves
from the graph, and constructs an answer that cites what it retrieved. If the graph
cannot answer, it says so rather than filling the gap from model recall.

If the skill does not engage on its own, ask for it by name: _"Use the ps-qna skill."_

---

## Production

> [!NOTE]
> **This path is for production/customer-tenant deployments** to a real Azure
> subscription, using [`scripts/deploy-ps.sh`](../../scripts/deploy-ps.sh). It is a
> separate, self-contained script from `deploy-llm.sh` (used by [Local Test step
> 5](#5-provision-the-azure-llm-backend)) — the two are not the same code path and
> both keep working independently. `deploy-ps.sh` provisions the LLM backend, the
> Entra app registrations, an AKS cluster, the Helm release, and public HTTPS
> exposure, all in one run.

### Prerequisites (Production)

| Tool | Why |
| --- | --- |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Everything `deploy-ps.sh` provisions |
| [jq](https://jqlang.org/download/) | Used by `deploy-ps.sh` to parse Azure CLI JSON output |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | Talks to the AKS cluster `deploy-ps.sh` creates |
| [Helm](https://helm.sh/docs/intro/install/) | Installs the Policy System chart |
| [kubelogin](https://azure.github.io/kubelogin/install.html) | Required to authenticate `kubectl`/`helm` against the AAD-enabled AKS cluster — see [step 4](#4-access-the-cluster-with-kubelogin) |

You'll need an Azure subscription where your signed-in identity has `Owner` or
`Contributor` at subscription scope (checked by the script before it touches
anything).

### 1. Sign in to Azure

```bash
az login

az account set --subscription <subscription-id>
```

### 2. Review scripts/ps-defaults.conf

`scripts/ps-defaults.conf` holds the evaluator-tunable defaults: region candidates,
chat/embedding model names and SKUs, capacities, and `TLS_CONTACT_EMAIL` (used for
Let's Encrypt expiry/revocation notices — leave blank to be prompted interactively).
The default SKUs are spike-proven to have quota on a fresh subscription; if your
subscription/region differs, see [Manual steps and operational
notes](#manual-steps-and-operational-notes) item 2 for how to discover the right
values before your first run.

### 3. Run scripts/deploy-ps.sh

```bash
scripts/deploy-ps.sh
```

This prints a confirmation table — region candidates, resource group, AIServices
account, both model deployments, Key Vault, AKS cluster name, and public DNS label,
all deterministically derived from your subscription id — and prompts
`Proceed with these values? [Y/n]`. Pass `--yes` to skip the prompt.

It then, in order: checks your subscription-level RBAC; registers required
resource providers; selects the first region candidate where both models are
Generally Available at the configured SKU and validates the configured capacities
against that region's live quota; provisions the resource group, AIServices
account, both model deployments, and Key Vault; creates the API and CLI Entra app
registrations (falling back to a printed manual command — see [Manual steps and
operational notes](#manual-steps-and-operational-notes) item 4 — if the signed-in
identity can't grant admin consent itself); checks the AKS node VM size is
allowed and vCPU quota is sufficient for this subscription in the selected region;
creates the AKS cluster with AAD authentication, Azure RBAC, disabled local
accounts, and Azure CNI network policy; syncs the LLM credentials into the cluster;
reconciles the Helm release with the auth issuer/audience/scopes wired in; enables
the AKS application-routing ingress add-on and sets a public DNS label; installs
cert-manager and a Let's Encrypt `ClusterIssuer`; and creates the TLS-terminated
Ingress exposing PS Service.

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
brew install Azure/kubelogin/kubelogin   # macOS

kubelogin convert-kubeconfig -l azurecli

kubectl get pods
```

ps-service and falkordb should both be in "Running" state. This is a manual,
per-operator prerequisite `deploy-ps.sh` does not automate — see [Manual steps and
operational notes](#manual-steps-and-operational-notes) item 1.

### 5. Rotate the API key later

```bash
scripts/deploy-ps.sh --rotate-key
```

Regenerates whichever Azure Cognitive Services API key slot isn't currently active
in Key Vault and writes the new value back. Fails clearly if run before a first
successful deploy.

### Manual steps and operational notes

Every item from the `deploy-ps-azure` spike's own "Manual steps a real installer
needs" list, checked against what `scripts/deploy-ps.sh` actually automates today:

1. **`kubelogin` — remains manual.** Install it and convert your kubeconfig before
   `kubectl`/`helm` will authenticate against the AAD-enabled cluster — see [step
   4](#4-access-the-cluster-with-kubelogin) above.
2. **AOAI SKU/quota discovery — partly automated.** `deploy-ps.sh` validates
   whatever SKU/capacity you configure against that region's live-reported range
   and quota, and fails with the exact numbers if insufficient — but discovering
   which SKU has real default quota for your subscription/region in the first
   place remains a manual step before you fill in `scripts/ps-defaults.conf`:
   ```bash
   az cognitiveservices model list --location <region> \
     --query "[?model.name=='gpt-5.4-mini'].model.skus[].name"

   az cognitiveservices usage list --location <region>
   ```
3. **AKS node VM-size allowlist + vCPU quota — now automated.** The spike left
   this as a manual step; `scripts/deploy-ps.sh` now checks both the subscription
   allowlist and vCPU family quota for the fixed `Standard_D4as_v7` node size
   before ever calling `az aks create`, failing with the actual restriction reason
   or vCPU shortfall rather than a generic error. No operator action needed here
   anymore.
4. **Global Admin admin-consent fallback — remains manual.** If the signed-in
   identity lacks Global Administrator / Privileged Role Administrator,
   `deploy-ps.sh` prints the exact command for a colleague with that role to run:
   ```bash
   az ad app permission admin-consent --id <cli-app-id>
   ```
   (the real `<cli-app-id>` is printed inline). Re-run `scripts/deploy-ps.sh`
   afterward — it detects the grant and continues past this step.
5. **`kubectl rollout restart` FalkorDB startup-race workaround — remains
   manual.** If PS Service's pod isn't `Ready` shortly after first install, once
   FalkorDB is confirmed `Running`:
   ```bash
   kubectl rollout restart deployment/policy-system-ps-service
   ```
   The underlying FalkorDB startup race is out of scope for this deployment
   script; only the workaround is documented here, not a fix.
6. **`NetworkPolicy` restricting FalkorDB to PS Service only — now automated.**
   The chart ships a `NetworkPolicy` template that restricts inbound connections
   to FalkorDB's pods to PS Service's pods only, applied automatically on every
   `helm upgrade --install` — `--network-policy azure` alone does not do this, but
   no separate operator step is needed either.
7. **Chart version pinning — remains a known gap.** `deploy-ps.sh`'s `CHART_REF`
   (`oci://ghcr.io/mindovermachine-dev/charts/policy-system`) carries no explicit
   `--version` pin, so `helm upgrade --install` always pulls whatever is latest at
   that OCI reference when you run it. A chart release landing between two runs
   can silently revert local fixes to `psService.auth.scopes`/`.audience` (or any
   other `values-prod.yaml` field) until the fix is republished in a later chart
   version. There is no flag today to pin a specific chart version — be aware of
   this before re-running `deploy-ps.sh` against an existing deployment.
8. **Claude Desktop consent-dialog gotcha — not a Policy System defect, but worth
   knowing.** The same plugin-install consent dialog described in [Local Test step
   9](#9-install-the-policy-system-plugin) may not render visibly in the normal
   window layout — check every Space/display, and try a full app relaunch or OS
   restart if it never appears. This applies equally when the plugin points at a
   Production-hosted PS Service.

### Teardown

Nothing is torn down automatically:

```bash
az group delete --name rg-policy-system --yes

az ad app delete --id $(az ad app list --display-name "Policy System API" --query "[0].appId" -o tsv)
az ad app delete --id $(az ad app list --display-name "Policy System CLI" --query "[0].appId" -o tsv)
```

The resource group delete covers everything RG-scoped (AKS, the AIServices
account, Key Vault, networking). The two Entra app registrations are tenant-level
and survive an RG delete, so they need their own delete calls.

---

## ps-cli

`ps-cli` is a command-line client for Policy System.

### Configuring which PS Service instance ps-cli targets

#### Single target (default)

Out of the box, `ps-cli` targets `http://127.0.0.1:8000`, matching PS Service's own
default. Point it elsewhere with the `PS_CLI_SERVICE_URL` env var, or a `ps-cli.toml`
(`service_url = "..."`) in your current directory:

#### Multiple named targets (contexts)

If you regularly switch between environments — dev, test, prod — `ps-cli` supports
named contexts, the way `kubectl` has contexts or `az` has subscriptions (in fact the
whole CLI, not just contexts, follows kubectl's `<verb> <resource>` pattern — see
[Command reference](#command-reference) below).

```bash
ps-cli config set-context dev --url https://dev.example.com
ps-cli config set-context prod --url https://prod.example.com

ps-cli config use-context prod
ps-cli config get-contexts
#   dev   https://dev.example.com
# * prod  https://prod.example.com   (* marks the current context)
```

Once a context is current, every command uses it — no `PS_CLI_SERVICE_URL` needed:

```bash
ps-cli get health   # targets prod
```

**Override for a single command** with `--context`, without changing what's current:

```bash
ps-cli --context dev get health   # targets dev, just this once
ps-cli config get-contexts        # still shows prod as current
```

**Resolution order** (highest wins): `PS_CLI_SERVICE_URL` env var > `--context` flag >
the current context in your config > the single-target fallback above.

Contexts are stored in `targets.toml` under `~/.config/ps-cli/` by default. Override
the location with `PS_CLI_CONFIG_DIR` (mirroring `gh`'s `GH_CONFIG_DIR`) if you want
an isolated config, e.g. for testing:

```bash
PS_CLI_CONFIG_DIR=/tmp/my-ps-cli-config ps-cli config get-contexts
```

`targets.toml` only ever holds context names and URLs — never a credential.

#### Credential storage

`ps-cli` has keyring-first credential storage built in — a stored credential is kept in
your OS keyring by default, keyed per context name, and falls back automatically to a
`credentials.toml` file (permissions restricted to your user only) when no OS keyring
backend is available, printing a warning every time it uses that fallback, naming the
file path — never the credential value:

```
⚠️  no OS keyring backend available; using /home/you/.config/ps-cli/credentials.toml instead (mode 0600). This is less secure than an OS keyring.
```

Log in with `ps-cli auth login` (requires a named context — `config
set-context`/`use-context` first, since a stored credential is keyed per context
name). This runs an OIDC device-authorization flow ([#57](https://github.com/mindovermachine-dev/policy-system/issues/57)):
`ps-cli` prints a verification URL and code, you complete sign-in in a browser, and
the resulting token is stored under the current context. `ps-cli auth status` shows
whether you're logged in (context, issuer, subject, expiry) without contacting
anything; `ps-cli auth logout` removes the stored credential.

Whether login is required at all depends on the target PS Service instance:
generic OIDC bearer-token validation against any OIDC-compliant provider (no
single vendor's IdP is assumed) is implemented server-side
([#58](https://github.com/mindovermachine-dev/policy-system/issues/58)), but a
given deployment only enforces it once configured with an issuer/audience — the
local-test deployment from step 7 runs with no auth required at all, so every
`ps-cli` command (and the plugin's `policy-system-graph` connector — see
[step 9](#9-install-the-policy-system-plugin)) works there with no login needed.
Re-running `config set-context` for an existing context name with a new `--url`
always clears any credential previously stored for that name, so nothing is
ever silently carried over to a new URL.

### Command reference

Global flags, usable before or after any subcommand:

| Flag | Description |
| --- | --- |
| `-v`, `--verbose` | Print the failure site (file:line) on error. |
| `--context <name>` | Use this named context's PS Service URL for this invocation only. Never persisted. |
| `--version` | Print PS-CLI client and PS Service versions and exit. |

| Command | Arguments | Description |
| --- | --- | --- |
| `ps-cli get health` | — | Report reachability, health (`/health`), and readiness (`/ready`) for the configured target, naming any unhealthy dependency; readiness reflects FalkorDB only — an unhealthy LLM Interface/Cellar-ELI is still named when present, but does not by itself make the target unready. |
| `ps-cli get catalog` | — | List every curated instrument in the local curated-content repo (id, title, source_type/jurisdiction). No PS Service connection needed. |
| `ps-cli ingest regulation <celex>` | `celex` — 10-character CELEX identifier (e.g. `32016R0679`) | Ingest a regulation through the full pipeline. |
| `ps-cli ingest document <document_path>` | `document_path` — a local `.json` file path; `ps-cli` reads it from your own machine and sends its content | Ingest an internal policy document. |
| `ps-cli restore instrument <instrument_id>` | `instrument_id` — the curated instrument's id (e.g. `CRA-1.0`) | Restore one curated instrument's pre-ingested artifact into PS Service. |
| `ps-cli export instrument <instrument_id> [destination]` | `instrument_id` — the already-ingested instrument's id (e.g. `CRA-1.0`); `destination` — optional local directory, defaults to the current directory | Export an already-ingested instrument's baseline/native/manifest files to a local destination. |
| `ps-cli check regulations` | — | Sweep every tracked instrument for amendments, re-ingesting any found; reports one outcome line per instrument. |
| `ps-cli near-misses list` | — | List every unresolved near-miss pending review from the company-merge dedup workflow. |
| `ps-cli near-misses resolve <review_id> --decision <decision>` | `review_id`; `--decision` (required) — `keep-separate` or `merge` | Resolve one pending review. `keep-separate` clears it with no other graph change; `merge` re-points every edge from the loser node onto the winner, deletes the loser, and deletes the pending review, atomically. |
| `ps-cli auth login` | — | Log in to the current context via OIDC device-flow (see [Credential storage](#credential-storage)). |
| `ps-cli auth status` | — | Show the current context's login status (issuer, subject, expiry) — reads the local store only, no network call. |
| `ps-cli auth logout` | — | Remove the current context's stored credential. |
| `ps-cli config set-context <name> --url <url>` | `name`, `--url` (required) | Create or update a named context's PS Service URL. Clears any credential previously stored for that name. |
| `ps-cli config use-context <name>` | `name` | Select the named context every subsequent command uses. |
| `ps-cli config get-contexts` | — | List every named context, marking the current one. |

Run `ps-cli --help` or `ps-cli <command> --help` for the same reference from the CLI
itself.

### Running commands

```bash
ps-cli get catalog                           # local curated catalog — no FalkorDB/LLM dependency
ps-cli ingest regulation 32016R0679          # full ingestion pipeline
ps-cli ingest document <document_path>.json  # reads the file locally, sends its content
```

`ingest regulation` and `ingest document` exercise the full pipeline, so the PS
Service instance you're targeting needs FalkorDB and its LLM interface configured —
check its `/ready` endpoint first if a command fails unexpectedly (see
[Troubleshooting](#troubleshooting) below).

A few behaviors worth knowing about `ingest regulation`:

- The `celex` argument is trimmed and format-validated before it's sent — a malformed
  value is rejected immediately, without a round trip to PS Service.
- A CELEX identifier doesn't have to be in PS Service's curated set to be ingestible:
  if it's not curated, PS Service resolves it against Cellar/ELI (the public EU
  document repository) directly. Ingestion isn't limited to the curated set.
- A real ingestion run takes minutes (a full CRA ingestion has measured ~10 minutes
  end to end). `ps-cli` prints each pipeline stage's name to stderr as it starts, so a
  long-running ingest doesn't look hung — the final `run_id` /
  `regulatory_instrument_id` / per-stage summary still prints to stdout only, once.

A few behaviors worth knowing about `export instrument`:

- `destination` defaults to your current working directory when omitted.
- The destination must already exist and be writable — `ps-cli` checks this before
  making any call to PS Service, so a bad path fails fast with an actionable error
  rather than after a wasted round trip.
- Re-running the command against the same destination overwrites `baseline.json` /
  `native.json` / `manifest.json` deterministically — there's no merge or append
  behavior to worry about.
- If the exported instrument is internal-source, `ps-cli` prints an explicit notice
  that the exported files may contain your organization's own confidential policy
  content.

### Troubleshooting

Referencing a context that doesn't exist exits non-zero and lists the valid names:

```bash
$ ps-cli config use-context staging
❌ context 'staging' is not defined in targets.toml
💡 valid contexts: dev, prod
```

A malformed `targets.toml` exits non-zero and names the file:

```
❌ /home/you/.config/ps-cli/targets.toml contains invalid TOML: Invalid value (at line 1, column 7)
```

If a command can't reach PS Service at all, `ps-cli` reports that distinctly from an
unhealthy server:

```
❌ Could not reach PS Service at http://127.0.0.1:8000.
💡 check PS_CLI_SERVICE_URL / ps-cli.toml, and that ps-service is running
```

Beyond that, PS Service's own health is what to check next — see
[Configuration reference](#configuration-reference) and
[Troubleshooting / FAQ](#troubleshooting--faq) below for `/health` vs `/ready`.

Note that `ready: ready` no longer implies the LLM Interface or Cellar/ELI are healthy —
readiness reflects FalkorDB only. An LLM Interface outage instead surfaces to
`ingest regulation`/`ingest document`/`check regulations` via that command's own
pre-flight failure message (`❌ LLM Interface is unavailable.`), before any pipeline
call is made.

`ps-cli get health` reports all three — reachability, health, and readiness — in one call:

```
$ ps-cli get health
reachable: yes
health: alive
ready: ready
```

```
$ ps-cli get health
❌ PS Service is reachable but not ready (health='alive', ready='not_ready').
💡 unhealthy dependencies: falkordb
```

### Uninstalling

`ps-cli` is installed as a `uv` tool, so it's removed the same way:

```bash
uv tool uninstall ps-cli
```

This removes the `ps-cli` executable and its isolated environment, but leaves your
config and any stored credentials behind so a reinstall doesn't lose them. To remove
those too:

```bash
rm -rf ~/.config/ps-cli   # or $PS_CLI_CONFIG_DIR, if you set that instead
```

This deletes `targets.toml` (your contexts) and the `credentials.toml` fallback store,
if either exists. If credentials were instead stored in your OS keyring (see
[Credential storage](#credential-storage) above), remove them per context — e.g. `keyring
del ps-cli <context-name>`, or via your OS's keychain/Credential Manager UI — since
`uv tool uninstall` has no visibility into the keyring.

If you created a project-local `ps-cli.toml` (see
[Single target (default)](#single-target-default) above), it isn't touched by any of
the above — delete it directly wherever you created it.

---

## Policy Editor

Authoring Policies, Standards, and Controls, and linking them to Capabilities.

> ❌ **Not yet designed.** No client, API surface, or timeline exists yet.

---

## Configuration reference

| Setting | Applies to | Default | Purpose |
| --- | --- | --- | --- |
| `PS_CLI_SERVICE_URL` (env var) | ps-cli | unset | Highest-precedence override for which PS Service instance ps-cli targets. |
| `ps-cli.toml` (`service_url`, in current directory) | ps-cli | none shipped | Project-local single-target override, lowest precedence. |
| `PS_CLI_CONFIG_DIR` (env var) | ps-cli | `~/.config/ps-cli/` | Where `targets.toml` / `credentials.toml` are read/written. |
| `targets.toml` (`[contexts]`, `current_context`) | ps-cli | none until `config set-context` is run | Named PS Service targets and which one is current. Never contains a credential. |
| `credentials.toml` | ps-cli | none until a credential is stored | Per-context credential fallback when no OS keyring backend is available, written by `ps-cli auth login` — see [Credential storage](#credential-storage). |

This table covers `ps-cli` only. The Policy System plugin's `policy-system-graph`
connector needs no configuration of its own — it runs as a local `ps-cli-mcp-bridge`
process (see [step 9](#9-install-the-policy-system-plugin)) that reuses whichever
`ps-cli` context is current, so every row above already governs it too.

---

## Operations: Backup & Restore

FalkorDB persists to a `PersistentVolumeClaim` when `falkordb.persistence.enabled=true`
(the default in both the local-test and production profiles — see the
[Helm Chart Values Reference](./helm-chart-values-reference.md)). With persistence on, back up and
restore the whole deployment using standard, unmodified tooling — no Policy System-specific
backup feature exists or is planned:

- **Volume snapshot (recommended for production):** snapshot the FalkorDB PVC using your
  cluster's `VolumeSnapshot` API, a cloud provider's disk-snapshot mechanism, or a tool like
  [Velero](https://velero.io/). Restore by provisioning a new PVC from that snapshot before
  FalkorDB starts.
- **Redis-native (`BGSAVE`):** trigger a snapshot (`redis-cli -h <falkordb-host> BGSAVE`, or
  rely on FalkorDB's automatic RDB snapshotting) and copy the resulting `dump.rdb` out of the
  volume. Restore by placing that file into a fresh PVC before FalkorDB's first start — Redis
  loads an existing RDB file on startup.

This backs up **everything** in the graph — every ingested regulation, internal policy, and
company-graph merge state. It is a different concern from
[#66](https://github.com/mindovermachine-dev/policy-system/issues/66)'s curated-catalog
restore, which seeds public reference content into any deployment (fresh or established)
without needing this backup/restore machinery at all.

---

## Troubleshooting / FAQ

| Symptom | Check |
| --- | --- |
| `ps-cli` reports "Could not reach PS Service" | Is the target URL right (`ps-cli config get-contexts` / `echo $PS_CLI_SERVICE_URL`)? Is PS Service actually running there? |
| A command fails right after connecting | `ps-cli get health` — reports whether FalkorDB, the LLM Interface, and Cellar/ELI are all reachable, and which is not if any aren't. `health: alive` with `ready: not_ready` means the process is up but FalkorDB is unreachable, or required ingestion config is incomplete — LLM Interface/Cellar-ELI issues are named in `unhealthy_dependencies` without flipping `ready` to `not_ready`. |
| `ingest regulation` / `ingest document` fails immediately with a config-related error | PS Service's ingestion-required config (`PS_LLMINTERFACE_MODEL`, `PS_LLMINTERFACE_EMBED_MODEL`, `PS_COMPANYMERGE_SIMILARITY_THRESHOLD`) is likely missing — this is a PS Service operator/deployer concern, see [CONTRIBUTING.md](../../CONTRIBUTING.md#configure-the-llm-interface). |
| `ingest regulation` / `ingest document` / `check regulations` fails immediately with "LLM Interface is unavailable" | PS Service's LLM Interface is unreachable — `ps-cli`'s pre-flight check caught it before any pipeline call; check PS Service's `/ready` endpoint and its LLM provider configuration. |
| Referencing a context that doesn't exist | `ps-cli` exits non-zero and lists valid context names — see [ps-cli Troubleshooting](#troubleshooting). |

## Glossary

Entities, relationships, and vocabulary used throughout this guide (Regulatory
Instrument, Obligation, Capability, Control, ...) are defined in
[`docs/artifacts/ps-domain-concepts.md`](./ps-domain-concepts.md).
