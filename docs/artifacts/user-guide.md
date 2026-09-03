# Policy System User Guide

This guide is for people **using** Policy System — asking compliance questions,
ingesting regulations or internal policies, or administering an instance. If you
want an overview of the project or its architecture, see [README.md](../../README.md).
If you want to build, test, or release the project, see
[CONTRIBUTING.md](../../CONTRIBUTING.md).

Policy System has three clients. Which section you need depends on what you're
doing, not your job title — see README's
[Target audiences](../../README.md#target-audiences) table if you want the
role-oriented view.

| I want to... | Use | Status |
| --- | --- | --- |
| Ask a compliance question in natural language | [Policy System plugin](#policy-system-plugin-ps-qna) | ❌ Not yet available |
| Ingest a regulation or internal policy, check service health, administer an instance | [ps-cli](#ps-cli) | ✅ Available |
| Author Policies, Standards, and Controls | Policy Editor | ❌ Not yet designed |

---

## Policy System plugin (ps-qna)

> ❌ **Not yet available.** The plugin itself does not exist yet
> ([#53](https://github.com/mindovermachine-dev/policy-system/issues/53)), and needs a
> remote MCP transport with per-user authentication
> ([#39](https://github.com/mindovermachine-dev/policy-system/issues/39)). This section
> will be filled in once those land — see README's
> [Getting started for local testing](../../README.md#getting-started-for-local-testing)
> for the target experience and current status of each step.

When available, this will cover: installing the plugin in Claude Desktop, pointing its
MCP connector at a Policy System deployment, and asking compliance questions
grounded in the knowledge graph.

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
| `ps-cli regulations list` | — | List the curated EU-regulation catalog (CELEX + title). No FalkorDB/LLM dependency. |
| `ps-cli regulations ingest <celex>` | `celex` — 10-character CELEX identifier (e.g. `32016R0679`) | Ingest a regulation through the full pipeline. |
| `ps-cli internal ingest <fixture_path>` | `fixture_path` — a `.json` path, resolved on PS Service's fixtures root, not read locally | Ingest an internal policy document. |
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

> ❌ **`ps-cli health` doesn't exist yet.** There's no single command that reports
> reachability/health/readiness for your current target — until it ships
> ([#68](https://github.com/mindovermachine-dev/policy-system/issues/68)), check
> `/health` and `/ready` directly (below).

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

## Troubleshooting / FAQ

| Symptom | Check |
| --- | --- |
| `ps-cli` reports "Could not reach PS Service" | Is the target URL right (`ps-cli config list-contexts` / `echo $PS_CLI_SERVICE_URL`)? Is PS Service actually running there? |
| A command fails right after connecting | `curl <target>/ready` — reports whether FalkorDB, the LLM Interface, and Cellar/ELI are all reachable, and which is not if any aren't. `/health` (liveness only) succeeding while `/ready` fails means the process is up but a dependency, or required config, isn't. |
| `regulations ingest` / `internal ingest` fails immediately with a config-related error | PS Service's ingestion-required config (`PS_LLMINTERFACE_MODEL`, `PS_LLMINTERFACE_EMBED_MODEL`, `PS_COMPANYMERGE_SIMILARITY_THRESHOLD`) is likely missing — this is a PS Service operator/deployer concern, see [CONTRIBUTING.md](../../CONTRIBUTING.md#configure-the-llm-interface). |
| I want a single command instead of curling `/health`/`/ready` | Not available yet — [#68](https://github.com/mindovermachine-dev/policy-system/issues/68). |
| Referencing a context that doesn't exist | `ps-cli` exits non-zero and lists valid context names — see [ps-cli Troubleshooting](#troubleshooting). |

## Glossary

Entities, relationships, and vocabulary used throughout this guide (Regulatory
Instrument, Obligation, Capability, Control, ...) are defined in
[`docs/artifacts/ps-domain-concepts.md`](./ps-domain-concepts.md).
