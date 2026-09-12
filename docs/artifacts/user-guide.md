# Policy System User Guide

## Table of Contents

- [Local Test](#local-test)
  - [Status of this path](#status-of-this-path)
  - [Prerequisites](#prerequisites)
  - [1. Install Claude Desktop](#1-install-claude-desktop)
  - [2. Install Podman and start its machine](#2-install-podman-and-start-its-machine)
  - [3. Clone the repo](#3-clone-the-repo)
  - [4. Create the local cluster](#4-create-the-local-cluster)
  - [5. Deploy Policy System](#5-deploy-policy-system)
  - [6. Install ps-cli](#6-install-ps-cli)
  - [7. Load regulations into the graph](#7-load-regulations-into-the-graph)
  - [8. Install the Policy System plugin](#8-install-the-policy-system-plugin)
  - [9. Ask a question](#9-ask-a-question)
  - [Troubleshooting (Local Test)](#troubleshooting-local-test)
- [Ollama / Local Model Support](#ollama--local-model-support)
- [Policy System plugin (ps-qna)](#policy-system-plugin-ps-qna)
- [ps-cli](#ps-cli)
  - [Install](#install)
  - [Configuring which PS Service instance ps-cli targets](#configuring-which-ps-service-instance-ps-cli-targets)
    - [Single target (default)](#single-target-default)
    - [Multiple named targets (contexts)](#multiple-named-targets-contexts)
    - [Credential storage](#credential-storage)
  - [Command reference](#command-reference)
  - [Running commands](#running-commands)
  - [Troubleshooting](#troubleshooting)
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
| Ask a compliance question in natural language | [Policy System plugin](#policy-system-plugin-ps-qna) | ✅ Available |
| Ingest a regulation or internal policy, check service health, administer an instance | [ps-cli](#ps-cli) | ✅ Available |
| Author Policies, Standards, and Controls | Policy Editor | ❌ Not yet designed |

---

## Local Test

> [!NOTE]
> **This path is for evaluators** trying Policy System on their own laptop via a
> Helm chart on a local `kind` cluster.

The same Helm chart is also intended to serve **production administrators**
deploying to a real Azure/AWS/on-prem cluster later, with a different values profile.
This walkthrough covers the local-test profile only; a production rollout guide
does not exist yet.                                                                    |

### Prerequisites

| Tool                                                                 | Why                                         |
| --------------------------------------------------------------------- | -------------------------------------------- |
| [Claude Desktop](https://claude.com/download)                        | Hosts the Policy System plugin              |
| [git](https://git-scm.com/downloads)                                 | Clones this repo                            |
| [Podman](https://podman.io/docs/installation)                        | Container runtime backing the local cluster |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | Runs a Kubernetes cluster on Podman         |
| [kubectl](https://kubernetes.io/docs/tasks/tools/)                   | Talks to the cluster                        |
| [Helm](https://helm.sh/docs/intro/install/)                          | Installs the Policy System chart            |


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

### 5. Deploy Policy System

```bash
brew install helm
```

```bash
helm install policy-system ./charts/policy-system --wait # This step can take a few minutes to complete as the container images are downloaded.

kubectl get pods    # ps-service and falkordb should both be in "Running" state

curl http://127.0.0.1:8000/health

curl http://127.0.0.1:8000/ready

open http://localhost:3001/login

```

`localhost:3001/login` opens to FalkorDB web ui used to explore the graph database

> [!TIP]
> **Updating to the latest version.** The chart is installed from your local checkout
> and pins the `ps-service` image version in `values.yaml`. That pin is maintained
> automatically by the release job — it always equals `Chart.yaml`'s `appVersion` —
> so you never edit it by hand; a new release is picked up by pulling the repo and
> upgrading the existing Helm release — not by re-running `helm install`:
>
> ```bash
> git pull
>
> helm upgrade policy-system ./charts/policy-system --reset-values --wait
>
> kubectl get pods -l app.kubernetes.io/component=ps-service \
>   -o custom-columns='NAME:.metadata.name,IMAGE:.spec.containers[0].image,STATUS:.status.phase'
> ```
>
> The `IMAGE` column should show the tag pinned in `charts/policy-system/values.yaml`.
> `--reset-values` matters: Helm otherwise carries forward any `--set` from a previous
> install or upgrade, so a tag you once pinned by hand would silently win over the
> chart's new default. Your graph data is kept — FalkorDB persists to a
> `PersistentVolumeClaim` (see [Operations: Backup & Restore](#operations-backup--restore)),
> so regulations loaded in step 7 do not need to be re-seeded.

### 6. Install ps-cli

`ps-cli` is a command-line client for PS Service's REST API: select and ingest EU
regulations from Cellar/ELI, ingest internal policies, and check service health and
readiness. It's a distributed client, installable independently of this repo like
`gh`/`az` — no clone/checkout needed. Installing it requires
[uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
curl -fsSL https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/ps-cli/install.sh | bash
```

This runs [`ps-cli/install.sh`](../../ps-cli/install.sh), which installs `ps-cli` via
`uv tool install` and puts it on `PATH` through `uv`'s tool-install shims. Verify:

```bash
ps-cli --version
```

### 7. Load regulations into the graph

A freshly deployed system has an empty graph and can answer nothing. Seed it:

```bash
ps-cli catalog list         # curated instruments available to restore

ps-cli catalog restore <id> # e.g. `ps-cli catalog 32024R2847` just printed
```

Until something is seeded, the system answers questions with an explicit "graph is unseeded" error rather than an empty result.

### 8. Install the Policy System plugin

In Claude Desktop: **Customize** → **Plugins** → **Add** → **Add marketplace** → **Add from a repository**, then add
this repo:

```text
URL:  `https://github.com/mindovermachine-dev/policy-system`
```

This installs the `ps-qna` skill. The plugin also declares a `policy-system-graph` MCP
connector, but that half is for a **hosted** PS Service — Claude Desktop evaluates
plugin and custom connectors from Anthropic's cloud, so it can never reach the
`127.0.0.1:8000` instance you deployed in step 5. Until a hosted instance exists its
URL is a placeholder (`https://ps.example.com/mcp/`) and the connector will show as
unreachable; that is expected.

**For local test, register PS Service as a local MCP server instead, under the name
`policy-system-graph-local`.** Claude Desktop launches local servers over stdio, so
`mcp-remote` bridges to the HTTP endpoint on your laptop. The `-local` suffix is
deliberate: the plugin's own connector is already named `policy-system-graph`, and two
connectors sharing one name lets the unreachable hosted one shadow the working local one
in a chat's toolset. The `ps-qna` skill accepts either name. Requires
[Node.js](https://nodejs.org/) (`node --version`).

In Claude Desktop: **Claude menu (menu bar)** → **Settings…** → **Developer** →
**Edit Config**, and add an `mcpServers` key alongside whatever is already there:

```json
{
  "mcpServers": {
    "policy-system-graph-local": {
      "command": "npx",
      "args": ["-y", "mcp-remote@latest", "http://127.0.0.1:8000/mcp/", "--transport", "http-only"]
    }
  }
}
```

Quit Claude Desktop fully (⌘Q) and relaunch. `policy-system-graph-local` should appear
under the **+** button → **Connectors** → **Manage connectors**, exposing two tools,
`domain_concepts` and `cypher`. If it doesn't, `tail -f ~/Library/Logs/Claude/mcp*.log` shows why. Tools bind
when a conversation starts, so open a **new** chat after relaunching — an existing chat
won't pick the connector up.

> [!NOTE]
> Do **not** use **Settings → Connectors → Add custom connector** for a local instance.
> That path insists on HTTPS because it connects from Anthropic's servers, not your
> machine — `localhost` is unreachable from there regardless of TLS.

### 9. Ask a question

```text
What obligations does the Cyber Resilience Act place on manufacturers,
and which of our policies cover them?
```

The skill grounds itself against the domain model, writes read-only Cypher, retrieves
from the graph, and constructs an answer that cites what it retrieved. If the graph
cannot answer, it says so rather than filling the gap from model recall.

If the skill does not engage on its own, ask for it by name: _"Use the ps-qna skill."_

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
named contexts, the way `kubectl` has contexts or `az` has subscriptions.

```bash
ps-cli config set-context dev --url https://dev.example.com
ps-cli config set-context prod --url https://prod.example.com

ps-cli config use-context prod
ps-cli config list-contexts
#   dev   https://dev.example.com
# * prod  https://prod.example.com   (* marks the current context)
```

Once a context is current, every command uses it — no `PS_CLI_SERVICE_URL` needed:

```bash
ps-cli regulations list   # targets prod
```

**Override for a single command** with `--context`, without changing what's current:

```bash
ps-cli --context dev regulations list   # targets dev, just this once
ps-cli config list-contexts             # still shows prod as current
```

**Resolution order** (highest wins): `PS_CLI_SERVICE_URL` env var > `--context` flag >
the current context in your config > the single-target fallback above.

Contexts are stored in `targets.toml` under `~/.config/ps-cli/` by default. Override
the location with `PS_CLI_CONFIG_DIR` (mirroring `gh`'s `GH_CONFIG_DIR`) if you want
an isolated config, e.g. for testing:

```bash
PS_CLI_CONFIG_DIR=/tmp/my-ps-cli-config ps-cli config list-contexts
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

> ❌ **No command stores a credential yet.** This is infrastructure ahead of the
> authentication work that will use it — today, PS Service's REST API takes no
> credential at all (loopback-only, no auth), so `ps-cli` never sends one. The one
> place this already runs is `config set-context`: re-running it for an existing
> context name with a new `--url` always clears any credential previously stored for
> that name, so nothing is ever silently carried over to a new URL once one *is*
> stored. Full credential use is pending Auth0 device-flow login
> ([#57](https://github.com/mindovermachine-dev/policy-system/issues/57)) and PS
> Service's bearer-token validation
> ([#58](https://github.com/mindovermachine-dev/policy-system/issues/58)).

### Command reference

Global flags, usable before or after any subcommand:

| Flag | Description |
| --- | --- |
| `-v`, `--verbose` | Print the failure site (file:line) on error. |
| `--context <name>` | Use this named context's PS Service URL for this invocation only. Never persisted. |
| `--version` | Print version information and exit. |

| Command | Arguments | Description |
| --- | --- | --- |
| `ps-cli health` | — | Report reachability, health (`/health`), and readiness (`/ready`) for the configured target, naming any unhealthy dependency; readiness reflects FalkorDB only — an unhealthy LLM Interface/Cellar-ELI is still named when present, but does not by itself make the target unready. |
| `ps-cli check` | — | Sweep every tracked instrument for amendments, re-ingesting any found; reports one outcome line per instrument. |
| `ps-cli regulations list` | — | List the curated EU-regulation catalog (CELEX + title). Static, PS Service-packaged data — does **not** reflect what's actually restored/ingested into the graph, and has no FalkorDB/LLM dependency. Tracked for removal: [#78](https://github.com/mindovermachine-dev/policy-system/issues/78). |
| `ps-cli regulations ingest <celex>` | `celex` — 10-character CELEX identifier (e.g. `32016R0679`) | Ingest a regulation through the full pipeline. |
| `ps-cli internal ingest <fixture_path>` | `fixture_path` — a `.json` path, resolved on PS Service's fixtures root, not read locally | Ingest an internal policy document. |
| `ps-cli catalog list` | — | List every curated instrument in the local curated-content repo (id, title, source_type/jurisdiction). No PS Service connection needed. |
| `ps-cli catalog restore <instrument_id>` | `instrument_id` — the curated instrument's id (e.g. `CRA-1.0`) | Restore one curated instrument's pre-ingested artifact into PS Service. |
| `ps-cli config set-context <name> --url <url>` | `name`, `--url` (required) | Create or update a named context's PS Service URL. Clears any credential previously stored for that name. |
| `ps-cli config use-context <name>` | `name` | Select the named context every subsequent command uses. |
| `ps-cli config list-contexts` | — | List every named context, marking the current one. |

Run `ps-cli --help` or `ps-cli <command> --help` for the same reference from the CLI
itself.

### Running commands

```bash
ps-cli regulations list                    # static curated catalog — no FalkorDB/LLM dependency
ps-cli regulations ingest 32016R0679        # full ingestion pipeline
ps-cli internal ingest <fixture_path>.json  # ingest an internal policy document
```

`regulations ingest` and `internal ingest` exercise the full pipeline, so the PS
Service instance you're targeting needs FalkorDB and its LLM interface configured —
check its `/ready` endpoint first if a command fails unexpectedly (see
[Troubleshooting](#troubleshooting) below).

A few behaviors worth knowing about `regulations ingest`:

- The `celex` argument is trimmed and format-validated before it's sent — a malformed
  value is rejected immediately, without a round trip to PS Service.
- A CELEX identifier doesn't have to be in the curated catalog (`regulations list`) to
  be ingestible: if it's not curated, PS Service resolves it against Cellar/ELI (the
  public EU document repository) directly. `regulations list` stays the fast, known-title
  discovery set; ingestion isn't limited to it.
- A real ingestion run takes minutes (a full CRA ingestion has measured ~10 minutes
  end to end). `ps-cli` prints each pipeline stage's name to stderr as it starts, so a
  long-running ingest doesn't look hung — the final `run_id` /
  `regulatory_instrument_id` / per-stage summary still prints to stdout only, once.

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
`regulations ingest`/`internal ingest`/`check` via that command's own pre-flight failure
message (`❌ LLM Interface is unavailable.`), before any pipeline call is made.

`ps-cli health` reports all three — reachability, health, and readiness — in one call:

```
$ ps-cli health
reachable: yes
health: alive
ready: ready
```

```
$ ps-cli health
❌ PS Service is reachable but not ready (health='alive', ready='not_ready').
💡 unhealthy dependencies: falkordb
```

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
| `credentials.toml` | ps-cli | none until a credential is stored | Per-context credential fallback when no OS keyring backend is available. Not yet used by any command — see [Credential storage](#credential-storage). |

This table covers `ps-cli` only — a row for the Policy System plugin's MCP endpoint
configuration will be added once it exists ([#53](https://github.com/mindovermachine-dev/policy-system/issues/53)).

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
| `ps-cli` reports "Could not reach PS Service" | Is the target URL right (`ps-cli config list-contexts` / `echo $PS_CLI_SERVICE_URL`)? Is PS Service actually running there? |
| A command fails right after connecting | `ps-cli health` — reports whether FalkorDB, the LLM Interface, and Cellar/ELI are all reachable, and which is not if any aren't. `health: alive` with `ready: not_ready` means the process is up but FalkorDB is unreachable, or required ingestion config is incomplete — LLM Interface/Cellar-ELI issues are named in `unhealthy_dependencies` without flipping `ready` to `not_ready`. |
| `regulations ingest` / `internal ingest` fails immediately with a config-related error | PS Service's ingestion-required config (`PS_LLMINTERFACE_MODEL`, `PS_LLMINTERFACE_EMBED_MODEL`, `PS_COMPANYMERGE_SIMILARITY_THRESHOLD`) is likely missing — this is a PS Service operator/deployer concern, see [CONTRIBUTING.md](../../CONTRIBUTING.md#configure-the-llm-interface). |
| `regulations ingest` / `internal ingest` / `check` fails immediately with "LLM Interface is unavailable" | PS Service's LLM Interface is unreachable — `ps-cli`'s pre-flight check caught it before any pipeline call; check PS Service's `/ready` endpoint and its LLM provider configuration. |
| Referencing a context that doesn't exist | `ps-cli` exits non-zero and lists valid context names — see [ps-cli Troubleshooting](#troubleshooting). |

## Glossary

Entities, relationships, and vocabulary used throughout this guide (Regulatory
Instrument, Obligation, Capability, Control, ...) are defined in
[`docs/artifacts/ps-domain-concepts.md`](./ps-domain-concepts.md).
