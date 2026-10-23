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
| 4 | Several machines on a LAN | [Backend on Linux](#backend-on-linux-headless) | [Client on Windows](#client-on-windows) | Verified 2026-10-23 on release 8.2.1, steps 1 to 6: Authentik passkey login (Android and iPhone QR code) and the Claude Desktop Code tab. Claude Code CLI and Claude Desktop were the latest versions that day; the exact versions were not recorded |
| 5 | Several machines on a LAN | [Backend on Linux](#backend-on-linux-headless) | [Client on macOS](#client-on-macos) | Unverified |
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

The cluster publishes the proxy on every network interface of the machine (port `30443`). Check
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

Check that it runs:

```bash
kubectl cluster-info --context kind-policy-system
```

> [!WARNING]
> If a `policy-system` kind cluster already exists from an older install, delete it and create
> it again with `kind delete cluster --name policy-system`. **This destroys the graph and both
> Postgres databases stored in that cluster.**

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

### Backend on Linux (headless)

Scenarios 4 and 5.

> [!NOTE]
> **Verified in scenario 4.** Scenario 5 uses the same steps with a macOS client.

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
before you create the cluster. What a clean Ubuntu or Fedora install needs is not recorded here yet.

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

The cluster publishes the proxy on every network interface of the machine (port `30443`). Check
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

Check that it runs:

```bash
kubectl cluster-info --context kind-policy-system
```

> [!WARNING]
> If a `policy-system` kind cluster already exists from an older install, delete it and create
> it again with `kind delete cluster --name policy-system`. **This destroys the graph and both
> Postgres databases stored in that cluster.**

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

- The hosts line on each client maps `ps.test` to an address of this machine that the client can reach. Use the address from the script's hosts line.
- Port `30443` accepts connections from the LAN. Check the firewall of the server machine.
- Reserve this machine's address in your router's DHCP settings. A new address breaks every client until its hosts line is updated.

You can check from another computer on the LAN with:

```bash
curl https://ps.test:30443
```

Expect a message that says "unauthenticated".

When you are done, continue on to: [Part 2](#part-2-install-the-client).

### Backend on WSL2

Scenarios 2 and 3. Run every command of this path inside a Linux
distribution on WSL2. The browser and the Claude Code CLI run on the Windows side: install the
client on Windows ([Client on Windows](#client-on-windows)) after this path.

> [!WARNING]
> **Unverified as a recipe.** A customer ran this setup (scenario 2) and it worked, but the
> install steps are not recorded here yet. Step 1 follows the Linux path.

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

The cluster publishes the proxy on every network interface of the machine (port `30443`). Check
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

Check that it runs:

```bash
kubectl cluster-info --context kind-policy-system
```

> [!WARNING]
> If a `policy-system` kind cluster already exists from an older install, delete it and create
> it again with `kind delete cluster --name policy-system`. **This destroys the graph and both
> Postgres databases stored in that cluster.**

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

Add the line the deploy script printed, with the backend's address:

```bash
echo '<backend address> ps.test' | sudo tee -a /etc/hosts
```

#### Step 2: Trust the CA

> [!WARNING]
> Copy `ca.pem` only. **Never copy `ca.key`** from the same folder. The CA can only vouch for the
> evaluator hostname and only for TLS server authentication, and the key stays on the backend
> machine.

Copy `ca.pem` from the backend machine to this machine:

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
and ends on a page that says **No Applications available**. That is expected. Everyone else
registers from an invitation the owner sends. If you are not the owner, skip this step.

#### Step 4: Install the Claude Code CLI

Follow [Claude Code setup](https://docs.claude.com/en/docs/claude-code/setup). Check it with:

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
and ends on a page that says **No Applications available**. That is expected. Everyone else
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

Lowest verified version: Claude Code CLI 2.1.236. Lower versions are untested.

#### Step 6: Claude Desktop (optional)

Install [Claude Desktop](https://claude.com/download) and sign in. Once you have authenticated the
plugin from the CLI in step 5, new Desktop Code sessions are signed in too. Quit Claude Desktop
fully and start it again after you set `PS_MCP_URL`. In testing, authenticating from inside
Claude Desktop did not work reliably. Do it from the CLI.

## Part 3: Reference

### What is exposed

The evaluator exposes one reverse proxy (nginx) to the network on purpose, so other machines can
log in. It serves PS Service and the allowlisted Authentik login paths on one port.

| Listener | Address | Reachable by | What it serves |
| --- | --- | --- | --- |
| Proxy over HTTPS | Host port `30443` on **all interfaces** (`0.0.0.0`) | Anyone who can reach port `30443` of the backend machine. On a shared LAN, everyone on the LAN. | PS Service (REST and MCP) and the allowlisted Authentik login paths |
| Authentik | ClusterIP only | Pods in the cluster only | The deploy script reaches its API through a loopback `kubectl port-forward` |
| PS Service | ClusterIP only | Pods in the cluster, and clients through the proxy | The REST API and the MCP endpoint |
| FalkorDB Browser | `127.0.0.1:3001` | The backend machine only | The graph explorer UI. Authentik does not protect it. |
| FalkorDB (Redis) | ClusterIP only | Pods in the cluster only | The graph database |

To reach the Authentik admin UI, see [Operations Guide: Reaching the Authentik admin UI
(evaluator)](./operations-guide.md#reaching-the-authentik-admin-ui-evaluator). The admin UI is not
served through the proxy.

To limit access to your own machine, block port `30443` from the LAN with the host firewall.

Risks that remain on a shared LAN: anyone who can reach port `30443` can attempt logins and probe
the allowlisted paths (every PS Service call still needs a bearer token), and a user can be
tricked into approving a device code an attacker started, so approve a code only when you started
the login yourself.

The local CA private key (`ca.key`) can create a certificate only for the evaluator hostname,
and only for TLS server authentication. Keep it on the backend machine with owner-only permissions
and never copy it. The Authentik bootstrap token is administrator-equivalent and is stored in the
Secret `policy-system-authentik-api-token`. Anyone with `kubectl` access can read it.

### Upgrade

Download the deploy bundle again, then run `scripts/deploy-ps-eval.sh` again with the same owner
email. The graph data stays. For the full procedure, see the [Operations
Guide](./operations-guide.md#updating-to-the-latest-version).

### Recover the owner

Run `scripts/deploy-ps-eval.sh` again with the same email. Anyone who can run `kubectl` against
the cluster can run it. What happens depends on the owner's state:

| Owner state | Result |
| --- | --- |
| No passkey registered (the link expired or was never used) | The script prints a fresh link. |
| A passkey is registered | The script changes nothing and prints no link. |
| The only passkey device is lost | Remove the device on the owner's user page in the Authentik admin UI (see [What is exposed](#what-is-exposed)), then run the script again. |

### Common problems

| Symptom | Cause and fix |
| --- | --- |
| The browser shows a certificate warning | The client has not trusted `ca.pem`. Do client step 2. |
| `ps.test` does not resolve | The hosts line is missing, or a different line maps `ps.test` to another address. The script reports a conflicting line and never edits it. Fix the hosts file. |
| The enrolment link no longer works | It expired or was used. Run the deploy script again with the owner's email. |
| The script cannot choose a LAN address | Run it again with `--lan-address <ip>`. |
| `/mcp` shows `ps-mcp` as failed with `Missing environment variables: PS_MCP_URL` | Set `PS_MCP_URL` (step 5) and start a new session. |
