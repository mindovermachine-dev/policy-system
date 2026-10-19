# Policy System Installation Guide

## Table of Contents

- [Part A: Server install](#part-a-server-install)
  - [Evaluator installation](#evaluator-installation)
    - [Prerequisites](#prerequisites)
    - [1. Install Podman and start its machine](#1-install-podman-and-start-its-machine)
    - [2. Download the deploy bundle](#2-download-the-deploy-bundle)
    - [3. Create the local cluster](#3-create-the-local-cluster)
    - [4. Provision the Azure LLM backend](#4-provision-the-azure-llm-backend)
    - [5. Deploy Policy System Backend](#5-deploy-policy-system-backend)
    - [6. Register the owner's passkey](#6-register-the-owners-passkey)
    - [What is exposed (evaluator)](#what-is-exposed-evaluator)
  - [Production installation](#production-installation)
    - [Prerequisites (Production)](#prerequisites-production)
    - [Download the deploy bundle (Production)](#download-the-deploy-bundle-production)
    - [1. Sign in to Azure](#1-sign-in-to-azure)
    - [2. Review scripts/ps-defaults.conf](#2-review-scriptsps-defaultsconf)
    - [3. Run scripts/deploy-ps-prod.sh](#3-run-scriptsdeploy-ps-prodsh)
    - [4. Access the cluster with kubelogin](#4-access-the-cluster-with-kubelogin)
    - [5. Enrol the owner's passkey](#5-enrol-the-owners-passkey)
    - [6. Invite users](#6-invite-users)
    - [What is exposed (production)](#what-is-exposed-production)
- [Part B: Client install](#part-b-client-install)
  - [B1. Install Claude Desktop](#b1-install-claude-desktop)
  - [B2. Map the name and trust the CA (evaluator only)](#b2-map-the-name-and-trust-the-ca-evaluator-only)
  - [B3. Install ps-cli](#b3-install-ps-cli)
  - [B4. Point ps-cli at the instance and log in](#b4-point-ps-cli-at-the-instance-and-log-in)
  - [B5. Install the Policy System Plugin](#b5-install-the-policy-system-plugin)
  - [Which Claude surfaces reach your deployment](#which-claude-surfaces-reach-your-deployment)
- [Reference](#reference)
  - [SystemOwner bootstrap](#systemowner-bootstrap)
  - [Upgrading an existing install](#upgrading-an-existing-install)
  - [Verification status](#verification-status)

This guide shows how to **deploy** Policy System and how to set up the computers that use it. It
has two parts:

- **Part A: Server install.** The operator does it once. Pick one of two paths:
  - An evaluator instance on your own laptop.
  - A production rollout to a customer tenant in Azure.
- **Part B: Client install.** Every person who uses the deployment does it on their own computer.
  This includes the server machine itself. Part B is the only place that describes client setup,
  for the evaluator and for production alike.

Each step sits in exactly one part. The two server paths are **separate**. Each has its own script
([`scripts/deploy-ps-eval.sh`](../../scripts/deploy-ps-eval.sh) and
[`scripts/deploy-ps-prod.sh`](../../scripts/deploy-ps-prod.sh)). Both scripts deploy the same
Helm chart. Pick one path in Part A and follow only that section.

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

# Part A: Server install

The operator does Part A once per deployment, on the machine that hosts it. It ends with a
running backend and the owner's passkey. Then everyone, the operator included, continues with
[Part B](#part-b-client-install).

## Evaluator installation

> [!NOTE]
> **This path is for evaluators.** It runs Policy System on your own laptop. It uses a Helm
> chart on a local `kind` cluster and the script
> [`scripts/deploy-ps-eval.sh`](../../scripts/deploy-ps-eval.sh).


### Prerequisites

Install these tools before you start:

| Tool                                                                 | Why                                         |
| --------------------------------------------------------------------- | -------------------------------------------- |
| [Podman](https://podman.io/docs/installation)                        | Container runtime for the local cluster     |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | Runs a Kubernetes cluster on Podman         |
| [kubectl](https://kubernetes.io/docs/tasks/tools/)                   | Talks to the cluster                        |
| [Helm](https://helm.sh/docs/intro/install/)                          | Installs the Policy System chart            |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Provisions the Azure LLM backend (step 4)   |
| [jq](https://jqlang.org/download/)                                   | Used by the Azure LLM scripts (step 4) and `deploy-ps-eval.sh` (step 5) |
| [openssl](https://www.openssl.org/)                                  | `deploy-ps-eval.sh` uses it to issue the local CA and certificate (step 5) |
| [curl](https://curl.se/)                                             | `deploy-ps-eval.sh` uses it to call the Authentik API (step 5) |

The computers that use the evaluator need their own tools. [Part B](#part-b-client-install) lists them.

### 1. Install Podman and start its machine

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
>
> **Proxy footprint: measured.** The evaluator proxy adds one more container (`nginx:1.31.7-alpine`).
> On a Podman machine with 9 CPUs and 16 GiB (macOS, 2026-10-20) it used 13 MiB of memory and 0m
> CPU idle, and 19 MiB and 417m CPU while 8 workers requested `/health` for 60 seconds. The
> proxy's memory is small next to the rest of the stack, so the minimum above is unchanged.
> The measurement was taken on a larger machine than the minimum, and it has no node-wide row
> because the kind cluster had no metrics-server. The declared `proxy.resources` limits are 128Mi
> memory and 500m CPU, so a sustained load near the measured 417m CPU is close to the CPU limit.
> To measure the footprint on your machine, follow [Operations Guide: Measuring the proxy
> footprint](./operations-guide.md#measuring-the-proxy-footprint-evaluator) with
> `scripts/measure-proxy-footprint.sh`.

### 2. Download the deploy bundle

The bundle holds the few files that steps 3 to 5 use: the kind cluster config and the deploy
scripts. The production path uses the same bundle. You do not need git or a clone of the repo.
Each release attaches the bundle. The bundle installs the chart of the same release, so the
scripts and the chart always match.

Go to the folder where you want the bundle. Download and unpack it:

```bash
curl -fsSL https://github.com/mindovermachine-dev/policy-system/releases/latest/download/ps-deploy-bundle.tgz | tar xz
```

Go to the bundle folder:

```bash
cd policy-system-deploy
```

Run all the remaining steps from this folder. They use relative paths, such as
`deploy/kind/cluster.yaml`.

> **Prefer a clone?** `git clone https://github.com/mindovermachine-dev/policy-system` and
> `cd policy-system` also work. Every step is the same. A clone installs the newest published
> chart instead of the chart of one release.

### 3. Create the local cluster

Install kind and kubectl:

```bash
brew install kind kubectl
```

Tell kind to use Podman:

```bash
export KIND_EXPERIMENTAL_PROVIDER=podman
```

Check that this machine is safe to expose. The cluster publishes the proxy on every network
interface of the machine (port `30443`), so the check lists the machine's addresses and fails
when one is outside the loopback, link-local and private ranges:

```bash
scripts/deploy-ps-eval.sh --check-exposure
```

The command prints `Exposure check passed` and exits `0` when every address is private. If it
fails, it names each public address. Disconnect or firewall that interface and run it again. If you
accept the exposure, pass `--allow-public-host`. A VPN address (for example Tailscale's
`100.64.0.0/10`) and a global IPv6 address also count as public. A machine with such an address
needs the flag on both calls: `--check-exposure --allow-public-host` here and `--allow-public-host`
on the `scripts/deploy-ps-eval.sh` run in step 5.

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
> 1. Run the exposure check and the `kind create cluster` command of this step again.
> 2. Run step 4 and the steps after it again.
> 3. Restore a PS Postgres backup (see [Operations Guide:
>    Restore](./operations-guide.md#restore)), or load the content again. See [User Guide: Load
>    curated content](./user-guide.md#load-curated-content).
>

### 4. Provision the Azure LLM backend

This step creates these resources in your own Azure subscription:

- An Azure Cognitive Services (`AIServices`) account.
- Two model deployments.
- A Key Vault.

It then syncs the credentials into the kind cluster from step 3. You can run both scripts
again safely. They do nothing when nothing changed.

Your signed-in Azure identity needs the `Owner` or `Contributor` role at subscription scope.

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
   `scripts/llm-defaults.conf`. To use other model names or capacities, edit that file before
you run the script.
2. It asks `Proceed with these values? [Y/n]`.
3. It checks your subscription permissions.
4. It checks the configured region and quota.
5. It provisions the resources. It prints a summary line only, never a secret value.

Sync the credentials into the cluster:

```bash
scripts/sync-llm-secrets-to-kind.sh
```

The script reads the three credentials from Key Vault. It writes them into the active kind
cluster as the Secret `policy-system-llm-credentials`.

### 5. Deploy Policy System Backend

One script does the whole deployment. Before you run it, make sure that:

- You are in the bundle folder from step 2 (or the repo root folder, if you cloned).
- Your kubectl context is the kind cluster. The script refuses to run against any other
  context than `kind-*`.
- The LLM Secret from step 4 exists.
- The cluster has the `30443` port mapping from step 3. If step 3's warning applies to you,
  recreate the cluster first.
- If step 3 needed `--allow-public-host`, pass it here too. The script runs the same exposure
  check first.

Run the script:

```bash
scripts/deploy-ps-eval.sh
```

The script asks for the **owner's email**. This person becomes the first `SystemOwner` and the
Authentik administrator.


The script does these things, in this order:

1. It checks that no interface of the machine holds a public address (see step 3). It stops
   before any cluster call, unless you passed `--allow-public-host`.
2. It checks that `kubectl`, `helm`, `openssl`, `jq`, `curl`, and `podman` (or `docker`) are
   on `PATH`. It stops and names every missing tool.
3. It finds the IP address of the kind node.
4. It creates a local certificate authority (CA) that can only vouch for the evaluator
   hostname. The CA has a critical name constraint for that hostname, is valid for at most 730
   days, and is limited to TLS server authentication. It creates a certificate for exactly that
   hostname. It stores both in the state directory with owner-only permissions. It creates the
   certificate again when its names differ or it is close to expiry. It replaces the CA, and
   the certificate with it, when the CA lacks the constraint, permits another name, is
   valid for longer than 730 days, or is close to expiry. It keeps the replaced CA next to the
   new one as `ca.replaced-<fingerprint prefix>.pem`.
5. It runs **one** `helm upgrade --install`. This sets the issuer
   (`https://<hostname>:30443/auth/application/o/ps-cli/`) and the bootstrap owner together. The
   bootstrap owner is the owner's email as `sub`, with that issuer. There is no second
   deployment. The script runs the upgrade again only when its values change.
6. It waits until Authentik, the proxy and PS Service are Ready. The proxy serves the local
   certificate. The script then checks from inside the PS Service pod that the issuer's OIDC
   discovery document can be fetched through the proxy, and stops with the cause when it cannot.
   Last, it checks from this machine, through port `30443` and verifying the CA, that `/health`
   answers, that the discovery document reports exactly the issuer the release was given, and that
   a request to `/mcp/` without a bearer token is refused with `401`.
7. It creates the owner as an Authentik administrator. The username and email are the address
   you gave. **No password is set, prompted for, or logged.** It prints a single-use,
   time-limited link to register a passkey.

The script ends with these items, in this order:

1. The client values for any machine that uses the evaluator: the hosts line (this machine's LAN
   address and the name), the `ca.pem` path, the `ps-cli config set-context` command and the
   `PS_MCP_URL` value. Keep this block. [Part B](#part-b-client-install) uses it on every client.
2. What is left to do on this machine. On Linux this lists the Debian/Ubuntu and Fedora/RHEL
   trust commands and never the macOS keychain command.
3. The `ps-cli` login command.
4. The enrolment link.

Check the pods:

```bash
kubectl get pods
```

Check that PS Service is healthy:

```bash
curl --cacert ~/.config/policy-system/eval-tls/ca.pem https://ps.test:30443/health
```

Check that PS Service is ready:

```bash
curl --cacert ~/.config/policy-system/eval-tls/ca.pem https://ps.test:30443/ready
```

Open the FalkorDB web UI. Use it to explore the graph database:

```bash
open http://localhost:3001/login
```

A new system has an empty graph and cannot answer questions. To seed it, see [User Guide: Load
curated content](./user-guide.md#load-curated-content).

To upgrade to a newer release later, see the [Operations
Guide](./operations-guide.md#updating-to-the-latest-version). Your graph data stays across
upgrades. Download the bundle again before you re-run `scripts/deploy-ps-eval.sh` after an
upgrade, because an old bundle installs the chart of its own release.

#### Upgrading an existing evaluator

Re-run `scripts/deploy-ps-eval.sh` with the new bundle. The upgrade changes what a cluster
created earlier exposes:

- Authentik and PS Service are `ClusterIP` Services and the proxy holds port `30443`. Re-running
  is the recovery if Helm reports the port as allocated while the Services switch over.
- The script creates a second Secret, `policy-system-local-ca`, that holds only the CA
  certificate. `policy-system-local-tls` holds the certificate and key, and only the proxy
  mounts it.
- Authentik no longer serves the local certificate. If its keypair `ps-local-tls` is still
  registered, the script prints how to remove it from the admin UI (System > Brands, then
  System > Certificates).
- A cluster created earlier keeps its `127.0.0.1:8000` host mapping. Nothing listens behind it
  any more, and the evaluator is no longer reached on `http://127.0.0.1:8000`: use
  `https://<hostname>:30443`, also on the evaluator machine.
- Signing credentials that were enrolled on the relying party `localhost` do not work on the new
  origin. Enrol them again after the upgrade.

#### Recover the owner

Run the script again with the same email. Whoever can run `kubectl` against the cluster can
run it. The result depends on the owner's state:

| Owner state | Result |
| --- | --- |
| No passkey registered (the link expired or was never used) | The script gives a fresh link. |
| A passkey is registered | The script changes nothing and gives no link. |
| A passkey is registered, but the only device is lost | Remove the device on the owner's user page in the Authentik admin UI. See [What is exposed (evaluator)](#what-is-exposed-evaluator) for how to reach it. Then run the script again. |
| The run failed (invalid email, Authentik unreachable, a missing tool) | The script exits with a non-zero code and prints the fix. It leaves no half-created user. A new run completes. |

### 6. Register the owner's passkey

This machine is also a client, so it needs the hostname and the local CA before the passkey page
opens. The closing output of the script in step 5 says what is still missing. Do
[B2, The server machine](#the-server-machine) now if it lists anything, then return here.

1. Open the enrolment link that the script printed last. Use a browser on this machine.
2. Register a passkey when the page asks. You can use a security key, the platform
   authenticator, or a phone. There is no username or password prompt.

Registering the passkey also logs you in.

The link works once. It expires after 30 minutes. If the link
expired, run `scripts/deploy-ps-eval.sh` again for a new link.

Registration ends on the Authentik application library. It shows **"No Applications
available"**. This is expected, not a failure. The only application is the `ps-cli` OAuth
client. It has no launch URL, so Authentik does not list it. You are registered and logged
in.

The backend is deployed and the owner is enrolled. Now do [Part B](#part-b-client-install) on this
machine and on every other machine that uses the evaluator. Colleagues who are not the owner
register their passkey from an invitation. See [SystemOwner bootstrap](#systemowner-bootstrap).

### What is exposed (evaluator)

The evaluator profile runs on one machine. It exposes one reverse proxy (nginx) to the local
network on purpose, so colleagues on the LAN can log in. The proxy serves PS Service and the
allowlisted Authentik login paths on one port. This table shows what is reachable and by whom:

| Listener | Address | Reachable by | What it serves |
| --- | --- | --- | --- |
| Proxy over HTTPS | Host port `30443` on **all interfaces** (`0.0.0.0`). `deploy/kind/cluster.yaml` maps it to the proxy's NodePort. | Anyone who can reach port `30443` of this machine. On a shared LAN, this is everyone on the LAN. | PS Service (the REST API and the MCP endpoint, every call needs an Authentik-issued bearer token) and the Authentik paths on the allowlist (`charts/policy-system/files/authentik-public-paths.txt`): the login, enrolment and device-code pages. |
| Authentik | ClusterIP only | Pods in the cluster only. | The deploy script reaches its API through a loopback `kubectl port-forward`. |
| PS Service | ClusterIP only | Pods in the cluster, and clients through the proxy. | The REST API and the MCP endpoint. |
| FalkorDB Browser | `127.0.0.1:3001` | This machine only | The graph explorer UI. Authentik does not protect it. |
| FalkorDB (Redis) | ClusterIP only | Pods in the cluster only. It is never mapped. | The graph database. |

**To reach the Authentik admin UI:**

1. Forward the Authentik server to this machine:
   `kubectl port-forward svc/policy-system-authentik-server 9000:80`.
2. Open `http://127.0.0.1:9000/auth/if/admin/` and sign in as the owner.

The admin UI is not served through the proxy.

**The exposure preflight.** `scripts/deploy-ps-eval.sh` stops before any cluster call when an
interface of the machine holds a public address, and `scripts/deploy-ps-eval.sh --check-exposure`
runs only that check (step 3 runs it before `kind create cluster`). Loopback, link-local, and
private addresses pass: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`,
`fc00::/7`, and `fe80::/10`. Everything else fails, including a VPN address in `100.64.0.0/10`. If
`ip` and `ifconfig` are both missing, the check fails because it cannot verify. `--allow-public-host`
accepts the exposure.

**Approval-page names.** The deploy script sets `psService.passkeySigning.allowedOrigins` to
`https://<hostname>:30443`, where `<hostname>` is the name you pass to `--hostname` (`ps.test` by
default). That origin is the WebAuthn relying party for signing approvals (`rp_id` is the
hostname), and a POST to an approval route (`/approvals/<id>/...`) on any other `Host` is answered
`421`. Open the approval page on `https://<hostname>:30443`, on this machine or any other that
maps the name and trusts the CA. The approval link the service hands out is built from the `Host`
of the request that created it, so a client that reached PS Service under a name that is not
listed receives a link that answers `421`; open it on `https://<hostname>:30443` instead.

**To limit access to your own machine:**

- Block port `30443` of the laptop from the LAN with the host firewall.

**Transport and accepted risks.** The proxy accepts TLS 1.2 and TLS 1.3 only, every listener
speaks TLS, and plain HTTP to port `30443` gets an nginx error page, never an API response.
It sets `Secure` and `SameSite=Lax` on every cookie it relays and adds `HttpOnly` to all but
Authentik's CSRF cookie, which the login page reads from JavaScript. A flag the upstream already
set is not repeated. It sends no HSTS header, because the certificate comes from a local CA. Only
the proxy pod mounts the private key of the local certificate. These risks remain on a shared LAN:

- **A rogue host on the LAN.** Anyone who can reach port `30443` can attempt logins and probe the
  allowlisted paths. Every PS Service call still needs an Authentik-issued bearer token.
- **Device-code phishing.** A user can be tricked into approving a device code that an attacker
  started. Approve a code only when you started the login yourself.
- **One shared origin.** PS Service and the Authentik login pages are served from one origin, so a
  script injected into one can read what the other sets for that origin.
- **MCP and DNS rebinding.** The MCP endpoint is protected by its bearer token alone; the loopback
  restriction of the former `127.0.0.1:8000` mapping no longer applies.

Things to know:

- The local CA private key (`ca.key`, in the state directory) can create a certificate only for
  the evaluator hostname and its subdomains, because the CA carries a critical name
  constraint, and only for TLS server authentication. Keep the key on the evaluator machine
  with owner-only permissions. Never copy it. Share `ca.pem` only.
- The Authentik API token of the chart is the Authentik bootstrap token. It is
  **administrator-equivalent**. It is stored in the Secret `policy-system-authentik-api-token`.
  Anyone with `kubectl` access to the cluster can read it. The scripts never print it.

---

## Production installation

**Experimental: this production path is not verified on AKS.** It was verified on `kind`
only. The AKS managed NGINX add-on, real Let's Encrypt issuance and real Azure resources
were not verified. Do not rely on it for a real deployment. See
[Verification status](#verification-status).

> [!NOTE]
> **This path is for production and customer-tenant deployments** to a real Azure
> subscription. It uses [`scripts/deploy-ps-prod.sh`](../../scripts/deploy-ps-prod.sh).
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

### Download the deploy bundle (Production)

The bundle holds the files `deploy-ps-prod.sh` needs: the script, `scripts/ps-defaults.conf` and
the production Helm values. You do not need git or a clone of the repo. Each release attaches
the bundle. The bundle installs the chart of the same release, so the script and the chart
always match.

Go to the folder where you want the bundle. Download and unpack it:

```bash
curl -fsSL https://github.com/mindovermachine-dev/policy-system/releases/latest/download/ps-deploy-bundle.tgz | tar xz
```

Go to the bundle folder:

```bash
cd policy-system-deploy
```

Run the remaining production steps from this folder. They use relative paths, such as
`scripts/ps-defaults.conf`. To upgrade later, download the bundle again.

> **Prefer a clone?** `git clone https://github.com/mindovermachine-dev/policy-system` and
> `cd policy-system` also work. A clone installs the newest published chart instead of the
> chart of one release.

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
- `AUTHZ_OWNER_EMAIL`. This is the owner's email. The owner is the first `SystemOwner` and
  Authentik administrator. Leave it blank to be prompted, or pass `--owner-email`.

The script does not read `AUTHZ_BOOTSTRAP_OWNER_SUBJECT` and `AUTHZ_BOOTSTRAP_OWNER_ISSUER`.
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

The summary names the secrets that the script wrote, but never their values. It also gives the
resulting URL and a single-use passkey-enrolment link. The script prints the link last, once:

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

A Let's Encrypt certificate serves the host. You do not need to trust a CA in the browser or
for `ps-cli`.

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

### 6. Invite users

The steps above provision the shared backend once. A user who is not the owner needs an
invitation before they can sign in:

1. The owner (or a `SystemAdmin`) runs the `ps-invite-user` skill.
2. The owner delivers the invite link. See [User Guide: Invite a new
   user](./user-guide.md#invite-a-new-user).
3. The invitee registers a passkey from that link. The invitee never sets a password. The
   account has no password set.

For the release that makes the link carry the public address, see [Verification
status](#verification-status).

Each person, the owner included, then sets up their own computer with [Part B](#part-b-client-install).
Production uses a Let's Encrypt certificate, so skip B2.

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

Authentik is served under the `/auth` prefix. The port-forward stays on your machine
(loopback). It is not public.

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
  - So "passkey-only" applies to enrolment and recovery. It does not come from removing
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

# Part B: Client install

Do Part B on **every machine that uses the deployment**: the server machine itself, and each
colleague's computer. The steps and values are the same on both. Only B2 differs between a server
machine and another machine, and only for the evaluator.

Get these values from the operator first:

| Value | Evaluator | Production |
| --- | --- | --- |
| Service URL | `https://ps.test:30443` (or the `--hostname` the operator chose) | `https://<label>.<region>.cloudapp.azure.com`, printed by [production step 3](#3-run-scriptsdeploy-ps-prodsh) |
| Hosts line and `ca.pem` | Needed. The closing output of `scripts/deploy-ps-eval.sh` prints them under "Client values" | Not needed. A Let's Encrypt certificate serves the host |
| `ps-cli` context | `eval` | `prod` |
| `PS_MCP_URL` | `<service URL>/mcp/` | `<service URL>/mcp/` |
| Your account | The owner's passkey ([step 6](#6-register-the-owners-passkey)), or an invitation | The owner's passkey ([step 5](#5-enrol-the-owners-passkey)), or an invitation |

## B1. Install Claude Desktop

1. Download Claude Desktop from [claude.com/download](https://claude.com/download) (macOS or
   Windows).
2. Sign in.

You can skip B1 if you only use the Claude Code CLI.

## B2. Map the name and trust the CA (evaluator only)

Production clients skip B2.

For the passkey page and `ps-cli` to work, every machine must do both of these:

- Map `ps.test` to the **LAN address of the server machine** in the hosts file. All machines
  use the same line, so one address works everywhere.
- Trust the local CA for TLS server authentication only.

Without both, the browser shows a certificate warning, passkey registration fails and
`ps-cli auth login` cannot verify the certificate.

### The server machine

For the passkey page to open, this machine must do both of these:

- Map `ps.test` to its own LAN address in the hosts file. This is the same line every other
  machine uses. The script finds the address: it takes the
  address of the default-route interface, else the only private IPv4 address of the machine. If
  neither gives one address (for example, because container bridges add several private
  addresses and there is no default route), the script stops before it changes anything and
  asks for `--lan-address <ip>`.
- Trust the local CA for TLS server authentication only. The CA file is
  `~/.config/policy-system/eval-tls/ca.pem`, or the same file under `PS_EVAL_STATE_DIR`.

The script checks both. Then one of these happens:

- **Both are in place.** The script prints "This machine is ready". Go to [B3](#b3-install-ps-cli).
- **One is missing.** The script asks whether to run the two `sudo` steps for you. The option
  `--apply-host-setup` answers yes in advance.
- **A hosts line maps `ps.test` to another address.** The script reports that line and
  never edits it, and never adds a second one. Edit or remove it so that `ps.test` maps to the
  LAN address, then run the script again.
- **You declined, or a `sudo` step failed.** Run the commands that the output lists. Replace
  `192.168.1.42` with the LAN address that the output shows:

```bash
echo '192.168.1.42 ps.test' | sudo tee -a /etc/hosts
```

On macOS, trust the CA for TLS server authentication only:

```bash
sudo security add-trusted-cert -d -r trustRoot -p ssl -k /Library/Keychains/System.keychain ~/.config/policy-system/eval-tls/ca.pem
```

On Linux, the system trust store has no switch for the purpose, so what limits the trust
depends on the distribution family. The CA itself is limited to TLS server authentication
(its extended key usage) and to `ps.test` (a critical name constraint):

- **Debian and Ubuntu.** The store ignores the extended key usage of a CA, so on this family
  only the name constraint limits what the CA can vouch for.

  ```bash
  sudo cp ~/.config/policy-system/eval-tls/ca.pem /usr/local/share/ca-certificates/ps-local-ca.crt && sudo update-ca-certificates
  ```

- **Fedora and RHEL.** The store takes the purposes from the extended key usage of the CA, so
  trust is limited to TLS server authentication, and it also enforces the name constraint.

  ```bash
  sudo cp ~/.config/policy-system/eval-tls/ca.pem /etc/pki/ca-trust/source/anchors/ps-local-ca.pem && sudo update-ca-trust
  ```

On other systems, import `ca.pem` as a trusted root for TLS server authentication in the
certificate store of the operating system or browser. Firefox has its own store.

### Another machine (a colleague on the LAN)

Do the same two things on every other machine, with the LAN address of the server machine:

1. **Map the hostname to the LAN address of the server machine.** Add this line to the hosts
   file of that machine. The closing output of the script prints the line with the address it
   found. To find the address by hand on macOS, run `ipconfig getifaddr en0` on the server
   machine.

   ```text
   192.168.1.42 ps.test
   ```

2. **Copy the CA certificate (`ca.pem`) from the server machine.**
   - The file is `~/.config/policy-system/eval-tls/ca.pem`. If you set `PS_EVAL_STATE_DIR`,
     the file is in that folder. The closing output of the script prints the full path.
   - Send the certificate only. **Never send `ca.key`** from the same folder.
   - You can use any file transfer: AirDrop, a shared drive, or `scp`.
   - Trust the copy for TLS server authentication only, with the commands above (on macOS,
     with `-p ssl`). `ps-cli` reads the same OS trust store.

Every colleague's hosts line points at that one address. **Reserve the address for the
server machine in the DHCP settings of your router** (a reserved DHCP address). If the
address changes, every colleague's browser stops reaching `ps.test` until the hosts line is
updated.

## B3. Install ps-cli

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

### Linux prerequisites

> [!NOTE]
> **Not yet verified on a headless host.** The commands in this section follow the failure that a
> real user hit on a headless Ubuntu machine. They have not been re-run end to end. See
> [Verification status](#verification-status).

On Linux, `ps-cli auth login`, `auth status` and `auth logout` store the credentials in the
operating system's Secret Service through the `gi` (PyGObject) module. Three facts decide the
setup:

- **`ps-cli` needs Python 3.14 or newer.** `uv` installs it in its own virtual environment (under
  `uv tool dir`) with a Python that `uv` fetches when the system has none. The `gi` module that
  `apt install python3-gi` provides is built for the **system** Python, so the tool environment
  cannot import it. That package does not fix the "missing a required system package" error, even
  though the error message of `ps-cli` names it.
- **`gi` has to be built inside the tool environment.** That needs the build dependencies in
  step 1 below.
- **The Secret Service needs a D-Bus session and an unlocked keyring.** A headless host (for
  example an SSH session) has neither by default.

On Debian or Ubuntu:

1. Install the build dependencies, the Secret Service library and a keyring:

   ```bash
   sudo apt install build-essential pkg-config libcairo2-dev libgirepository-2.0-dev gir1.2-secret-1 libsecret-1-0 gnome-keyring dbus-user-session
   ```

   On a release without `libgirepository-2.0-dev`, install `libgirepository1.0-dev` instead, and
   pin `pygobject<3.51` in step 3.

2. Install `ps-cli` with the command at the start of this step.

3. Add `pygobject` to the tool environment of `ps-cli`:

   ```bash
   uv pip install --python "$(uv tool dir)/ps-cli/bin/python" pygobject
   ```

   Run this again after every `ps-cli` upgrade. The upgrade rebuilds the tool environment
   and drops `pygobject`.

4. On a **headless host**, start a D-Bus session and unlock the keyring inside it. Run every
   `ps-cli` command in that session:

   ```bash
   dbus-run-session -- bash
   ```

   ```bash
   echo -n '<choose a keyring password>' | gnome-keyring-daemon --unlock
   ```

   Each new SSH session needs these two commands again. On a desktop session the keyring already
   runs, so skip this item.

For other distributions, install the equivalent packages. They are not verified.

## B4. Point ps-cli at the instance and log in

The account that you sign in with must first have a passkey. The owner registers it from the
link the deploy script printed. Everyone else registers it from an invitation. Neither person sets
a password.

On the machine that ran `scripts/deploy-ps-eval.sh`, the script already set and selected the
`eval` context, unless that context was already correct, or `ps-cli` was not on `PATH` while the
script ran (see B3). Elsewhere, set it. Use the **service URL** from the table at the top of Part B.

Evaluator:

```bash
ps-cli config set-context eval --url https://ps.test:30443
```

```bash
ps-cli config use-context eval
```

Production:

```bash
ps-cli config set-context prod --url https://<label>.<region>.cloudapp.azure.com
```

```bash
ps-cli config use-context prod
```

If you do not have the production URL, run `scripts/deploy-ps-prod.sh` again. It makes no changes
when nothing differs, and it prints the URL again.

Log in:

```bash
ps-cli auth login
```

`ps-cli auth login` runs an OIDC device-authorization flow. It prints a verification URL and a
code. Open the URL in the browser where you registered the passkey. Complete the sign-in.

Check the connection:

```bash
ps-cli get health
```

If `auth login` reports that it could not verify the TLS certificate, this machine does not trust
the CA. Do [B2](#b2-map-the-name-and-trust-the-ca-evaluator-only).

> **Why no environment variable:** `ps-cli` checks the Authentik certificate against the
> operating system trust store. B2 adds the local CA to that store. On Linux, `SSL_CERT_FILE` or
> `SSL_CERT_DIR` also work. On macOS and Windows, `ps-cli` ignores them.

For what each command does, see [User Guide: Set up ps-cli](./user-guide.md#set-up-ps-cli). For
credential storage, see the [Appendix](./user-guide.md#appendix-ps-cli-reference).

## B5. Install the Policy System Plugin

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

3. Set the PS Service URL.

   The plugin's Policy System MCP connector (`ps-mcp`) reads its URL from the `PS_MCP_URL`
   environment variable. Add it to the `env` block of `~/.claude/settings.json`:

   ```json
   {
     "env": {
       "PS_MCP_URL": "https://ps.test:30443/mcp/"
     }
   }
   ```

   This is the evaluator value. Use the production URL for a production deployment.

   - Evaluator: the proxy serves PS Service on port `30443`, so the URL is
     `https://<hostname>:30443/mcp/` (`https://ps.test:30443/mcp/` by default). The script prints
     this value as `PS_MCP_URL=...` in its closing summary.
   - Production: the service URL with `/mcp/` added, for example
     `https://<label>.<region>.cloudapp.azure.com/mcp/`.
   - The trailing slash is part of the URL.
   - If `PS_MCP_URL` is not set, `/mcp` shows `ps-mcp` as failed with
     `Missing environment variables: PS_MCP_URL`. Set it, then start a new Claude Code session.
   - The plugin does not use `userConfig` for the URL. A `${user_config.*}` value never reached
     Claude Desktop - Code sessions, and an unset one drops the server silently.

4. Quit Claude Desktop fully (⌘Q). Start it again. Open a **new** chat.

5. Sign in from the Claude Code CLI. In a terminal, run `claude`, then `/mcp`, select
   `plugin:ps-plugin:ps-mcp`, and choose **Authenticate**.

   - Once you have signed in here, a new Claude Desktop - Code session is signed in too.

   - The browser opens Authentik. Sign in with your own account. The plugin uses the
     pre-registered `ps-cli` OAuth client, so you enter no token and need no `ps-cli` install.
     PS Service attributes your calls to your own identity-provider `sub`.
   - Authentik shows its consent screen, because Claude Code sends `prompt=consent`. This is
     expected. Approve it.
   - The callback port `53682` must be free on your machine.
   - If a server you added by hand has the same URL as the plugin server, Claude Code does not
     load the plugin server, and `/mcp` shows no error. Remove one of the two.

6. Check the installation. Ask:

   ```text
   List the policy system tools you have available
   ```

Lowest verified versions: Claude Code CLI 2.1.236, and the Claude Desktop app 2.31226.1 (it
bundles Claude Code 2.1.295). Lower versions are untested.

To ask a question, see the [User Guide](./user-guide.md#using-claude-desktop---code).

## Which Claude surfaces reach your deployment

Claude Chat and Cowork call a remote connector from Anthropic's cloud. So they can only reach a
PS Service and an Authentik that are public on the internet. Claude Code runs on your machine,
so it connects from there. The evaluator is a non-public deployment (local `kind`, Authentik on
the LAN only). See [What is exposed (evaluator)](#what-is-exposed-evaluator). A production
deployment has a public hostname. See [What is exposed (production)](#what-is-exposed-production).

| Surface | Public deployment | Non-public deployment | Status |
| --- | --- | --- | --- |
| Claude Code (CLI) | Works | Works (connects from your machine) | Login, refresh and plugin-declared server: Verified (Claude Code 2.1.236). Non-public: Expected (inferred, not run). Evaluator on `ps.test`, `PS_MCP_URL` set to `https://ps.test:30443/mcp/`: pending, not yet verified on this name. |
| Claude Desktop - Code tab | Expected to work | Expected to work (runs locally) | Verified for the `PS_MCP_URL` setting path (Desktop app 2.31226.1, bundled Claude Code 2.1.295), against a stub with no authentication. A plugin-declared `oauth.clientId` in Desktop is untested (not tested). Login in the Desktop Code tab: Expected, not yet verified for a public deployment. Evaluator on `ps.test`: pending, not yet verified on this name. Sign in from the Claude Code CLI first. Then a new Desktop Code session is signed in. |
| Claude Desktop Chat and Cowork | Works as a manually added custom connector (URL plus Client ID `ps-cli`): GO | Does not work (needs public PS Service and Authentik) | Expected for everything beyond the login result. |
| claude.ai | Expected to work | Does not work (same reason) | Expected, not yet verified (not tested). |

This describes the direct connector: a remote connector that logs in against Authentik. The
plugin declares it as a remote HTTP server with no local process. The table lists what is verified per
row by issue. Everything else here is expected. Chat and Cowork support is a later, separate
plugin, because the skills accept only the tool names of `plugin:ps-plugin:ps-mcp`. The plugin
declaration in Claude Desktop (`oauth.clientId`) is untested, and claude.ai is not tested.

**The connector must use the existing `ps-cli` client**, not a new one. The token's `aud` is the
client ID, and PS Service accepts it only when `PS_AUTH_AUDIENCE` equals that client ID.
The chart provisions the client. The grants and redirect URIs are in the
[IdP configuration contract](./idp-configuration-contract.md#grants-and-redirect-uris-of-the-ps-cli-provider).
For an identity provider other than Authentik, see
[Other identity providers](./idp-configuration-contract.md#other-identity-providers).

Two behaviours of Claude Code 2.1.236 (CLI), Verified. They are also expected in the
Desktop Code tab, but not separately verified there. They are not claimed for Chat, Cowork or
claude.ai:

- **Consent screen.** Claude Code sends `prompt=consent`, so Authentik shows its consent screen
  at login even though the authorization flow is implicit consent. This is expected. Approve it.
- **Same URL hides the plugin server.** If a plugin server has the same URL as a server you added
  by hand, Claude Code does not load the plugin server, and `/mcp` shows no error. Remove one of
  the two. An unresolved `${user_config.*}` value also drops the server silently. `userConfig` does
  not reach Desktop Code sessions, so the plugin uses `PS_MCP_URL`.

---

# Reference

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

This applies to both the [Evaluator](#5-deploy-policy-system-backend) and the
[Production](#3-run-scriptsdeploy-ps-prodsh) installation. Only the script and the Authentik
base URL are different.

To become `SystemOwner`:

1. **Register a passkey** from the link that the script printed
   ([evaluator](#6-register-the-owners-passkey),
   [production](#5-enrol-the-owners-passkey)).
2. **Log in with `ps-cli`** (`ps-cli auth login`). Claude Desktop plugin sign-in is separate.
   See [B5](#b5-install-the-policy-system-plugin).
3. **Make the first role-gated call** as the owner. For example, list the access roles with the
   `ps-manage-access-roles` skill. That call wins the bootstrap and makes you `SystemOwner`.
   For what to do next, see the [User Guide's Role System](./user-guide.md#role-system).

`SystemOwner` counts as `SystemAdmin` or above. So the owner can invite users directly
with the `ps-invite-user` skill. The owner does not need `SystemAdmin` first.

The owner is both the Authentik administrator and the PS `SystemOwner`. This is acceptable for
a single-owner deployment. To split the two roles later, change the roles.

### Check the owner's identity (optional)

To confirm that the owner's identity matches what PS Service expects, read `sub` and `iss`
from an access token. `ps-cli` never prints or stores an access token. Request one with the
Authentik device flow.

`<issuer>` is the value of `psService.auth.issuer`:

- Evaluator: `https://ps.test:30443/auth/application/o/ps-cli/` (it uses your `--hostname`).
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

   - Evaluator and production: `http://127.0.0.1:9000/auth/if/admin/`

2. Store the token in a Secret with the key `PS_AUTHENTIK_API_TOKEN`.
3. Set `psService.authentik.existingSecret` to the name of that Secret. See the [Helm Chart
   Values Reference](./helm-chart-values-reference.md#authentik-and-local-tls-values).
4. Run the deploy script again.

**Evaluator cluster with no data to keep:** it is simpler to delete the cluster and start
again.

**Cluster with the old plain-HTTP port mapping:** you must recreate it in all cases. See the
warning in [step 3](#3-create-the-local-cluster).

### Moving an existing evaluator off a .local hostname

**Applies to:** evaluators installed with a hostname ending in `.local` (the former default).

Claude Code cannot resolve `.local` names. It fails with `ENOTFOUND`,
even when `/etc/hosts` maps the name. The evaluator name is `ps.test`, and
`deploy-ps-eval.sh` rejects any `--hostname` that ends in `.local`. The hostname is also the
issuer and the WebAuthn relying-party ID, so moving an install changes all three. The steps:

1. Download the bundle of the release that contains this change (step 2), or pull it if you
   cloned the repo.
2. Run `scripts/deploy-ps-eval.sh --owner-email <same email>`. Before it touches the cluster, it
   prints what this machine still needs and the reset command in step 5. Read it first.
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
   [B2, The server machine](#the-server-machine).
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
6. Client install (Part B): follow only Part B, plus the "Client values" block that
   `scripts/deploy-ps-eval.sh` printed, on each of the four machines below. Record the result in
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
7. Client install matrix (Part B). For each cell, record the OS and version. Then record one
   result per check: `ps-cli get health` after login; Claude Code (CLI) connected to the plugin
   server; Desktop Code tab connected to the plugin server:
   macOS, same machine as the server:
   macOS, separate machine:
   Linux, same machine as the server:
   Linux, separate machine (headless: dbus session and keyring steps used, yes/no):
```

**After evidence is recorded.** Update the [surface table](#which-claude-surfaces-reach-your-deployment). In the CLI row and in the Desktop row, replace the pending sentence with the verified result and the recorded versions. For the CLI row, give the Claude Code version. For the Desktop row, give the Desktop app version and the bundled Claude Code version. Change the Desktop non-public status from "Expected" to "Verified" only with recorded evidence.

**Graph mutation log.** Upgrading to a version with the graph mutation log also runs a
provisioning Job. The Job applies the ordinary `ps_state` migrations. It also creates the
owner-protected `graph_log` tables in `ps_state`. The Postgres init script cannot do this on an
existing cluster. The upgrade command must pass `--wait --wait-for-jobs`. See the [Operations Guide: Upgrading to the graph mutation
log](./operations-guide.md#upgrading-to-the-graph-mutation-log).

## Verification status

This section lists what the flows above were verified against, and what they were not. Read it
before you rely on a claim for a real deployment.

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
  - A separate container with the CA in its trust store reached the proxy over HTTPS through
    the `0.0.0.0:30443` mapping of the host.
  - A second browser profile completed a passkey device login. The hostname was mapped to the
    LAN address.
  - Browser trust in that run used a test flag. It did not use an imported CA. The macOS
    keychain was untouched.
- **Client install (Part B): not yet verified.** Part B restructured the client steps into one
  place. It was not followed on a clean machine yet. Nothing below is claimed as verified until the
  matrix in [Recording verification evidence](#recording-verification-evidence) is filled in:
  - macOS, same machine as the server: `ps-cli`, Claude Code CLI and the Desktop Code tab
    verified (see above).
  - macOS, separate machine: pending.
  - Linux, same machine as the server: pending.
  - Linux, separate machine: pending.
  - The Linux prerequisites in [B3](#b3-install-ps-cli) are the recipe to test. A real user hit
    the failures on a headless Ubuntu machine, and the commands in B3 have not been re-run end to
    end. The package names for a recent Ubuntu release (`libgirepository-2.0-dev`) and the
    `gnome-keyring-daemon --unlock` step are the parts most likely to need a correction.
- **Production path: verified on `kind`, not on AKS.** The real `deploy-ps-prod.sh` and
  `values-prod.yaml` ran against `kind`. The run used `ingress-nginx` and a stand-in Azure CLI.
  It skipped the cert-manager and Let's Encrypt steps.
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
- **Direct Claude connector: partly verified.** The Claude Code CLI (2.1.236) logged in as the `ps-cli` client against a standalone Authentik, using the plugin-declared server. It also refreshed across sessions. The Desktop Code tab read the `PS_MCP_URL` setting from `settings.json` against a stub with no authentication (Desktop app 2.31226.1). A manually added custom connector in Claude Desktop Chat and Cowork logged in against a public deployment (GO). None of this was run through these scripts. Not verified: the plugin-declared `oauth.clientId` in Desktop, claude.ai, and the plugin on a live deployment until a result is recorded. See [Which Claude surfaces reach your deployment](#which-claude-surfaces-reach-your-deployment).
- **The invitation link on production is built on the public address.**
  - `invite_user` calls Authentik in-cluster, so it builds the invitee's link from
    `PS_AUTHENTIK_PUBLIC_URL`, which `deploy-ps-prod.sh` sets through `psService.authentik.publicUrl`.
  - Without that variable the link starts with the in-cluster address
    `http://policy-system-authentik-server/auth`. Replace that start with
    `https://<label>.<region>.cloudapp.azure.com/auth` by hand before you give the link to the
    invitee.
  - The evaluator is not affected. Users can already reach its Authentik address.
  - `invite_user` through PS Service on production was **not** exercised end to end.
- **A checkout chart.** To run the scripts against the chart in a checkout instead of the
  published chart, set `PS_CHART_REF=./charts/policy-system`. `deploy-ps-eval.sh` and
  `deploy-ps-prod.sh` both use it.
