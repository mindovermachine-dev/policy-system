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
    - [Reset an earlier account-scoped marketplace (manual)](#reset-an-earlier-account-scoped-marketplace-manual)
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

This guide covers **deploying** Policy System — either an evaluator instance on your
own laptop, or a production customer-tenant rollout to Azure. The two are **separate
paths** with separate scripts ([`scripts/deploy-ps-eval.sh`](../../scripts/deploy-ps-eval.sh)
and [`scripts/deploy-ps-prod.sh`](../../scripts/deploy-ps-prod.sh)) that deploy the same Helm
chart; pick one and follow only its section. Both ask for the owner's email, create that
person as an Authentik administrator and the first `SystemOwner`, and print a single-use link
to register a passkey — no password is ever set. If you want an overview of the project see
[README.md](../../README.md). If you want to build, test, or release the project, see
[CONTRIBUTING.md](../../CONTRIBUTING.md). Once your instance is deployed, see the [User
Guide](./user-guide.md) for asking questions and using `ps-cli`, and the [Operations
Guide](./operations-guide.md) for rotating credentials, upgrading, backing up, and tearing
down an instance.

---

## Evaluator installation

> [!NOTE]
> **This path is for evaluators** trying Policy System on their own laptop via a
> Helm chart on a local `kind` cluster, using
> [`scripts/deploy-ps-eval.sh`](../../scripts/deploy-ps-eval.sh).

The same Helm chart also serves **production administrators** deploying to a real
Azure subscription, with a different values profile — see [Production
installation](#production-installation). This walkthrough covers the evaluator profile
only. The evaluator profile runs the bundled Authentik identity provider over HTTPS with a
locally issued certificate (passkeys only work in a secure browser context), and logging in
is required.

### Prerequisites

| Tool                                                                 | Why                                         |
| --------------------------------------------------------------------- | -------------------------------------------- |
| [Claude Desktop](https://claude.com/download)                        | Hosts the Policy System Plugin              |
| [git](https://git-scm.com/downloads)                                 | Clones this repo                            |
| [Podman](https://podman.io/docs/installation)                        | Container runtime backing the local cluster |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | Runs a Kubernetes cluster on Podman         |
| [kubectl](https://kubernetes.io/docs/tasks/tools/)                   | Talks to the cluster                        |
| [Helm](https://helm.sh/docs/intro/install/)                          | Installs the Policy System chart            |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Provisions the Azure LLM backend (step 5)   |
| [jq](https://jqlang.org/download/)                                   | Used by the Azure LLM bootstrap scripts (step 5) and `deploy-ps-eval.sh` (step 7) |
| [openssl](https://www.openssl.org/)                                  | `deploy-ps-eval.sh` issues the local CA and certificate with it (step 7) |
| [curl](https://curl.se/)                                             | `deploy-ps-eval.sh` calls Authentik's API with it (step 7) |


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
> **Already have a `policy-system` kind cluster from before this guide's HTTPS Authentik
> setup?** `kind`'s `extraPortMappings` are fixed at cluster creation and cannot be
> changed on an existing cluster. The evaluator profile serves Authentik over HTTPS on host
> port `30443`, and older clusters were created with a plain-HTTP mapping on `30080`
> instead — an old cluster does not have the `30443` mapping, so you must delete and
> recreate it:
> ```bash
> kind delete cluster --name policy-system
> ```
> **This destroys all existing kind PVC data** — FalkorDB's graph, the PS Postgres
> database and Authentik's Postgres database all live on PVCs backed by this cluster's
> local storage. Re-run the `kind create cluster` command above, then re-run step 5 onward;
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
bind a non-loopback host, and every container image binds `0.0.0.0`), so the deploy runs with
the bypass **off**. Authentik is served over HTTPS with a locally issued certificate, because
browsers only offer passkeys in a secure context, and `ps-cli` refuses to send credentials to
a non-`https`, non-loopback issuer. This needs the `30443` port mapping in
`deploy/kind/cluster.yaml` from step 4, so make sure step 4's cluster-recreate warning
doesn't apply to you.

One script does the whole deploy. Run it from the repo root with your kubectl context set to
the kind cluster (it refuses to run against anything but a `kind-*` context) and the LLM
Secret from step 5 in place:

```bash
scripts/deploy-ps-eval.sh
```

It prompts for the **owner's email** — the person who becomes the first `SystemOwner` and the
Authentik administrator. Options:

| Option | Meaning |
| --- | --- |
| `--owner-email <address>` | The owner's email; skips the prompt. The address is both the Authentik username and the OIDC `sub` PS Service expects. |
| `--hostname <name>` | The name Authentik is served under (default `authentik.local`). It must resolve to this machine on every machine that logs in. |
| `--apply-host-setup` | Runs, through `sudo`, the steps this machine still lacks: mapping the hostname to `127.0.0.1` in `/etc/hosts` and (macOS) trusting the local CA in the system keychain. Without it an interactive run asks first, and a `--yes` run only prints the commands. Nothing happens when both are already in place. |
| `--yes` | Never prompt. An owner email is then required (`--owner-email`). |

`PS_OWNER_LINK_TTL` (minutes the enrolment link stays valid, default `30`), `PS_EVAL_STATE_DIR`
(where the local CA and certificate live, default `~/.config/policy-system/eval-tls`) and
`PS_ROLLOUT_TIMEOUT` are optional environment overrides. You do not need `PS_CHART_REF`: the
script deploys the published chart unless you point it at a checkout, for example
`PS_CHART_REF=./charts/policy-system` (run `helm dependency build charts/policy-system` once
first) — see [Verification status](#verification-status) for when the published chart contains
this flow.

In one pass the script:

1. Checks that `kubectl`, `helm`, `openssl`, `jq`, `curl` and `podman` (or `docker`) are on
   `PATH`, and stops naming every missing one.
2. Detects the kind node's IP address (PS Service's pod must resolve the Authentik hostname to
   it, so its token validation reaches the same Authentik a browser reaches).
3. Creates a local certificate authority once, and a certificate for the hostname (regenerated
   automatically when the hostname changes or it is close to expiry), stored under the state
   directory with owner-only permissions.
4. Runs **one** `helm upgrade --install`, which sets the issuer
   (`https://<hostname>:30443/application/o/ps-cli/`) and the bootstrap owner (the owner's email
   as `sub`, with that issuer) together — there is no second deploy. It re-runs the upgrade only
   when its values changed.
5. Waits for Authentik and PS Service to be Ready, and makes Authentik serve the local
   certificate.
6. Creates the owner as an Authentik administrator — username and email are the address you gave,
   **no password is set, prompted for or logged** — and prints a single-use, time-limited link
   to register a passkey.

It ends with what is left to do on this machine, the `ps-cli` login command, and the enrolment link last. Then check
the pods and PS Service:

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

`localhost:3001/login` opens the FalkorDB web UI used to explore the graph database.

**Re-running the script is the owner-recovery path** (it is gated by cluster access — whoever
can run `kubectl` against the cluster can run it). With the same email:

- an owner who has **no passkey registered** (the link expired or was never used) gets a fresh
  link;
- an owner who already has a passkey is left unchanged and no link is issued. If the only
  device was lost, remove it from the owner's user page in the Authentik admin UI (see [What
  is exposed (evaluator)](#what-is-exposed-evaluator) for how to reach it) and re-run;
- a failed run — invalid email, Authentik unreachable, a missing tool — exits non-zero with the
  fix, leaves no half-created user, and a re-run completes.

Once deployed, see the [Operations Guide](./operations-guide.md#updating-to-the-latest-version)
for how to upgrade to a newer release later — your graph data is kept across upgrades.

A freshly deployed system has an empty graph and can answer nothing — see the [User
Guide: Load curated content](./user-guide.md#load-curated-content) for seeding it.

### 8. Register your passkey and log in with ps-cli

The script's closing output tells you what, if anything, is still left to do on this machine.
Work through the parts below in order.

#### This machine: hostname and certificate

For the passkey page to open, this machine must resolve `authentik.local` to `127.0.0.1` and
trust the local CA (`~/.config/policy-system/eval-tls/ca.pem`, or under `PS_EVAL_STATE_DIR`).
The script checks both. If either is missing it asks whether to run the two `sudo` steps for
you (`--apply-host-setup` answers yes in advance); if everything is in place it prints
"This machine is ready" and you can skip to the next part. If you declined, or the `sudo` step
failed, run what the output lists:

```bash
echo '127.0.0.1 authentik.local' | sudo tee -a /etc/hosts
```

```bash
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ~/.config/policy-system/eval-tls/ca.pem
```

The second command is macOS only. On other systems, import `ca.pem` as a trusted root
certificate in the operating system's or browser's certificate store (Firefox keeps its own
store).

#### Register the owner's passkey

Open the enrolment link the script printed last, in a browser on this machine. There is no
username or password prompt: the page asks you to register a passkey (a security key, the
platform authenticator, or a phone), and registering it also logs you in. The link works once
and expires after `PS_OWNER_LINK_TTL` minutes (default 30); if it lapsed, re-run
`scripts/deploy-ps-eval.sh` for a fresh one.

Registration ends on Authentik's own application library, which shows **"No Applications
available"**. That is expected, not a failure: the only application is the `ps-cli` OAuth
client, which has no launch URL, so Authentik does not list it. You are registered and logged
in; continue with the next part.

#### Log in with ps-cli

The script has already pointed `ps-cli` at the instance: it set and selected the `eval`
context (`http://127.0.0.1:8000`), unless that context was already correct. It cannot log in
for you, because the login needs the passkey you just registered. Run the command the script
printed:

```bash
ps-cli auth login
```

`ps-cli` verifies Authentik's certificate against the operating system trust store, which the
script has already added the local CA to, so no environment variable is needed. If `auth login`
reports that it could not verify the TLS certificate, the CA is not trusted on this machine (for
example a second machine: see the colleague steps below). On Linux, `SSL_CERT_FILE` or
`SSL_CERT_DIR` also work; on macOS and Windows they are ignored.

`auth login` prints a verification URL and code; open it in the browser you registered the
passkey in and complete the sign-in. Then verify:

```bash
ps-cli get health
```

If the script printed the context commands instead (because `ps-cli` was not on `PATH` when it
ran, see step 6), run them once first:

```bash
ps-cli config set-context eval --url http://127.0.0.1:8000
```

```bash
ps-cli config use-context eval
```

See [User Guide: Point ps-cli at your
instance](./user-guide.md#point-ps-cli-at-your-instance) for what each command does and the
[Appendix](./user-guide.md#appendix-ps-cli-reference) for credential storage. `PS Service`
itself is addressed over plain HTTP on the loopback address, which `ps-cli` allows; only the
Authentik issuer needs HTTPS and the CA.

#### Another machine (a colleague on the LAN)

Repeat the hostname and certificate setup on every other machine whose browser or `ps-cli`
will log in, with two differences:

- **Map the hostname to the evaluator laptop's LAN IP**, not loopback (for example
  `ipconfig getifaddr en0` on macOS on the laptop), in that machine's hosts file:
  ```
  192.168.1.42 authentik.local
  ```
- **Copy the CA certificate from the evaluator's machine.** It is the file
  `~/.config/policy-system/eval-tls/ca.pem` (under `PS_EVAL_STATE_DIR` instead, if you set it;
  the script's closing output prints the full path). Send the certificate only, **never
  `ca.key`** in the same folder. Any file transfer works (AirDrop, a shared drive, or `scp`
  if Remote Login is enabled on the laptop).
  Then trust that copy as above; `ps-cli` and the plugin's bridge read the same OS trust store.

Without both, their browser shows a certificate warning and passkey registration does not
work. The colleague registers their passkey from the invitation link the owner issues (see
[SystemOwner bootstrap](#systemowner-bootstrap)), and sets up `ps-cli` with the context
commands above.

### 9. Install the Policy System Plugin

The plugin ships in two named parts: the **Policy System Marketplace** (id `ps-marketplace`),
which is this repo, and the **Policy System Plugin** (id `ps-plugin`) it lists. Add the marketplace
first, then install the plugin from it.

1. In Claude Desktop: **Customize** → **Plugins** → **Add** → **Add marketplace** → **Add from a
   repository**, then add this repo:

   ```text
   URL:  `https://github.com/mindovermachine-dev/policy-system`
   ```

   Or from the Claude Code CLI: `claude plugin marketplace add mindovermachine-dev/policy-system`.

2. Install the plugin from that marketplace: in Claude Desktop, pick **Policy System Plugin** under
   **Customize** → **Plugins**; in Claude Code, run `/plugin install ps-plugin@ps-marketplace` (shell
   form: `claude plugin install ps-plugin@ps-marketplace`).

The marketplace manifest has no display-name field, so "Policy System Marketplace" is the name this
guide uses; the id you will see in lists and commands is `ps-marketplace`.

This installs the full set of Policy System skills (`ps-qna`, `ps-author-policy`,
`ps-check-regulations`, `ps-ingest-regulation`, `ps-assess-instrument-applicability`,
`ps-invite-user`, `ps-manage-access-roles`, `ps-list-audit-events`,
`ps-near-miss-review`, `ps-restore-instrument`, `ps-policy-lifecycle`, and
`ps-get-catalog-listing`) and its shared **Policy System MCP** connector (server id `ps-mcp`).
Claude is expected to list it as `plugin:ps-plugin:ps-mcp` and to name its tools
`mcp__plugin_ps-plugin_ps-mcp__<tool>` (expected; verify with `claude mcp list`). Unlike a
typical remote connector, this one runs **locally** — the plugin declares it as a
`stdio` server backed by `ps-cli-mcp-bridge` (installed alongside `ps-cli` in step 6), which reaches whichever PS Service instance `ps-cli`'s current
context points at ([Configuring which PS Service instance ps-cli
targets](./user-guide.md#configuring-which-ps-service-instance-ps-cli-targets)).
The bridge reuses the `ps-cli` login from step 8: it sends the stored access token as an
`Authorization` header and refreshes it against Authentik. It verifies Authentik's certificate
against the operating system trust store, like `ps-cli`, so it needs no environment variable
and works under Claude Desktop's own environment once the local CA is trusted by the OS.

Quit Claude Desktop fully (⌘Q) and relaunch after installing, then open a **new** chat
— tools bind when a conversation starts. `ps-mcp` exposes the tools
backing each of the skills above — including `domain_concepts` and `cypher` for direct
graph queries — plus the catalog-source, access-role, and audit-event tools used by the
[Role System](./user-guide.md#role-system). If nothing binds, check
`~/Library/Logs/Claude/mcp*.log` and, separately, the bridge's own log at
`~/.config/ps-cli/mcp-bridge.log` (`$PS_CLI_CONFIG_DIR/mcp-bridge.log` if that's set) —
written independently of whatever the host does with the bridge's stderr.

Several of these tools — ingesting/restoring/exporting curated content and running
change-checks — require the caller to hold the `ComplianceOfficer`
[access role](./user-guide.md#role-system) first. Nobody holds any role on a fresh instance
until its first role-gated call by the owner: see [SystemOwner bootstrap](#systemowner-bootstrap)
and [Manage access roles](./user-guide.md#manage-access-roles).

Once installed, see the [User Guide](./user-guide.md#using-claude-desktop) for how to
ask a question.

#### Reset an earlier account-scoped marketplace (manual)

Claude Desktop registers marketplaces against your Claude **account**, so a marketplace added
under an earlier name stays registered until you remove it. Earlier checkouts of this repo named
the marketplace `policy-system`. If Claude Desktop shows that marketplace, or Desktop's marketplace
refresh fails with `NOT_REGISTERED`, reset it by hand. These steps are manual UI and account steps
that no script performs; the on-screen wording is not verified here, so check each step on screen.

1. In Claude Desktop, open **Customize** → **Plugins** and find any marketplace other than
   **Policy System Marketplace** (`ps-marketplace`) whose source is
   `github: mindovermachine-dev/policy-system`. Remove it, and remove any plugin copy it installed
   (removing a marketplace is expected to uninstall its plugins; verify on screen).
2. Optionally, from the Claude Code CLI, run `claude plugin marketplace list`. For each marketplace
   other than `ps-marketplace` that points at this repo, run
   `claude plugin marketplace remove <name-shown-in-list>`.
3. Re-add this repo: **Customize** → **Plugins** → **Add** → **Add marketplace** → **Add from a
   repository**, then `https://github.com/mindovermachine-dev/policy-system`. Expect exactly one
   marketplace, **Policy System Marketplace** (`ps-marketplace`).
4. Install **Policy System Plugin** (`ps-plugin`). Expect its version to equal the `version` in
   `ps-skills/ps-plugin/.claude-plugin/plugin.json` on `main`, all 12 skills to be listed, and
   **Check for new version** to complete without an error.
5. Run `claude plugin marketplace list` and confirm `ps-marketplace` appears, and that Claude
   Desktop shows the same id. Refreshing the marketplace in Desktop must not fail with
   `NOT_REGISTERED`.

Acceptance checklist (tick each on screen):

- [ ] Exactly one marketplace is registered for this repo: **Policy System Marketplace**
      (`ps-marketplace`).
- [ ] **Policy System Plugin** installs at the expected version with all 12 skills, and
      **Check for new version** completes without an error.
- [ ] `claude plugin marketplace list` and Claude Desktop show the same marketplace id, and a
      refresh does not fail with `NOT_REGISTERED`.

### What is exposed (evaluator)

The evaluator profile is built for one trusted person on their own machine, and it
deliberately exposes Authentik to the local network. Exactly what is reachable, and by whom:

| Listener | Address | Reachable by | What it serves |
| --- | --- | --- | --- |
| Authentik over HTTPS | host port `30443` on **all interfaces** (`0.0.0.0`), mapped by `deploy/kind/cluster.yaml` to the Authentik server's HTTPS NodePort | Anyone who can reach this machine's port `30443` — on a shared LAN, everyone on it | **All of Authentik**: the login, enrolment and device-code pages, its whole API, and the **admin UI** (`https://<hostname>:30443/if/admin/`). Protected only by Authentik's own login (a passkey). This is deliberate: it is what lets a colleague on the LAN log in ([issue #160](https://github.com/mindovermachine-dev/policy-system/issues/160)). |
| Authentik over plain HTTP | NodePort `30080` inside the kind node only | Not mapped to the host: nothing outside the node, and not the LAN | The deploy script uses it through a loopback `kubectl port-forward` while the certificate is being set up. |
| PS Service | `127.0.0.1:8000` | This machine only | REST API and MCP endpoint; every call needs an Authentik-issued bearer token. |
| FalkorDB Browser | `127.0.0.1:3001` | This machine only | The graph explorer UI; it is not behind Authentik. |
| FalkorDB (Redis) | ClusterIP only | In-cluster pods only, never mapped | The graph database. |

**Reaching the Authentik admin UI:** it is on the same HTTPS listener as everything else —
open `https://<hostname>:30443/if/admin/` (default `https://authentik.local:30443/if/admin/`)
in a browser that trusts the CA, signed in as the owner. No port-forward is needed. To
restrict access to your own machine, do not let the LAN reach the laptop's port `30443`
(host firewall) and do not enrol colleagues.

Things to know:

- The local CA's private key (`ca.key`, in the state directory) can mint a certificate the
  browsers that trust `ca.pem` will accept for any name. It stays on the evaluator's machine
  with owner-only permissions; never copy it. Distribute `ca.pem` only.
- The chart's Authentik API token is the Authentik bootstrap token: it is **administrator-
  equivalent**, stored in the Secret `policy-system-authentik-api-token` (readable by anyone
  with `kubectl` access to the cluster) and never printed by the scripts.
- The evaluator profile is not for production: never expose it on the public internet.

---

## Production installation

> [!NOTE]
> **This path is for production/customer-tenant deployments** to a real Azure
> subscription, using [`scripts/deploy-ps-prod.sh`](../../scripts/deploy-ps-prod.sh). It is a
> separate, self-contained script from `deploy-llm.sh` (used by [Evaluator
> installation step 5](#5-provision-the-azure-llm-backend)) and from
> [`deploy-ps-eval.sh`](#7-deploy-policy-system-backend) — they are not the same
> code path and all keep working independently. `deploy-ps-prod.sh` provisions the LLM
> backend, an AKS cluster, the Helm release with the bundled Authentik identity
> provider, and public HTTPS exposure, all in one run, then creates the owner.

### Prerequisites (Production)

| Tool | Why | Verify |
| --- | --- | --- |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Everything `deploy-ps-prod.sh` provisions | `az version` |
| [jq](https://jqlang.org/download/) | Used by `deploy-ps-prod.sh` to parse Azure CLI JSON output | `jq --version` |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | Talks to the AKS cluster `deploy-ps-prod.sh` creates; also carries the `port-forward` the owner-creation step and admin access use | `kubectl version --client` |
| [Helm](https://helm.sh/docs/intro/install/) | Installs the Policy System chart | `helm version` |
| [curl](https://curl.se/) | Calls Authentik's API to create the owner | `curl --version` |
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
chat/embedding model names and SKUs, capacities, `TLS_CONTACT_EMAIL` (used for
Let's Encrypt expiry/revocation notices — leave blank to be prompted interactively) and
`AUTHZ_OWNER_EMAIL` (the owner's email: the first `SystemOwner` and Authentik administrator —
leave blank to be prompted, or pass `--owner-email`). The older
`AUTHZ_BOOTSTRAP_OWNER_SUBJECT`/`AUTHZ_BOOTSTRAP_OWNER_ISSUER` pair is no longer read: the
script derives the owner's identity from the email and the public hostname, so there is no
placeholder identity and no second deploy (see [SystemOwner bootstrap](#systemowner-bootstrap)).
The default SKUs are proven to have quota on a fresh subscription; if your
subscription/region differs, see the [Operations Guide](./operations-guide.md#manual-steps-and-operational-notes)
item 1 for how to discover the right values before your first run.

### 3. Run scripts/deploy-ps-prod.sh

```bash
scripts/deploy-ps-prod.sh
```

This prints a confirmation table — region candidates, resource group, AIServices
account, both model deployments, Key Vault, AKS cluster name, and public DNS label,
all deterministically derived from your subscription id — and prompts
`Proceed with these values? [Y/n]`. It also prompts for the **owner's email** when neither
`--owner-email <address>` nor `AUTHZ_OWNER_EMAIL` supplies it; an invalid address stops the run
before any Azure call. Pass `--yes` to skip the confirmation prompt (`--yes` never prompts for
the email, so combine it with `--owner-email` or `AUTHZ_OWNER_EMAIL`).

Each phase prints a `==> <step>` progress line as it starts. The whole run is
idempotent — re-running with nothing changed does no work and reports so. After the
Helm release it waits for Authentik, then creates the owner as an Authentik administrator
(username and email are the address you gave; **no password is set, prompted for or logged**),
and it ends with a summary naming which secrets were written (never their values), the
resulting URL, and a single-use passkey-enrolment link, printed last and once:

```
Policy System provisioned. Wrote secrets: AZURE-API-BASE, AZURE-API-KEY, AZURE-API-VERSION.
PS Service: https://<label>.<region>.cloudapp.azure.com
Open this single-use link in a browser to register your passkey (valid 30 minutes):
https://<label>.<region>.cloudapp.azure.com/auth/if/flow/ps-passkey-recovery/...
```

Because the Authentik admin API is not on the public Ingress (see [What is exposed
(production)](#what-is-exposed-production)), the script creates the owner through a temporary
loopback `kubectl port-forward` to the Authentik Service and prints the link on the public
address. It sets the bootstrap owner's `sub` (the email) and issuer in the same deploy that
creates the release. The Helm release is re-applied only when one of its script-managed values
changed (the LLM secret, the four `psService.auth.*` values, the owner subject and issuer, and
the two Authentik URLs).

### 4. Access the cluster with kubelogin

`deploy-ps-prod.sh` creates the AKS cluster with `--enable-aad --enable-azure-rbac
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
per-operator, per-machine prerequisite `deploy-ps-prod.sh` does not automate.

Once deployed, see the [Operations Guide](./operations-guide.md#production-operations)
for rotating the API key, manual operational notes, and teardown.

### 5. Enrol the owner's passkey

Open the link `deploy-ps-prod.sh` printed in a browser. There is no username or password
prompt: the page asks you to register a passkey, and registering it also logs you in. The
link works once and lapses after 30 minutes (`PS_OWNER_LINK_TTL` sets another duration in
minutes). Production uses a real Let's Encrypt certificate, so there is no CA to trust in the
browser or for `ps-cli`. After enrolment the browser ends on `https://<host>/auth/`, a path
Authentik's public allowlist does not serve, so PS Service answers it — an error or
"not found" page there is expected; you are enrolled and signed in.

**Re-running the script is the owner-recovery path**, gated by access to the cluster (the
run needs `kubectl` against it). With the same email: an owner with no passkey registered
(link expired or unused) gets a fresh link; an owner who already has one is left unchanged and
no link is issued. If the only device was lost, remove it from the owner's user page in the
Authentik admin UI (reached by port-forward, below) and re-run.

### 6. Set up each user's computer

The steps above provision the shared backend once. Every person who wants to query
this instance — via `ps-cli` directly or the Claude Desktop plugin — separately needs
the following on their own machine; none of it is done by `scripts/deploy-ps-prod.sh`.
A user who is not the owner first needs an invitation: the owner (or a `SystemAdmin`) runs
the `ps-invite-user` skill and delivers the invite link — see [User Guide: Invite a new
user](./user-guide.md#invite-a-new-user). The invitee registers a passkey from that link and
never sets a password (their account has no password set); see [Verification
status](#verification-status) for the release that makes the link carry the public address.

**Install Claude Desktop.** Download from [claude.com/download](https://claude.com/download)
(macOS or Windows) and sign in.

**Install `ps-cli`.** Requires [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
curl -fsSL https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/ps-cli/install.sh | bash
```

**Point `ps-cli` at this instance and log in.** See [User Guide: Point ps-cli at your
instance](./user-guide.md#point-ps-cli-at-your-instance) — use the URL [step
3](#3-run-scriptsdeploy-ps-prodsh) printed (`https://<label>.<region>.cloudapp.azure.com`).
A production instance is deployed with the bundled Authentik identity provider wired in, so
logging in is required, exactly as on the evaluator instance.

**Install the Policy System Plugin.** Same as [Evaluator installation, step
9](#9-install-the-policy-system-plugin): add the Policy System Marketplace (`ps-marketplace`) in
Claude Desktop (**Customize** → **Plugins** → **Add** → **Add marketplace** → **Add from a
repository**, then this repo, `https://github.com/mindovermachine-dev/policy-system`), then install
the Policy System Plugin (`ps-plugin`) with `/plugin install ps-plugin@ps-marketplace`. The plugin's
Policy System MCP connector (`ps-mcp`) runs a local
`ps-cli-mcp-bridge` that reuses whichever `ps-cli` context is current, so once it's set to
`prod` above, the plugin talks to this instance with no separate configuration — and
sends the stored `prod` credential as an `Authorization` header, same as any other
authenticated `ps-cli` call.

### What is exposed (production)

`deploy-ps-prod.sh` publishes PS Service and a **restricted** part of Authentik on one public
hostname, `https://<label>.<region>.cloudapp.azure.com`, served with a Let's Encrypt
certificate. Authentik's own Ingress routes only the eight path prefixes an end user's login
and enrolment need:

| Public path | Serves |
| --- | --- |
| `/auth/application/o/` | OIDC discovery, authorize, token, device and JWKS endpoints |
| `/auth/device` | The device-code page `ps-cli auth login` sends you to |
| `/auth/flows/-/default/` | Authentik's default flow redirects |
| `/auth/if/flow/` | The login, enrolment and recovery flow pages |
| `/auth/api/v3/flows/executor/` | The flow executor those pages call |
| `/auth/api/v3/root/config/` | Flow-page configuration |
| `/auth/api/v3/core/brands/current/` | Flow-page branding |
| `/auth/static/` | Flow-page JavaScript, CSS and images |

| Surface | Public? | Notes |
| --- | --- | --- |
| The eight paths above | Yes, to anyone on the internet | Login, enrolment and recovery run here; each flow is gated by Authentik itself (an enrolment or recovery link, a passkey). |
| PS Service (`/` and everything else on the host) | Yes | Every call needs an Authentik-issued bearer token; `/health` and `/ready` are unauthenticated. |
| Authentik admin UI (`/auth/if/admin/`), admin and core APIs, the invitation API, `/auth/if/user/` | **No** | Not routed: the request is answered by PS Service, never Authentik. Verified on ingress-nginx, not on AKS (see [Verification status](#verification-status)). |
| The owner enrolment link, and the owner-creation API calls | Link: public path. API: **no** | The link opens on the public `/auth/if/flow/` path; the script creates the owner and the link through a cluster-internal `kubectl port-forward`. |
| PS Service to Authentik (`invite_user`) | No | PS Service calls Authentik's API on its in-cluster address (`http://policy-system-authentik-server/auth`) and only builds the invitee's link on the public address. |
| FalkorDB, Authentik's Postgres, PS Postgres | No | ClusterIP only, behind NetworkPolicies. |

**Reaching the Authentik admin UI:** through a port-forward from a machine with cluster access
(see [step 4](#4-access-the-cluster-with-kubelogin)). Leave this running while you use it:

```bash
kubectl port-forward svc/policy-system-authentik-server 9000:80
```

Then open `http://localhost:9000/auth/if/admin/` and sign in as the owner. Note the `/auth`
prefix in production. The port-forward stays on your machine (loopback); nothing about it is
public.

Things to know:

- The Authentik API token is the Authentik bootstrap token. It is **administrator-equivalent**,
  chart-generated once, stored in the Secret `policy-system-authentik-api-token`, readable by
  anyone with `kubectl` access to the cluster, never printed by the scripts, and — because
  Authentik applies a bootstrap token once per tenant — it does **not** rotate.
- The bundled `akadmin` account and the owner have no usable password (verified live), and
  an invitee has **no password set** (an empty hash) — all of them register passkeys instead.
  The shipped login flow still contains a password stage, and an administrator can set a user's
  password in the admin UI, so "passkey-only" is enforced for enrolment and recovery, not by
  removing the password stage. Live checks showed a submitted password being rejected for both
  the owner and an invitee.
- Production TLS termination (Let's Encrypt, cert-manager) is unchanged by this flow.

---

For `ps-cli` configuration reference (context/credential storage, env vars, config
files), see [User Guide: Appendix — ps-cli reference](./user-guide.md#appendix-ps-cli-reference).
For PS Service / chart-level configuration, see the [Helm Chart Values
Reference](./helm-chart-values-reference.md).

---

## SystemOwner bootstrap

Both deploy scripts set the bootstrap owner in **one deploy**: the owner's email you give the
script is created as the Authentik username and becomes the OIDC `sub` in the ID token
(the bundled `ps-cli` provider uses `sub_mode: user_username`), and the script sets
`psService.authzBootstrapOwner.subject` to that email and `.issuer` to the Authentik issuer in
the same Helm release that deploys everything else. Nobody needs to log in first to discover
their own identity, and there is no placeholder identity to replace afterwards.

The first authenticated caller whose `(sub, iss)` matches those two values is granted
`SystemOwner`, exactly once. Anyone else who reaches an empty instance first gets nothing (the
attempt is audited as `access_role.bootstrap_rejected`). This applies to both the
[Evaluator](#7-deploy-policy-system-backend) and [Production](#3-run-scriptsdeploy-ps-prodsh)
installs — only the script and the Authentik base URL differ.

1. **Enrol a passkey** from the link the script printed ([evaluator](#8-register-your-passkey-and-log-in-with-ps-cli),
   [production](#5-enrol-the-owners-passkey)).
2. **Log in with `ps-cli`** (`ps-cli auth login`) — or let the Claude Desktop plugin, which reuses
   that login, make the call.
3. **Make the first role-gated call** as the owner — for example listing access roles through the
   `ps-manage-access-roles` skill. That call wins the bootstrap and makes you `SystemOwner`. See
   the [User Guide's Role System](./user-guide.md#role-system) for what to do with it.

`SystemOwner` counts as `SystemAdmin` or above, so the owner can invite users directly with the
`ps-invite-user` skill without being granted `SystemAdmin` first. The owner is both the
Authentik administrator and the PS `SystemOwner`; that is acceptable for a single-owner
deployment, and splitting the two later is a role change.

To confirm the owner's identity matches what PS Service expects, read your `sub` and `iss` back
from an access token. `ps-cli` never prints or stores an access token, so request one with
Authentik's device flow. `<issuer>` is the value of `psService.auth.issuer` — evaluator:
`https://authentik.local:30443/application/o/ps-cli/` (your `--hostname`); production:
`https://<label>.<region>.cloudapp.azure.com/auth/application/o/ps-cli/`. The evaluator needs
`--cacert` pointing at the local CA on each `curl`:

```bash
ISSUER=<issuer>
DEVICE_ENDPOINT=$(curl -s "${ISSUER}.well-known/openid-configuration" | jq -r .device_authorization_endpoint)
TOKEN_ENDPOINT=$(curl -s "${ISSUER}.well-known/openid-configuration" | jq -r .token_endpoint)
curl -s -d client_id=ps-cli -d "scope=openid profile email" "$DEVICE_ENDPOINT"
```

Open the returned `verification_uri_complete` in a browser and sign in with your passkey, then
exchange the returned `device_code` (it expires after a few minutes; rerun the previous command
if it does):

```bash
curl -s -d client_id=ps-cli \
  -d grant_type=urn:ietf:params:oauth:grant-type:device_code \
  -d device_code=<device_code> "$TOKEN_ENDPOINT" | jq -r .access_token
```

Decode the access token's payload (the middle, dot-separated part) — no signature check is
needed here, you're only reading your own claims. `sub` must equal the owner email, character
for character:

```bash
echo '<access_token>' | jq -R 'split(".")[1] | gsub("-";"+") | gsub("_";"/") | . + ("="*((4 - length % 4) % 4)) | @base64d | fromjson | {sub, iss}'
```

## Upgrading an existing install

An install made before this flow existed used a fixed placeholder as the API token PS Service
sends to Authentik (`ps-service-authentik-dev-token`), which no Authentik token matched. That
value and the `psService.authentik.apiToken` key are gone: the chart now generates one random
64-character token in the Secret `policy-system-authentik-api-token` and hands the same value to
Authentik as `AUTHENTIK_BOOTSTRAP_TOKEN` and to PS Service as `PS_AUTHENTIK_API_TOKEN`. A
values file that still sets `psService.authentik.apiToken` no longer has any effect. An
existing Secret still holding the old placeholder is replaced by a generated token on upgrade.

On an Authentik that had no bootstrap token yet, the first start after the upgrade creates the
token from the new value, so re-running the deploy script is enough. Authentik applies the
bootstrap token **once per tenant and never rotates it**: if the Secret and Authentik disagree
(for instance the Secret was edited after Authentik had already created its token), the deploy
script stops before creating anything, with an error that Authentik rejected the shared API
token (HTTP 401 or 403) — the token is never printed. To fix it:

1. Create an API token in the Authentik admin UI (evaluator: `kubectl port-forward
   svc/policy-system-authentik-server 9000:80`, then `http://127.0.0.1:9000/if/admin/`;
   production: the same port-forward, then `http://127.0.0.1:9000/auth/if/admin/`).
2. Store it in a Secret with key `PS_AUTHENTIK_API_TOKEN` and set
   `psService.authentik.existingSecret` to that Secret's name (see the [Helm Chart Values
   Reference](./helm-chart-values-reference.md#authentik-and-local-tls-values)).
3. Re-run the deploy script.

On an evaluator cluster with no data worth keeping, deleting the cluster and starting again is
the simpler route. A `kind` cluster created for the old plain-HTTP port mapping must be
recreated regardless — see the warning in [step 4](#4-create-the-local-cluster).

## Verification status

What the flows above were verified against, and what they were not. Read this before relying
on a claim for a real deployment.

- **Evaluator path: verified live** on a clean `kind` cluster (Podman) with the real script —
  one deploy, owner passkey enrolment in a browser, `ps-cli auth login` and the MCP bridge with
  `SSL_CERT_FILE`, `SystemOwner` granted on the first role-gated call, a second user invited and
  enrolled, owner re-run recovery, certificate refresh without a manual restart.
- **A second machine on the LAN was simulated, not tried on physical hardware.** A separate
  container with the CA in its trust store reached Authentik over HTTPS through the host's
  `0.0.0.0:30443` mapping, and a second browser profile completed a passkey device login with the
  hostname mapped to the LAN address. Browser trust in that run used a test flag rather than an
  imported CA (macOS keychain untouched).
- **Production path: verified on `kind`, not on AKS.** The real `deploy-ps-prod.sh` and
  `values-prod.yaml` ran against `kind` with `ingress-nginx`, a stand-in Azure CLI and the
  cert-manager/Let's Encrypt steps skipped. That proved the Ingress routes exactly the eight
  paths above and blocks the admin UI and admin APIs, that the owner link printed on the public
  address enrols a passkey through the Ingress with no blocked flow request, and that a re-run
  leaves the owner untouched. **Not verified:** AKS's managed NGINX add-on, real Let's Encrypt
  issuance through this flow, and real Azure resources.
- **The invitation link on production needs an unreleased PS Service image.** `invite_user`
  calls Authentik in-cluster and must build the invitee's link on the public address. That is
  the `PS_AUTHENTIK_PUBLIC_URL` support added by issue #165, which is in the repository but in
  no published release yet — the latest release, 3.11.0, does not contain it. Until a release
  that includes it is published and deployed, production `invite_user` returns a link that
  begins with the in-cluster address `http://policy-system-authentik-server/auth`; replace that
  beginning with `https://<label>.<region>.cloudapp.azure.com/auth` by hand before giving the
  link to the invitee. The evaluator is not affected (its Authentik address is already
  reachable by users). The same release applies to the published Helm chart: until one that
  contains this flow is published, run the scripts with `PS_CHART_REF=./charts/policy-system`
  from a checkout (`deploy-ps-eval.sh`; `deploy-ps-prod.sh` honours it too). `invite_user`
  through PS Service on production was **not** exercised end to end.
