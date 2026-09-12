# Contributing

Thank you for considering contributing to this project!

Note: We are in the transition from prototype to full implementation and the instructions here are a mix of both. As the actual implementation progresses, prototype instructions will be removed.

## Table of Contents

- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
  - [Option A: Dev Container (recommended)](#option-a-dev-container-recommended)
  - [Option B: Local setup (no dev container)](#option-b-local-setup-no-dev-container)
  - [Create a virtual environment and install dependencies](#create-a-virtual-environment-and-install-dependencies)
  - [Start PS Service (process harness)](#start-ps-service-process-harness)
  - [Run PS Service as a container](#run-ps-service-as-a-container)
  - [Use ps-cli](#use-ps-cli)
    - [Run from a repo checkout (local development)](#run-from-a-repo-checkout-local-development)
  - [Configure the LLM Interface](#configure-the-llm-interface)
    - [Azure](#azure)
    - [Ollama (local, no cloud credentials/cost)](#ollama-local-no-cloud-credentialscost)
  - [Configure Company Merge](#configure-company-merge)
  - [MCP Streamable HTTP endpoint](#mcp-streamable-http-endpoint)
  - [Claude Desktop (alternative to Claude Code)](#claude-desktop-alternative-to-claude-code)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Releasing](#releasing)
  - [How the bump is decided](#how-the-bump-is-decided)
  - [What gets synced](#what-gets-synced)
  - [Push races self-heal](#push-races-self-heal)
  - [Verifying a release](#verifying-a-release)
  - [Prereleases](#prereleases)
  - [Manually re-running a release](#manually-re-running-a-release)
  - [ps-cli is released the same way](#ps-cli-is-released-the-same-way)
- [Delivery Process](#delivery-process)
- [Reporting Issues](#reporting-issues)
- [Discussions](#discussions)

## Getting Started

Contributions go through `devx-cafe/gh-tt`'s issue-branch workflow: pick up an
issue on a dedicated branch, then hand off to CI, which merges to `main` once
checks pass. No fork and no manual Pull Request in the common case.

1. Install the extension once:

   ```bash
   gh extension install devx-cafe/gh-tt
   ```

   On macOS, `gh tt` runs under whichever `python3` is first on `PATH`. The
   system interpreter at `/usr/bin/python3` is 3.9 and the extension needs
   3.10 or newer, so it fails with `TypeError: unsupported operand type(s)
for |`. Point it at a modern interpreter in your shell profile:

   ```bash
   export PYTHON=/opt/homebrew/bin/python3
   ```

2. Start work on an issue -- this creates and checks out an issue branch:

   ```bash
   gh tt workon -i <issue-number>
   # or, to create a new issue and start on it in one step:
   gh tt workon -t "<issue title>"
   ```

   The issue title must carry a conventional-commit type (`feat: ...`, `fix: ...`,
   etc. -- see [Releasing](#releasing) for the full allow-list): `gh tt deliver`
   (step 5) uses the issue title verbatim as the squashed `ready/*` commit's header,
   and that header is what the automated release job reads to classify the commit and
   decide whether/how to bump the version.

3. Make your changes. Run tests (see [Testing](#testing) below).

4. Commit and push your progress to the issue branch as you go:

   ```bash
   gh tt wrapup "<commit message>"
   ```

   Repeat steps 3-4 as many times as needed.

5. When the issue is done, squash the issue branch onto a `ready/*` branch
   and push it:

   ```bash
   gh tt deliver
   ```

   Pushing a `ready/*` branch triggers `on_ready.yml`: it runs the
   `trunk-worthy` check suite, then auto-merges to `main` -- no manual Pull
   Request needed for this path. Once merged, `ps-service` and `ps-cli`
   releases are cut automatically -- see [Releasing](#releasing) below.

After cloning, also run these once:

```bash
git config blame.ignoreRevsFile .git-blame-ignore-revs
uv run pre-commit install-hooks
```

Do not run `pre-commit install` — it refuses when `core.hooksPath` is set, which
this repo sets. `install-hooks` only prepares the hook environments; the hook
itself runs via `.githooks/pre-commit`.

## Development Setup

This is very early mostly exploration development. To load data into the graph database and ask questions, you will need FalkorDB and Python 3.14, set up either via the dev container (recommended) or locally.

### Option A: Dev Container (recommended)

Requires VSCode with the Dev Containers extension.

The devcontainer (`.devcontainer/`) is a Docker Compose setup with two
services:

- `app` — the dev/backend container VS Code attaches to.
- `falkordb` — the community-maintained `falkordb/falkordb:latest` image,
  started automatically alongside `app`.

Reopen the repo in the container (VS Code: "Reopen in Container") and both
services start together — no manual `podman run`/`docker run` step needed.

- `falkordb` has its own network namespace and is reachable from inside the
  dev container as **`falkordb:6379`**, not `localhost:6379`. The `app` service
  sets `PS_FALKORDB_HOST=falkordb` / `PS_FALKORDB_PORT=6379`, and every tool
  reads those variables (`tools/graph-ingestion`, `tools/graph-query`,
  `ps-service`), so no `--host` flag is needed in normal use. This matches the
  deployed topology, where ps-service resolves FalkorDB by hostname on a shared
  network — see ["Run PS Service as a container"](#run-ps-service-as-a-container).
  Ports `6379` (FalkorDB) and `3000` (FalkorDB Browser, at
  http://localhost:3000) are published on the `falkordb` service, so both stay
  reachable from your host machine as before.
- `falkordb` deliberately does **not** share `app`'s network namespace. It used
  to (`network_mode: service:app`), which made it a podman-level dependent of
  the dev container; VS Code's "Rebuild Container" removes that container with a
  bare `podman rm -f`, and podman refuses while a dependent exists, so every
  rebuild failed with `has dependent containers which must be removed before it`.
- `.falkordb-data/` at the repo root (bind-mounted to
  `/var/lib/falkordb/data`) persists the graph to disk, so recreating the
  containers doesn't lose data. `.falkordb-data/` is git-ignored; don't
  commit it.
- RDB snapshotting is on by default (`redis-cli config get save` →
  `3600 1 300 100 60 10000`) — the volume mount is what makes those snapshots
  durable, not an extra flag. AOF (`appendonly`) is off by default; turn it on
  only if you need tighter durability than periodic RDB snapshots for local
  work, e.g. `docker compose -f .devcontainer/docker-compose.yml exec falkordb redis-cli config set appendonly yes`.

Once the container is running, skip ahead to
["Create a virtual environment"](#create-a-virtual-environment-and-install-dependencies) below.

### Option B: Local setup (no dev container)

Requires:

- Podman
- FalkorDB
- Python 3.14

1. Install Podman
2. Set up a container with FalkorDB

```bash
mkdir -p .falkordb-data
podman run -d --name falkordb \
  -p 6379:6379 \
  -p 3000:3000 \
  -v $(pwd)/.falkordb-data:/var/lib/falkordb/data \
  falkordb/falkordb:latest
```

- `-p 3000:3000` exposes FalkorDB Browser (the graph-visualization UI bundled
  in the image) at http://localhost:3000. Without it, the browser still runs
  inside the container, but there's no way to reach it from the host.
- `-v $(pwd)/.falkordb-data:/var/lib/falkordb/data` persists the graph to disk
  at that path (FalkorDB's own `dir` config, confirmed via `redis-cli config
get dir`), so `podman stop`/`start`, or even removing and recreating the
  container, doesn't lose data. Without a volume mount, the graph lives only
  in the container's writable layer — gone the moment the container is
  removed (`podman rm`), which is also the only way to add a port mapping
  that wasn't there at creation time. `.falkordb-data/` is git-ignored; don't
  commit it.
- RDB snapshotting is already on by default (`redis-cli config get save` →
  `3600 1 300 100 60 10000`) — the volume mount is what makes those snapshots
  durable, not an extra flag. AOF (`appendonly`) is off by default; turn it on
  only if you need tighter durability than periodic RDB snapshots for local
  work, e.g. `podman exec falkordb redis-cli config set appendonly yes`.

### Create a virtual environment and install dependencies

A single `.venv` at the repo root (via the repo-root `pyproject.toml`/
`uv.lock`) covers `ps-cli` and the graph-ingestion/graph-query tools --
there is no separate per-tool environment to set up.

```bash
uv sync
```

Keep `.venv` activated (or otherwise on `PATH`) whenever you run
`tools/graph-query/ps.py` directly (a local-dev-only fallback — see the
deprecation note at the top of that file) — `ps.py` resolves `python3` from
`PATH` rather than a hardcoded interpreter (deliberately: this keeps
`ps.py` itself, not a generic `python3`, as the thing the harness
allowlists), so it uses whichever environment is currently active. The
`ps-qna` skill shipped in the `policy-system` plugin
(`ps-skills/policy-system/skills/ps-qna/`) queries PS Service over its own
MCP connector instead, and needs no local `.venv` activation. Scripts
invoked via `uv run` (e.g. `tools/graph-ingestion/load_all.sh`,
`ps-ingestion`) don't need activation at all -- `uv run` resolves the
repo-root `.venv` on its own.

1. Load test data into graph

### Start PS Service (process harness)

`ps-service` currently exposes only a minimal process harness: a FastAPI app
served by uvicorn with `/health` (liveness) and `/ready` (readiness)
endpoints, no domain routes yet.

Or, to run it detached (backgrounded, PID-tracked, logs to
`logs/ps-service-stdout.log`) instead of holding a foreground terminal:

```bash
scripts/ps-service.sh start
scripts/ps-service.sh status
scripts/ps-service.sh stop
```

`start` sources `.env` itself (check-env.example) and waits for `/health` before returning (fails
loudly if the process exits or doesn't come up within
`PS_SERVICE_STARTUP_TIMEOUT_SECONDS`, default 30s). `stop` sends `SIGTERM`
and waits for uvicorn's graceful shutdown, same as the manual path below.

Once it's running, verify it's alive:

```bash
curl http://127.0.0.1:8000/health
```

`/ready` is stricter than `/health` — it only reports `ready` once FalkorDB,
the LLM Interface, and Cellar/ELI are all confirmed reachable at startup AND
every ingestion-required config value (`PS_LLMINTERFACE_MODEL`,
`PS_LLMINTERFACE_EMBED_MODEL`, `PS_COMPANYMERGE_SIMILARITY_THRESHOLD`) is
set. The dependency half stays `ready` only as long as each keeps succeeding
on real traffic (self-heals on the next success if one fails mid-run, no
restart needed); the config half can't self-heal — it's fixed at startup, so
fixing a missing env var needs a restart:

```bash
curl http://127.0.0.1:8000/ready
```

If you haven't set up FalkorDB (see [Option B](#option-b-local-setup-no-dev-container)
above), configured the LLM Interface (see the next section), or set
`PS_COMPANYMERGE_SIMILARITY_THRESHOLD` (see
[Configure Company Merge](#configure-company-merge) below) yet, `/ready`
will report `not_ready` — check `logs/ps-service.jsonl` for a `startup`/
`warning` entry naming which dependency, or which config field(s) (`extra.
missing_config`), is the problem. Cellar/ELI needs no local setup (it's a
public endpoint), so it only fails here if you're offline.

To stop it, press Ctrl-C in the terminal it's running in, or send it
`SIGTERM` from another terminal (`kill -TERM <pid>`) — either way, uvicorn's
built-in graceful shutdown handles it: no forced kill needed. If you started
it via `scripts/ps-service.sh start`, use `scripts/ps-service.sh stop`
instead — it already tracks the PID for you.

### Run PS Service as a container

The repo's `Dockerfile` builds the same runtime image the release pipeline
publishes to `ghcr.io/mindovermachine-dev/ps-service`. Each release publishes two
tags — the release semver (e.g. `1.2.3`) and `latest`. Both are multi-arch
manifest lists built on native amd64 and arm64 runners, so one reference works on
Apple Silicon and on x86; `docker buildx imagetools inspect` on either tag shows
both platforms.

```bash
podman pull ghcr.io/mindovermachine-dev/ps-service:1.2.3
```

Pulling without credentials only works once the GHCR package has been made
public. That is a one-time manual step: the release workflow's `GITHUB_TOKEN`
cannot change package visibility, so until a human flips it at
`https://github.com/orgs/mindovermachine-dev/packages/container/ps-service/settings`
(Danger Zone → Change visibility → Public), an anonymous pull fails with
`unauthorized`.

To build the image yourself, run this from the repo root — the build context is
the whole workspace, because `uv.lock` and the workspace manifests live there,
and `.dockerignore` trims it to the paths the build actually reads:

```bash
podman build -t ps-service:local-test .
```

Run it against FalkorDB on an explicit network, so the service resolves FalkorDB
by hostname, and publish the port so the host can reach the service:

```bash
podman network create ps-net
podman run -d --name falkordb --network ps-net falkordb/falkordb:latest
podman run -d --name ps-service --network ps-net -p 18000:8000 \
  -e PS_FALKORDB_HOST=falkordb \
  ps-service:local-test

curl http://127.0.0.1:18000/health
```

The image sets `PS_SERVICE_HOST=0.0.0.0`, which is what makes the published port
reachable; the source default stays loopback-only, and the container is the only
place that binding is widened. Structured logs land in
`/var/log/ps-service/ps-service.jsonl` inside the container.

`/ready` reports `not_ready` in this setup, for the same reasons as the process
harness above: it answers `not_ready` without LLM configuration and without
Cellar/ELI reachability. Pass the LLM Interface and Company Merge settings in
with `--env-file .env`, and give the container outbound network access, before
expecting `ready`.

Clean up with:

```bash
podman rm -f ps-service falkordb && podman network rm ps-net
```

### Use ps-cli

`ps-cli` is a thin operator client for PS Service's REST API — this is the
primary way to drive the system by hand (ingest a regulation, list the
catalog) without writing `curl`/Python against the API directly.

For installing `ps-cli` and everyday usage (configuring which PS Service
instance it targets, named contexts, credential storage), see
the [user guide](./docs/artifacts/user-guide.md#ps-cli). The rest of this section covers the
contributor-only path: running `ps-cli` straight from a repo checkout.

Auth0-based OIDC login (OAuth 2.0 Device Authorization Grant) for
individual-operator identity, once targeting a non-local PS Service instance,
is still planned, not yet implemented.

#### Run from a repo checkout (local development)

For repo-local development, or before an installable `ps-cli` release exists,
invoke it as a module, from the repo root, with PS Service already
running (previous section) — this path stays fully supported alongside the
installed one described in the [user guide](./docs/artifacts/user-guide.md#ps-cli):

```bash
uv run python -m ps_cli --version
uv run python -m ps_cli regulations list
uv run python -m ps_cli regulations ingest 32016R0679
uv run python -m ps_cli internal ingest <fixture_path>.json
```

`regulations list`/`regulations ingest` only need PS Service itself —
`regulations list` serves a static curated catalog, no FalkorDB/LLM
dependency. `internal ingest` and real ingestion runs exercise the full
pipeline, so PS Service needs FalkorDB and the LLM Interface configured (see
below) — check `/ready` first if a command fails unexpectedly.

Target resolution (`PS_CLI_SERVICE_URL`, `ps-cli.toml`, named contexts) works
the same way whether `ps-cli` was installed via `uv tool install` or invoked
as a module from a checkout — see the [user guide](./docs/artifacts/user-guide.md#ps-cli) for the
full precedence order:

```bash
PS_CLI_SERVICE_URL=http://127.0.0.1:9000 uv run python -m ps_cli regulations list
```

### Configure the LLM Interface

`ps_service.llm_interface` (`route_completion`/`route_embedding`) routes to
whatever LLM Provider `PS_LLMINTERFACE_MODEL`/`PS_LLMINTERFACE_EMBED_MODEL`
name, via LiteLLM. Both are `<provider>/<model-or-deployment-name>` strings
passed straight through to `litellm.completion`/`litellm.embedding` — the
provider prefix (`azure/`, `ollama/`, ...) is what tells LiteLLM which
credential env vars to resolve. `ServiceConfig` only ever carries the two
model-name strings; it never sees credentials. Copy
[`.env.example`](.env.example) to `.env` (git-ignored) and fill in one of
the two options below.

#### Azure

```bash
PS_LLMINTERFACE_MODEL=azure/gpt-5.4-mini
PS_LLMINTERFACE_EMBED_MODEL=azure/text-embedding-3-large
AZURE_API_KEY=<your key>
AZURE_API_BASE=<your resource endpoint>
```

This is the exact configuration live acceptance tests run
against. LiteLLM resolves `AZURE_API_KEY`/`AZURE_API_BASE` itself — never
pass them explicitly to `litellm`/`route_completion`/`route_embedding`.

Sanity-check via the public API (`set -a && source .env && set +a` first so
the shell has the Azure vars, then):

```bash
uv run --project ps-service python3 -c "
from ps_service.logging.facade import configure
configure()
from ps_service.llm_interface import route_completion, route_embedding, ChatMessage

r = route_completion([ChatMessage(role='user', content='Say OK')], model='azure/gpt-5.4-mini')
print(r.model, r.text)

e = route_embedding('hello world', model='azure/text-embedding-3-large')
print(e.model, len(e.vector))
"
```

`configure()` is needed because `route_completion`/`route_embedding` log
through the Logging component, which requires a configured default emitter
before first use outside of `main.py`'s normal process startup.

#### Ollama (local, no cloud credentials/cost)

Requires a local [Ollama](https://ollama.com) install with a chat and an
embedding model pulled (`ollama list` to check first):

```bash
ollama pull phi3:mini
ollama pull nomic-embed-text
```

```bash
PS_LLMINTERFACE_MODEL=ollama/phi3:mini
PS_LLMINTERFACE_EMBED_MODEL=ollama/nomic-embed-text
```

No credential env vars are needed against a default-config local Ollama
instance (`localhost:11434`) — confirmed empirically: LiteLLM connects with
nothing else set. Only set `OLLAMA_API_BASE` if Ollama is reachable
somewhere other than the default host/port (also confirmed empirically: a
wrong `OLLAMA_API_BASE` produces a clear `APIConnectionError` rather than
silently falling back).

Sanity-check the same way as Azure, just swap the models:

```bash
uv run --project ps-service python3 -c "
from ps_service.logging.facade import configure
configure()
from ps_service.llm_interface import route_completion, route_embedding, ChatMessage

r = route_completion([ChatMessage(role='user', content='Say OK')], model='ollama/phi3:mini')
print(r.model, r.text)

e = route_embedding('hello world', model='ollama/nomic-embed-text')
print(e.model, len(e.vector))
"
```

### Configure Company Merge

`ps_service.company_merge.merge.merge_baseline_graph` needs
`PS_COMPANYMERGE_SIMILARITY_THRESHOLD` set — the dedupe-match cutoff (a float
greater than 0.0 and at most 1.0) it uses when deciding whether two company
mentions from different regulations refer to the same real-world entity:

```bash
PS_COMPANYMERGE_SIMILARITY_THRESHOLD=0.85
```

Unlike `PS_LLMINTERFACE_MODEL`/`PS_LLMINTERFACE_EMBED_MODEL`, there's no
provider choice to make here — just the one value. If it's unset, `/ready`
reports `not_ready` and `POST /ingestions` (`ps-cli regulations ingest`)
fails fast with `ingestion_config_incomplete` before doing any I/O.

### MCP Streamable HTTP endpoint

MCP Interface's `server` (the `cypher` and `domain_concepts` tools, the
`psdomain://concepts` resource — the tool serves the same text as the
resource, for hosts such as Claude Desktop that let the model call tools
but not read resources) is reachable one way: the Streamable HTTP transport from
issue #39, mounted at `/mcp` inside the same FastAPI app that already
serves `/health`/`/ready`/REST — same process, same port, no separate
service to start. MCP's stdio transport was removed once the `policy-system` plugin
made this endpoint the only supported client path. Once PS Service is
running (see above), the endpoint is:

```text
http://127.0.0.1:8000/mcp/
```

Note the trailing slash: the MCP app is mounted at `/mcp` with
`streamable_http_path="/"`, so `/mcp` answers with a `307` redirect and
`/mcp/` is the real endpoint.

There is no real authentication on this endpoint yet — the only supported
local, no-credential path is the opt-in local-test bypass from issue #67
(`PS_SERVICE_LOCAL_TEST_BYPASS=true`, loopback-bind only, warns on every
start); see the [user guide's local-test walkthrough](./docs/artifacts/user-guide.md#8-install-the-policy-system-plugin)
for how that bypass is used against this exact endpoint. Real per-user
authentication/authorization/rate-limiting for a network-reachable
deployment remains deferred — see the "Authentication is explicitly open,
not resolved" note in the [PS Service container architecture doc's MCP
Interface section](./docs/architecture/ps-service-container-architecture.md#mcp-interface),
tracked as issue #39's Group 3 / issue #65.

### Claude Desktop (alternative to Claude Code)

Install the `policy-system` plugin, then register the `/mcp/` endpoint
above as a local MCP server named `policy-system-graph-local` in
`claude_desktop_config.json` (an `mcp-remote` stdio→HTTP bridge) — see
[the user guide's Local Test walkthrough, step 8](./docs/artifacts/user-guide.md#8-install-the-policy-system-plugin).
The plugin's own `policy-system-graph` connector targets a hosted PS
Service and is evaluated from Anthropic's cloud, so it can never reach
`localhost`; the `ps-qna` skill recognises both names and uses whichever
is reachable.

Do **not** reuse the name `policy-system-graph` for the local server. Two
connectors under one name let the unreachable hosted one shadow the working
local one in a chat's toolset, which the skill reports as an unreachable
connector. For raw local graph access without standing up PS Service, call
`tools/graph-query/ps.py` from a shell instead.

## Coding Standards

See [`docs/coding-standards/level2-python-instructions.md`](docs/coding-standards/level2-python-instructions.md)
and [`docs/coding-standards/level1-coding-principles.md`](docs/coding-standards/level1-coding-principles.md).

Python code is linted with **ruff** (`select = ["ALL"]` minus a documented opt-out
list), formatted with **ruff format**, type-checked with **basedpyright** in strict
mode, and its dependencies audited with **pip-audit**. Config lives in the root
`pyproject.toml`. All four run in CI (`trunk-worthy` wave) and in the pre-commit
hook; a violation blocks the commit and fails CI.

## Testing

When implementing code use TDD as the default way to ensure appropriate test coverage.

Run the default suite — everything except the slow, environment-dependent marker
groups, which are opt-out on the command line:

```bash
uv run pytest -m "not integration and not llm_live and not cellar_live and not falkordb_live and not container_image"
```

Every excluded group is registered in the root `pyproject.toml` with the reason it
is slow, and each is run explicitly when you need it. `container_image` builds and
runs the runtime image, so it needs `docker` or `podman` and several minutes on a
cold build:

```bash
uv run pytest ps-service/tests/test_container_image.py -m container_image
```

Run the full local gate exactly as CI does:

```bash
uv sync --group dev
uv run ruff check . && uv run ruff format --check . && uv run basedpyright && \
  uv export --no-emit-workspace --format requirements-txt --no-hashes | uvx pip-audit@2.10.1 -r /dev/stdin
```

(or `uv run pre-commit run --all-files`).

## Releasing

A release is cut automatically. The `release` job in
`.github/workflows/on_main.yml` runs on every push to `main`, except a push whose
head commit message starts with `chore(release):` (that guard stops the job from
releasing off its own release commit). It classifies the commits landed since the
last release tag, computes the next version, writes it into the synced files below,
and commits/tags/pushes the result as `github-actions[bot]` — no one runs a release
command by hand.

### How the bump is decided

The job reads each commit's conventional-commit header
(`type(scope)!: description`) and classifies it:

- `feat!` (or any type with `!` after it), or a `BREAKING CHANGE:` /
  `BREAKING-CHANGE:` footer in the body → **major**
- `feat` → **minor**
- `fix` or `perf` → **patch**
- `docs`, `chore`, `ci`, `test`, `refactor`, `style`, `build` → no bump on their own
- a header that doesn't parse as one of those ten allowed types (`feat fix perf docs
chore ci test refactor style build`) is warned about in the job summary and does
  not bump

When several commits landed since the last tag, the highest bump among them wins.

### Where a bad header is caught

The same `scripts/release/lint-commit-header.sh` runs at three points, earliest first:

- `.githooks/pre-push` — lints the head commit of any push to `ready/*` on your
  machine, so `gh tt deliver` fails locally before CI ever runs. Issue-branch
  (`wrapup`) pushes are not linted; their headers are squashed away.
- `pr-to-ready.yml` — lints the PR title (as `<title> - resolves #N`) before the
  takt action squashes it onto a `ready/*` branch.
- `on_ready.yml` — the CI gate `merge-to-trunk` depends on; the backstop.

When the header's leading token looks like a scope used as a type (`company_merge:
...`, `ps-service/ps-cli: ...`) the lint prints a `hint:` with the `type(scope): ...`
rewrite. Since `gh tt deliver` takes the header from the issue title, fix the _issue
title_ first and deliver again, or the next delivery will fail the same way.

### What gets synced

The computed version is written into every version-lockstep file, in one commit:

- `ps-service/pyproject.toml` — `[project] version`
- `ps-cli/pyproject.toml` — `[project] version`
- `charts/policy-system/Chart.yaml` — `version` and `appVersion`
- `charts/policy-system/values.yaml` — `psService.image.tag` only (the sibling
  `falkordb.image.tag` is never touched)
- `ps-skills/policy-system/.claude-plugin/plugin.json` — `version`

`uv.lock` is re-locked in the same commit so it stays consistent with the two
`pyproject.toml` bumps. The commit, the annotated tag, and the push all land
atomically.

### Push races self-heal

Because the release lands by pushing to `main`, a release push can be rejected if
another push to `main` won the race first. That is expected, not an incident: the
next push to `main` — typically the very commit that won the race — recomputes the
version from the now-current tag state and releases normally. Nothing needs to be
retried, force-pushed, or fixed by hand.

### Verifying a release

```bash
docker buildx imagetools inspect ghcr.io/mindovermachine-dev/ps-service:<tag>
```

Each published tag must resolve to a manifest list containing both `linux/amd64`
and `linux/arm64`. The package also carries `build-amd64` and `build-arm64`
staging tags — these are the per-architecture images the manifest list points at,
and they are expected.

The tag itself must point at a commit that is an ancestor of `main`; the
`verify-tag-on-main` job checks this and fails the release if it does not hold.

### Prereleases

Prerelease-form tags (semver 2.0, e.g. `1.2.4-pre.1`) are never produced by the
automated flow and are not supported by the release pipeline.

### Manually re-running a release

`.github/workflows/on_semver.yml` also accepts a `workflow_dispatch` run against an
existing tag, taking either a bare (`0.12.0`) or `v`-prefixed (`v0.12.0`) tag as
input. This re-runs the image build/publish/GitHub-release steps for a tag that
already exists — it does not cut a new version.

### ps-cli is released the same way

`ps-cli` is released in lockstep with `ps-service` by the same automated job — its
`pyproject.toml` version is one of the synced fields above. The separate CLI tag
procedure is retired: there is no independent tag or release step for `ps-cli`
anymore.

An operator who needs install-time integrity beyond "trust the tag" can still
install against the exact commit SHA a release points at instead of the tag name:

```bash
uv tool install "git+https://github.com/mindovermachine-dev/policy-system@<commit-sha>#subdirectory=ps-cli"
```

A commit SHA cannot be silently re-pointed the way a tag can.

## Delivery Process

- Keep each issue branch focused on a single change.
- Give the issue a clear title/description — `gh tt workon` uses it, and
  `gh tt deliver` carries it through to the squashed `ready/*` commit.
- Ensure all checks pass before running `gh tt deliver` (see
  [Getting Started](#getting-started) above).
- Every release adds a `chore(release)` commit to `main` (see
  [Releasing](#releasing) above), so a `ready/**` branch already in flight when
  that happens may need a rebase before its own `gh tt deliver`.

## Reporting Issues

Describe how bugs and feature requests should be submitted.

## Discussions

Use the GitHub discussions feature to discuss or clarify topics that are not specific TODOs (those belong as Issues in the repo).
