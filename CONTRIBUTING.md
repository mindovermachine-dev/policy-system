# Contributing

Thank you for considering contributing to this project!

Note: We are in the transition from prototype to full implementation and the instructions here are a mix of both. As the actual implementation progresses, prototype instructions will be removed.

## Getting Started

1. Fork the repository.
2. Create a feature branch:
   git checkout -b feature/my-change
3. Make your changes.
4. Run tests.
5. Commit your changes.
6. Open a Pull Request.

After cloning, run these once:

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

- `falkordb` uses `network_mode: service:app`, i.e. it shares the `app`
  container's network namespace, so it's reachable at `localhost:6379` from
  inside the dev container — matching every tool's default host (see
  `tools/graph-ingestion`, `tools/graph-query`). Ports `6379` (FalkorDB) and
  `3000` (FalkorDB Browser, at http://localhost:3000) are published on the
  `app` service in `.devcontainer/docker-compose.yml`.
- `.devcontainer/.falkordb-data` (bind-mounted to
  `/var/lib/falkordb/data`) persists the graph to disk, so recreating the
  containers doesn't lose data. `.falkordb-data/` is git-ignored; don't
  commit it.
- RDB snapshotting is on by default (`redis-cli config get save` →
  `3600 1 300 100 60 10000`) — the volume mount is what makes those snapshots
  durable, not an extra flag. AOF (`appendonly`) is off by default; turn it on
  only if you need tighter durability than periodic RDB snapshots for local
  work, e.g. `docker compose -f .devcontainer/docker-compose.yml exec falkordb redis-cli config set appendonly yes`.

An alternative compose file, `.devcontainer/docker-compose.hostname.yml`,
gives `falkordb` its own network namespace/hostname instead of sharing
`app`'s — reachable as `falkordb:6379` rather than `localhost:6379`. Swap it
in via `devcontainer.json`'s `dockerComposeFile` if you want to experiment
with that topology; it requires passing `--host falkordb` (or
`FALKORDB_HOST=falkordb`) to the tools below, since their defaults are
`localhost`.

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

### Option C: Helm chart on a local kind cluster (planned, not yet implemented)

The target local-dev deployment path is a Helm chart deploying both PS
Service and FalkorDB onto a local [`kind`](https://kind.sigs.k8s.io/) cluster
running under Podman — mirroring the eventual Azure/AWS/on-prem Kubernetes
production target more closely than Options A/B above. No chart exists yet;
this section is a placeholder for that work. Options A/B remain how to
actually develop today.

### Create a virtual environment and install dependencies

A single `.venv` at the repo root (via the repo-root `pyproject.toml`/
`uv.lock`) covers `spikes/ps-cli` and the graph-ingestion/graph-query tools --
there is no separate per-tool environment to set up.

```bash
uv sync
```

Keep `.venv` activated (or otherwise on `PATH`) whenever you run
`tools/graph-query/ps.py` or use the `policy-question`/`falsification-step`
skills — `ps.py` resolves `python3` from `PATH` rather than a hardcoded
interpreter (deliberately: this keeps `ps.py` itself, not a generic
`python3`, as the thing the harness allowlists), so it uses whichever
environment is currently active. Scripts invoked via `uv run` (e.g.
`tools/graph-ingestion/load_all.sh`, `ps-ingestion`) don't need activation
at all -- `uv run` resolves the repo-root `.venv` on its own.

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

### Use ps-cli

`ps-cli` is a thin operator client for PS Service's REST API — this is the
primary way to drive the system by hand (ingest a regulation, list the
catalog) without writing `curl`/Python against the API directly. It has no
`[project.scripts]` entry point yet, so invoke it as a module, from the repo
root, with PS Service already running (previous section):

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

By default `ps-cli` targets `http://127.0.0.1:8000`, matching PS Service's
own default. Point it elsewhere with the `PS_CLI_SERVICE_URL` env var, or a
`ps-cli.toml` (`service_url = "..."`) in your current directory — the env
var wins over the file, which wins over the packaged default.

```bash
PS_CLI_SERVICE_URL=http://127.0.0.1:9000 uv run python -m ps_cli regulations list
```

**Planned (not yet implemented):** `ps-cli` is meant to be a distributed client, installed independently of this repo like `gh`/`az` — not a workspace-only script. The direction decided so far: `uv tool install git+https://github.com/<org>/policy-system@<tag>` (internal-only for now, no public PyPI publish), per-target config (e.g. dev/test/prod side by side) instead of a single `service_url`, and Auth0-based OIDC login (OAuth 2.0 Device Authorization Grant) for individual-operator identity once targeting a non-local PS Service instance. None of this exists yet — the invocation and config above are the only implemented path today.

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

### Claude Desktop (alternative to Claude Code)

Requires the same FalkorDB setup and data load as above. Claude Desktop has
no shell, so retrieval goes through `tools/graph-query/mcp_server.py` (MCP)
instead of `ps.py` directly — `uv sync` as above already installs `mcp`,
part of the repo-root `pyproject.toml`'s dependencies.

1. Add to `claude_desktop_config.json` (macOS:
   `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

   ```json
   {
     "mcpServers": {
       "policy-system-graph": {
         "command": "/absolute/path/to/.venv/bin/python3",
         "args": ["/absolute/path/to/tools/graph-query/mcp_server.py"]
       }
     }
   }
   ```

   Non-default host/port/graph: set `PS_FALKORDB_HOST`/`PS_FALKORDB_PORT`/`PS_FALKORDB_GRAPH` in an `"env"` block.

2. Restart Claude Desktop.
3. Upload `tools/graph-query/policy-question.zip` as a skill in Desktop's settings.
4. Ask a question (see above). If it doesn't auto-engage, say "Use the Policy Question skill" first.

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

Run the full local gate exactly as CI does:

```bash
uv sync --group dev
uv run ruff check . && uv run ruff format --check . && uv run basedpyright && \
  uv export --no-emit-workspace --format requirements-txt --no-hashes | uvx pip-audit@2.10.1 -r /dev/stdin
```

(or `uv run pre-commit run --all-files`).

## Pull Request Process

- Keep PRs focused on a single change.
- Provide a clear description.
- Link related issues if applicable.
- Ensure all checks pass.

## Reporting Issues

Describe how bugs and feature requests should be submitted.

## Discussions

Use the GitHub discussions feature to discuss or clarify topics that are not specific TODOs (those belong as Issues in the repo).
