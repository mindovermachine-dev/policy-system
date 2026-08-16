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

This is very early mostly exploration development. To load data into tthe graph database and ask questions, you will need:

- Podman
- FalkorDB
- VSCode
- Python 3.14

### Getting started

1. Install Podman
2. Setup a container with FalkorDB

```
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

3. Create a virtual environment and install the Python dependencies for the graph-ingestion and graph-query tools

```
uv venv
source .venv/bin/activate
uv pip install -r tools/graph-ingestion/requirements.txt -r tools/graph-query/requirements.txt
```

Keep `.venv` activated (or otherwise on `PATH`) whenever you run
`tools/graph-query/ps.py` or use the `policy-question`/`falsification-step`
skills — `ps.py` resolves `python3` from `PATH` rather than a hardcoded
interpreter, so it uses whichever environment is currently active.

4. Load test data into graph

NOTE: This is a destructure data load. It will reset the database and delete all existing data.

```
tools/graph-ingestion/load_all.sh
```
5. Start asking questions to the graph like:


```
Show me the names of the roles defined in CRA
```

If the skill (in the .claude/skills and .github/skills folder) does not automatically kick in the mention it explicitly:

```
Use the Policy Question skill. Show me the names of the roles defined in CRA
```



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