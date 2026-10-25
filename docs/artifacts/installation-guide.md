# Policy System Installation Guide

## Table of Contents

- [Part A: Server install](#part-a-server-install)
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
  - [B2. Install ps-cli](#b2-install-ps-cli)
  - [B3. Point ps-cli at the instance and log in](#b3-point-ps-cli-at-the-instance-and-log-in)
  - [B4. Install the Policy System Plugin](#b4-install-the-policy-system-plugin)
  - [Which Claude surfaces reach your deployment](#which-claude-surfaces-reach-your-deployment)
- [Reference](#reference)
  - [SystemOwner bootstrap](#systemowner-bootstrap)
  - [Upgrading an existing install](#upgrading-an-existing-install)
  - [Verification status](#verification-status)

This guide shows how to **deploy** Policy System to production and how to set up the computers that
use it.

> [!NOTE]
> **To install an evaluator on your own computer or LAN, use the [Evaluator Installation
> Guide](./evaluator-installation-guide.md).** This guide covers the production path only.

It has two parts:

- **Part A: Server install.** The operator does it once: a production rollout to a customer tenant
  in Azure with [`scripts/deploy-ps-prod.sh`](../../scripts/deploy-ps-prod.sh).
- **Part B: Client install.** Every person who uses the deployment does it on their own computer.
  Part B describes the `ps-cli` and plugin setup for a production deployment. The evaluator's
  client steps are in the [Evaluator Installation
  Guide](./evaluator-installation-guide.md#part-2-install-the-client).

The production script does these three things (the evaluator script does the same):

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

The operator does Part A once per deployment. It ends with a running backend and the owner's passkey. Then everyone, the operator included, continues with
[Part B](#part-b-client-install).

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
5. It waits for Authentik after the Helm release. The release sets `psService.publicUrl` to
   `https://<public hostname>`.
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
Production uses a Let's Encrypt certificate, so there is no hosts line or CA to install.

### What is exposed (production)

`deploy-ps-prod.sh` publishes **restricted** parts of PS Service and of Authentik on one public
hostname: `https://<label>.<region>.cloudapp.azure.com`. A Let's Encrypt certificate serves it.
The Helm chart renders both Ingresses from `psService.publicUrl`; the script does not apply any
Ingress manifest. An Ingress left by an earlier script run is taken over by the release
automatically before the upgrade, and its certificate Secret name is unchanged.

The Ingress of PS Service routes only the paths in the chart file `files/ps-service-public-paths.txt`:
`/status`, `/.well-known/`, and the API and MCP prefixes. It has no catch-all rule, so the
in-cluster probes `/livez` and `/readyz`, `/metrics` and the interactive API documentation are
never routed.

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
| The PS Service paths in the allowlist above | Yes | Every call needs an Authentik-issued bearer token. `/status` needs no token; a valid one adds the dependency detail. |
| PS Service `/livez`, `/readyz`, `/metrics` and the interactive API documentation | **No** | Not routed by the chart Ingress; in-cluster only. |
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
  - Login runs through the bundled `ps-login` flow, which has no password stage and asks only
    for a passkey (user verification required).
  - An account without a registered passkey, including `akadmin`, is denied. It regains access
    through an admin recovery link.
  - A password set by an administrator in the admin UI is not usable at login.
  - Three failed passkey assertions for a typed username lock that sign-in out (the fourth
    attempt is denied). Failed autofill sign-ins are not counted.
  - Authentik's shipped `default-authentication-flow` is not used by the brand but stays
    reachable by its direct URL. On an install upgraded from an earlier revision, a reputation
    Deny binding left on that flow is not removed and has no effect on login through `ps-login`.
  - The chart's blueprint is applied to the pinned Authentik image in CI. To run that check
    locally, use `uv run pytest -m authentik_blueprint -q -n0` (needs podman or docker; the
    first run pulls a 1.4 GB image).
- This flow does not change production TLS termination (Let's Encrypt, cert-manager).

---

For the `ps-cli` configuration reference (context and credential storage, environment
variables, config files), see [User Guide: Appendix — ps-cli
reference](./user-guide.md#appendix-ps-cli-reference). For PS Service and chart configuration,
see the [Helm Chart Values Reference](./helm-chart-values-reference.md).

---

# Part B: Client install

Do Part B on **every machine that uses the deployment**. For an evaluator, the hosts line, the CA
and the plugin are in [Part 2 of the Evaluator Installation
Guide](./evaluator-installation-guide.md#part-2-install-the-client). Do those first, then B1 and the
steps you need here.

Get these values from the operator first:

| Value | Evaluator | Production |
| --- | --- | --- |
| Service URL | `https://ps.test:30443` (or the `--hostname` the operator chose) | `https://<label>.<region>.cloudapp.azure.com`, printed by [production step 3](#3-run-scriptsdeploy-ps-prodsh) |
| Hosts line and `ca.pem` | Needed. See the [Evaluator Installation Guide](./evaluator-installation-guide.md#part-2-install-the-client) | Not needed. A Let's Encrypt certificate serves the host |
| `ps-cli` context | `eval` | `prod` |
| `PS_MCP_URL` | `<service URL>/mcp/` | `<service URL>/mcp/` |
| Your account | The owner's passkey (see the [Evaluator Installation Guide](./evaluator-installation-guide.md#step-3-register-the-passkey-owner-only)), or an invitation | The owner's passkey ([step 5](#5-enrol-the-owners-passkey)), or an invitation |

## B1. Install Claude Desktop

1. Download Claude Desktop from [claude.com/download](https://claude.com/download) (macOS or
   Windows).
2. Sign in.

You can skip B1 if you only use the Claude Code CLI.

## B2. Install ps-cli

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

## B3. Point ps-cli at the instance and log in

The account that you sign in with must first have a passkey. The owner registers it from the
link the deploy script printed. Everyone else registers it from an invitation. Neither person sets
a password.

On the machine that ran `scripts/deploy-ps-eval.sh`, the script already set and selected the
`eval` context, unless that context was already correct, or `ps-cli` was not on `PATH` while the
script ran (see B2). Elsewhere, set it. Use the **service URL** from the table at the top of Part B.

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
the CA. Do [Part 2 of the Evaluator Installation Guide](./evaluator-installation-guide.md#part-2-install-the-client).

> **Why no environment variable:** `ps-cli` checks the Authentik certificate against the
> operating system trust store. Trusting the local CA (Part 2 of the Evaluator Installation Guide) adds it to that store. On Linux, `SSL_CERT_FILE` or
> `SSL_CERT_DIR` also work. On macOS and Windows, `ps-cli` ignores them.

For what each command does, see [User Guide: Set up ps-cli](./user-guide.md#set-up-ps-cli). For
credential storage, see the [Appendix](./user-guide.md#appendix-ps-cli-reference).

## B4. Install the Policy System Plugin

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

   - Evaluator: the ingress controller serves PS Service on port `30443`, so the URL is
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
the LAN only). See [What is exposed](./evaluator-installation-guide.md#what-is-exposed) in the Evaluator Installation Guide. A production
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

This applies to both the [Evaluator](./evaluator-installation-guide.md) and the
[Production](#3-run-scriptsdeploy-ps-prodsh) installation. Only the script and the Authentik
base URL are different.

To become `SystemOwner`:

1. **Register a passkey** from the link that the script printed
   ([evaluator](./evaluator-installation-guide.md#step-3-register-the-passkey-owner-only),
   [production](#5-enrol-the-owners-passkey)).
2. **Log in with `ps-cli`** (`ps-cli auth login`). Claude Desktop plugin sign-in is separate.
   See [B4](#b4-install-the-policy-system-plugin).
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

**Public URL (breaking change).** PS Service now requires its public origin. The Helm value
`psService.publicUrl` (environment variable `PS_PUBLIC_URL`) is rendered into the Deployment,
and the chart fails to render without it unless `psService.localTestBypass.enabled=true`. Set it
**before** you upgrade:

- Production: `https://<hostname>`, the public hostname of the install.
- Evaluator: `https://<hostname>:30443`.

`scripts/deploy-ps-prod.sh` and `scripts/deploy-ps-eval.sh` set it for you, so re-run the script
before `scripts/ps-upgrade.sh`; on a production release without the value, `ps-upgrade.sh` stops
and names it.
If you run `helm upgrade` yourself, add `--set psService.publicUrl=https://<hostname>`. The value
has no path, query or fragment, and uses `https` unless the local-test bypass is on. See the
[Helm Chart Values
Reference](./helm-chart-values-reference.md).

**Authentik API token.** **Applies to:** installs made before this flow existed.

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
warning in [step 3](./evaluator-installation-guide.md#step-3-create-the-cluster).

**Graph mutation log.** Upgrading to a version with the graph mutation log also runs a
provisioning Job. The Job applies the ordinary `ps_state` migrations. It also creates the
owner-protected `graph_log` tables in `ps_state`. The Postgres init script cannot do this on an
existing cluster. The upgrade command must pass `--wait --wait-for-jobs`. See the [Operations Guide: Upgrading to the graph mutation
log](./operations-guide.md#upgrading-to-the-graph-mutation-log).

## Verification status

This section lists what the flows above were verified against, and what they were not. Read it
before you rely on a claim for a real deployment.

- **Evaluator path: see the evaluator guide.** What is verified for each scenario is in the
  [Supported scenarios](./evaluator-installation-guide.md#supported-scenarios) table. The older evidence
  record is in the [Operations Guide](./operations-guide.md#evaluator-verification-history).
- **Production path: verified on `kind`, not on AKS.** The real `deploy-ps-prod.sh` and
  `values-prod.yaml` ran against `kind`. The run used `ingress-nginx` and a stand-in Azure CLI.
  It skipped the cert-manager and Let's Encrypt steps.
  - **Verified** (observed on `kind` with an Ingress applied by the script; the chart renders the
    Ingresses, which unit tests and render checks cover, and no kind run has observed the chart-rendered Ingresses):
    - The Ingress routes exactly the eight paths above.
    - The Ingress blocks the admin UI and the admin APIs.
    - The owner link on the public address enrols a passkey through the Ingress. No flow
      request was blocked.
    - A re-run leaves the owner untouched.
  - **Not verified:**
    - The AKS managed NGINX add-on.
    - That an existing certificate and its TLS Secret are kept when the release takes over an
      Ingress, and that a first install on an empty cluster converges within the script's
      10 minute helm wait. Unit tests cover only the call order and the Ingress names, class,
      host and Secret name.
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
