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
podman run -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
```
3. Load test data into graph

NOTE: This is a destructure data load. It will reset the database and delete all existing data.

```
tools/graph-ingestion/load_all.sh
```
4. Start asking questions to the graph like:


```
Show me the names of the roles defined in CRA
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