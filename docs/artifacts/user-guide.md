# Policy System User Guide

This guide is for people **using** Policy System — deploying a local-test instance,
asking compliance questions, ingesting regulations or internal policies, or
administering an instance. If you want an overview of the project or its
architecture, see [README.md](../../README.md). If you want to build, test, or
release the project, see [CONTRIBUTING.md](../../CONTRIBUTING.md).

Policy System has three clients. Which section you need depends on what you're
doing, not your job title — see README's
[Target audiences](../../README.md#target-audiences) table if you want the
role-oriented view.

| I want to... | Use | Status |
| --- | --- | --- |
| Try Policy System on my own laptop | [Local Test](#local-test) | ❌ Not yet available |
| Ask a compliance question in natural language | [Policy System plugin](#policy-system-plugin-ps-qna) | ⚠️ Plugin built, structurally verified; end-to-end install unverified |
| Ingest a regulation or internal policy, check service health, administer an instance | [ps-cli](#ps-cli) | ✅ Available |
| Author Policies, Standards, and Controls | Policy Editor | ❌ Not yet designed |

## Table of Contents

- [Local Test](#local-test)
  - [Status of this path](#status-of-this-path)
  - [Prerequisites](#prerequisites)
  - [1. Install Claude Desktop](#1-install-claude-desktop)
  - [2. Install Podman and start its machine](#2-install-podman-and-start-its-machine)
  - [3. Create the local cluster](#3-create-the-local-cluster)
  - [4. Deploy Policy System](#4-deploy-policy-system)
    - [Chart values reference](#chart-values-reference)
  - [5. Load regulations into the graph](#5-load-regulations-into-the-graph)
  - [6. Install the Policy System plugin](#6-install-the-policy-system-plugin)
  - [7. Ask a question](#7-ask-a-question)
  - [Troubleshooting (Local Test)](#troubleshooting-local-test)
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

---

## Local Test

> [!IMPORTANT]
> **This section describes the target experience, and most of it does not work yet.**
>
> It is written ahead of the implementation deliberately, so that the gaps between
> "what we intend" and "what exists" are visible and trackable rather than discovered
> by the first person who tries it. Each step below carries its real status.
>
> **This path is for evaluators** trying Policy System on their own laptop via a
> Helm chart on a local `kind` cluster. **Contributors** building or testing the
> codebase itself should use [CONTRIBUTING.md](../../CONTRIBUTING.md) Option A or
> B instead — a devcontainer or local venv, with FalkorDB in a Podman container
> and Claude Desktop wired to a locally-spawned MCP server. That path works now.

The same Helm chart is also intended to serve **production administrators**
deploying to a real Azure/AWS/on-prem cluster later, with a different values
profile — see [#59](https://github.com/mindovermachine-dev/policy-system/issues/59).
This walkthrough covers the local-test profile only; a production rollout guide
does not exist yet.

### Status of this path

| Step                        | Status                                                            | Tracking                                                                                                                                                                                                              |
| --------------------------- | ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Install Claude Desktop   | ✅ Works                                                          | —                                                                                                                                                                                                                     |
| 2. Install Podman           | ✅ Works                                                          | —                                                                                                                                                                                                                     |
| 3. Create the local cluster | ✅ Works                                                          | —                                                                                                                                                                                                                     |
| 4. Deploy Policy System     | ✅ Works                                                          | —                                                                                                                                                                                                                     |
| 5. Load regulations         | ✅ Works                                                          | —                                                                                                                                                                                                                     |
| 6. Install the plugin       | ⚠️ Plugin built, structurally verified (JSON-checked, content-reconciled); end-to-end install against a real Claude Desktop/Code instance has **not** been run | [#53](https://github.com/mindovermachine-dev/policy-system/issues/53) ← [#39](https://github.com/mindovermachine-dev/policy-system/issues/39) ← [#67](https://github.com/mindovermachine-dev/policy-system/issues/67) |
| 7. Ask a question           | ⚠️ Runbook below is human-run only — not verified end-to-end in this environment | —                                                                                                                                                                                                                     |

### Prerequisites

| Tool                                                                 | Why                                         | Install                       |
| --------------------------------------------------------------------- | -------------------------------------------- | ------------------------------ |
| [Claude Desktop](https://claude.com/download)                        | Hosts the Policy System plugin              | Download for macOS or Windows |
| [Podman](https://podman.io/docs/installation)                        | Container runtime backing the local cluster | `brew install podman` (macOS) |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | Runs a Kubernetes cluster on Podman         | `brew install kind`           |
| [kubectl](https://kubernetes.io/docs/tasks/tools/)                   | Talks to the cluster                        | `brew install kubectl`        |
| [Helm](https://helm.sh/docs/intro/install/)                          | Installs the Policy System chart            | `brew install helm`           |

No LLM provider is required to evaluate the system. Restoring curated content and asking
questions both work without one. You only need provider credentials to ingest a regulation
outside the curated catalog — see
[CONTRIBUTING.md](../../CONTRIBUTING.md#configure-the-llm-interface) for options, including
Ollama for a fully local, no-cost setup.

### 1. Install Claude Desktop

Download and sign in. No Policy System configuration is needed yet.

### 2. Install Podman and start its machine

```bash
podman machine init --cpus 4 --memory 8192
podman machine start
podman info    # confirm the machine is running
```

kind under Podman needs a machine with enough headroom to run a control plane plus both
Policy System containers. 4 CPUs / 8 GB is the tested floor.

### 3. Create the local cluster

```bash
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

### 4. Deploy Policy System

> [!IMPORTANT]
> **If you're using the default `llm.provider=ollama` and Ollama runs on your Podman
> host** (not in-cluster), PS Service's pods cannot resolve `host.containers.internal`
> on their own — Podman only injects that hostname into the kind node container's own
> `/etc/hosts`, not into a Pod's separate network namespace. Look up your Podman
> network's gateway IP and pass it along:
>
> ```bash
> podman network inspect podman --format '{{(index .Subnets 0).Gateway}}'
> # commonly 10.88.0.1 on a default rootful install
> ```
>
> ```bash
> helm install policy-system ./charts/policy-system \
>   --set llm.provider=ollama \
>   --set psService.ollamaHostGatewayIP=10.88.0.1 \
>   --wait
> ```
>
> Without `psService.ollamaHostGatewayIP` set, `/ready` will likely never turn healthy
> under the Ollama provider, since PS Service can't reach the LLM.

```bash
helm install policy-system ./charts/policy-system \
  --set llm.provider=ollama \
  --wait

kubectl get pods    # ps-service and falkordb should reach Running
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

`/health` reports process liveness; `/ready` additionally reports that every dependency
— FalkorDB, the configured LLM provider — is currently reachable. Wait for `/ready`
before continuing.

The chart pulls PS Service from `ghcr.io/mindovermachine-dev/ps-service`; pin a release
tag rather than tracking `latest` when you want a local test you can reproduce later.

LLM provider credentials, when you need them, are supplied as chart values backed by a
Kubernetes secret, never baked into the image.

### Chart values reference

Every operator-facing key in `charts/policy-system/values.yaml` (local-test default) and
`charts/policy-system/values-prod.yaml` (production override file, passed via
`-f values-prod.yaml`):

| Key | Default (local-test) | Purpose |
| --- | --- | --- |
| `psService.image.repository` | `ghcr.io/mindovermachine-dev/ps-service` | PS Service image. |
| `psService.image.tag` | `"0.2.0"` | PS Service image tag — pin a release, don't track `latest`. |
| `psService.service.type` | `NodePort` (`ClusterIP` in prod) | PS Service Service type. `NodePort` is what `deploy/kind/cluster.yaml`'s `extraPortMappings` targets locally; prod has no kind-specific reachability mechanism, so it's `ClusterIP`-only there. |
| `psService.service.nodePort` | `30800` | Fixed NodePort behind host port `8000` (via `extraPortMappings`). Not set in prod (no `nodePort` field when `type: ClusterIP`). |
| `psService.companyMerge.similarityThreshold` | `0.85` | `PS_COMPANYMERGE_SIMILARITY_THRESHOLD` — fuzzy-match threshold for company entity merging. |
| `psService.localTestBypass.enabled` | `false` | `PS_SERVICE_LOCAL_TEST_BYPASS` — opt-in auth bypass for local evaluation. Off by default even under the local-test profile; an evaluator flips it explicitly to use the plugin path (step 6) without OIDC. |
| `psService.ollamaHostGatewayIP` | `""` | IP of the Podman network gateway, used to render a `hostAliases` entry so pods can resolve `host.containers.internal` when `llm.provider=ollama`. Empty by default — the chart can't know this statically. See the callout above. |
| **`llm.provider`** | `ollama` (`azure` in prod) | **(AC-BI-003)** Selects the LLM backend: `ollama` or `azure`. Drives `PS_LLMINTERFACE_MODEL`/`PS_LLMINTERFACE_EMBED_MODEL` and whether a Secret renders. |
| **`llm.existingSecret`** | `""` | **(AC-BI-003)** Set to reuse an operator-managed Secret name instead of `llm.azure.*` below. |
| **`llm.azure.apiKey`** | `""` | **(AC-BI-003)** Azure API key. Never set a real value here in a committed file — pass via `--set` or use `llm.existingSecret`. Rendered into a Kubernetes `Secret` (`templates/secret.yaml`), never a ConfigMap or plaintext env var. |
| **`llm.azure.apiBase`** | `""` | **(AC-BI-003)** Azure API base URL. Same secret-backed handling as `apiKey`. |
| `llm.ollama.apiBase` | `"http://host.containers.internal:11434"` | `OLLAMA_API_BASE` — set only when `llm.provider=ollama` and non-empty. |
| **`falkordb.persistence.enabled`** | `true` (both profiles) | **(AC-BI-008)** Toggles FalkorDB storage between a `PersistentVolumeClaim` (default) and an `emptyDir` (ephemeral — data lost on pod restart). No manual manifest edits needed — flip via `--set`/`-f` and `helm upgrade`. |
| `falkordb.persistence.storageClassName` | `""` | Empty string = let the cluster pick its own default StorageClass. Never hardcoded to kind's default StorageClass name (both profiles) — override explicitly for a real cluster if needed. |
| `falkordb.browser.enabled` | `true` (`false` in prod) | **(AC-BI-009)** FalkorDB Browser UI Service. On by default for local-test convenience, off in prod. |
| `falkordb.browser.nodePort` | `30300` | Fixed NodePort behind host port `3000` (via `extraPortMappings`). Only applies when `falkordb.browser.enabled=true`. |
| `falkordb.image.repository` / `falkordb.image.tag` | `falkordb/falkordb` / `latest` | FalkorDB image. |

Immediately after installing, `falkordb.persistence.enabled` (on by default — data
survives a pod restart) and the `llm.*` keys (which provider, and how its credentials
reach the pod) are the two settings worth double-checking against your intended setup.
Persistent storage means the PVC needs a StorageClass available in your cluster; a
default `kind` cluster provisions one automatically, so this works out of the box locally
too. See [Operations: Backup & Restore](#operations-backup--restore) for backing up that
volume once persistence is on.

#### Example: `helm upgrade`

```bash
# Example: disable persistent storage for a disposable evaluation run
helm upgrade policy-system ./charts/policy-system \
  --set llm.provider=ollama \
  --set falkordb.persistence.enabled=false \
  --wait
```

PS Service's Deployment is untouched by this upgrade — only FalkorDB's
Deployment/PVC change — so an in-flight PS Service pod is not restarted just because
you changed a FalkorDB-only value.

### 5. Load regulations into the graph

A freshly deployed system has an empty graph and can answer nothing. Seed it:

```bash
ps-cli catalog list                    # curated instruments available to restore (id, title, source_type/jurisdiction)
ps-cli catalog restore <instrument_id> # e.g. `ps-cli catalog restore CRA-1.0` — the id `catalog list` just printed
ps-cli regulations list                # confirm what landed (CELEX + title)
```

`catalog restore` takes the instrument's id as a positional argument, not a `--instrument`
flag. Curated instruments are pre-ingested graphs published in the `curated-content` git
folder and restored as a data load, seconds rather than minutes, with no LLM provider or
extraction run. Regulations outside the curated set are still ingested live with
`ps-cli regulations ingest <celex>`, which does require a configured LLM provider.

If `ps-cli` was installed via `uv tool install` with no local checkout, fetch
`curated-content/` before running `catalog list`/`catalog restore` — e.g.
`git clone --depth 1 --filter=blob:none --sparse https://github.com/mindovermachine-dev/policy-system && cd policy-system && git sparse-checkout set curated-content`,
then either run `ps-cli` from inside that clone or set `PS_CLI_CURATED_REPO_PATH` to its
`curated-content` path.

Until something is seeded, the system answers questions with an explicit "graph is unseeded"
error rather than an empty result.

> ℹ️ **Curated content ships as an empty scaffold today.** The `curated-content/` folder,
> its export/restore mechanism, `ps-cli`'s `catalog list`/`catalog restore` commands, and
> the unseeded-graph error above are all implemented ([#66](https://github.com/mindovermachine-dev/policy-system/issues/66)) — verified end to end against a real PS Service/FalkorDB with no LLM
> provider configured, including a real question answered correctly over the MCP transport
> against restored content. What is **not** yet done is populating `curated-content/` with
> real per-instrument artifacts for CRA/GDPR/NIS2 and the internal Engineering Practices
> baseline — a one-time project-maintainer step, run once per instrument via
> `tools/curated-export/export_instrument.py` against an already-ingested source, tracked
> separately from this issue. Until that lands, `catalog list` returns no instruments on a
> plain checkout of this repository.

### 6. Install the Policy System plugin

The plugin lives in this repo at `ps-skills/policy-system/` — a `ps-qna` skill plus a
bundled MCP connector (`.mcp.json`), installable via the repo-root marketplace manifest
`.claude-plugin/marketplace.json`.

In Claude Desktop: **+** next to the prompt box → **Plugins** → **Add plugin**, then add
this repo (`https://github.com/mindovermachine-dev/policy-system`) as a marketplace and
install `policy-system`.

One install brings both the `ps-qna` skill and its MCP connector — the skill arrives
already wired to the transport it needs, with no separate connector setup and no
credential pasting. Claude Desktop prompts for the plugin's two `userConfig` fields
(defined in `ps-skills/policy-system/.claude-plugin/plugin.json`):

| Field | Required | Purpose |
| --- | --- | --- |
| `ps_service_url` | Yes | Full URL to PS Service's MCP endpoint. Defaults to `http://localhost:8000/mcp` — correct for this walkthrough's local-test deployment unchanged. |
| `operator_token` | No | Individual-operator bearer credential, forwarded as `Authorization: Bearer <token>`. Leave blank for a local-test deployment. |

Point the connector at your local deployment when prompted for the endpoint URL (or accept
the default, which already matches):

```text
http://localhost:8000/mcp
```

A local-test deployment runs with authentication disabled, so there is nothing to log into.
That mode is opt-in via `PS_SERVICE_LOCAL_TEST_BYPASS=true`, refuses to bind anything but
loopback, and warns on every startup — it is for evaluation only.

> ⚠️ **Built, not end-to-end verified.** The plugin's files (`plugin.json`, `.mcp.json`,
> `skills/ps-qna/SKILL.md`, the marketplace manifest) exist and are structurally valid —
> JSON-checked and content-reconciled against the corrected design in
> `.orchestrator/tracker/issue-53-ps-qna-plugin/CHANGES.md` — but installing it against a
> real Claude Desktop/Code instance has **not** been run by any automated process; this
> development sandbox has no Claude Desktop plugin-install surface to run it against. See
> [7. Ask a question](#7-ask-a-question) below for the human-run smoke-test runbook. Remote
> MCP transport with per-user authentication is tracked separately on
> [#39](https://github.com/mindovermachine-dev/policy-system/issues/39). The local-test
> bypass mode described above is implemented
> ([#67](https://github.com/mindovermachine-dev/policy-system/issues/67)) — set
> `PS_SERVICE_LOCAL_TEST_BYPASS=true` on a loopback-bound instance to run under it. Full
> per-user authentication remains required for any non-local deployment; it is deferred,
> not dropped, and stays tracked on
> [#39](https://github.com/mindovermachine-dev/policy-system/issues/39)/[#58](https://github.com/mindovermachine-dev/policy-system/issues/58)
> (not [#65](https://github.com/mindovermachine-dev/policy-system/issues/65), which is only
> the now-deprioritized credential-flow spike). **Known limitation relevant to this step:**
> `operator_token` is accepted and forwarded, but PS Service does not yet validate it — a
> human running this step should not expect real per-user auth enforcement, only that a
> local-test-bypass deployment answers questions with the field left blank.

### 7. Ask a question

```text
What obligations does the Cyber Resilience Act place on manufacturers,
and which of our policies cover them?
```

The skill grounds itself against the domain model, writes read-only Cypher, retrieves
from the graph, and constructs an answer that cites what it retrieved. If the graph
cannot answer, it says so rather than filling the gap from model recall.

If the skill does not engage on its own, ask for it by name: _"Use the ps-qna skill."_

#### Smoke-test runbook (human-run)

> ⚠️ **This runbook is human-run, not automated.** It has not been executed end-to-end by
> a sub-agent or the orchestrator in this development environment — there is no
> `kind`/`kubectl`/`podman`/live Claude Desktop plugin-install surface in this sandbox
> (the same constraint already documented for
> [#59](https://github.com/mindovermachine-dev/policy-system/issues/59)'s Helm chart). This
> is a deliberate, honest scoping decision, not an oversight: what has been verified is
> that steps 1-5 above already work (populating a graph is achievable today), and that the
> plugin's files are structurally valid; what has **not** been verified is a live
> Claude Desktop turn against a real deployment.

To actually run the smoke test:

1. Complete steps 1-6 above against a real machine: a working Claude Desktop install, a
   local `kind` cluster with Policy System deployed, and CRA content seeded via
   `ps-cli catalog restore CRA-1.0` (step 5 — already proven to work).
2. Install the `policy-system` plugin per step 6, pointing `ps_service_url` at
   `http://localhost:8000/mcp` (the default) and leaving `operator_token` blank, since the
   local-test deployment runs under `PS_SERVICE_LOCAL_TEST_BYPASS=true` with no credential
   validation.
3. In Claude Desktop, ask exactly the question from step 7 above: _"What obligations does
   the Cyber Resilience Act place on manufacturers, and which of our policies cover
   them?"_ — reusing the CRA content step 5 already seeded, no new fixture data needed.
4. Confirm the `ps-qna` skill engages (automatically, or by asking for it by name), that it
   issues a Cypher query over the `policy-system-graph` MCP connector rather than answering
   from model recall, and that the answer cites what it retrieved from the graph (e.g.
   `source_ref`s pointing at the restored CRA content).
5. If it fails, check [Troubleshooting (Local Test)](#troubleshooting-local-test) below —
   in particular "Plugin installed but no cypher tool" for connector/endpoint issues.

**Do not expect real per-user authentication enforcement during this smoke test.**
`operator_token` exists in `plugin.json` and is forwarded as a bearer header by
`.mcp.json`, but PS Service does not yet validate it server-side (tracked on
[#39](https://github.com/mindovermachine-dev/policy-system/issues/39) Group 3 /
[#58](https://github.com/mindovermachine-dev/policy-system/issues/58)) — a passing smoke
test here demonstrates the plugin/connector/skill mechanics work, not that per-user auth is
enforced.

### Troubleshooting (Local Test)

| Symptom                                | Check                                                                                                                                            |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Service unreachable                    | `ps-cli health` — reports reachability, health, readiness, and which dependency (if any) is unhealthy                                            |
| Pods stuck `Pending`                   | `podman machine` sizing — the control plane plus both containers need ~8 GB                                                                      |
| `/ready` returns unhealthy             | `kubectl logs deploy/ps-service` — usually FalkorDB or an LLM provider is unreachable                                                            |
| Answers say "not present in the graph" | Step 5 — the graph is probably empty; run `ps-cli regulations list`                                                                                  |
| Plugin installed but no cypher tool    | The MCP connector's endpoint URL, and whether `/ready` is green                                                                                  |

---

## Policy System plugin (ps-qna)

> ⚠️ **Built, structurally verified; end-to-end install unverified in this environment.**
> The plugin lives at `ps-skills/policy-system/` (skill: `skills/ps-qna/SKILL.md`;
> connector: `.mcp.json`; metadata and `userConfig`:
> `.claude-plugin/plugin.json`), installable via the repo-root marketplace manifest
> `.claude-plugin/marketplace.json` ([#53](https://github.com/mindovermachine-dev/policy-system/issues/53)).
> Its files are JSON-checked and content-reconciled, but no automated process in this
> development sandbox can install it into a real Claude Desktop/Code instance to confirm
> it end-to-end — see [6. Install the Policy System plugin](#6-install-the-policy-system-plugin)
> and its [smoke-test runbook](#smoke-test-runbook-human-run) for the accurate status and
> the human-run verification steps. Remote MCP transport with per-user authentication
> remains tracked separately on [#39](https://github.com/mindovermachine-dev/policy-system/issues/39).

Installing the plugin in Claude Desktop, pointing its MCP connector (`policy-system-graph`)
at a Policy System deployment via the `ps_service_url` userConfig field, and asking
compliance questions grounded in the knowledge graph are all covered in
[Local Test](#local-test) steps 6-7 above — that is the canonical walkthrough; this section
just points at it rather than duplicating it.

---

## ps-cli

`ps-cli` is a command-line client for PS Service's REST API: select and ingest EU
regulations from Cellar/ELI, ingest internal policies, and check service health and
readiness.

### Install

`ps-cli` is a distributed client, installable independently of this repo like `gh`/`az`
— no clone/checkout needed:

```bash
uv tool install "git+https://github.com/mindovermachine-dev/policy-system@ps-cli-v0.1.1#subdirectory=ps-cli"
```

`uv` builds the wheel from the tagged git ref and puts `ps-cli` on `PATH` via its
tool-install shims. Verify:

```bash
ps-cli --version
```

Replace `ps-cli-v0.1.1` with the latest `ps-cli-v*` tag (`git ls-remote --tags
https://github.com/mindovermachine-dev/policy-system 'ps-cli-v*'`). Tags on this
repository are not currently protected against force-move/re-pointing. If you need
install-time integrity beyond "trust the tag," pin the exact commit SHA the tag points
at instead:

```bash
uv tool install "git+https://github.com/mindovermachine-dev/policy-system@<commit-sha>#subdirectory=ps-cli"
```

### Configuring which PS Service instance ps-cli targets

#### Single target (default)

Out of the box, `ps-cli` targets `http://127.0.0.1:8000`, matching PS Service's own
default. Point it elsewhere with the `PS_CLI_SERVICE_URL` env var, or a `ps-cli.toml`
(`service_url = "..."`) in your current directory:

```bash
PS_CLI_SERVICE_URL=http://127.0.0.1:9000 ps-cli regulations list
```

This is all you need for a single environment. It keeps working unchanged even after
you start using named contexts below — `ps-cli` only looks for a context configuration
if one exists.

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
| `ps-cli health` | — | Report reachability, health (`/health`), and readiness (`/ready`) for the configured target, naming any unhealthy dependency. |
| `ps-cli regulations list` | — | List the curated EU-regulation catalog (CELEX + title). No FalkorDB/LLM dependency. |
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
(the default in both the local-test and production profiles — see
[Chart values reference](#chart-values-reference)). With persistence on, back up and
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
| A command fails right after connecting | `ps-cli health` — reports whether FalkorDB, the LLM Interface, and Cellar/ELI are all reachable, and which is not if any aren't. `health: alive` with `ready: not_ready` means the process is up but a dependency, or required config, isn't. |
| `regulations ingest` / `internal ingest` fails immediately with a config-related error | PS Service's ingestion-required config (`PS_LLMINTERFACE_MODEL`, `PS_LLMINTERFACE_EMBED_MODEL`, `PS_COMPANYMERGE_SIMILARITY_THRESHOLD`) is likely missing — this is a PS Service operator/deployer concern, see [CONTRIBUTING.md](../../CONTRIBUTING.md#configure-the-llm-interface). |
| Referencing a context that doesn't exist | `ps-cli` exits non-zero and lists valid context names — see [ps-cli Troubleshooting](#troubleshooting). |

## Glossary

Entities, relationships, and vocabulary used throughout this guide (Regulatory
Instrument, Obligation, Capability, Control, ...) are defined in
[`docs/artifacts/ps-domain-concepts.md`](./ps-domain-concepts.md).
