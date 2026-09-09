# Copilot Instructions

## Project Overview

Policy System ingests EU regulations and internal business policies into a unified
compliance knowledge graph (FalkorDB) and answers questions against it. Two deployable
containers: **PS Service** (Python 3.14, FastAPI, ingestion/mapping/query pipeline,
MCP + REST interfaces) and **FalkorDB** (graph database). Clients: `ps-cli`, the
`ps-qna` Claude plugin, and Policy Editor (not yet designed). See
[README.md](../README.md) and [docs/](../docs) for architecture and domain concepts.

## Tech Stack & Structure

- `uv` workspace at the repo root; members `ps-service/` and `ps-cli/`, each with its
  own `pyproject.toml` and `src/` layout. No shared internal package between them.
- Coding standards: [`docs/coding-standards/level1-coding-principles.md`](../docs/coding-standards/level1-coding-principles.md)
  and [`level2-python-instructions.md`](../docs/coding-standards/level2-python-instructions.md).

## Quality Gates

- Lint/format: `ruff check` / `ruff format` (`select = ["ALL"]` minus a documented
  opt-out list in the root `pyproject.toml`).
- Types: `basedpyright` in strict mode.
- Dependencies: `pip-audit` against the resolved `uv.lock`.
- Tests: `pytest` (`uv run pytest -m "not integration and not llm_live and not cellar_live and not falkordb_live and not container_image"` for the default fast suite).
- All four run in CI (`trunk-worthy` wave, via `gh insitu run trunk-worthy`) and in the
  pre-commit hook (`.githooks/pre-commit`, installed via `uv run pre-commit
install-hooks` — never `pre-commit install`, which refuses since `core.hooksPath` is
  set).
- After AI-driven edits, run `gh insitu run fix-all` — required because AI edits
  bypass editor format-on-save.

## Contribution Workflow

- Issue-branch workflow via `devx-cafe/gh-tt` (`gh tt workon`, `gh tt wrapup`, `gh tt
deliver`) — see [CONTRIBUTING.md](../CONTRIBUTING.md) for the full flow, local dev
  setup (devcontainer or local venv + FalkorDB), and the release process.

## CI And Pipeline Notes

- Keep runner compatibility with `ubuntu-latest`.
