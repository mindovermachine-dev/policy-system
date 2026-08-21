<!-- © 2026 Cartman ApS. All rights reserved. -->
# Level 2: Tech-Stack Coding Standards — Python

<!--
  No system-config.md or Tech Stack artifact exists in this repo yet, so
  this file was written directly rather than generated from one. If those
  artifacts are added later, reconcile this file against them.
-->

**Technology:** Python 3.14, uv workspace
**Tech Version:** python@3.14
**Last Reviewed:** 2026-08-21

---

## Project Structure

- Repo is a uv workspace (`[tool.uv.workspace]` at root); each deployable/installable unit is a workspace member with its own `pyproject.toml`: `ps-service/`, `ps-cli/`.
- Each member uses `src/` layout: `<member>/src/<package>/`, `<member>/tests/` mirroring the `src/` substructure.
- `ps-service` and `ps-cli` are fully decoupled — no shared internal package between them. Each vendors its own copy of anything it needs (e.g. an LLM client wrapper), even if the code looks similar.
- Component module names under `ps_service/` must match the "Domain path" column in `docs/architecture/ps-solution-architecture.md`, via a mechanical transform: take the domain path's last segment and insert underscores at word boundaries (`ps.service.domainmapper` → `ps_service/domain_mapper/`). If a module is renamed, update the domain path in the architecture doc in the same change — they must never drift apart.

## Naming Conventions

- Standard PEP 8: `snake_case` for modules, functions, variables; `PascalCase` for classes; `UPPER_SNAKE_CASE` for constants.
- Component package names are nouns matching their architectural component (`query_engine`, `domain_mapper`), not generic terms.

## Dependency Injection Patterns

- No DI framework. Use plain constructor injection — components take their dependencies (FalkorDB client, LLM client, etc.) as constructor/function arguments, not as module-level globals or singletons.
- Business logic must not construct its own infrastructure clients (e.g. a `FalkorDB(...)` instance) inline — accept it as a parameter so it can be substituted in tests.

## Data Modeling

- Use **Pydantic** for fixed-shape domain entities that cross component boundaries — the PS Conceptual Model types (Role, Requirement, Obligation, Capability, Policy, Standard, Control; see `docs/artifacts/ps-domain-concepts.md`) — and for any REST/MCP API request/response payloads.
- Pydantic is also the default for LLM-structured-extraction outputs (LLM Interface, Domain Mapper) — pass a Pydantic model as the response schema rather than hand-parsing LLM text.
- Do **not** model raw Cypher query results as per-entity Pydantic models — their shape depends on the query. Use a single generic envelope instead: `QueryResult(columns: list[str], rows: list[list[Any]], row_count: int)`, matching the shape the existing `tools/graph-query/mcp_server.py` prototype already returns.

## API Patterns

[TBD — REST framework for `ps_service/api/` not yet decided; decide when `api/` is actually implemented, not before]

## Error Handling Patterns

- Domain-specific exception types per component, not generic `Exception`/`ValueError` — follow the existing precedent in `spikes/ps-cli` (e.g. `MissingBaselineError`, `GraphNotFoundError`, `RestoreError`) rather than returning ambiguous error strings.
- Result types (e.g. `MergeResult`, `IngestResult`) are acceptable for operations with a meaningful success payload alongside possible partial failure — mirrors the pattern already used in `spikes/ps-cli`.
- Never swallow exceptions silently; never log secrets or credentials (LLM provider keys, FalkorDB credentials).

## Testing Patterns

- pytest. `tests/` mirrors `src/<package>/` substructure 1:1 — a module's tests live in the corresponding `tests/<component>/` directory.
- Mock at component boundaries (e.g. mock the FalkorDB client, mock the LLM client) — not internals of the component under test.
- Test names describe scenario and expected outcome (`test_raises_when_graph_missing`, not `test_download_2`).

## Configuration & Secrets

- Environment variable convention: `PS_<COMPONENT>_<SETTING>`, e.g. `PS_FALKORDB_HOST`, `PS_FALKORDB_PORT`, `PS_FALKORDB_GRAPH` — matches the existing precedent in `tools/graph-query/ps.py`, which `mcp_interface` inherits from.
- No secrets committed to the repo (`.env` is gitignored). LLM provider credentials are resolved by LiteLLM per its own provider convention, not hardcoded.

## Build & Package Management

- `uv` workspace at the repo root; one shared `uv.lock` across all members.
- Each member (`ps-service`, `ps-cli`) has its own `pyproject.toml` with only the dependencies it actually needs — do not add a dependency to a member that doesn't use it.
- Build backend: `hatchling`, with `[tool.hatch.build.targets.wheel] packages = ["src/<package>"]` per member.
