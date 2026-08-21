# Contributing

Thank you for considering contributing to this project!

## Getting Started

1. Fork the repository.
2. Create a feature branch:
   git checkout -b feature/my-change
3. Make your changes.
4. Run tests.
5. Commit your changes.
6. Open a Pull Request.

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

NOTE: This is a destructure data load. It will reset the database and delete all existing data.

```bash
tools/graph-ingestion/load_all.sh
```

1. Start asking questions to the graph like:

```text
Show me the names of the roles defined in CRA
```

If the skill (in the .claude/skills and .github/skills folder) does not automatically kick in the mention it explicitly:

```text
Use the Policy Question skill. Show me the names of the roles defined in CRA
```

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

To be defined.

## Testing

When implementing code use TDD as the default way to ensure appropriate test coverage.

## Pull Request Process

- Keep PRs focused on a single change.
- Provide a clear description.
- Link related issues if applicable.
- Ensure all checks pass.

## Reporting Issues

Describe how bugs and feature requests should be submitted.

## Discussions

Use the GitHub discussions feature to discuss or clarify topics that are not specific TODOs (those belong as Issues in the repo).
