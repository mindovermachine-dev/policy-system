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

The cluster config binds PS Service's REST and MCP ports to fixed host ports via
`extraPortMappings`, so clients reach a stable URL. This must be set at cluster creation
— it cannot be added to a running cluster — and it is what keeps the system reachable
without a `kubectl port-forward` held open in a terminal.

`deploy/kind/cluster.yaml` already names the cluster `policy-system`; the explicit
`--name policy-system` flag is a defensive guard in case your shell already has
`KIND_CLUSTER_NAME` set from another project, which would otherwise silently override
the config file's name.

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
$ ps-cli --version
```

PS Service isn't deployed yet at this point, so the second line reports
`unavailable (...)` — that's expected here, and `ps-cli --version` still exits 0.

There is no separate upgrade command — re-run `install.sh` whenever a newer release is available; re-running is the documented upgrade path and installs over whatever version is currently on `PATH`.

### 7. Deploy Policy System Backend

```bash
brew install helm
```

```bash
helm upgrade --install policy-system oci://ghcr.io/mindovermachine-dev/charts/policy-system \
  --set llm.existingSecret=policy-system-llm-credentials --wait
```

`llm.existingSecret` points the chart at the
credentials step 5 synced into this cluster — Azure is the chart's default provider, so this flag is
required unless you opted into `llm.provider=ollama` instead (see step 5's tip). This step can take a
few minutes to complete as the container images are downloaded.

Run the command below to check if ps-service and falkordb are in "Running" state

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

`localhost:3001/login` opens to FalkorDB web ui used to explore the graph database

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

This installs the `ps-qna` skill and its `policy-system-graph` MCP connector. Unlike a
typical remote connector, this one runs **locally** — the plugin declares it as a
`stdio` server backed by `ps-cli-mcp-bridge` (installed alongside `ps-cli` in step 6), which reaches whichever PS Service instance `ps-cli`'s current
context points at ([Configuring which PS Service instance ps-cli
targets](./user-guide.md#configuring-which-ps-service-instance-ps-cli-targets)).
That's why it works against the local `127.0.0.1:8000` instance from step 7 with no
extra setup: local-test PS Service runs with no auth required at all, and the bridge
sends no `Authorization` header when nothing is stored — the same shape as every other
unauthenticated `ps-cli` call.

Quit Claude Desktop fully (⌘Q) and relaunch after installing, then open a **new** chat
— tools bind when a conversation starts. `policy-system-graph` expose two tools,
`domain_concepts` and `cypher`. If it doesn't, check `~/Library/Logs/Claude/mcp*.log`
and, separately, the bridge's own log at `~/.config/ps-cli/mcp-bridge.log`
(`$PS_CLI_CONFIG_DIR/mcp-bridge.log` if that's set) — written independently of whatever
the host does with the bridge's stderr.

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
```

```bash
az account set --subscription <subscription-id>
```

### 2. Review scripts/ps-defaults.conf

`scripts/ps-defaults.conf` holds the evaluator-tunable defaults: region candidates,
chat/embedding model names and SKUs, capacities, and `TLS_CONTACT_EMAIL` (used for
Let's Encrypt expiry/revocation notices — leave blank to be prompted interactively).
The default SKUs are proven to have quota on a fresh subscription; if your
subscription/region differs, see the [Operations Guide](./operations-guide.md#manual-steps-and-operational-notes)
item 2 for how to discover the right values before your first run.

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
registrations (falling back to a printed manual command — see the [Operations
Guide](./operations-guide.md#manual-steps-and-operational-notes) item 4 — if the
signed-in identity can't grant admin consent itself); checks the AKS node VM size is
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

macOS.

```bash
brew install Azure/kubelogin/kubelogin
```

```bash
kubelogin convert-kubeconfig -l azurecli
```

```bash
kubectl get pods
```

ps-service and falkordb should both be in "Running" state. This is a manual,
per-operator prerequisite `deploy-ps.sh` does not automate — see the [Operations
Guide](./operations-guide.md#manual-steps-and-operational-notes) item 1.

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
