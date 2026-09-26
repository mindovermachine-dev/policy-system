# Policy System User Guide

## Table of Contents

- [Using ps-cli](#using-ps-cli)
  - [Install ps-cli](#install-ps-cli)
  - [Point ps-cli at your instance](#point-ps-cli-at-your-instance)
  - [Load curated content](#load-curated-content)
  - [Find and ingest a regulation from EUR-Lex](#find-and-ingest-a-regulation-from-eur-lex)
- [Using Claude Desktop](#using-claude-desktop)
  - [Ask a question](#ask-a-question)
- [Glossary](#glossary)
- [Appendix: ps-cli reference](#appendix-ps-cli-reference)
  - [Command reference](#command-reference)
  - [Configuring which PS Service instance ps-cli targets](#configuring-which-ps-service-instance-ps-cli-targets)
    - [Single target (default)](#single-target-default)
    - [Multiple named targets (contexts)](#multiple-named-targets-contexts)
    - [Credential storage](#credential-storage)
  - [Running commands](#running-commands)
  - [Troubleshooting](#troubleshooting)
  - [Uninstalling](#uninstalling)
  - [Configuration reference](#configuration-reference)

This guide is for people **using** an already-deployed Policy System instance —
asking compliance questions and ingesting regulations through Claude Desktop, and
ingesting internal policies or administering an instance through `ps-cli`. If you want an
overview of the project see [README.md](../../README.md). If you want to build,
test, or release the project, see [CONTRIBUTING.md](../../CONTRIBUTING.md). To
deploy a new instance, see the [Installation Guide](./installation-guide.md); to
upgrade, rotate credentials, back up, or tear down an instance, see the
[Operations Guide](./operations-guide.md).

| I want to... | Use |
| --- | --- |
| Ingest an internal policy, check service health, administer an instance | [Using ps-cli](#using-ps-cli) |
| Ask a compliance question in natural language, or ingest a regulation by CELEX | [Using Claude Desktop](#using-claude-desktop) |

---

## Using ps-cli

`ps-cli` is a command-line client for Policy System. This section walks through the
flow a new user follows: install it, point it at an instance, load some content, and
ingest a regulation. For the full command surface and advanced configuration, see the
[Appendix](#appendix-ps-cli-reference).

### Install ps-cli

```bash
curl -fsSL https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/ps-cli/install.sh | bash
```

```bash
ps-cli --version
```

See [Installation Guide: Install ps-cli](./installation-guide.md#6-install-ps-cli)
for what this does (checksum-verified release install via `uv`, no clone needed) and
its prerequisites.

### Point ps-cli at your instance

**Evaluator / local-test instance**: nothing to do. Out of the box, `ps-cli` targets
`http://127.0.0.1:8000`, matching what a local `kind` deployment (see [Installation
Guide: Evaluator installation](./installation-guide.md#evaluator-installation))
listens on, and no login is required against it.

**Production instance**: first find the URL — whoever ran `scripts/deploy-ps.sh` has
it; it's printed at the end of that run (`PS Service: https://<label>.<region>.cloudapp.azure.com`).

Forgot it? `scripts/deploy-ps.sh` is idempotent — re-running it makes no changes if
nothing's different, and it reprints the URL unconditionally, even on that no-op run.
Then:

```bash
ps-cli config set-context prod --url https://<label>.<region>.cloudapp.azure.com
```

```bash
ps-cli config use-context prod
```

```bash
ps-cli auth login
```

`auth login` runs an OIDC device-authorization flow: `ps-cli` prints a verification
URL and code, you complete sign-in in a browser, and the resulting token is stored
under the current context. Production instances are deployed with Entra auth wired
in, so this step is required there.

Every subsequent `ps-cli` command, and the Claude Desktop plugin's
`policy-system-graph` connector, uses whichever context is current. See the
[Appendix](#configuring-which-ps-service-instance-ps-cli-targets) for switching
between multiple environments, per-command overrides, and credential storage.

### Load curated content

The `ps-get-catalog-listing` skill (see [Using Claude Desktop](#using-claude-desktop))
lists every curated instrument PS Service's configured curated-content source
currently serves — external and internal, no local checkout needed. Ask Claude to
use it, e.g. _"Use the ps-get-catalog-listing skill to list the curated catalog."_

Restore one from its pre-ingested artifact — faster than a full ingestion pipeline
run — using the id the listing printed. The `ps-restore-instrument` skill fetches
the artifact and restores it, given just the `instrument_id`: _"Use the
ps-restore-instrument skill to restore CRA-1.0."_ This loads whichever curated
instrument — external or internal, e.g. an Engineering Practices standard — is
registered under that id. A freshly deployed instance has an empty graph and
answers nothing until something is restored or ingested.

### Find and ingest a regulation from EUR-Lex

Not every regulation is curated. Ingesting one by CELEX identifier — curated or
not — is done through the `ps-ingest-regulation` skill (see [Using Claude
Desktop](#using-claude-desktop)), not `ps-cli`: ask Claude to ingest the
regulation, e.g. _"Use the ps-ingest-regulation skill to ingest 32016R0679 as
gdpr."_ The skill always asks for both the CELEX identifier and a `short_name`,
even for a curated regulation — never guess `short_name` on the user's behalf.

To find a regulation's CELEX identifier:

1. Go to [eur-lex.europa.eu](https://eur-lex.europa.eu) and search by title or
   keyword (e.g. "general data protection regulation").
2. Open the regulation's page and switch to its **Document information** tab.
3. Read off the **CELEX number** — a 10-character code such as `32016R0679`.

If the CELEX is outside the curated set, the skill resolves it against
Cellar/ELI — the EU's public document repository — directly.

The skill runs the full pipeline — minutes, not seconds (a full CRA ingestion
has measured ~10 minutes end to end) — and needs the target PS Service
instance's FalkorDB and LLM interface configured. It reports the resolved
`regulatory_instrument_id` and each pipeline stage's outcome, or a specific
named error if something fails.

---

## Using Claude Desktop

Once the Policy System plugin is installed (see [Installation Guide: Install the
Policy System plugin](./installation-guide.md#8-install-the-policy-system-plugin)),
you can ask compliance questions directly in a Claude Desktop chat. The plugin's
`policy-system-graph` MCP connector reaches whichever PS Service instance `ps-cli`'s
current context points at (see [Point ps-cli at your
instance](#point-ps-cli-at-your-instance)).

### Ask a question

```text
What obligations does the Cyber Resilience Act place on manufacturers,
and which of our policies cover them?
```

The `ps-qna` skill grounds itself against the domain model, writes read-only Cypher,
retrieves from the graph, and constructs an answer that cites what it retrieved. If
the graph cannot answer, it says so rather than filling the gap from model recall.

If the skill does not engage on its own, ask for it by name: _"Use the ps-qna skill."_

---

## Glossary

Entities, relationships, and vocabulary used throughout this guide (Regulatory
Instrument, Obligation, Capability, Control, ...) are defined in
[`docs/artifacts/ps-domain-concepts.md`](./ps-domain-concepts.md).

---

## Appendix: ps-cli reference

The full command surface and configuration options for `ps-cli`. Most day-to-day
usage only needs the flow in [Using ps-cli](#using-ps-cli) above.

### Command reference

Global flags, usable before or after any subcommand:

| Flag | Description |
| --- | --- |
| `-v`, `--verbose` | Print the failure site (file:line) on error. |
| `--context <name>` | Use this named context's PS Service URL for this invocation only. Never persisted. |
| `--version` | Print PS-CLI client and PS Service versions and exit. |

| Command | Arguments | Description |
| --- | --- | --- |
| `ps-cli get health` | — | Report reachability, health (`/health`), and readiness (`/ready`) for the configured target, naming any unhealthy dependency; readiness reflects FalkorDB only — an unhealthy LLM Interface/Cellar-ELI is still named when present, but does not by itself make the target unready. |
| `ps-cli ingest document <document_path>` | `document_path` — a local `.json` file path; `ps-cli` reads it from your own machine and sends its content | Ingest an internal policy document. |
| `ps-cli export instrument <instrument_id> [destination]` | `instrument_id` — the already-ingested instrument's id (e.g. `CRA-1.0`); `destination` — optional local directory, defaults to the current directory | Export an already-ingested instrument's baseline/native/manifest files to a local destination. |
| `ps-cli auth login` | — | Log in to the current context via OIDC device-flow (see [Credential storage](#credential-storage)). |
| `ps-cli auth status` | — | Show the current context's login status (context, issuer) — reads the local store only, no network call. |
| `ps-cli auth logout` | — | Remove the current context's stored credential. |
| `ps-cli config set-context <name> --url <url>` | `name`, `--url` (required) | Create or update a named context's PS Service URL. Clears any credential previously stored for that name. |
| `ps-cli config use-context <name>` | `name` | Select the named context every subsequent command uses. |
| `ps-cli config get-contexts` | — | List every named context, marking the current one. |

Run `ps-cli --help` or `ps-cli <command> --help` for the same reference from the CLI
itself.

### Configuring which PS Service instance ps-cli targets

#### Single target (default)

Out of the box, `ps-cli` targets `http://127.0.0.1:8000`, matching PS Service's own
default. Point it elsewhere with the `PS_CLI_SERVICE_URL` env var, or a `ps-cli.toml`
(`service_url = "..."`) in your current directory:

#### Multiple named targets (contexts)

If you regularly switch between environments — dev, test, prod — `ps-cli` supports
named contexts, the way `kubectl` has contexts or `az` has subscriptions (in fact the
whole CLI, not just contexts, follows kubectl's `<verb> <resource>` pattern — see
[Command reference](#command-reference) above).

```bash
ps-cli config set-context dev --url https://dev.example.com
```

```bash
ps-cli config set-context prod --url https://prod.example.com
```

```bash
ps-cli config use-context prod
```

```bash
ps-cli config get-contexts
```

Once a context is current, every command uses it:

```bash
ps-cli get health
```

Targets prod.

**Override for a single command** with `--context`, without changing what's current:

```bash
ps-cli --context dev get health
```

Targets dev, just this once.

```bash
ps-cli config get-contexts
```

Still shows prod as current.

**Resolution order** (highest wins): `PS_CLI_SERVICE_URL` env var > `--context` flag >
the current context in your config > the single-target fallback above.

Contexts are stored in `targets.toml` under `~/.config/ps-cli/` by default. Override
the location with `PS_CLI_CONFIG_DIR` (mirroring `gh`'s `GH_CONFIG_DIR`) if you want
an isolated config, e.g. for testing:

```bash
PS_CLI_CONFIG_DIR=/tmp/my-ps-cli-config ps-cli config get-contexts
```

`targets.toml` only ever holds context names and URLs — never a credential.

#### Credential storage

`ps-cli` stores credentials in your OS keyring only, keyed per context name. Only a
`refresh_token` and its `issuer` are ever persisted; the access token obtained from it
is never written to keyring or disk — it lives in memory for the current `ps-cli`
invocation only, refreshed exactly once per invocation, and is discarded when the
process exits. If the OS keyring is unavailable or a keyring operation fails for any
reason, `ps-cli` raises an actionable error naming the context and the failure type
(never the credential value) instead of falling back to writing a file.

Log in with `ps-cli auth login` (requires a named context — `config
set-context`/`use-context` first, since a stored credential is keyed per context
name). This runs an OIDC device-authorization flow ([#57](https://github.com/mindovermachine-dev/policy-system/issues/57)):
`ps-cli` prints a verification URL and code, you complete sign-in in a browser, and
the resulting token is stored under the current context. `ps-cli auth status` shows
whether you're logged in (context, issuer) without contacting anything; `ps-cli auth
logout` removes the stored credential.

Whether login is required at all depends on the target PS Service instance:
generic OIDC bearer-token validation against any OIDC-compliant provider (no
single vendor's IdP is assumed) is implemented server-side
([#58](https://github.com/mindovermachine-dev/policy-system/issues/58)), but a
given deployment only enforces it once configured with an issuer/audience — a
local-test deployment (see [Installation Guide: Evaluator
installation](./installation-guide.md#evaluator-installation)) runs with no auth
required at all, so every `ps-cli` command (and the plugin's `policy-system-graph`
connector — see [Using Claude Desktop](#using-claude-desktop)) works there with no
login needed. Re-running `config set-context` for an existing context name with a
new `--url` always clears any credential previously stored for that name, so nothing
is ever silently carried over to a new URL.

### Running commands

The curated catalog listing and restore-from-catalog are both `ps-cli`-external now
— reached via the `ps-get-catalog-listing`/`ps-restore-instrument` skills (see
[Load curated content](#load-curated-content)), not a `ps-cli` subcommand.

```bash
ps-cli ingest document <document_path>.json
```

Reads the file locally, sends its content. (Ingesting a regulation by CELEX is
done through the `ps-ingest-regulation` skill, not `ps-cli` — see [Find and
ingest a regulation from EUR-Lex](#find-and-ingest-a-regulation-from-eur-lex).)

`ingest document` exercises the full pipeline, so the PS Service instance you're
targeting needs FalkorDB and its LLM interface configured — check its `/ready`
endpoint first if a command fails unexpectedly (see
[Troubleshooting](#troubleshooting) below).

A few behaviors worth knowing about `export instrument`:

- `destination` defaults to your current working directory when omitted.
- The destination must already exist and be writable — `ps-cli` checks this before
  making any call to PS Service, so a bad path fails fast with an actionable error
  rather than after a wasted round trip.
- Re-running the command against the same destination overwrites `baseline.json` /
  `native.json` / `manifest.json` deterministically — there's no merge or append
  behavior to worry about.
- If the exported instrument is internal-source, `ps-cli` prints an explicit notice
  that the exported files may contain your organization's own confidential policy
  content.

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

Beyond that, PS Service's own health is what to check next — see the [Operations
Guide: Troubleshooting / FAQ](./operations-guide.md#troubleshooting--faq) for
`/health` vs `/ready`.

Note that `ready: ready` no longer implies the LLM Interface or Cellar/ELI are healthy —
readiness reflects FalkorDB only. An LLM Interface outage instead surfaces to
`ps-cli ingest document` (and to the `ps-ingest-regulation`/
`ps-check-regulations` skills) via that command's own pre-flight failure message
(`❌ LLM Interface is unavailable.` for `ps-cli`, `error: LLM Interface is unavailable.`
for the skills), before any pipeline call is made.

`ps-cli get health` reports all three — reachability, health, and readiness — in one call:

```
$ ps-cli get health
reachable: yes
health: alive
ready: ready
```

```
$ ps-cli get health
❌ PS Service is reachable but not ready (health='alive', ready='not_ready').
💡 unhealthy dependencies: falkordb
```

### Uninstalling

`ps-cli` is installed as a `uv` tool, so it's removed the same way:

```bash
uv tool uninstall ps-cli
```

This removes the `ps-cli` executable and its isolated environment, but leaves your
config and any stored credentials behind so a reinstall doesn't lose them. To remove
those too:

```bash
rm -rf ~/.config/ps-cli
```

Or `$PS_CLI_CONFIG_DIR`, if you set that instead.

This deletes `targets.toml` (your contexts), if it exists. Credentials are stored in
your OS keyring (see [Credential storage](#credential-storage) above), not under this
directory, so remove them per context separately — e.g. `keyring del ps-cli
<context-name>`, or via your OS's keychain/Credential Manager UI — since `uv tool
uninstall` has no visibility into the keyring.

If you created a project-local `ps-cli.toml` (see
[Single target (default)](#single-target-default) above), it isn't touched by any of
the above — delete it directly wherever you created it.

### Configuration reference

| Setting | Applies to | Default | Purpose |
| --- | --- | --- | --- |
| `PS_CLI_SERVICE_URL` (env var) | ps-cli | unset | Highest-precedence override for which PS Service instance ps-cli targets. |
| `ps-cli.toml` (`service_url`, in current directory) | ps-cli | none shipped | Project-local single-target override, lowest precedence. |
| `PS_CLI_CONFIG_DIR` (env var) | ps-cli | `~/.config/ps-cli/` | Where `targets.toml` is read/written. |
| `targets.toml` (`[contexts]`, `current_context`) | ps-cli | none until `config set-context` is run | Named PS Service targets and which one is current. Never contains a credential. |

This table covers `ps-cli` only. The Policy System plugin's `policy-system-graph`
connector needs no configuration of its own — it runs as a local `ps-cli-mcp-bridge`
process that reuses whichever `ps-cli` context is current, so every row above already
governs it too.

See the [Helm Chart Values Reference](./helm-chart-values-reference.md) for PS
Service / chart-level configuration.
