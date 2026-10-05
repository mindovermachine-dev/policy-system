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
  - [8. Register your passkey and log in with ps-cli](#8-register-your-passkey-and-log-in-with-ps-cli)
  - [9. Install the Policy System Plugin](#9-install-the-policy-system-plugin)
  - [What is exposed (evaluator)](#what-is-exposed-evaluator)
- [Production installation](#production-installation)
  - [Prerequisites (Production)](#prerequisites-production)
  - [1. Sign in to Azure](#1-sign-in-to-azure)
  - [2. Review scripts/ps-defaults.conf](#2-review-scriptsps-defaultsconf)
  - [3. Run scripts/deploy-ps-prod.sh](#3-run-scriptsdeploy-ps-prodsh)
  - [4. Access the cluster with kubelogin](#4-access-the-cluster-with-kubelogin)
  - [5. Enrol the owner's passkey](#5-enrol-the-owners-passkey)
  - [6. Set up each user's computer](#6-set-up-each-users-computer)
  - [What is exposed (production)](#what-is-exposed-production)
- [SystemOwner bootstrap](#systemowner-bootstrap)
- [Upgrading an existing install](#upgrading-an-existing-install)
- [Verification status](#verification-status)

This guide shows how to **deploy** Policy System. You can deploy it in one of two ways:

- An evaluator instance on your own laptop.
- A production rollout to a customer tenant in Azure.

The two are **separate paths**. Each path has its own script
([`scripts/deploy-ps-eval.sh`](../../scripts/deploy-ps-eval.sh) and
[`scripts/deploy-ps-prod.sh`](../../scripts/deploy-ps-prod.sh)). Both scripts deploy the same
Helm chart. Pick one path and follow only that section.

Both scripts do the same three things:

1. They ask for the owner's email.
2. They create that person as an Authentik administrator and as the first `SystemOwner`.
3. They print a single-use link to register a passkey. No password is ever set.

Other documents:

- For an overview of the project, see [README.md](../../README.md).
- To build, test, or release the project, see [CONTRIBUTING.md](../../CONTRIBUTING.md).
- To ask questions and use `ps-cli` after deployment, see the [User Guide](./user-guide.md).
- To rotate credentials, upgrade, back up, or tear down an instance, see the
  [Operations Guide](./operations-guide.md).

---

## Evaluator installation

> [!NOTE]
> **This path is for evaluators.** It runs Policy System on your own laptop. It uses a Helm
> chart on a local `kind` cluster and the script
> [`scripts/deploy-ps-eval.sh`](../../scripts/deploy-ps-eval.sh).

Production administrators use the same Helm chart with a different values profile. See
[Production installation](#production-installation). This section covers the evaluator
profile only.

The evaluator profile has these properties:

- It runs the bundled Authentik identity provider over HTTPS with a locally issued
  certificate.
- Login is required.

> **Why HTTPS:** Passkeys work only in a secure browser context.

### Prerequisites

Install these tools before you start:

| Tool                                                                 | Why                                         |
| --------------------------------------------------------------------- | -------------------------------------------- |
| [Claude Desktop](https://claude.com/download)                        | Hosts the Policy System Plugin              |
| [git](https://git-scm.com/downloads)                                 | Clones this repo                            |
| [Podman](https://podman.io/docs/installation)                        | Container runtime for the local cluster     |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | Runs a Kubernetes cluster on Podman         |
| [kubectl](https://kubernetes.io/docs/tasks/tools/)                   | Talks to the cluster                        |
| [Helm](https://helm.sh/docs/intro/install/)                          | Installs the Policy System chart            |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Provisions the Azure LLM backend (step 5)   |
| [jq](https://jqlang.org/download/)                                   | Used by the Azure LLM scripts (step 5) and `deploy-ps-eval.sh` (step 7) |
| [openssl](https://www.openssl.org/)                                  | `deploy-ps-eval.sh` uses it to issue the local CA and certificate (step 7) |
| [curl](https://curl.se/)                                             | `deploy-ps-eval.sh` uses it to call the Authentik API (step 7) |

### 1. Install Claude Desktop

1. Download Claude Desktop from [claude.com/download](https://claude.com/download) (macOS or
   Windows).
2. Sign in.

### 2. Install Podman and start its machine

Install Podman:

```bash
brew install podman
```

Create the Podman machine:

```bash
podman machine init --cpus 4 --memory 8192
```

Start the Podman machine:

```bash
podman machine start
```

Check that the machine runs:

```bash
podman info
```

> **Why 4 CPUs and 8 GB:** kind under Podman needs enough headroom to run a control plane and
> both Policy System containers. 4 CPUs and 8 GB is the tested minimum.

### 3. Clone the repo

Go to the folder where you want the repo.

Clone the repo:

```bash
git clone https://github.com/mindovermachine-dev/policy-system
```

Go to the repo root folder:

```bash
cd policy-system
```

Run all the remaining steps from this folder. They use repo-relative paths, such as
`deploy/kind/cluster.yaml` and `./charts/policy-system`.

### 4. Create the local cluster

Install kind and kubectl:

```bash
brew install kind kubectl
```

Tell kind to use Podman:

```bash
export KIND_EXPERIMENTAL_PROVIDER=podman
```

Create the cluster:

```bash
kind create cluster --config deploy/kind/cluster.yaml --name policy-system
```

Check that the cluster runs:

```bash
kubectl cluster-info --context kind-policy-system
```

The command prints the cluster information when the cluster is up.

> [!WARNING]
> **Do you already have a `policy-system` kind cluster from before the HTTPS Authentik
> setup?** If yes, delete it and create it again.
>
> ```bash
> kind delete cluster --name policy-system
> ```
>
> **This destroys all existing kind PVC data.** FalkorDB's graph, the PS Postgres database,
> and Authentik's Postgres database all live on PVCs in this cluster's local storage.
>
> After you delete the cluster:
>
> 1. Run the `kind create cluster` command above again.
> 2. Run step 5 and the steps after it again.
> 3. Reload the FalkorDB data. See [User Guide: Load curated
>    content](./user-guide.md#load-curated-content).
>
> **Why:** kind fixes its `extraPortMappings` when it creates a cluster. You cannot change
> them later. The evaluator profile serves Authentik over HTTPS on host port `30443`. Older
> clusters have a plain-HTTP mapping on `30080` and do not have the `30443` mapping.

### 5. Provision the Azure LLM backend

This step creates these resources in your own Azure subscription:

- An Azure Cognitive Services (`AIServices`) account.
- Two model deployments.
- A Key Vault.

It then syncs the credentials into the kind cluster from step 4. You do this step once per
subscription. You can run both scripts again safely. They do nothing when nothing changed.

Your signed-in identity needs the `Owner` or `Contributor` role at subscription scope.

Sign in to Azure:

```bash
az login
```

Run the provisioning script:

```bash
scripts/deploy-llm.sh
```

The script does these things:

1. It prints a confirmation table. The table shows the region candidates, resource group,
   account, both model deployments, and the Key Vault. The script reads all values from
   `scripts/llm-defaults.conf`. To use other model names or capacities, edit that file first.
2. It asks `Proceed with these values? [Y/n]`.
3. It checks your subscription permissions.
4. It checks the configured region and quota.
5. It provisions the resources. It prints a summary line only, never a secret value.

If the AIServices account is soft-deleted, the script finds it. It asks before it permanently
purges the account to release the model capacity. Add `--yes` to accept the deployment
prompt and the purge prompt without questions.

Sync the credentials into the cluster:

```bash
scripts/sync-llm-secrets-to-kind.sh
```

The script reads the three credentials from Key Vault. It writes them into the active kind
cluster as the Secret `policy-system-llm-credentials`.

> **Why it is safe:** The script stops unless your current `kubectl` context is a `kind-*`
> context. It cannot write Azure credentials into the wrong cluster.

To rotate this key later, see the [Operations
Guide](./operations-guide.md#rotating-the-azure-llm-api-key).

### 6. Install ps-cli

`ps-cli` is a command-line client for the PS Service REST API. You can use it to:

- Select and ingest EU regulations from Cellar/ELI.
- Ingest internal policies.
- Check service health and readiness.

You install `ps-cli` on its own, like `gh` or `az`. You do not need to clone this repo.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first. Then run:

```bash
curl -fsSL https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/ps-cli/install.sh | bash
```

The script [`ps-cli/install.sh`](../../ps-cli/install.sh) does these things:

1. It finds the **latest non-prerelease GitHub Release** of `ps-cli`.
2. It downloads the wheel of that release.
3. It checks the SHA-256 of the wheel against the `SHA256SUMS` asset of the release.
4. It installs the wheel with `uv tool install`.
5. It adds `ps-cli` to your `PATH` through the `uv` tool-install shims.

Check the installation:

```
ps-cli --version
```

The second line of the output shows `unavailable (...)`. This is expected, because PS Service
is not deployed yet. The command still exits with code 0.

**To upgrade:** run `install.sh` again. It installs over the version that is on your `PATH`.
There is no separate upgrade command.

**Linux only:** `ps-cli auth login`, `auth status`, and `auth logout` need `gir1.2-secret-1`
to use the OS Secret Service for encrypted credential storage. On Debian or Ubuntu, install
it:

```bash
sudo apt install python3-gi gir1.2-secret-1
```

If the package is missing, `ps-cli` prints an error with the exact install command. It does
not crash.

### 7. Deploy Policy System Backend

One script does the whole deployment. Before you run it, make sure that:

- You are in the repo root folder.
- Your kubectl context is the kind cluster. The script refuses to run against any other
  context than `kind-*`.
- The LLM Secret from step 5 exists.
- The cluster has the `30443` port mapping from step 4. If step 4's warning applies to you,
  recreate the cluster first.

Run the script:

```bash
scripts/deploy-ps-eval.sh
```

The script asks for the **owner's email**. This person becomes the first `SystemOwner` and the
Authentik administrator.

Options:

| Option | Meaning |
| --- | --- |
| `--owner-email <address>` | The owner's email. Skips the prompt. The address is the Authentik username and the OIDC `sub` that PS Service expects. |
| `--hostname <name>` | The name that serves Authentik. Default: `authentik.local`. Every machine that logs in must resolve this name to this machine. |
| `--apply-host-setup` | Runs the missing host steps through `sudo`. The steps are: map the hostname to `127.0.0.1` in `/etc/hosts`, and (macOS) trust the local CA in the system keychain. Without this option, an interactive run asks first. A `--yes` run only prints the commands. If both steps are already done, nothing happens. |
| `--yes` | Never prompts. You must then give `--owner-email`. |

Optional environment variables:

| Variable | Meaning | Default |
| --- | --- | --- |
| `PS_OWNER_LINK_TTL` | Minutes that the enrolment link stays valid | `30` |
| `PS_EVAL_STATE_DIR` | Folder for the local CA and certificate | `~/.config/policy-system/eval-tls` |
| `PS_ROLLOUT_TIMEOUT` | Rollout timeout | script default |

You do not need `PS_CHART_REF`. By default, the script deploys the published chart. To deploy
a checkout instead, run `helm dependency build charts/policy-system` once. Then set
`PS_CHART_REF=./charts/policy-system`. To find out when the published chart contains this
flow, see [Verification status](#verification-status).

The script does these things, in this order:

1. It checks that `kubectl`, `helm`, `openssl`, `jq`, `curl`, and `podman` (or `docker`) are
   on `PATH`. It stops and names every missing tool.
2. It finds the IP address of the kind node.
3. It creates a local certificate authority once. It creates a certificate for the hostname.
   It stores both in the state directory with owner-only permissions. It creates the
   certificate again when the hostname changes or the certificate is close to expiry.
4. It runs **one** `helm upgrade --install`. This sets the issuer
   (`https://<hostname>:30443/application/o/ps-cli/`) and the bootstrap owner together. The
   bootstrap owner is the owner's email as `sub`, with that issuer. There is no second
   deployment. The script runs the upgrade again only when its values change.
5. It waits until Authentik and PS Service are Ready. It makes Authentik serve the local
   certificate.
6. It creates the owner as an Authentik administrator. The username and email are the address
   you gave. **No password is set, prompted for, or logged.** It prints a single-use,
   time-limited link to register a passkey.

The script ends with these items, in this order:

1. What is left to do on this machine.
2. The `ps-cli` login command.
3. The enrolment link.

> **Why a real login:** The evaluator profile uses OIDC login against the bundled Authentik.
> `psService.localTestBypass.enabled=true` cannot start in a container. The bypass refuses to
> bind a non-loopback host, and every container image binds `0.0.0.0`. The deployment
> therefore runs with the bypass **off**.
>
> **Why HTTPS and a local certificate:** Browsers offer passkeys only in a secure context.
> Also, `ps-cli` refuses to send credentials to an issuer that is not `https` and not
> loopback.
>
> **Why the hostname must resolve to the kind node:** The PS Service pod must resolve the
> Authentik hostname to the node IP. Its token validation then reaches the same Authentik that
> a browser reaches.

Check the pods:

```bash
kubectl get pods
```

Check that PS Service is healthy:

```bash
curl http://127.0.0.1:8000/health
```

Check that PS Service is ready:

```bash
curl http://127.0.0.1:8000/ready
```

Open the FalkorDB web UI. Use it to explore the graph database:

```bash
open http://localhost:3001/login
```

A new system has an empty graph and cannot answer questions. To seed it, see [User Guide: Load
curated content](./user-guide.md#load-curated-content).

To upgrade to a newer release later, see the [Operations
Guide](./operations-guide.md#updating-to-the-latest-version). Your graph data stays across
upgrades.

#### Recover the owner

Run the script again with the same email. Whoever can run `kubectl` against the cluster can
run it. The result depends on the owner's state:

| Owner state | Result |
| --- | --- |
| No passkey registered (the link expired or was never used) | The script gives a fresh link. |
| A passkey is registered | The script changes nothing and gives no link. |
| A passkey is registered, but the only device is lost | Remove the device on the owner's user page in the Authentik admin UI. See [What is exposed (evaluator)](#what-is-exposed-evaluator) for how to reach it. Then run the script again. |
| The run failed (invalid email, Authentik unreachable, a missing tool) | The script exits with a non-zero code and prints the fix. It leaves no half-created user. A new run completes. |

### 8. Register your passkey and log in with ps-cli

The closing output of the script tells you what is still left to do on this machine. Do the
three parts below in order.

#### This machine: hostname and certificate

For the passkey page to open, this machine must do both of these:

- Resolve `authentik.local` to `127.0.0.1`.
- Trust the local CA. The CA file is `~/.config/policy-system/eval-tls/ca.pem`, or the same
  file under `PS_EVAL_STATE_DIR`.

The script checks both. Then one of these happens:

- **Both are in place.** The script prints "This machine is ready". Go to the next part.
- **One is missing.** The script asks whether to run the two `sudo` steps for you. The option
  `--apply-host-setup` answers yes in advance.
- **You declined, or a `sudo` step failed.** Run the commands that the output lists:

```bash
echo '127.0.0.1 authentik.local' | sudo tee -a /etc/hosts
```

```bash
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ~/.config/policy-system/eval-tls/ca.pem
```

The second command is for macOS only. On other systems, import `ca.pem` as a trusted root
certificate in the certificate store of the operating system or browser. Firefox has its own
store.

#### Register the owner's passkey

1. Open the enrolment link that the script printed last. Use a browser on this machine.
2. Register a passkey when the page asks. You can use a security key, the platform
   authenticator, or a phone. There is no username or password prompt.

Registering the passkey also logs you in.

The link works once. It expires after `PS_OWNER_LINK_TTL` minutes (default 30). If the link
expired, run `scripts/deploy-ps-eval.sh` again for a new link.

Registration ends on the Authentik application library. It shows **"No Applications
available"**. This is expected, not a failure. The only application is the `ps-cli` OAuth
client. It has no launch URL, so Authentik does not list it. You are registered and logged
in. Go to the next part.

#### Log in with ps-cli

The script already pointed `ps-cli` at the instance. It set and selected the `eval` context
(`http://127.0.0.1:8000`), unless that context was already correct. The script cannot log in
for you, because the login needs the passkey you just registered.

If the script printed the context commands instead, run them first. This happens when `ps-cli`
was not on `PATH` while the script ran (see step 6):

```bash
ps-cli config set-context eval --url http://127.0.0.1:8000
```

```bash
ps-cli config use-context eval
```

Log in:

```bash
ps-cli auth login
```

`ps-cli auth login` prints a verification URL and a code. Open the URL in the browser where you
registered the passkey. Complete the sign-in.

Check the connection:

```bash
ps-cli get health
```

If `auth login` reports that it could not verify the TLS certificate, this machine does not
trust the CA. This happens, for example, on a second machine. See [Another machine](#another-machine-a-colleague-on-the-lan).

> **Why no environment variable:** `ps-cli` checks the Authentik certificate against the
> operating system trust store. The script already added the local CA to that store. On
> Linux, `SSL_CERT_FILE` or `SSL_CERT_DIR` also work. On macOS and Windows, `ps-cli` ignores
> them.
>
> **Why HTTP for PS Service:** `ps-cli` allows plain HTTP to PS Service on the loopback
> address. Only the Authentik issuer needs HTTPS and the CA.

For what each command does, see [User Guide: Point ps-cli at your
instance](./user-guide.md#point-ps-cli-at-your-instance). For credential storage, see the
[Appendix](./user-guide.md#appendix-ps-cli-reference).

#### Another machine (a colleague on the LAN)

Do the hostname and certificate setup on every other machine whose browser or `ps-cli` logs in.
Two things are different:

1. **Map the hostname to the LAN IP of the evaluator laptop, not to loopback.** Add this
   line to the hosts file of that machine. To find the IP on macOS, run `ipconfig getifaddr en0`
   on the laptop.

   ```
   192.168.1.42 authentik.local
   ```

2. **Copy the CA certificate from the evaluator machine.**
   - The file is `~/.config/policy-system/eval-tls/ca.pem`. If you set `PS_EVAL_STATE_DIR`,
     the file is in that folder. The closing output of the script prints the full path.
   - Send the certificate only. **Never send `ca.key`** from the same folder.
   - You can use any file transfer: AirDrop, a shared drive, or `scp` (if Remote Login is on
     for the laptop).
   - Trust the copy as described above. `ps-cli` and the plugin bridge read the same OS trust
     store.

Without both changes, the browser shows a certificate warning and passkey registration fails.

The colleague registers a passkey from the invitation link that the owner issues. See
[SystemOwner bootstrap](#systemowner-bootstrap). The colleague then sets up `ps-cli` with the
context commands above.

### 9. Install the Policy System Plugin

The plugin has two named parts:

- The **Policy System Marketplace** (id `ps-marketplace`). This is this repo.
- The **Policy System Plugin** (id `ps-plugin`). The marketplace lists it.

Add the marketplace first. Then install the plugin from it.

1. Add the marketplace.

   In Claude Desktop - Code: **Settings** → **Plugins** → **Add** → **Add marketplace** →
   **Add from a repository**. Then enter this repo:

   ```text
   https://github.com/mindovermachine-dev/policy-system
   ```

   Or use the Claude Code CLI:

   ```text
   claude plugin marketplace add mindovermachine-dev/policy-system
   ```

2. Install the plugin.

   In Claude Desktop - Code: under **Settings** → **Plugins**, select **Policy System
   Plugin**.

   Or use the Claude Code CLI:

   ```text
   /plugin install ps-plugin@ps-marketplace
   ```

   Or use the shell:

   ```text
   claude plugin install ps-plugin@ps-marketplace
   ```

3. Quit Claude Desktop fully (⌘Q). Start it again. Open a **new** chat.

4. Check the installation. Ask:

   ```text
   List the policy system tools you have available
   ```

To ask a question, see the [User Guide](./user-guide.md#using-claude-desktop---code).

### What is exposed (evaluator)

The evaluator profile is for one trusted person on their own machine. It exposes Authentik to
the local network on purpose. This table shows what is reachable and by whom:

| Listener | Address | Reachable by | What it serves |
| --- | --- | --- | --- |
| Authentik over HTTPS | Host port `30443` on **all interfaces** (`0.0.0.0`). `deploy/kind/cluster.yaml` maps it to the HTTPS NodePort of the Authentik server. | Anyone who can reach port `30443` of this machine. On a shared LAN, this is everyone on the LAN. | **All of Authentik**: the login, enrolment, and device-code pages, the whole API, and the **admin UI** (`https://<hostname>:30443/if/admin/`). Only the Authentik login (a passkey) protects it. This is on purpose: it lets a colleague on the LAN log in ([issue #160](https://github.com/mindovermachine-dev/policy-system/issues/160)). |
| Authentik over plain HTTP | NodePort `30080` inside the kind node only | Nobody outside the node. It is not mapped to the host and not reachable from the LAN. | The deploy script uses it through a loopback `kubectl port-forward` while it sets up the certificate. |
| PS Service | `127.0.0.1:8000` | This machine only | The REST API and the MCP endpoint. Every call needs an Authentik-issued bearer token. |
| FalkorDB Browser | `127.0.0.1:3001` | This machine only | The graph explorer UI. Authentik does not protect it. |
| FalkorDB (Redis) | ClusterIP only | Pods in the cluster only. It is never mapped. | The graph database. |

**To reach the Authentik admin UI:**

1. Open `https://<hostname>:30443/if/admin/` in a browser that trusts the CA. The default is
   `https://authentik.local:30443/if/admin/`.
2. Sign in as the owner.

The admin UI uses the same HTTPS listener as everything else. You do not need a port-forward.

**To limit access to your own machine:**

- Block port `30443` of the laptop from the LAN with the host firewall.
- Do not enrol colleagues.

Things to know:

- The local CA private key (`ca.key`, in the state directory) can create a certificate for any
  name. Browsers that trust `ca.pem` accept that certificate. Keep the key on the evaluator
  machine with owner-only permissions. Never copy it. Share `ca.pem` only.
- The Authentik API token of the chart is the Authentik bootstrap token. It is
  **administrator-equivalent**. It is stored in the Secret `policy-system-authentik-api-token`.
  Anyone with `kubectl` access to the cluster can read it. The scripts never print it.
- The evaluator profile is not for production. Never expose it on the public internet.

---

## Production installation

> [!NOTE]
> **This path is for production and customer-tenant deployments** to a real Azure
> subscription. It uses [`scripts/deploy-ps-prod.sh`](../../scripts/deploy-ps-prod.sh).
>
> `deploy-ps-prod.sh` is separate and self-contained. It is not the same code path as
> `deploy-llm.sh` (used by [Evaluator installation step
> 5](#5-provision-the-azure-llm-backend)) or
> [`deploy-ps-eval.sh`](#7-deploy-policy-system-backend). All three keep working
> independently.
>
> In one run, `deploy-ps-prod.sh` provisions:
>
> - The LLM backend.
> - An AKS cluster.
> - The Helm release with the bundled Authentik identity provider.
> - Public HTTPS exposure.
>
> It then creates the owner.

### Prerequisites (Production)

Install these tools before you start:

| Tool | Why | Verify |
| --- | --- | --- |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Everything that `deploy-ps-prod.sh` provisions | `az version` |
| [jq](https://jqlang.org/download/) | `deploy-ps-prod.sh` uses it to parse Azure CLI JSON output | `jq --version` |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | Talks to the AKS cluster that `deploy-ps-prod.sh` creates. Also carries the `port-forward` for the owner-creation step and admin access. | `kubectl version --client` |
| [Helm](https://helm.sh/docs/intro/install/) | Installs the Policy System chart | `helm version` |
| [curl](https://curl.se/) | Calls the Authentik API to create the owner | `curl --version` |
| [kubelogin](https://azure.github.io/kubelogin/install.html) | Authenticates `kubectl` and `helm` against the AAD-enabled AKS cluster. See [step 4](#4-access-the-cluster-with-kubelogin). | `kubelogin --version` |

Your signed-in identity needs the `Owner` or `Contributor` role at subscription scope. The
script checks this before it changes anything.

### 1. Sign in to Azure

Sign in:

```bash
az login
```

Select the subscription:

```bash
az account set --subscription <subscription-id>
```

### 2. Review scripts/ps-defaults.conf

The file `scripts/ps-defaults.conf` holds the defaults that you can tune:

- Region candidates.
- Chat and embedding model names and SKUs.
- Capacities.
- `TLS_CONTACT_EMAIL`. Let's Encrypt sends expiry and revocation notices to this address. Leave
  it blank to be prompted.
- `AUTHZ_OWNER_EMAIL`. This is the owner's email: the first `SystemOwner` and Authentik
  administrator. Leave it blank to be prompted, or pass `--owner-email`.

The script no longer reads `AUTHZ_BOOTSTRAP_OWNER_SUBJECT` and `AUTHZ_BOOTSTRAP_OWNER_ISSUER`.
It derives the owner's identity from the email and the public hostname. There is no
placeholder identity and no second deployment. See [SystemOwner
bootstrap](#systemowner-bootstrap).

The default SKUs have quota on a fresh subscription. If your subscription or region is
different, find the right values before your first run. See item 1 in the [Operations
Guide](./operations-guide.md#manual-steps-and-operational-notes).

### 3. Run scripts/deploy-ps-prod.sh

Run the script:

```bash
scripts/deploy-ps-prod.sh
```

The script does these things:

1. It prints a confirmation table. The table shows the region candidates, resource group,
   AIServices account, both model deployments, Key Vault, AKS cluster name, and public DNS
   label. The script derives all values from your subscription id.
2. It asks `Proceed with these values? [Y/n]`.
3. It asks for the **owner's email**, unless `--owner-email <address>` or `AUTHZ_OWNER_EMAIL`
   gives it. An invalid address stops the run before any Azure call.
4. It prints a `==> <step>` progress line at the start of each phase.
5. It waits for Authentik after the Helm release.
6. It creates the owner as an Authentik administrator. The username and email are the address
   you gave. **No password is set, prompted for, or logged.**
7. It prints a summary.

Add `--yes` to skip the confirmation prompt. `--yes` never prompts for the email. Combine it
with `--owner-email` or `AUTHZ_OWNER_EMAIL`.

The whole run is idempotent. A new run with no changes does no work and reports this.

The summary names the secrets that the script wrote (never their values), the resulting URL,
and a single-use passkey-enrolment link. The script prints the link last, once:

```
Policy System provisioned. Wrote secrets: AZURE-API-BASE, AZURE-API-KEY, AZURE-API-VERSION.
PS Service: https://<label>.<region>.cloudapp.azure.com
Open this single-use link in a browser to register your passkey (valid 30 minutes):
https://<label>.<region>.cloudapp.azure.com/auth/if/flow/ps-passkey-recovery/...
```

> **Why a port-forward:** The Authentik admin API is not on the public Ingress. See [What is
> exposed (production)](#what-is-exposed-production). The script creates the owner through a
> temporary loopback `kubectl port-forward` to the Authentik Service. It prints the link on
> the public address.
>
> **One deployment:** The script sets the `sub` (the email) and the issuer of the bootstrap
> owner in the same deployment that creates the release.
>
> **When Helm runs again:** The script applies the Helm release again only when one of its
> managed values changed. These values are the LLM secret, the four `psService.auth.*`
> values, the owner subject and issuer, and the two Authentik URLs.

### 4. Access the cluster with kubelogin

`deploy-ps-prod.sh` creates the AKS cluster with `--enable-aad --enable-azure-rbac
--disable-local-accounts`. A plain `kubeconfig` from `az aks get-credentials` cannot
authenticate on its own. The script already ran that command for you. `kubectl` and `helm`
need `kubelogin` to complete the Azure AD sign-in.

Each operator must do this step on each machine. The script does not do it.

Convert the kubeconfig:

```bash
kubelogin convert-kubeconfig -l azurecli
```

Check the pods:

```bash
kubectl get pods
```

Both `ps-service` and `falkordb` must be in the "Running" state.

For API key rotation, manual operational notes, and teardown, see the [Operations
Guide](./operations-guide.md#production-operations).

### 5. Enrol the owner's passkey

1. Open the link that `deploy-ps-prod.sh` printed in a browser.
2. Register a passkey when the page asks. There is no username or password prompt.

Registering the passkey also logs you in.

The link works once. It lapses after 30 minutes. `PS_OWNER_LINK_TTL` sets another duration in
minutes.

Production uses a real Let's Encrypt certificate. You do not need to trust a CA in the browser
or for `ps-cli`.

After enrolment, the browser ends on `https://<host>/auth/`. The Authentik public allowlist
does not serve this path, so PS Service answers it. An error or "not found" page is
expected. You are enrolled and signed in.

#### Recover the owner

Run the script again with the same email. You need `kubectl` access to the cluster. The result
depends on the owner's state:

| Owner state | Result |
| --- | --- |
| No passkey registered (link expired or unused) | The script gives a fresh link. |
| A passkey is registered | The script changes nothing and gives no link. |
| A passkey is registered, but the only device is lost | Remove the device on the owner's user page in the Authentik admin UI. Reach it [by port-forward](#what-is-exposed-production). Then run the script again. |

### 6. Set up each user's computer

The steps above provision the shared backend once. Each person who queries this instance needs
the items below on their own machine. This applies to `ps-cli` and to the Claude Desktop plugin.
`scripts/deploy-ps-prod.sh` does none of it.

A user who is not the owner needs an invitation first:

1. The owner (or a `SystemAdmin`) runs the `ps-invite-user` skill.
2. The owner delivers the invite link. See [User Guide: Invite a new
   user](./user-guide.md#invite-a-new-user).
3. The invitee registers a passkey from that link. The invitee never sets a password. The
   account has no password set.

For the release that makes the link carry the public address, see [Verification
status](#verification-status).

**Install Claude Desktop.** Download it from [claude.com/download](https://claude.com/download)
(macOS or Windows). Sign in.

**Install `ps-cli`.** Install [uv](https://docs.astral.sh/uv/getting-started/installation/)
first. Then run:

```bash
curl -fsSL https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/ps-cli/install.sh | bash
```

**Point `ps-cli` at this instance and log in.** Use the URL that [step
3](#3-run-scriptsdeploy-ps-prodsh) printed (`https://<label>.<region>.cloudapp.azure.com`).
See [User Guide: Point ps-cli at your
instance](./user-guide.md#point-ps-cli-at-your-instance). Login is required, as on the
evaluator instance. Production deploys the bundled Authentik identity provider.

**Install the Policy System Plugin.**

1. Add the Policy System Marketplace (`ps-marketplace`) in Claude Desktop: **Customize** →
   **Plugins** → **Add** → **Add marketplace** → **Add from a repository**. Enter this repo:
   `https://github.com/mindovermachine-dev/policy-system`.
2. Install the Policy System Plugin (`ps-plugin`):
   `/plugin install ps-plugin@ps-marketplace`.

For more detail, see [Evaluator installation, step 9](#9-install-the-policy-system-plugin).

> **No extra plugin configuration:** The plugin's Policy System MCP connector (`ps-mcp`) runs a
> local `ps-cli-mcp-bridge`. The bridge uses the current `ps-cli` context. When the context is
> `prod`, the plugin talks to this instance. The bridge sends the stored `prod` credential as an
> `Authorization` header, like any other authenticated `ps-cli` call.

> **Logout and login while Claude runs:** You do not need to restart Claude. After
> `ps-cli auth logout`, the next plugin call fails with "no stored credentials" and tells you to
> run `ps-cli auth login`. The bridge sends nothing without a credential. After
> `ps-cli auth login`, the next call uses the new credential. The bridge writes a log to
> `~/.config/ps-cli/mcp-bridge.log`. Each call line shows `token_resolution_latency` (time to get
> the token) and `upstream_latency` (time PS Service took to answer).

### What is exposed (production)

`deploy-ps-prod.sh` publishes PS Service and a **restricted** part of Authentik on one public
hostname: `https://<label>.<region>.cloudapp.azure.com`. A Let's Encrypt certificate serves it.

The Ingress of Authentik routes only the eight path prefixes that an end user needs for login
and enrolment:

| Public path | Serves |
| --- | --- |
| `/auth/application/o/` | OIDC discovery, authorize, token, device, and JWKS endpoints |
| `/auth/device` | The device-code page that `ps-cli auth login` sends you to |
| `/auth/flows/-/default/` | The default flow redirects of Authentik |
| `/auth/if/flow/` | The login, enrolment, and recovery flow pages |
| `/auth/api/v3/flows/executor/` | The flow executor that those pages call |
| `/auth/api/v3/root/config/` | The flow-page configuration |
| `/auth/api/v3/core/brands/current/` | The flow-page branding |
| `/auth/static/` | The flow-page JavaScript, CSS, and images |

This table shows what is public and what is not:

| Surface | Public? | Notes |
| --- | --- | --- |
| The eight paths above | Yes, to anyone on the internet | Login, enrolment, and recovery run here. Authentik gates each flow itself (an enrolment or recovery link, a passkey). |
| PS Service (`/` and everything else on the host) | Yes | Every call needs an Authentik-issued bearer token. `/health` and `/ready` need no token. |
| The Authentik admin UI (`/auth/if/admin/`), the admin and core APIs, the invitation API, and `/auth/if/user/` | **No** | Not routed. PS Service answers the request, never Authentik. Verified on ingress-nginx, not on AKS. See [Verification status](#verification-status). |
| The owner enrolment link and the owner-creation API calls | Link: public path. API: **no** | The link opens on the public `/auth/if/flow/` path. The script creates the owner and the link through a cluster-internal `kubectl port-forward`. |
| PS Service to Authentik (`invite_user`) | No | PS Service calls the Authentik API on its in-cluster address (`http://policy-system-authentik-server/auth`). It builds the invitee's link on the public address only. |
| FalkorDB, Authentik's Postgres, PS Postgres | No | ClusterIP only, behind NetworkPolicies. |

**To reach the Authentik admin UI:**

1. Start a port-forward from a machine with cluster access (see [step
   4](#4-access-the-cluster-with-kubelogin)). Leave it running while you use the UI:

   ```bash
   kubectl port-forward svc/policy-system-authentik-server 9000:80
   ```

2. Open `http://localhost:9000/auth/if/admin/`.
3. Sign in as the owner.

Production uses the `/auth` prefix. The port-forward stays on your machine (loopback). It is
not public.

Things to know:

- The Authentik API token is the Authentik bootstrap token. It is **administrator-equivalent**.
  - The chart generates it once.
  - It is stored in the Secret `policy-system-authentik-api-token`.
  - Anyone with `kubectl` access to the cluster can read it.
  - The scripts never print it.
  - It does **not** rotate, because Authentik applies a bootstrap token once per tenant.
- The bundled `akadmin` account and the owner have no usable password (verified live). An
  invitee has **no password set** (an empty hash). All of them register passkeys instead.
  - The shipped login flow still contains a password stage.
  - An administrator can set a user's password in the admin UI.
  - Therefore "passkey-only" applies to enrolment and recovery. It does not come from removing
    the password stage.
  - Live checks showed that Authentik rejected a submitted password for both the owner and an
    invitee.
- This flow does not change production TLS termination (Let's Encrypt, cert-manager).

---

For the `ps-cli` configuration reference (context and credential storage, environment
variables, config files), see [User Guide: Appendix — ps-cli
reference](./user-guide.md#appendix-ps-cli-reference). For PS Service and chart configuration,
see the [Helm Chart Values Reference](./helm-chart-values-reference.md).

---

## SystemOwner bootstrap

Both deploy scripts set the bootstrap owner in **one deployment**:

- The owner's email that you give the script becomes the Authentik username. It also becomes
  the OIDC `sub` in the ID token. (The bundled `ps-cli` provider uses
  `sub_mode: user_username`.)
- In the same Helm release that deploys everything else, the script sets
  `psService.authzBootstrapOwner.subject` to that email. It sets `.issuer` to the Authentik
  issuer.

Nobody needs to log in first to find their own identity. There is no placeholder identity to
replace later.

The first authenticated caller whose `(sub, iss)` matches those two values gets `SystemOwner`,
exactly once. Anyone else who reaches an empty instance first gets nothing. PS Service audits
that attempt as `access_role.bootstrap_rejected`.

This applies to both the [Evaluator](#7-deploy-policy-system-backend) and the
[Production](#3-run-scriptsdeploy-ps-prodsh) installation. Only the script and the Authentik
base URL are different.

To become `SystemOwner`:

1. **Register a passkey** from the link that the script printed
   ([evaluator](#8-register-your-passkey-and-log-in-with-ps-cli),
   [production](#5-enrol-the-owners-passkey)).
2. **Log in with `ps-cli`** (`ps-cli auth login`). Or let the Claude Desktop plugin make the
   call. The plugin reuses that login.
3. **Make the first role-gated call** as the owner. For example, list the access roles with the
   `ps-manage-access-roles` skill. That call wins the bootstrap and makes you `SystemOwner`.
   For what to do next, see the [User Guide's Role System](./user-guide.md#role-system).

`SystemOwner` counts as `SystemAdmin` or above. The owner can therefore invite users directly
with the `ps-invite-user` skill. The owner does not need `SystemAdmin` first.

The owner is both the Authentik administrator and the PS `SystemOwner`. This is acceptable for
a single-owner deployment. To split the two roles later, change the roles.

### Check the owner's identity (optional)

To confirm that the owner's identity matches what PS Service expects, read `sub` and `iss`
from an access token. `ps-cli` never prints or stores an access token. Request one with the
Authentik device flow.

`<issuer>` is the value of `psService.auth.issuer`:

- Evaluator: `https://authentik.local:30443/application/o/ps-cli/` (it uses your `--hostname`).
- Production: `https://<label>.<region>.cloudapp.azure.com/auth/application/o/ps-cli/`.

On the evaluator, add `--cacert` with the path to the local CA to each `curl` command.

Request a device code:

```bash
ISSUER=<issuer>
DEVICE_ENDPOINT=$(curl -s "${ISSUER}.well-known/openid-configuration" | jq -r .device_authorization_endpoint)
TOKEN_ENDPOINT=$(curl -s "${ISSUER}.well-known/openid-configuration" | jq -r .token_endpoint)
curl -s -d client_id=ps-cli -d "scope=openid profile email" "$DEVICE_ENDPOINT"
```

Open the returned `verification_uri_complete` in a browser. Sign in with your passkey.

Exchange the returned `device_code` for an access token. The code expires after a few minutes.
If it expired, run the previous command again.

```bash
curl -s -d client_id=ps-cli \
  -d grant_type=urn:ietf:params:oauth:grant-type:device_code \
  -d device_code=<device_code> "$TOKEN_ENDPOINT" | jq -r .access_token
```

Decode the payload of the access token (the middle, dot-separated part). You do not need a
signature check, because you only read your own claims. `sub` must equal the owner email,
character for character:

```bash
echo '<access_token>' | jq -R 'split(".")[1] | gsub("-";"+") | gsub("_";"/") | . + ("="*((4 - length % 4) % 4)) | @base64d | fromjson | {sub, iss}'
```

## Upgrading an existing install

**Applies to:** installs made before this flow existed.

Those installs used a fixed placeholder as the API token that PS Service sends to Authentik
(`ps-service-authentik-dev-token`). No Authentik token matched it. These items are gone:

- The placeholder value.
- The `psService.authentik.apiToken` key.

The chart now generates one random 64-character token. It stores the token in the Secret
`policy-system-authentik-api-token`. It gives the same value to Authentik as
`AUTHENTIK_BOOTSTRAP_TOKEN` and to PS Service as `PS_AUTHENTIK_API_TOKEN`.

- A values file that still sets `psService.authentik.apiToken` has no effect.
- An existing Secret that holds the old placeholder gets a generated token on upgrade.

**If Authentik had no bootstrap token yet:** the first start after the upgrade creates the
token from the new value. Run the deploy script again. That is enough.

**If the Secret and Authentik disagree:** Authentik applies the bootstrap token **once per
tenant** and **never rotates it**. This can happen when someone edits the Secret after
Authentik already created its token. The deploy script then stops before it creates anything.
It reports that Authentik rejected the shared API token (HTTP 401 or 403). It never prints the
token.

To fix it:

1. Create an API token in the Authentik admin UI. Start a port-forward, then open the UI:

   ```bash
   kubectl port-forward svc/policy-system-authentik-server 9000:80
   ```

   - Evaluator: `http://127.0.0.1:9000/if/admin/`
   - Production: `http://127.0.0.1:9000/auth/if/admin/`

2. Store the token in a Secret with the key `PS_AUTHENTIK_API_TOKEN`.
3. Set `psService.authentik.existingSecret` to the name of that Secret. See the [Helm Chart
   Values Reference](./helm-chart-values-reference.md#authentik-and-local-tls-values).
4. Run the deploy script again.

**Evaluator cluster with no data to keep:** it is simpler to delete the cluster and start
again.

**Cluster with the old plain-HTTP port mapping:** you must recreate it in all cases. See the
warning in [step 4](#4-create-the-local-cluster).

## Verification status

This section lists what the flows above were verified against, and what they were not. Read it
before you rely on a claim for a real deployment.

- **Evaluator path: verified live** on a clean `kind` cluster (Podman) with the real script.
  The test covered:
  - One deployment.
  - Owner passkey enrolment in a browser.
  - `ps-cli auth login` and the MCP bridge with `SSL_CERT_FILE`.
  - `SystemOwner` granted on the first role-gated call.
  - A second user invited and enrolled.
  - Owner re-run recovery.
  - Certificate refresh without a manual restart.
- **A second machine on the LAN: simulated, not tried on physical hardware.**
  - A separate container with the CA in its trust store reached Authentik over HTTPS through
    the `0.0.0.0:30443` mapping of the host.
  - A second browser profile completed a passkey device login. The hostname was mapped to the
    LAN address.
  - Browser trust in that run used a test flag. It did not use an imported CA. The macOS
    keychain was untouched.
- **Production path: verified on `kind`, not on AKS.** The real `deploy-ps-prod.sh` and
  `values-prod.yaml` ran against `kind`. The run used `ingress-nginx`, a stand-in Azure CLI,
  and skipped the cert-manager and Let's Encrypt steps.
  - **Verified:**
    - The Ingress routes exactly the eight paths above.
    - The Ingress blocks the admin UI and the admin APIs.
    - The owner link on the public address enrols a passkey through the Ingress. No flow
      request was blocked.
    - A re-run leaves the owner untouched.
  - **Not verified:**
    - The AKS managed NGINX add-on.
    - Real Let's Encrypt issuance through this flow.
    - Real Azure resources.
- **The invitation link on production needs an unreleased PS Service image.**
  - `invite_user` calls Authentik in-cluster. It must build the invitee's link on the public
    address.
  - Issue #165 added the `PS_AUTHENTIK_PUBLIC_URL` support for this. It is in the repository
    but in no published release. The latest release, 3.11.0, does not contain it.
  - Until a release with it is published and deployed, production `invite_user` returns a link
    that starts with the in-cluster address `http://policy-system-authentik-server/auth`.
    Before you give the link to the invitee, replace that start with
    `https://<label>.<region>.cloudapp.azure.com/auth` by hand.
  - The evaluator is not affected. Users can already reach its Authentik address.
  - `invite_user` through PS Service on production was **not** exercised end to end.
- **The published Helm chart has the same limit.** Until a chart that contains this flow is
  published, run the scripts with `PS_CHART_REF=./charts/policy-system` from a checkout.
  `deploy-ps-eval.sh` and `deploy-ps-prod.sh` both honour it.
