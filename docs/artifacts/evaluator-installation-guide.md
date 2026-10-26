# Policy System Evaluator Installation Guide

## Table of Contents

- [Part 0: Start here](#part-0-start-here)
- [Part 1: Install the backend](#part-1-install-the-backend)
- [Part 2: Install the client](#part-2-install-the-client)
- [Part 3: Reference](#part-3-reference)

## Part 0: Start here

This guide installs Policy System so you can **evaluate** it. It is not a production install.

An install has two parts:

- The **backend** runs Policy System on a local `kind` cluster, with passkey login through the
  bundled Authentik. You install it once, on one machine, the backend machine (Part 1).
- A **client** is any machine that uses it. It needs a hosts line, trust for the local
  certificate authority (CA), the Claude Code CLI and the Policy System plugin. You install it on
  every machine that uses Policy System (Part 2).

One laptop can run both. The table below shows the supported setups.

### Supported scenarios

| Scenario | Setup | Backend | Client | Status |
| --- | --- | --- | --- | --- |
| 1 | One user, one laptop | [Backend on macOS](#backend-on-macos) | [Client on macOS](#client-on-macos) | Verified |
| 2 | One user, one laptop | [Backend on WSL2](#backend-on-wsl2) | [Client on Windows](#client-on-windows) | Verified |
| 3 | Several machines on a LAN | [Backend on WSL2](#backend-on-wsl2) | [Client on Windows](#client-on-windows) | Failing: a Windows client cannot connect to the backend, cause unknown |
| 4 | Several machines on a LAN | [Backend on Linux](#backend-on-linux-headless) | [Client on Windows](#client-on-windows) | Verified |
| 5 | Several machines on a LAN | [Backend on Linux](#backend-on-linux-headless) | [Client on macOS](#client-on-macos) | Verified |
| 6 | Several machines on a LAN | [Backend on macOS](#backend-on-macos) | [Client on macOS](#client-on-macos) | Unverified |

In the LAN scenarios (3 to 6), the backend path also has a LAN step. Do it.

## Part 1: Install the backend

Do this once, on the backend machine. Pick your OS and follow that section from top to
bottom:

- [Backend on macOS](#backend-on-macos)
- [Backend on Linux (headless)](#backend-on-linux-headless)
- [Backend on WSL2](#backend-on-wsl2) (a Windows machine)

### Backend on macOS

Scenarios 1 and 6.

#### Step 1: Install the tools and download the bundle

The backend needs these tools:

| Tool | Why |
| --- | --- |
| [Podman](https://podman.io/docs/installation) (or Docker) | Container engine for the cluster |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | Runs a Kubernetes cluster in containers |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | Talks to the cluster |
| [Helm](https://helm.sh/docs/intro/install/) | Installs the Policy System chart |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Provisions the Azure LLM backend (step 4) |
| [jq](https://jqlang.org/download/) | Used by the Azure scripts and the deploy script |
| [openssl](https://www.openssl.org/) | The deploy script issues the local CA and certificate with it |
| [curl](https://curl.se/) | The deploy script calls the Authentik API with it |

Install Podman:

```bash
brew install podman
```

Create the Podman machine. 4 CPUs and 8 GB is the tested minimum:

```bash
podman machine init --cpus 4 --memory 8192
```

Start it:

```bash
podman machine start
```

Check that it runs:

```bash
podman info
```

> **Why 4 CPUs and 8 GB:** kind under Podman needs enough headroom to run a control plane and
> both Policy System containers. 4 CPUs and 8 GB is the tested minimum.
>
> **Footprint: measured.** The evaluator runs one ingress controller (ingress-nginx controller
> v1.15.1) in front of PS Service and Authentik. It was measured on its own on 2026-10-24, on a
> Podman machine with 4 CPUs and 8 GiB (macOS, arm64), with a stub backend behind it and without
> the rest of the Policy System stack. It used 60 MiB of memory and 4m CPU idle (120 seconds), and
> 77 MiB and 760m CPU while 8 clients requested the liveness probe over HTTPS for 60 seconds. The full stack
> was not run with it, so the minimum above is not re-validated for the full stack; the controller
> is heavier than the reverse proxy it replaced, and the controller's declared request is 100m CPU
> and 90Mi memory with no limits. The memory and CPU figures are the controller's cgroup readings.
> To measure the footprint on your machine, follow [Operations Guide: Measuring the ingress
> controller footprint](./operations-guide.md#measuring-the-ingress-controller-footprint-evaluator).

Install kind and kubectl:

```bash
brew install kind kubectl
```

Install Helm, the Azure CLI and jq from the links in the table.

Download the deploy bundle. It holds the kind cluster config and the deploy scripts. You do not
need git. Go to the folder where you want the bundle, then download and unpack it:

```bash
curl -fsSL https://github.com/mindovermachine-dev/policy-system/releases/latest/download/ps-deploy-bundle.tgz | tar xz
```

Go to the bundle folder. Run all the remaining steps of this path from it:

```bash
cd policy-system-deploy
```

#### Step 2: Check that the machine is safe to expose

The cluster publishes the ingress controller on every network interface of the machine (port `30443`). Check
the machine first. The check lists the machine's addresses and fails when one is outside the
loopback, link-local and private ranges:

```bash
scripts/deploy-ps-eval.sh --check-exposure
```

It prints `Exposure check passed` and exits `0` when every address is private. If it fails,
it names each public address. Disconnect or firewall that interface and run the check again. If
you accept the exposure, pass `--allow-public-host` here and on the deploy script in step 5. A VPN
address, such as Tailscale's `100.64.0.0/10`, and a global IPv6 address count as public.

#### Step 3: Create the cluster

Tell kind to use Podman:

```bash
export KIND_EXPERIMENTAL_PROVIDER=podman
```

Create the cluster:

```bash
kind create cluster --config deploy/kind/cluster.yaml --name policy-system
```

Install the ingress controller (ingress-nginx), the only component that listens on port `30443`,
and wait until it is ready:

```bash
kubectl apply -f deploy/kind/ingress-nginx.yaml
kubectl wait --namespace ingress-nginx --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller --timeout=180s
```

Check that it runs:

```bash
kubectl cluster-info --context kind-policy-system
```

> [!WARNING]
> If a `policy-system` kind cluster already exists from an older install, delete it and create
> it again with `kind delete cluster --name policy-system`. **This destroys the graph and both
> Postgres databases stored in that cluster.**
>
> After you delete it, run the exposure check of step 2 and the `kind create cluster` and
> `kubectl apply` commands of this step again, then the steps after it. Restore a PS Postgres backup (see [Operations Guide:
> Restore](./operations-guide.md#restore)) or load the content again (see [User Guide: Load curated
> content](./user-guide.md#load-curated-content)).

#### Step 4: Provision the Azure LLM backend

This creates an Azure Cognitive Services account, two model deployments and a Key Vault in your
Azure subscription. Your signed-in identity needs the `Owner` or `Contributor` role at
subscription scope. Both scripts are safe to run again.

Sign in to Azure:

```bash
az login
```

Run the provisioning script. It prints the values it will use and asks `Proceed with these
values? [Y/n]`. The values are in `scripts/llm-defaults.conf`:

```bash
scripts/deploy-llm.sh
```

Sync the credentials into the cluster:

```bash
scripts/sync-llm-secrets-to-kind.sh
```

#### Step 5: Run the deploy script

Before you run it, check that your kubectl context is the kind cluster (the script refuses any
other context) and that step 4 created the LLM secret. Then run:

```bash
scripts/deploy-ps-eval.sh --owner-email <owner address>
```

The owner becomes the first `SystemOwner` and the Authentik administrator. Leave the email out
and the script asks for it. Add these flags when they apply:

| Flag | When |
| --- | --- |
| `--allow-public-host` | Step 2 needed it. |
| `--lan-address <ip>` | The script cannot tell which address of this machine clients reach, or picks the wrong one. |
| `--apply-host-setup` | Use it when this machine is also a client. The script adds the hosts line and trusts the CA, both with `sudo`, without asking. Without it, an interactive run asks first and a non-interactive run only prints the commands. |

When the script finishes, **keep three things from its output**:

- The **hosts line** (`<address> ps.test`).
- The **path of `ca.pem`**, by default `~/.config/policy-system/eval-tls/ca.pem`.
- The **owner enrolment link**, printed last.

The output also mentions `ps-cli`. You do not need it. The enrolment link is single-use and
expires after 30 minutes by default. It is a credential: do not share it. If it expires, run the
script again with the same email for a new link.

Once the client steps for this machine are done, check that PS Service is healthy:

```bash
curl --cacert ~/.config/policy-system/eval-tls/ca.pem https://ps.test:30443/health
```

A new system has an empty graph. To load content, see [User Guide: Load curated
content](./user-guide.md#load-curated-content).

#### Step 6: Make the backend reachable from the LAN (only if other machines will use it)

Skip this step when this machine is the only client.

Other machines reach the backend on `https://ps.test:30443`. For that to work:

- The hosts line on each client maps `ps.test` to an address of this machine that the client can
  reach. Use the address from the script's hosts line. If it is wrong, run the deploy script
  again with `--lan-address <ip>`.
- Port `30443` accepts connections from the LAN. Check the firewall of this machine.
- Reserve this machine's address in your router's DHCP settings. A new address breaks every
  client until its hosts line is updated.

When you are done, install the client on every machine that uses Policy System, this one
included: [Part 2](#part-2-install-the-client).

### Backend on Linux (headless)

Scenarios 4 and 5.

> [!NOTE]
> **Verified in scenarios 4 and 5.**

#### Step 1: Install the tools and download the bundle

The backend needs these tools:

| Tool | Why |
| --- | --- |
| [Podman](https://podman.io/docs/installation) (or Docker) | Container engine for the cluster |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | Runs a Kubernetes cluster in containers |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | Talks to the cluster |
| [Helm](https://helm.sh/docs/intro/install/) | Installs the Policy System chart |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Provisions the Azure LLM backend (step 4) |
| [jq](https://jqlang.org/download/) | Used by the Azure scripts and the deploy script |
| [openssl](https://www.openssl.org/) | The deploy script issues the local CA and certificate with it |
| [curl](https://curl.se/) | The deploy script calls the Authentik API with it |

Install Podman (or Docker) with your distribution's package manager. Podman on Linux runs
natively, so there is no `podman machine` step. Install kind, kubectl, Helm, the Azure CLI and jq
from the links in the table. `openssl` and `curl` are usually installed already.

kind has extra requirements for rootless Podman. Read [kind: rootless](https://kind.sigs.k8s.io/docs/user/rootless/)
before you create the cluster.

Download the deploy bundle. It holds the kind cluster config and the deploy scripts. You do not
need git. Go to the folder where you want the bundle, then download and unpack it:

```bash
curl -fsSL https://github.com/mindovermachine-dev/policy-system/releases/latest/download/ps-deploy-bundle.tgz | tar xz
```

Go to the bundle folder. Run all the remaining steps of this path from it:

```bash
cd policy-system-deploy
```

#### Step 2: Check that the machine is safe to expose

The cluster publishes the ingress controller on every network interface of the machine (port `30443`). Check
the machine first. The check lists the machine's addresses and fails when one is outside the
loopback, link-local and private ranges:

```bash
scripts/deploy-ps-eval.sh --check-exposure
```

It prints `Exposure check passed` and exits `0` when every address is private. If it fails,
it names each public address. Disconnect or firewall that interface and run the check again. If
you accept the exposure, pass `--allow-public-host` here and on the deploy script in step 5. A VPN
address, such as Tailscale's `100.64.0.0/10`, and a global IPv6 address count as public.

#### Step 3: Create the cluster

Tell kind to use Podman:

```bash
export KIND_EXPERIMENTAL_PROVIDER=podman
```

Create the cluster:

```bash
kind create cluster --config deploy/kind/cluster.yaml --name policy-system
```

Install the ingress controller (ingress-nginx), the only component that listens on port `30443`,
and wait until it is ready:

```bash
kubectl apply -f deploy/kind/ingress-nginx.yaml
kubectl wait --namespace ingress-nginx --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller --timeout=180s
```

Check that it runs:

```bash
kubectl cluster-info --context kind-policy-system
```

> [!WARNING]
> If a `policy-system` kind cluster already exists from an older install, delete it and create
> it again with `kind delete cluster --name policy-system`. **This destroys the graph and both
> Postgres databases stored in that cluster.**
>
> After you delete it, run the exposure check of step 2 and the `kind create cluster` and
> `kubectl apply` commands of this step again, then the steps after it. Restore a PS Postgres backup (see [Operations Guide:
> Restore](./operations-guide.md#restore)) or load the content again (see [User Guide: Load curated
> content](./user-guide.md#load-curated-content)).

#### Step 4: Provision the Azure LLM backend

This creates an Azure Cognitive Services account, two model deployments and a Key Vault in your
Azure subscription. Your signed-in identity needs the `Owner` or `Contributor` role at
subscription scope. Both scripts are safe to run again.

Sign in to Azure:

```bash
az login
```

Run the provisioning script. It prints the values it will use and asks `Proceed with these
values? [Y/n]`. The values are in `scripts/llm-defaults.conf`:

```bash
scripts/deploy-llm.sh
```

Sync the credentials into the cluster:

```bash
scripts/sync-llm-secrets-to-kind.sh
```

#### Step 5: Run the deploy script

Before you run it, check that your kubectl context is the kind cluster (the script refuses any
other context) and that step 4 created the LLM secret. Then run:

```bash
scripts/deploy-ps-eval.sh --owner-email <owner address>
```

The owner becomes the first `SystemOwner` and the Authentik administrator. Leave the email out
and the script asks for it. Add these flags when they apply:

| Flag | When |
| --- | --- |
| `--allow-public-host` | Step 2 needed it. |
| `--lan-address <ip>` | The script cannot tell which address of this machine clients reach, or picks the wrong one. |
| `--apply-host-setup` | Use it only when this machine is also a client. The script adds the hosts line with `sudo`, without asking. It never trusts the CA on Linux: it prints the commands. |

When the script finishes, **keep three things from its output**:

- The **hosts line** (`<address> ps.test`).
- The **path of `ca.pem`**, by default `~/.config/policy-system/eval-tls/ca.pem`.
- The **owner enrolment link**, printed last.

The output also mentions `ps-cli`. You do not need it. The enrolment link is single-use and
expires after 30 minutes by default. It is a credential: do not share it. If it expires, run the
script again with the same email for a new link.

Once a client is set up (Part 2), open `https://ps.test:30443/health` in a browser on that client.
It loads without a certificate warning.

A new system has an empty graph. To load content, see [User Guide: Load curated
content](./user-guide.md#load-curated-content).

#### Step 6: Make the backend reachable from the LAN

Other machines reach the backend on `https://ps.test:30443`. For that to work:

- The hosts line on each client maps `ps.test` to an address of this machine that the client can
  reach. Use the address from the script's hosts line. If it is wrong, run the deploy script
  again with `--lan-address <ip>`.
- Port `30443` accepts connections from the LAN. Check the firewall of this machine.
- Reserve this machine's address in your router's DHCP settings. A new address breaks every
  client until its hosts line is updated.

You can check from another computer on the LAN that the port is reachable. That computer has
not trusted the CA yet and has no hosts line, so use the backend's address and skip the
certificate check:

```bash
curl -k https://<backend address>:30443
```

Expect a message that says "unauthenticated".

When you are done, continue on to: [Part 2](#part-2-install-the-client).

### Backend on WSL2

Scenarios 2 and 3. Run every command of this path inside a Linux
distribution on WSL2. The browser and the Claude Code CLI run on the Windows side: install the
client on Windows ([Client on Windows](#client-on-windows)) after this path.

#### Step 1: Install the tools and download the bundle

The backend needs these tools:

| Tool | Why |
| --- | --- |
| [Podman](https://podman.io/docs/installation) (or Docker) | Container engine for the cluster |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | Runs a Kubernetes cluster in containers |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | Talks to the cluster |
| [Helm](https://helm.sh/docs/intro/install/) | Installs the Policy System chart |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Provisions the Azure LLM backend (step 4) |
| [jq](https://jqlang.org/download/) | Used by the Azure scripts and the deploy script |
| [openssl](https://www.openssl.org/) | The deploy script issues the local CA and certificate with it |
| [curl](https://curl.se/) | The deploy script calls the Authentik API with it |

Install Podman (or Docker) inside the WSL2 distribution with its package manager. Podman on Linux
runs natively, so there is no `podman machine` step. Install kind, kubectl, Helm, the Azure CLI
and jq from the links in the table. `openssl` and `curl` are usually installed already.

kind has extra requirements for rootless Podman. Read [kind: rootless](https://kind.sigs.k8s.io/docs/user/rootless/)
before you create the cluster.

Download the deploy bundle. It holds the kind cluster config and the deploy scripts. You do not
need git. Go to the folder where you want the bundle, then download and unpack it:

```bash
curl -fsSL https://github.com/mindovermachine-dev/policy-system/releases/latest/download/ps-deploy-bundle.tgz | tar xz
```

Go to the bundle folder. Run all the remaining steps of this path from it:

```bash
cd policy-system-deploy
```

#### Step 2: Check that the machine is safe to expose

The cluster publishes the ingress controller on every network interface of the machine (port `30443`). Check
the machine first. The check lists the machine's addresses and fails when one is outside the
loopback, link-local and private ranges:

```bash
scripts/deploy-ps-eval.sh --check-exposure
```

It prints `Exposure check passed` and exits `0` when every address is private. If it fails,
it names each public address. Disconnect or firewall that interface and run the check again. If
you accept the exposure, pass `--allow-public-host` here and on the deploy script in step 5. A VPN
address, such as Tailscale's `100.64.0.0/10`, and a global IPv6 address count as public.

#### Step 3: Create the cluster

Tell kind to use Podman:

```bash
export KIND_EXPERIMENTAL_PROVIDER=podman
```

Create the cluster:

```bash
kind create cluster --config deploy/kind/cluster.yaml --name policy-system
```

Install the ingress controller (ingress-nginx), the only component that listens on port `30443`,
and wait until it is ready:

```bash
kubectl apply -f deploy/kind/ingress-nginx.yaml
kubectl wait --namespace ingress-nginx --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller --timeout=180s
```

Check that it runs:

```bash
kubectl cluster-info --context kind-policy-system
```

> [!WARNING]
> If a `policy-system` kind cluster already exists from an older install, delete it and create
> it again with `kind delete cluster --name policy-system`. **This destroys the graph and both
> Postgres databases stored in that cluster.**
>
> After you delete it, run the exposure check of step 2 and the `kind create cluster` and
> `kubectl apply` commands of this step again, then the steps after it. Restore a PS Postgres backup (see [Operations Guide:
> Restore](./operations-guide.md#restore)) or load the content again (see [User Guide: Load curated
> content](./user-guide.md#load-curated-content)).

#### Step 4: Provision the Azure LLM backend

This creates an Azure Cognitive Services account, two model deployments and a Key Vault in your
Azure subscription. Your signed-in identity needs the `Owner` or `Contributor` role at
subscription scope. Both scripts are safe to run again.

Sign in to Azure:

```bash
az login
```

Run the provisioning script. It prints the values it will use and asks `Proceed with these
values? [Y/n]`. The values are in `scripts/llm-defaults.conf`:

```bash
scripts/deploy-llm.sh
```

Sync the credentials into the cluster:

```bash
scripts/sync-llm-secrets-to-kind.sh
```

#### Step 5: Run the deploy script

Before you run it, check that your kubectl context is the kind cluster (the script refuses any
other context) and that step 4 created the LLM secret. Then run:

```bash
scripts/deploy-ps-eval.sh --owner-email <owner address>
```

The owner becomes the first `SystemOwner` and the Authentik administrator. Leave the email out
and the script asks for it. Add these flags when they apply:

| Flag | When |
| --- | --- |
| `--allow-public-host` | Step 2 needed it. |
| `--lan-address <ip>` | The script cannot tell which address of this machine clients reach, or picks the wrong one. |
| `--apply-host-setup` | Do not use it. The script edits the hosts file inside the Linux distribution, not the Windows hosts file. Do the Windows steps of Part 2 by hand. |

When the script finishes, **keep three things from its output**:

- The **hosts line** (`<address> ps.test`).
- The **path of `ca.pem`**, by default `~/.config/policy-system/eval-tls/ca.pem`.
- The **owner enrolment link**, printed last.

The output also mentions `ps-cli`. You do not need it. The enrolment link is single-use and
expires after 30 minutes by default. It is a credential: do not share it. If it expires, run the
script again with the same email for a new link.

Once the client steps on Windows are done, open `https://ps.test:30443/health` in a browser on
Windows. It loads without a certificate warning.

A new system has an empty graph. To load content, see [User Guide: Load curated
content](./user-guide.md#load-curated-content).

#### Step 6: Make the backend reachable from the LAN (only if other machines will use it)

> [!WARNING]
> **Unverified.** Scenario 3 currently fails here or in the client steps, and the cause is not
> known.

Skip this step when this machine is the only client.

Other machines reach the backend on `https://ps.test:30443`. For that to work:

- The hosts line on each client maps `ps.test` to an address of this machine that the client can
  reach. Use the address from the script's hosts line. If it is wrong, run the deploy script
  again with `--lan-address <ip>`.
- Port `30443` accepts connections from the LAN. Check the firewall of this machine.
- Reserve this machine's address in your router's DHCP settings. A new address breaks every
  client until its hosts line is updated.

When you are done, install the client on every machine that uses Policy System, this one
included: [Part 2](#part-2-install-the-client).

## Part 2: Install the client

Do this on every machine that uses Policy System, the backend machine included. Pick your OS and
follow that section from top to bottom:

- [Client on macOS](#client-on-macos)
- [Client on Windows](#client-on-windows)

Do Part 1 first. Then, on every client, add the hosts line and trust the CA. The owner then opens
the enrolment link within 30 minutes. Only after that does anyone authenticate the plugin,
because that login needs a registered passkey. Get these from whoever ran Part 1: the hosts
line, `ca.pem` and, for the owner, the enrolment link.

### Client on macOS

Scenarios 1, 5 and 6.

#### Step 1: Add the hosts line

If this machine is also the backend and you ran the deploy script with `--apply-host-setup`, the
line is already there. Skip this step.

Add the line the deploy script printed, with the backend's address:

```bash
echo '<backend address> ps.test' | sudo tee -a /etc/hosts
```

#### Step 2: Trust the CA

If this machine is also the backend and you ran the deploy script with `--apply-host-setup`, the
CA is already trusted. Skip this step.

> [!WARNING]
> Copy `ca.pem` only. **Never copy `ca.key`** from the same folder. The CA can only vouch for the
> evaluator hostname and only for TLS server authentication, and the key stays on the backend
> machine.

Copy `ca.pem` from the backend machine to this machine. If this machine is also the backend, skip
the copy and use `~/.config/policy-system/eval-tls/ca.pem` in the next command instead of
`~/ca.pem`:

```bash
scp <user>@<backend address>:~/.config/policy-system/eval-tls/ca.pem ~/ca.pem
```

Trust it for TLS server authentication:

```bash
sudo security add-trusted-cert -d -r trustRoot -p ssl -k /Library/Keychains/System.keychain ~/ca.pem
```

#### Step 3: Register the passkey (owner only)

Open the enrolment link in a browser on this machine and register a passkey. A security key, the
platform authenticator or a phone all work. There is no password. Registration also logs you in
and ends on a PS Service page at `https://<host>/enrolled` that says the passkey is registered and
you are signed in, and names `ps-cli auth login`. Close the tab. Everyone else
registers from an invitation the owner sends. If you are not the owner, skip this step.

#### Step 4: Install the Claude Code CLI

This needs [Homebrew](https://brew.sh).

```bash
brew install --cask claude-code
```

Check it with:

```bash
claude --version
```

#### Step 5: Install the plugin and authenticate it

The plugin adds the Policy System skills and connects to the backend's MCP endpoint.

1. Add the marketplace and install the plugin from the Claude Code CLI:

   ```text
   claude plugin marketplace add mindovermachine-dev/policy-system
   ```

   ```text
   claude plugin install ps-plugin@ps-marketplace
   ```

2. Set the service URL. Add `PS_MCP_URL` to the `env` block of `~/.claude/settings.json`. The
   trailing slash is part of the URL:

   ```json
   {
     "env": {
       "PS_MCP_URL": "https://ps.test:30443/mcp/"
     }
   }
   ```

   If you gave `--hostname` to the deploy script, use that name instead of `ps.test`. The
   script prints the value as `PS_MCP_URL=...`.

3. Authenticate from the CLI. Start a new session with `claude`, run `/mcp`, select
   `plugin:ps-plugin:ps-mcp` and choose **Authenticate**.

   - A browser opens Authentik. Sign in with your passkey.
   - Authentik shows a consent screen. This is expected. Approve it.
   - Port `53682` must be free on your machine.

4. Check it. Ask Claude: `List the policy system tools you have available`.

#### Step 6: Claude Desktop (optional)

Install [Claude Desktop](https://claude.com/download) and sign in. Once you have authenticated the
plugin from the CLI in step 5, new Desktop Code sessions are signed in too. Quit Claude Desktop
fully and start it again after you set `PS_MCP_URL`. In testing, authenticating from inside
Claude Desktop did not work reliably. Do it from the CLI.

### Client on Windows

Scenarios 2, 3 and 4.

Run PowerShell as administrator for steps 1 and 2.

#### Step 1: Add the hosts line

Open the hosts file in Notepad:

```powershell
notepad C:\Windows\System32\drivers\etc\hosts
```

Add the line the deploy script printed, with the backend's address, and save:

```text
<backend address> ps.test
```

#### Step 2: Trust the CA

> [!WARNING]
> Copy `ca.pem` only. **Never copy `ca.key`** from the same folder. The CA can only vouch for the
> evaluator hostname and only for TLS server authentication, and the key stays on the server
> machine.

Copy `ca.pem` from the backend machine to this machine:

```powershell
scp <user>@<backend address>:~/.config/policy-system/eval-tls/ca.pem $HOME\ca.pem
```

Add it to the trusted root store:

```powershell
certutil -addstore -f Root $HOME\ca.pem
```

#### Step 3: Register the passkey (owner only)

Open the enrolment link in a browser on this machine and register a passkey. A security key, the
platform authenticator or a phone all work. There is no password. Registration also logs you in
and ends on a PS Service page at `https://<host>/enrolled` that says the passkey is registered and
you are signed in, and names `ps-cli auth login`. Close the tab. Everyone else
registers from an invitation the owner sends. If you are not the owner, skip this step.

#### Step 4: Install the Claude Code CLI

```powershell
winget install Anthropic.ClaudeCode
```

The CLI runs on Windows, not inside WSL2.

#### Step 5: Install the plugin and authenticate it

The plugin adds the Policy System skills and connects to the backend's MCP endpoint.

1. Add the marketplace and install the plugin from the Claude Code CLI:

   ```text
   claude plugin marketplace add mindovermachine-dev/policy-system
   ```

   ```text
   claude plugin install ps-plugin@ps-marketplace
   ```

2. Set the service URL. Add `PS_MCP_URL` to the `env` block of `%USERPROFILE%\.claude\settings.json`. The
   trailing slash is part of the URL:

   ```json
   {
     "env": {
       "PS_MCP_URL": "https://ps.test:30443/mcp/"
     }
   }
   ```

   If you gave `--hostname` to the deploy script, use that name instead of `ps.test`. The
   script prints the value as `PS_MCP_URL=...`.

3. Authenticate from the CLI. Start a new session with `claude`, run `/mcp`, select
   `plugin:ps-plugin:ps-mcp` and choose **Authenticate**.

   - A browser opens Authentik. Sign in with your passkey.
   - Authentik shows a consent screen. This is expected. Approve it.
   - Port `53682` must be free on your machine.

4. Check it. Ask Claude: `List the policy system tools you have available`.

#### Step 6: Claude Desktop (optional)

Install [Claude Desktop](https://claude.com/download) and sign in. Once you have authenticated the
plugin from the CLI in step 5, new Desktop Code sessions are signed in too. Quit Claude Desktop
fully and start it again after you set `PS_MCP_URL`. In testing, authenticating from inside
Claude Desktop did not work reliably. Do it from the CLI.

## Part 3: Reference

### What is exposed

The evaluator exposes one ingress controller (ingress-nginx) to the network on purpose, so other
machines can log in. The chart Ingress routes PS Service and the allowlisted Authentik login paths
through it on one port. PS Service's `/metrics` has no Ingress rule, so the controller answers it
`404`.

| Listener | Address | Reachable by | What it serves |
| --- | --- | --- | --- |
| Ingress controller over HTTPS | Host port `30443` on **all interfaces** (`0.0.0.0`) | Anyone who can reach port `30443` of the backend machine. On a shared LAN, everyone on the LAN. | PS Service (REST and MCP) and the allowlisted Authentik login paths |
| Authentik | ClusterIP only | Pods in the cluster only | The deploy script reaches its API through a loopback `kubectl port-forward` |
| PS Service | ClusterIP only | Pods in the cluster, and clients through the ingress controller | The REST API and the MCP endpoint |
| FalkorDB Browser | `127.0.0.1:3001` | The backend machine only | The graph explorer UI. Authentik does not protect it. |
| FalkorDB (Redis) | ClusterIP only | Pods in the cluster only | The graph database |

To reach the Authentik admin UI, see [Operations Guide: Reaching the Authentik admin UI
(evaluator)](./operations-guide.md#reaching-the-authentik-admin-ui-evaluator). The admin UI is not
routed by the Ingress. It is reached by port-forward only.

To limit access to your own machine, block port `30443` from the LAN with the host firewall.

**Transport and accepted risks.** The controller serves HTTPS on port `30443` with the local
certificate in the Secret `policy-system-local-tls`, and answers any path or host the Ingress does
not list with `404`. Cookies pass through as Authentik sets them; the controller adds no cookie
flags. The controller sends no HSTS header for this certificate, which comes from a local CA. The
private key of the certificate is in a Secret that anyone with `kubectl` access to the cluster can
read. Paths that normalise into an allowlisted path (for example through a percent-encoded dot or
slash) are forwarded as the normalised path; paths that leave the allowlist get `404`. These risks
remain on a shared LAN:

- **A rogue host on the LAN.** Anyone who can reach port `30443` can attempt logins and probe the
  allowlisted paths (`charts/policy-system/files/authentik-public-paths.txt`). Every PS Service
  call still needs an Authentik-issued bearer token.
- **Device-code phishing.** A user can be tricked into approving a device code that an attacker
  started. Approve a code only when you started the login yourself.
- **One shared origin.** PS Service and the Authentik login pages are served from one origin, so a
  script injected into one can read what the other sets for that origin.
- **MCP and DNS rebinding.** The MCP endpoint is protected by its bearer token alone.
- **The Authentik admin UI and API are not routed.** They are not in the allowlist, so the Ingress
  has no rule for them; reaching the admin UI is by port-forward.

**Approval-page names.** The deploy script sets `psService.passkeySigning.allowedOrigins` to
`https://<hostname>:30443`, where `<hostname>` is the name you pass to `--hostname` (`ps.test` by
default). That origin is the WebAuthn relying party for signing approvals (`rp_id` is the
hostname), and a POST to an approval route (`/approvals/<id>/...`) on any other `Host` is answered
`421`. Open the approval page on `https://<hostname>:30443`, on any machine that maps the name and
trusts the CA. The approval link the service hands out is built from the `Host` of the request that
created it, so a client that reached PS Service under a name that is not listed receives a link that
answers `421`; open it on `https://<hostname>:30443` instead.

**The exposure check.** `scripts/deploy-ps-eval.sh` stops before any cluster call when an interface
of the machine holds a public address, and `scripts/deploy-ps-eval.sh --check-exposure` runs only
that check. Loopback, link-local, and private addresses pass: `10.0.0.0/8`, `172.16.0.0/12`,
`192.168.0.0/16`, `169.254.0.0/16`, `fc00::/7`, and `fe80::/10`. Everything else fails, including a
VPN address in `100.64.0.0/10`. If `ip` and `ifconfig` are both missing, the check fails because it
cannot verify. `--allow-public-host` accepts the exposure. A machine with a public address needs the
flag on both calls: `--check-exposure --allow-public-host` in the check step and
`--allow-public-host` on the deploy script.

The local CA private key (`ca.key`, in the state directory) can create a certificate only for the
evaluator hostname and its subdomains, because the CA carries a critical name constraint, and only
for TLS server authentication. Keep the key on the backend machine with owner-only permissions.
Never copy it. Share `ca.pem` only.

The Authentik API token of the chart is the Authentik bootstrap token. It is
**administrator-equivalent**. It is stored in the Secret `policy-system-authentik-api-token`.
Anyone with `kubectl` access to the cluster can read it. The scripts never print it.

### What the deploy script does

`scripts/deploy-ps-eval.sh` does these things, in this order:

1. It checks that no interface of the machine holds a public address. It stops before any cluster
   call, unless you passed `--allow-public-host`.
2. It checks that `kubectl`, `helm`, `openssl`, `jq`, `curl`, and `podman` (or `docker`) are on
   `PATH`. It stops and names every missing tool.
3. It finds the IP address of the kind node.
4. It creates a local certificate authority (CA) that can only vouch for the evaluator hostname.
   The CA has a critical name constraint for that hostname, is valid for at most 730 days, and is
   limited to TLS server authentication. It creates a certificate for exactly that hostname. It
   stores both in the state directory with owner-only permissions. It creates the certificate again
   when its names differ or it is close to expiry. It replaces the CA, and the certificate with it,
   when the CA lacks the constraint, permits another name, is valid for longer than 730 days, or is
   close to expiry. It keeps the replaced CA next to the new one as
   `ca.replaced-<fingerprint prefix>.pem`.
5. It runs **one** `helm upgrade --install`. This sets the issuer
   (`https://<hostname>:30443/auth/application/o/ps-cli/`) and the bootstrap owner together. The
   bootstrap owner is the owner's email as `sub`, with that issuer. There is no second deployment.
   The script runs the upgrade again only when its values change.
6. It waits until Authentik, the ingress controller and PS Service are Ready. The script then checks
   from inside the PS Service pod that the issuer's OIDC discovery document can be fetched through
   the ingress.
   Last, it checks from this machine, through port `30443` and verifying the CA, that `/status`
   answers, that the in-cluster probes are not routed (`/livez` answers `404`), that the discovery document reports exactly the issuer the release was given, and that a
   request to `/mcp/` without a bearer token is refused with `401`.
7. It creates the owner as an Authentik administrator. The username and email are the address you
   gave. **No password is set, prompted for, or logged.** It prints a single-use, time-limited link
   to register a passkey.

An evaluator that ran the older reverse proxy keeps its cluster: run
`kubectl apply -f deploy/kind/ingress-nginx.yaml` once, then the deploy script. The script deletes
the old proxy Service, which held port `30443`, and its Helm upgrade removes the proxy workload.

The hostname is `ps.test` by default. The script rejects any `--hostname` that ends in `.local`,
because Claude Code cannot resolve `.local` names. The hostname is also the issuer and the WebAuthn
relying-party ID, so it cannot change later without losing every signing passkey.

The address that the script prints in the hosts line is the address of the default-route interface,
else the only private IPv4 address of the machine. If neither gives one address (for example,
because container bridges add several private addresses and there is no default route), the script
stops before it changes anything and asks for `--lan-address <ip>`. A hosts line that maps the name
to another address is reported and never edited.

### After the install

To keep the evaluator running, see [Operations Guide: Evaluator (local-test)
operations](./operations-guide.md#evaluator-local-test-operations). It covers
[updating to the latest version](./operations-guide.md#updating-to-the-latest-version) and
[owner recovery and certificate renewal](./operations-guide.md#owner-recovery-certificate-renewal-and-re-running-the-script).

### Common problems

| Symptom | Cause and fix |
| --- | --- |
| The browser shows a certificate warning | The client has not trusted `ca.pem`. Do client step 2. |
| `ps.test` does not resolve | The hosts line is missing, or a different line maps `ps.test` to another address. The script reports a conflicting line and never edits it. Fix the hosts file. |
| The enrolment link no longer works | It expired or was used. See [Operations Guide: Owner recovery](./operations-guide.md#owner-recovery-certificate-renewal-and-re-running-the-script). |
| The owner's only passkey device is lost or stolen | On the Server run `scripts/deploy-ps-eval.sh --reset-owner-passkey --owner-email <email>` and open the new link. The signing passkey is re-enrolled at the next approval. See [Operations Guide: Resetting the owner's passkeys](./operations-guide.md#resetting-the-owners-passkeys-after-a-lost-or-stolen-device). |
| The script cannot choose a LAN address | Run it again with `--lan-address <ip>`. |
| `/mcp` shows `ps-mcp` as failed with `Missing environment variables: PS_MCP_URL` | Set `PS_MCP_URL` (step 5) and start a new session. |
