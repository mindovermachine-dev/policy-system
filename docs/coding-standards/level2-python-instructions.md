<!-- © 2026 Cartman ApS. All rights reserved. -->
# Level 2: Tech-Stack Coding Standards — Python

**Technology:** Python 3.14, uv workspace
**Tech Version:** python@3.14
**Last Reviewed:** 2026-08-22

---

**Precedence:** more specific scope overrides less specific. If a member section states a rule for the same concern a Common rule addresses, the member's rule governs that member — Common states the default, not a floor every section must additionally satisfy.

---

## Common

### Project Structure

- Repo is a uv workspace (`[tool.uv.workspace]` at root); each deployable/installable unit is a workspace member with its own `pyproject.toml`: `ps-service/`, `ps-cli/`.
- Each member uses `src/` layout: `<member>/src/<package>/`, `<member>/tests/` mirroring the `src/` substructure.
- `ps-service` and `ps-cli` are fully decoupled — no shared internal package between them. Each vendors its own copy of anything it needs (e.g. an LLM client wrapper), even if the code looks similar.

### Naming Conventions

- Standard PEP 8: `snake_case` for modules, functions, variables; `PascalCase` for classes; `UPPER_SNAKE_CASE` for constants.

### Dependency Injection Patterns

- No DI framework. Use plain constructor injection — components take their dependencies (FalkorDB client, LLM client, etc.) as constructor/function arguments, not as module-level globals or singletons.
- Business logic must not construct its own infrastructure clients (e.g. a `FalkorDB(...)` instance) inline — accept it as a parameter so it can be substituted in tests.

### Error Handling

- Never swallow exceptions silently.

### Subprocess & Process Execution

- Never `shell=True`. Pass command arguments as a list, never as a concatenated/interpolated string.
- Always set an explicit timeout on any subprocess or external-process call.

### Testing Patterns

- pytest. `tests/` mirrors `src/<package>/` substructure 1:1 — a module's tests live in the corresponding `tests/<component>/` directory.
- Mock at component boundaries (e.g. mock the FalkorDB client, mock the LLM client) — not internals of the component under test.
- Test names describe scenario and expected outcome (`test_raises_when_graph_missing`, not `test_download_2`).

### Configuration & Secrets

- No secrets committed to the repo (`.env` is gitignored). LLM provider credentials are resolved by LiteLLM per its own provider convention, not hardcoded.
- Never log secrets or credentials (LLM provider keys, FalkorDB credentials).

### Build & Package Management

- `uv` workspace at the repo root; one shared `uv.lock` across all members.
- Each member (`ps-service`, `ps-cli`) has its own `pyproject.toml` with only the dependencies it actually needs — do not add a dependency to a member that doesn't use it.
- Build backend: `hatchling`, with `[tool.hatch.build.targets.wheel] packages = ["src/<package>"]` per member.
- Dependency vulnerability scanning: `uv pip audit` runs in CI against the shared `uv.lock` and blocks merge on a known CVE — covers transitive dependencies pulled in via LiteLLM's provider SDKs, not just direct dependencies.

### Linting & Code Quality

- `ruff`, selecting at least: `B` (bugbear), `SIM` (simplify), `RET` (return-statement cleanliness), `C4` (comprehension cleanup), `PERF`/`FURB` (performance & modernization anti-patterns), `PIE` (misc simplifications), `I` (import sorting) — matches the proven selection in `gh-tt`'s `pyproject.toml`, used as reference.
- Max cyclomatic complexity: 8 (`ruff`'s `mccabe` check), per L1's Cyclomatic Complexity principle — stricter than `gh-tt`'s reference `max-complexity = 10`; ps-service/ps-cli intentionally diverge from the `gh-tt` value here.
- Absolute imports only, no relative imports (`ban-relative-imports = "all"` in `gh-tt`'s ruff config) — clearer with the `src/` layout's nested packages.
- DRY: don't duplicate logic across modules — extract a shared function/class within the same package once a pattern repeats a third time.
- Docstrings required on public functions/classes and anything crossing a component boundary; a one-line summary is enough unless the behavior is non-obvious (the MCP tool-docstring rule under ps-service is a stricter special case of this, not a separate rule).

### Types Handling

- Code must pass Pylance strict mode.
- No implicit Any.
- No unknown or partially-known types.
- Every function must declare parameter and return types.
- Empty collections must be explicitly typed.
- Generic types must always specify type parameters.
- Use TypedDict for dictionary schemas.
- Use dataclasses for structured data.
- Use Protocol for interfaces.
- Use cast() only when unavoidable and document why.
- Avoid disabling type checks with type: ignore unless absolutely necessary.
- Public APIs must be fully typed.

---

## ps-service

### Project Structure

- Component module names under `ps_service/` must match the "Domain path" column in `docs/architecture/ps-solution-architecture.md`, via a mechanical transform: take the domain path's last segment and insert underscores at word boundaries (`ps.service.domainmapper` → `ps_service/domain_mapper/`). If a module is renamed, update the domain path in the architecture doc in the same change — they must never drift apart.

### Naming Conventions

- Component package names are nouns matching their architectural component (`query_engine`, `domain_mapper`), not generic terms.

### Data Modeling

- Use **Pydantic** for fixed-shape domain entities that cross component boundaries — the PS Conceptual Model types (Role, Requirement, Obligation, Capability, Policy, Standard, Control; see `docs/artifacts/ps-domain-concepts.md`) — and for any REST/MCP API request/response payloads.
- Pydantic is also the default for LLM-structured-extraction outputs (LLM Interface, Domain Mapper) — pass a Pydantic model as the response schema rather than hand-parsing LLM text.
- Do **not** model raw Cypher query results as per-entity Pydantic models — their shape depends on the query. Use a single generic envelope instead: `QueryResult(columns: list[str], rows: list[list[Any]], row_count: int)`, matching the shape the existing `tools/graph-query/mcp_server.py` prototype already returns.
- At a trust boundary (MCP tool args, REST payloads), a Pydantic model is a validation control, not just a modeling convenience — use `Field()` constraints (`min_length`, `pattern`, etc.) on anything that flows into query construction, file paths, or a further LLM call. Static typing (see Types Handling) catches shape mismatches at dev-time; it does not validate values at runtime.

### Query Safety

- Parameterize Cypher queries: pass values via `params={...}`, never interpolate them into the query string — follow the existing precedent in `tools/graph-ingestion/merge_capabilities.py`'s `graph.query(query, params={"id": ...})`.
- The one exception is labels/relationship types, which Cypher cannot parameterize (see `load_graph.py`'s `f"MATCH (n:{label})"`) — validate against the allow-list of known schema labels (`docs/artifacts/ps-domain-concepts.md`) before interpolating; never interpolate a raw user- or LLM-supplied string there.
- The write-clause regex guard (`query_engine/cypher_query.py`'s `_WRITE_CLAUSE`) is defense-in-depth, not the boundary — the real boundary is the FalkorDB connection's own privileges: the read-only query surface (Query Engine, MCP Interface) must connect as a database user/role restricted to read-only operations.
- Bound query cost: no query execution timeout or result-size cap exists yet in the read-only query surface (Query Engine / MCP Interface) — an open risk once FalkorDB is reachable over a network (issue #38). Enforce both at the FalkorDB driver/connection level when addressed.

### API Patterns

- REST framework: **FastAPI** — chosen for its native Pydantic integration (already mandated above for REST/MCP request/response payloads), async support matching the rest of `ps-service`, and auto-generated OpenAPI docs.
- Routing, middleware, and dependency-injection conventions for `ps_service/api/` are not yet decided — establish them when `api/` endpoints beyond a health check are actually implemented, not before.

### Entrypoint / Process Lifecycle Patterns

- The `ps-service` entrypoint runs as a FastAPI app served by `uvicorn`, invoked programmatically — no custom `asyncio` signal handling. SIGTERM/SIGINT graceful shutdown is delegated entirely to uvicorn's built-in handling, with `timeout_graceful_shutdown` set explicitly rather than left at its default.
- Startup and shutdown logic lives in FastAPI's `lifespan` context manager, not the deprecated `@app.on_event(...)` decorators.
- Health is split into two endpoints, matching the standard liveness/readiness distinction — do not collapse them into one:
  - `/health` (liveness) — reports "alive" as soon as the ASGI server is accepting connections, independent of startup progress. Must never check external dependencies: a dependency outage should never cause a liveness failure/restart.
  - `/ready` (readiness) — reports "ready" only once `lifespan`'s startup block completes (today: Logging `configure()` + startup log entry). This is the extension point for future dependency checks (FalkorDB, LLM provider) as components get wired in — the endpoint shape does not change when those checks are added later.
- Both endpoints are localhost-only and unauthenticated, matching the posture already established for `ps_service/api/` in Open Decisions below.
- Readiness state is process-local (e.g. a module-level flag set inside `lifespan`) — valid under the current single-worker assumption; revisit if `ps_service/api/` ever runs multi-worker (same open item already flagged for Logging's multi-process write safety).

### MCP Interface Patterns

- **Delegate, don't reimplement**: an MCP tool function is a thin wrapper over existing engine logic — e.g. `mcp_server.py`'s `cypher` tool calls `ps_service.query_engine.execute_cypher_query` in-process rather than reimplementing query execution or the write-clause guard, which live only in `ps_service.query_engine.cypher_query`. Validation and guards live in exactly one place; the MCP layer never duplicates them.
- **Errors are return values, not exceptions**: a tool function catches failures from its delegate and returns an `error: ...` string/JSON payload rather than raising — propagate the delegate's *result-shaped* error verbatim (e.g. a Cypher syntax error), but sanitize anything lower-level (driver internals, connection details, file paths) before it crosses the MCP boundary. Differs from ps-service's domain-specific-exception pattern and ps-cli's `PsCliError` pattern: the MCP protocol conveys failure through the result payload, not a raised exception.
- **Tool docstrings are client-facing**: a `@server.tool()` function's docstring is read by the calling LLM client to decide how and when to invoke the tool — it is tool-usage guidance (expected input shape, return shape, what triggers the error case), not an internal implementation note for other developers.

### LLM Interface Patterns

- Treat ingested/document-sourced and graph-sourced content as untrusted data, never as instructions — never interpolate it directly into a system/instruction prompt. Delimit it clearly (e.g. a dedicated user-content field or tag) so the model can distinguish data from instructions.
- This applies wherever ingested content reaches an LLM, not only inside `llm_interface`/`domain_mapper` — MCP Interface returns graph content to external LLM clients (e.g. Claude via the PS Question Skill), and regulation/policy text ingested into the graph may itself contain crafted text. Treat that return path with the same untrusted-content handling.

### Error Handling

- Domain-specific exception types per component, not generic `Exception`/`ValueError` — follow the existing precedent in `spikes/ps-cli` (e.g. `MissingBaselineError`, `GraphNotFoundError`, `RestoreError`) rather than returning ambiguous error strings.
- Result types (e.g. `MergeResult`, `IngestResult`) are acceptable for operations with a meaningful success payload alongside possible partial failure — mirrors the pattern already used in `spikes/ps-cli`.

### Configuration & Secrets

- Environment variable convention: `PS_<COMPONENT>_<SETTING>`, e.g. `PS_FALKORDB_HOST`, `PS_FALKORDB_PORT`, `PS_FALKORDB_GRAPH` — matches the existing precedent in `tools/graph-query/ps.py`, which `mcp_interface` inherits from.
- LLM Interface routing config: `PS_LLMINTERFACE_MODEL` (chat model for `RouteCompletion`), `PS_LLMINTERFACE_EMBED_MODEL` (embedding model for `RouteEmbedding`) — plain model-name strings passed directly to `litellm.completion(model=...)`/`litellm.embedding(model=...)`, no router/fallback config. Both are new fields on the same `ServiceConfig` (`ps_service/config.py`), resolved by the same `load_config()` call as `PS_SERVICE_*`/`PS_LOGGING_DIR` — no separate config module. Provider credentials are never part of this config; LiteLLM resolves them from its own provider-specific env vars per the Common section's Configuration & Secrets rule above.

---

## ps-cli

These patterns are adapted from the proven shape of the `gh-tt` CLI (a separate, mature repo used as reference).

### Entry Point & Command Dispatch

- **Thin entry point**: `__main__.py` only imports and calls a single `main()` from the package's top-level CLI module — no logic lives in `__main__.py` itself.
- **Dispatch, not branching**: `main()` parses args, then dispatches via a `dict[str, Callable]` mapping command name → handler function, not a long `if`/`elif` chain. Each subcommand's logic lives in its own handler function.

### Argument Parsing

- Use `argparse` with parent parsers for flags shared across subcommands (e.g. `-v`/`--verbose`), one subparser per command, `add_mutually_exclusive_group()` for conflicting options, and `set_defaults()` for values derived at parse time rather than computed ad hoc inside handlers.

### Error Handling

- **Single user-facing error type**: user-facing errors (invalid state, missing precondition, bad input) are raised as one `PsCliError` exception via an `assert_contract(*, contract: bool, msg: str, hint: str | None = None)` helper — not the many domain-specific exception types used in `ps-service`. Only `PsCliError` is caught, in one `try/except` around command dispatch in `main()`; format as `msg` (plus `hint` if given) to stderr, then `sys.exit(1)`. Success falls through to an explicit `sys.exit(0)`.
- **Let bugs crash**: exceptions that are not `PsCliError` are bugs, not user errors — do not catch them into a generic error message. Let them propagate with a full traceback. Standard Python tracebacks don't print local variable values, so this is safe by default — but if enhanced/pretty-traceback tooling (e.g. `rich`, anything that dumps locals) is ever added, it must not be enabled where a local could hold a secret (an API key or token passed as a function argument).

### Output

- **Silence on success**: no output unless the command's purpose is to print something (e.g. a query result). Silence signals success, matching standard CLI conventions (`git`, unix tools) — do not add progress/informational prints "just in case."

### Configuration & Secrets

- Ship a default config file inside the package and fail fast (assert it exists) if it's missing; merge a project-root override file over the defaults (deep-merge, override wins) rather than requiring every setting to be redefined per project.

### Testing Patterns

- Mark tests that invoke real external processes (`gh`, `git`, FalkorDB) as `integration`, separate from fast mock-based `unittest` tests, so the default test run stays fast — mirrors the `unittest`/`integration`/`smoke`/`dev` marker split `gh-tt` uses.

---

## Open Decisions

Not yet coding patterns — each needs a decision before the area it touches ships, tracked here instead of as asides inside the topic sections above.

- **API authentication/authorization**: REST framework is now decided (FastAPI, ps-service → API Patterns) but auth/authz is still open — decide before any `ps_service/api/` endpoint beyond a health check ships. Do not ship an unauthenticated API by default.
- **PII handling**: ingested content may contain PII (this system explicitly models GDPR). Needs a product/legal decision on retention/logging limits and third-party LLM data-processing terms before ingesting real business data.
