<!-- © 2026 Cartman ApS. All rights reserved. -->

# Architecture Audit - Component Granularity and Boundaries (Issue #250, umbrella #246)

This document is built slice by slice. Slices covered: package dependency direction and cycles, component granularity, duplicated helpers and abstractions, distribution boundaries, closure.

## Scope examined

Covered: every package under `ps-service/src/ps_service`, with `ps-cli`, `tools/*` and `ps-test-support` for the dependency direction between distributions. The `ps-service/src` tree has 282 tracked files and 269 importable modules.

| Path                      | Python files | Lines |
| ------------------------- | ------------ | ----- |
| `ps-service/src`          | 282          | 72090 |
| `ps-cli/src`              | 25 (with `ps-test-support`) | 5166  |
| `ps-test-support/src`     | (included above) | 716   |
| `tools/**/*.py` | 12           | 4576  |

The uv workspace members are `ps-service`, `ps-cli` and `ps-test-support` (`pyproject.toml:2`); `tools/*` are scripts outside the workspace.

Reference for comparison: `docs/architecture/ps-service-container-architecture.md`. The `api/` package is documented as implementation wiring that is not a named component (`:74`, `:2301`). The C4 Component Overview lists 24 components (`:303-326`).

## Method and commands

All tools run ephemerally through `uvx`; no dependency or lock file changed. Python 3.14 syntax (`except A, B:`) is used in the sources, so AST tools run under `uvx python@3.14`; the system `python3` cannot parse them. Commands run from the repository root in `zsh`; quote globs.

- Size: `find ps-service/src -name '*.py' | xargs wc -l | sort -rn`.
- Import graph, package edges, mutual pairs: `grimp` (counts module-level, function-local and `TYPE_CHECKING` imports alike; every edge below says which kind it is).

```text
PYTHONPATH=ps-service/src uvx --from grimp python -B - <<'PY'
import collections, grimp
g = grimp.build_graph("ps_service", include_external_packages=False)
top = lambda m: ".".join(m.split(".")[:2])
e = collections.Counter()
for m in g.modules:
    for i in g.find_modules_directly_imported_by(m):
        if top(m) != top(i):
            e[(top(m), top(i))] += 1
for (a, b), n in sorted(e.items()):
    if a < b and (b, a) in e:
        print("mutual", a, b, n, e[(b, a)])
PY
```

- Contracts: `import-linter`, config embedded below (save as `contracts.ini` outside the repo):

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=ps-service/src:ps-cli/src:ps-test-support/src \
  uvx --from import-linter lint-imports --config contracts.ini
```

```ini
[importlinter]
root_packages =
    ps_service
    ps_cli
    ps_test_support
include_external_packages = False

[importlinter:contract:cli-not-service]
name = ps_cli must not import ps_service
type = forbidden
source_modules = ps_cli
forbidden_modules = ps_service

[importlinter:contract:service-not-cli]
name = ps_service must not import ps_cli or test support
type = forbidden
source_modules = ps_service
forbidden_modules =
    ps_cli
    ps_test_support

[importlinter:contract:leaf-foundations]
name = foundation packages import no feature package
type = forbidden
source_modules =
    ps_service.logging
    ps_service.dependency_health
    ps_service.domain_schema
forbidden_modules =
    ps_service.api
    ps_service.graph_gateway
    ps_service.ingestion

[importlinter:contract:entry-layer]
name = components do not import the api entry layer
type = forbidden
source_modules =
    ps_service.audit
    ps_service.auth
    ps_service.authz
    ps_service.change_monitor
    ps_service.company_merge
    ps_service.config
    ps_service.curated_source
    ps_service.domain_mapper
    ps_service.domain_schema
    ps_service.export
    ps_service.graph_cleanup
    ps_service.graph_gateway
    ps_service.ingestion
    ps_service.ingestion_runs
    ps_service.invitations
    ps_service.llm_interface
    ps_service.passkey_signing
    ps_service.persistence
    ps_service.policy_lifecycle
    ps_service.query_engine
    ps_service.restore
    ps_service.runtime_config
    ps_service.state_migrations
forbidden_modules = ps_service.api
```

Result on this tree: the first three contracts are kept, the fourth (`entry-layer`) is broken with the edges listed in the findings below.

- Duplicate helpers: list same-named top-level functions defined in two or more modules, then compare the bodies with `ast.dump` after dropping the docstring and normalising the name. Run with `uvx python@3.14 -B script.py` where the script does, for every `ps-service/src/ps_service/**/*.py` file, `ast.parse`, collects `FunctionDef` and `ClassDef` nodes by name, and groups by `ast.dump` of the body plus arguments (classes: body plus decorators). The candidate names come from `grep -rn "^def <name>\|^async def <name>"`. Helper groups with equal dumps are "body-identical"; the same name with different dumps is "not equal".
- Importer counts: `grep -rl "ps_service.api.<module>\|api import <module>" ps-service ps-cli tools | wc -l`.
- Each edge is opened at its `file:line` and classified as module-level, `TYPE_CHECKING` only, or function-local.

## Severity scale

- High: a defect or contract breach caused by the structure (wrong behaviour, broken documented rule).
- Medium: a structural defect that raises the cost of every later change (dependency inversion, a module that mixes unrelated responsibilities).
- Low: contained or type-only structural untidiness.
- n/a: reviewed, reported as a false positive or accepted design.

No repository-wide dependency-direction rule exists in `docs/architecture` or `docs/coding-standards`. The only stated rule is scoped to Change Monitor (`docs/architecture/ps-service-container-architecture.md:1367`); the standards basis for the inversion findings is Dependency Inversion in `docs/coding-standards/level1-coding-principles.md:60-63`. Inversion findings are therefore a judgement and are rated Medium at most.

## Package measurements (before)

Packages under `ps_service` by size (lines, files) and cross-package fan-in/fan-out (distinct packages importing it / imported by it):

| Package            | Files | Lines | Fan-in | Fan-out |
| ------------------ | ----- | ----- | ------ | ------- |
| `graph_gateway`    | 37    | 10539 | 12     | 7       |
| `api`              | 20    | 8395  | 7      | 17      |
| `graph_cleanup`    | 17    | 7048  | 1      | 11      |
| `company_merge`    | 16    | 4909  | 4      | 7       |
| `mcp_interface`    | 4     | 4683  | 1      | 17      |
| `policy_lifecycle` | 7     | 4641  | 1      | 8       |
| `ingestion`        | 22    | 4133  | 9      | 6       |
| `domain_mapper`    | 12    | 3865  | 6      | 6       |
| `passkey_signing`  | 11    | 2914  | 4      | 6       |
| `domain_schema`    | 13    | 2556  | 3      | 0       |
| `change_monitor`   | 10    | 2235  | 1      | 6       |
| `authz`            | 8     | 1998  | 6      | 5       |
| `restore`          | 10    | 1900  | 1      | 7       |
| `audit`            | 6     | 1419  | 13     | 4       |
| `ingestion_runs`   | 6     | 1076  | 3      | 5       |
| `persistence`      | 5     | 1072  | 12     | 3       |
| `export`           | 8     | 1002  | 3      | 5       |
| `curated_source`   | 11    | 983   | 3      | 6       |
| `auth`             | 8     | 975   | 3      | 3       |
| `logging`          | 6     | 925   | 22     | 0       |
| `llm_interface`    | 9     | 828   | 5      | 3       |
| `runtime_config`   | 5     | 757   | 4      | 5       |
| `invitations`      | 6     | 483   | 2      | 3       |
| `query_engine`     | 5     | 463   | 1      | 3       |
| `dependency_health`| 2     | 160   | 15     | 0       |

Single modules at the package root: `config.py` (912 lines, fan-in 20, fan-out 1), `state_migrations.py` (36 lines, fan-in 2, fan-out 6), `main.py` (1170 lines, fan-in 1, fan-out 14).

Modules over 800 lines (`find ... | xargs wc -l`): `mcp_interface/mcp_server.py` 4538; `policy_lifecycle/service.py` 2227; `api/ingestion_orchestration.py` 2208; `graph_gateway/gateway.py` 1532; `policy_lifecycle/graph_writer.py` 1523; `graph_gateway/store.py` 1284; `company_merge/graph_writer.py` 1241; `ingestion/adapters/internal_seed/persist.py` 1217; `main.py` 1170; `graph_cleanup/executors.py` 1069; `api/change_check_orchestration.py` 968; `domain_mapper/derivation.py` 961; `passkey_signing/router.py` 927; `config.py` 912; `graph_cleanup/models.py` 907; `api/restore_orchestration.py` 888; `graph_cleanup/service.py` 860; `graph_cleanup/group_builders.py` 828; `api/routes.py` 812. Outside `ps-service`: `tools/company-merge/company_merge_similarity_sweep.py` 1055, `tools/domain-mapper/exclusion_audit.py` 874, `ps-cli/.../http_client.py` 745, `ps-cli/.../device_flow.py` 601, `ps-test-support/.../mock_oidc_provider.py` 593.

## Findings

### Slice 1: dependency direction and cycles

Package-level result: one strongly connected component of 19 packages (`api`, `audit`, `auth`, `authz`, `change_monitor`, `company_merge`, `config`, `curated_source`, `domain_mapper`, `export`, `graph_gateway`, `ingestion`, `ingestion_runs`, `llm_interface`, `passkey_signing`, `persistence`, `restore`, `runtime_config`, `state_migrations`) and seven mutual pairs (import counts `a->b / b->a`): `api`/`auth` 2/2, `api`/`authz` 4/1, `api`/`curated_source` 8/2, `api`/`passkey_signing` 4/7, `config`/`curated_source` 2/3, `domain_mapper`/`ingestion` 3/1, `graph_gateway`/`state_migrations` 1/1. Module-level cycles: three groups (see A-S1-06 to A-S1-08). Cross-distribution edges (`ps_cli` to `ps_service`, `ps_service` to `ps_cli` or `ps_test_support`): none.

| id       | location                                                                                                                                                                                                                                     | severity | evidence                                                                                                                                                                                                                                                                                                                                               | disposition                                                                                                                                              |
| -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A-S1-01  | Components import the `api` entry layer: `auth.middleware:23` (`api.error_handlers`), `auth.protected_resource:24` (`api.models`), `authz.service:19` (`api.errors`), `curated_source.catalog_client:20` and `instrument_lookup:21` (`api.catalog`, the latter `TYPE_CHECKING` only), `graph_cleanup.dependencies:15` (`api.graph_write_access`), `graph_cleanup.executors:24` and `graph_submission:36` (`api.errors`) | Medium   | `lint-imports` contract `entry-layer` broken; all edges are module-level except where stated. Transitively `policy_lifecycle`, `state_migrations` and `graph_gateway.provision` reach `api.errors` through `authz.service`. Importer files: `api.errors` 49, `api.graph_write_access` 46, `api.catalog` 34, `api.models` 20, `api.error_handlers` 9. | issue #270                                                                                                                                               |
| A-S1-02  | `passkey_signing.router:53-61` (`api.dependencies`, `api.errors`, `api.graph_write_access`, `api.near_miss_review_orchestration`), `passkey_signing.service:30` (`api.errors`) and `:38` (`api.near_miss_review_orchestration`, `TYPE_CHECKING` only), `passkey_signing.error_handlers:24` (`api.errors`) | Medium   | The package holds REST wiring (router, error handlers) that is itself entry-layer code, which explains seven `passkey_signing` to `api` edges against four back. `api.dependencies` has 43 importer files, `api.near_miss_review_orchestration` 10.                                                                                                   | issue #270                                                                                                                                               |
| A-S1-03  | Package-level strongly connected component of 19 packages                                                                                                                                                                                    | Medium   | Computed from the package edges (Tarjan over the grimp package graph). The four `api` mutual pairs come from A-S1-01 and A-S1-02; the remaining pairs are A-S1-04 to A-S1-06. Removing A-S1-01 and A-S1-02 breaks the four `api` pairs.                                                                                       | issue #270                                                                                                                                               |
| A-S1-04  | `config.py:16-17` imports `curated_source.errors` and `curated_source.source_url` (module level); `curated_source.catalog_client:31`, `resolve:29`, `config_key:26` import `config.ServiceConfig` (`TYPE_CHECKING` only)                    | Low      | Type-only in one direction, but the configuration module depends on a feature component to validate one URL setting; 23 importer files for the two `curated_source` modules.                                                                                                                                                                        | issue #271                                                                                                                                               |
| A-S1-05  | `ingestion/adapters/internal_seed/persist.py:419-505` (eight function-local imports of `domain_mapper.identity`) and `domain_mapper.derivation:55`, `graph_writer:57` (module level) / `extraction:40` (`TYPE_CHECKING`) importing `ingestion.graph_submission` | Low      | Each local import carries a lint suppression stating it keeps `ps_service.main` off Domain Mapper at import. `domain_mapper.identity` is pure id formulas used by 4 packages (7 source files, about 30 test files).                                                                                                                                  | issue #271                                                                                                                                               |
| A-S1-06  | `graph_gateway.provision:54` imports `state_migrations`; `state_migrations:22` imports `graph_gateway.MIGRATIONS_DIR` (both module level)                                                                                                    | Low      | Package-level mutual pair, but no module-level cycle: `graph_gateway/__init__` does not import `provision`, and grimp reports no module group for it. Startup bootstrap order.                                                                                                                                                                      | no action: acyclic at module level; the package pair is a startup bootstrap dependency                                                                    |
| A-S1-07  | Module cycle `api.error_handlers` <-> `api.stage_failure_log` (`error_handlers:466` function-local import)                                                                                                                                   | Low      | Two modules in one package; one edge is a function-local import with a lint suppression.                                                                                                                                                                                                                                                             | no action: contained inside one package, single local edge                                                                                               |
| A-S1-08  | Module cycle of five in `graph_gateway` (`gateway`, `reconciler`, `retry`, `startup_retry`, `swap`)                                                                                                                                          | Low      | All back-edges are `from ps_service.graph_gateway.gateway import GatewaySettings` under `TYPE_CHECKING` (`reconciler:23`, `retry:32`, `startup_retry:21`, `swap:32`); no runtime cycle.                                                                                                                                             | no action: type-only; moving `GatewaySettings` is a cosmetic option for the gateway owner                                                                |
| A-S1-09  | Module cycle `graph_cleanup.dependencies` <-> `graph_cleanup.graph_submission`                                                                                                                                                               | Low      | `graph_submission:58` imports `dependencies.GraphCleanupDependencies` under `TYPE_CHECKING` only.                                                                                                                                                                                                                                                   | no action: type-only                                                                                                                                     |
| A-S1-10  | Cross-distribution edges and foundation layering                                                                                                                                                                                             | n/a      | `lint-imports`: `ps_cli` imports no `ps_service`; `ps_service` imports no `ps_cli` or `ps_test_support`; `logging`, `dependency_health`, `domain_schema` import no feature package (3 contracts kept, 0 broken).                                                                                                                                    | no action: rules hold; adoption as a CI guard is tracked in #260                                                                                         |

### Slice 2: granularity, outliers and thin packages

Each outlier was read as a list of top-level definitions (`grep -n '^def \|^async def \|^class ' <file>`), not by line count alone.

| id       | location                                                                                                                                          | severity | evidence                                                                                                                                                                                                                                                                                                                                                | disposition                                                                                                                           |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| A-S2-01  | `ps-service/src/ps_service/mcp_interface/mcp_server.py` (4538 lines, 84 top-level definitions)                                                      | Medium   | One module holds ingestion-run tools, change check, near-miss tools, catalog-source tools, restore, invitations, `cypher` and `domain_concepts`, access-role and audit tools, policy-draft tools with their parsers, and graph-cleanup tools. The container document names the file in its Implementation Guide (`:1039`).                              | issue #272 (split recommendation inside)                                                                                              |
| A-S2-02  | `api/ingestion_orchestration.py` (2208 lines, 65 definitions)                                                                                       | Medium   | Stage protocols and wiring (L119-487), CELEX/Cellar/short-name resolution and collision checks (L535-1133), the catalog pipeline (L1190-1733), the internal-seed pipeline (L1733-2065) and default openers (L2065-2151).                                                                                                                                 | issue #273                                                                                                                            |
| A-S2-03  | `main.py` (1170 lines, 28 definitions)                                                                                                            | Medium   | ASGI middleware classes (L125-219), loopback-bypass guard (L253-304), startup migrations and graph replay/drift lifecycle (L351-716), dependency probes (L304-344, L737-816) and `create_app` (L844-1135).                                                                                                                                                 | issue #273                                                                                                                            |
| A-S2-04  | `policy_lifecycle/service.py` (2227 lines, 46 definitions)                                                                                        | Low      | Draft creation (L537), read view (L810), four transitions with gates and fork cascade (L895-1740) and three draft-edit use cases (L1741-2227) in one component module.                                                                                                                                                                                   | issue #273                                                                                                                            |
| A-S2-05  | Remaining modules over 800 lines: `graph_gateway/gateway.py`, `policy_lifecycle/graph_writer.py`, `graph_gateway/store.py`, `company_merge/graph_writer.py`, `ingestion/adapters/internal_seed/persist.py`, `graph_cleanup/executors.py`, `api/change_check_orchestration.py`, `domain_mapper/derivation.py`, `passkey_signing/router.py`, `config.py`, `graph_cleanup/models.py`, `api/restore_orchestration.py`, `graph_cleanup/service.py`, `graph_cleanup/group_builders.py`, `api/routes.py` | n/a      | Definition lists show one responsibility each: the gateway class and its settings; a Postgres log store; per-node-family primitives builders and persisters; one builder pipeline for the internal seed; execute/verify pairs per cleanup action; a route table; a flat `ServiceConfig` plus its field parsers; plain data models. Size follows the number of operations, not mixed concerns. | no action: single responsibility per module; revisit if a split is wanted for navigation only                                           |
| A-S2-06  | `authz/store.py` (794), `company_merge/dedup.py` (793)                                                                                            | n/a      | Just below the 800-line mark; one store class, one dedup pipeline.                                                                                                                                                                                                                                                                                            | no action: below the split threshold, one responsibility |
| A-S2-07  | Outliers outside `ps-service`: `tools/company-merge/company_merge_similarity_sweep.py` (1055), `tools/domain-mapper/exclusion_audit.py` (874), `ps-cli` `http_client.py` (745), `device_flow.py` (601), `ps-test-support` `mock_oidc_provider.py` (593) | n/a      | Each is one command or one client with its data classes and parsers; `http_client.py` is the REST client plus response parsers, `mock_oidc_provider.py` one mock server. None mixes unrelated tools.                                                                                                                                                          | no action: single-purpose scripts and clients                                                                                          |
| A-S2-08  | Thin packages `dependency_health` (160 lines, fan-in 15, fan-out 0), `query_engine` (463), `invitations` (483)                                      | n/a      | `dependency_health` is a registry leaf. `query_engine` holds the write-clause guard and bounded execution (`cypher_query.py:58`, `:157`) so it is not a pass-through. `invitations` is a client, service and startup hook.                                                                                                                                | no action: coherent components                                                                                                        |
| A-S2-09  | Thin modules: `state_migrations.py` (36), `api/catalog_entry.py` (34), `company_merge/similarity.py` (42), per-package `errors.py` files (21-41)   | n/a      | Each defines one value type, one pure function or one exception family; none only forwards a call. `api/catalog_entry.py` exists so the ingest path avoids importing `api.catalog`.                                                                                                                                                                       | no action: not trivial wrappers                                                                                                       |
| A-S2-10  | `ps_service.domain_schema` has no row in the C4 Component Overview; the Domain Path column writes `ps.service.domainmapper` where the package is `ps_service.domain_mapper` | Low      | `domain_schema` is cited at `docs/architecture/ps-service-container-architecture.md:1029`, `:1655`, `:1800` but missing from `:303-326`. `state_migrations` and `config` are single modules covered by Persistence (`:322`) and Process Harness (`:326`); `api` is deliberately unnamed (`:74`, `:2301`). Every multi-word package differs in spelling. | issue #254 (comment posted; documentation audit owns the edit)                                                                        |

### Slice 3: duplicated helpers and abstractions

| id       | location                                                                                                                                                                                                                                                      | severity | evidence                                                                                                                                                                                                                                                                                                           | disposition                                                                                                                       |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| A-S3-01  | `edge_identity` in `change_monitor/graph_submission.py:166`, `graph_cleanup/graph_submission.py:187`, `policy_lifecycle/graph_submission.py:212`, `company_merge/graph_submission.py:70`, `ingestion/adapters/internal_seed/reconcile.py:62`                  | Medium   | Body-identical. The docstring at `change_monitor/graph_submission.py:166-172` calls it "a deliberate near-duplicate (pinned equal to Graph Cleanup's by a test)"; three tests pin the copies (`tests/change_monitor/test_succession_gateway.py:428`, `tests/graph_cleanup/test_graph_submission.py:154`, `tests/policy_lifecycle/test_graph_submission.py:189`). `restore/leg_primitives.py:44` has a different signature. | issue #274 (consolidating a recorded independence decision is a design choice)                                                    |
| A-S3-02  | `single_tenant_graph_name` in `change_monitor/falkordb_client.py:123`, `policy_lifecycle/graph_submission.py:174`, `company_merge/falkordb_client.py:150`; `_open_single_tenant_graph` in `graph_cleanup/dependencies.py:70`, `api/near_miss_review_orchestration.py:339`, `api/ingestion_orchestration.py:2087` | Low      | Body-identical within each group; the `policy_lifecycle` docstring calls its copy a deliberate near-duplicate and the `change_monitor` one says it mirrors Company Merge's exactly. The `api` wrappers `_single_tenant_graph_name` (`ingestion_orchestration.py:1895`) and `_default_single_tenant_graph_name` (`restore_orchestration.py:795`) delegate to Company Merge's function and are not copies. | issue #274                                                                                                                        |
| A-S3-03  | `read_log_position` in `graph_cleanup`, `policy_lifecycle`, `restore` `graph_submission.py`; `require_caught_up` in `ingestion`, `graph_cleanup`, `policy_lifecycle` `graph_submission.py`                                                                    | n/a      | Same shape, not equal: each maps failures to its component's exception, and the signatures differ (`ingestion` generic over the error type, `restore` takes a gateway and a graph name).                                                                                                                           | no action: per-component error translation, not duplication                                                                         |
| A-S3-04  | `_unstorable` in `ingestion/graph_writer.py:156`, `domain_mapper/graph_writer.py:125`, `company_merge/graph_writer.py:164`, `ingestion/adapters/internal_seed/persist.py:786`                                                                                   | n/a      | Same shape, each raises a different exception type (`IngestionPersistenceError`, the Domain Mapper, Company Merge and `InternalSeedError` classes); consolidation would need a parametrised factory, which is not a behaviour-neutral change.                                                                      | no action: not equal; component-specific exception types                                                                           |
| A-S3-05  | `connect`, `select_graph`, `connect_from_config`, `native_graph_name` copies in the `falkordb_client.py` modules; `GraphHandle` (6 copies) and `GraphQueryResult` (6) protocols; `_ConnectivityProbe` (3, identical in `change_monitor`, `domain_mapper`, `company_merge`) | Medium   | Body-identical groups of 5 (`connect`, `select_graph`), 6 (`connect_from_config`, including the gateway's) and 3 (`native_graph_name`, `_ConnectivityProbe`); protocol bodies fall into several unequal groups. The open issue #231 states this scope.                                                                                                         | issue #231 (comment posted)                                                                                                      |
| A-S3-06  | `export/serialize.py:48`, `export/embeddings.py:55` private `_QueryResult` protocols and the `_rows` helpers beside them                                                                                                                                      | Low      | Body-identical, same package; #231 names Export's graph-handle protocols and a shared read helper.                                                                                                                                                                                                                 | issue #231 (comment posted)                                                                                                      |
| A-S3-07  | `_FetchResponse` in `curated_source/http_fetch.py:23`, `ingestion/adapters/cellar_eli/fetch.py:44` (identical) and `invitations/client.py:35` (different); `_RegulatoryInstrumentNode` in `domain_mapper/extraction.py:49`, `company_merge/graph_reader.py:173` (identical) | n/a      | Private structural `Protocol`s limited to the fields each module reads. The Company Merge copy states "this component keeps its own copy rather than importing the Domain Mapper's".                                                                                                                               | no action: documented component independence, private structural types                                                            |
| A-S3-08  | `GraphWriteTarget` (`graph_cleanup`, `policy_lifecycle`, `company_merge` `graph_submission.py`), `_GraphQueryHandle` (`graph_gateway/key_port.py:208`, `export/falkordb_connection.py:20`), `_Overlay` (`graph_gateway/noop_filter.py:163`, `restore/merge_primitives.py:99`), `_QueryResult` in `api/ingestion_orchestration.py:119` | n/a      | Same names, unequal definitions.                                                                                                                                                                                                                                                                                    | no action: false positive (different shapes)                                                                                      |

### Slice 4: boundaries between ps-service, ps-cli, tools and ps-test-support

One-owner matrix (who owns each shared piece of logic):

| Shared logic                                             | Owner                                                           | Other places                                                                                       | Verdict |
| -------------------------------------------------------- | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | ------- |
| REST request/response shapes (`StageOutcome`, error body) | `ps_service.api.models` (server)                                | `ps_cli/models.py`: client-side `TypedDict`s of the wire format                                    | one owner per side; the wire format is the contract |
| Local seed-file JSON Schema check                        | `ps_cli/intake_validation.py` (pre-flight)                      | `ps_service` validates the same file server-side (module docstring says so)                        | deliberate defence in depth |
| OIDC discovery, JWT verification                         | `ps_service/auth` (resource server)                             | `ps_cli/oidc_discovery.py` (client: resolves metadata, no verification)                            | different roles, no shared logic |
| Domain graph reads/writes                                | `ps_service` components                                         | `tools/*` import `ps_service` modules as a library; no tool re-implements a component              | one owner |
| Raw graph loading and capability merging for local data  | `tools/graph-ingestion/*.py` (own `falkordb` access, no `ps_service` import) | `ps_service.ingestion` / `graph_cleanup` do the governed equivalents                    | dev utilities, see A-S4-02 |
| Read-only Cypher shell                                   | `tools/graph-query/ps.py`                                       | `ps-plugin` MCP connector (the module docstring marks the tool deprecated for end users)           | superseded for users, kept for contributors |
| Test doubles and live-test markers                       | `ps-test-support` (workspace member, dev dependency of `ps-service` and `ps-cli`) | consumed by 27 tracked files; not imported by any source module (import-linter contract 2) | one owner |

Checks run: `ps_cli` never imports `ps_service` and `ps_service` never imports `ps_cli` or `ps_test_support` (contracts 1 and 2 kept). All 12 files under `tools/` parse under Python 3.14 (`ast.parse`, no execution).

| id      | location                                                                                                                                                                                                  | severity | evidence                                                                                                                                                                                                                                                                                                                  | disposition                                                                                                                         |
| ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| A-S4-01 | `ps-cli/src/ps_cli/http_client.py:98,111`, `device_flow.py:130,148`, `oidc_discovery.py:150,172`: `_raise_connection_error` and `_raise_read_timeout_error` (three copies each)                           | Low      | Not duplicates: each targets a different host and raises different text or type. `http_client` words messages "PS Service at {base_url}" and uses a PS_CLI_SERVICE_URL hint; `device_flow` names the bare URL with a generic hint and `PsCliError`; `oidc_discovery` raises `CannotVerifyError` with a `detail` field. Docstrings in the latter two state the separation | no action: the bodies differ in exception type, message and hint, so unifying changes user-visible output (not behaviour-neutral) |
| A-S4-02 | `tools/graph-ingestion/load_graph.py`, `find_capability_duplicates.py`, `merge_capabilities.py`                                                                                                           | Low      | They open FalkorDB directly (`from falkordb import FalkorDB`), outside the governed write gateway, and import nothing from `ps_service`. Their README describes reusable local dataset loaders; the data model and write-path concerns belong to the data-model audit                                                      | no action: documented contributor utilities with their own README; write-path and schema questions are owned by #256                |
| A-S4-03 | `tools/curated-export/export_instrument.py:54`, `tools/backup-restore-verify/seed_content.py:24-26`: import `ps_service.api.*` (`export_audit_actions`, `graph_write_access`, `models`, `restore_orchestration`) | Low      | Operator tools reuse entry-layer glue as a library, the same dependency the audit counts for components in A-S1-01. They are not workspace members (`pyproject.toml:2`) but are in strict-lint scope                                                                                                                     | issue #270: the move of shared `api` helpers decides these import paths; the tools are updated with it                              |
| A-S4-04 | `ps-test-support/src/ps_test_support/mock_oidc_provider.py` (593 lines)                                                                                                                                   | n/a      | Single responsibility (one mock IdP); size comes from the device-flow and token endpoints                                                                                                                                                                                                                                 | no action: one cohesive test double                                                                                                  |

## False positives and reasons

Same-named functions that are not duplicates (bodies differ). The three `_raise_connection_error` and `_raise_read_timeout_error` pairs in ps-cli also differ in exception type, message and hint (A-S4-01).

- `check_connectivity` (7 modules, all bodies unequal) and `check_connectivity_from_config` (4, all unequal): each probes its own resource (FalkorDB, the LLM provider, Cellar, Postgres).
- `_row_from_record` (5 stores, all unequal), `_rows` in `change_monitor/succession.py:108` against Export's `_rows` (unequal), `_log` (4), `_text` (3), `_edge` (3), `reset_for_tests` (4 registries) all unequal by AST; `create_policy_draft` (service, graph writer, MCP tool: three layers of one operation) and `main` (3 entry points) share only a name.
- `connect` and `select_graph` in `graph_gateway/connection.py` differ in body from the five `falkordb_client` copies (the `connect_from_config` bodies do match, see A-S3-05).

## Superseded and removed items

Nothing was superseded: every finding is routed to an issue or recorded as no action, and no source, test, configuration or documentation file was changed. Proof that no tracked file changed, and that no symbol was removed:

```text
git status --short   ->   ?? docs/audits/architecture-audit.md   (the only change)
git diff --stat      ->   (empty: no tracked file modified)
```

Because no symbol was removed, the repository-wide removal search (excluding `docs/audits` and `.orchestrator`) has nothing to look for.

## Sibling-issue review

Every issue below was read from its body (`gh issue view <n> --json body`), not from its title. "Commented" means a `gh issue comment` was posted because scope, dependencies or order changed.

| Issue | Subject                                      | Outcome                                                                                                                                  |
| ----- | -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| #231  | graph-handle protocol copies                 | Commented: Export's `_QueryResult`/`_rows`, `_ConnectivityProbe`, `_FetchResponse` review; sequence with #274                            |
| #251  | coding-standards conformance                 | Commented: module-size outliers owned by #272 and #273                                                                                   |
| #252  | secure code and deployment security          | No change needed: scope is behaviour and deployment security; #270 and #273 move code but keep package names; no overlap in findings     |
| #253  | logging cleanliness and data leakage         | No change needed: the audit found no logging-layer edge into feature packages (contract 3 kept)                                           |
| #254  | documentation vs code                        | Commented: `domain_schema` row gap, path naming drift, paths that change if #272/#273/#270 land                                          |
| #255  | API and contract consistency                 | Commented: sequence error/model reshaping with #270                                                                                      |
| #256  | data model, persistence, state migrations    | Commented: `provision` <-> `state_migrations` cycle classified as bootstrap; #274 touches the graph-name scheme                          |
| #257  | configuration, scripts and chart hygiene     | Commented: `config.py` cycle with `curated_source` is in #271; sequence config-key findings with it                                      |
| #258  | dependencies, licenses, base images          | No change needed: no dependency or lockfile change was made or proposed by this audit                                                    |
| #259  | observability, resilience, operability       | Commented: #273 splits `main.py`; `dependency_health` is a correct leaf                                                                  |
| #260  | automated guardrails                         | Commented: import-linter contracts ready for adoption; the `entry-layer` contract waits on #270                                          |
| #263  | production symbols reachable only from tests | Commented: `_RegulatoryInstrumentNode` is recorded as deliberate independence (A-S3-07); sequence deletions before #270/#273 moves       |
| #264  | declare starlette and redis dependencies     | No change needed: dependency declaration only; the audit's redis-using modules (graph writers) are unaffected by the proposed pin        |
| #265  | remove closed spikes                         | No change needed: `spikes/` is outside the audited trees and was not touched                                                              |
| #266  | blame-ignore placeholder revision            | No change needed: repository hygiene only                                                                                                |
| #267  | single-source reasoning skill                | No change needed: `.claude`/`.github` skills are not in scope                                                                            |
| #268  | identical-body tests claiming distinct needs | No change needed: test strength only; none of the listed test files tests a module this audit proposes to move                           |
| #269  | mutation-testing gaps                        | No change needed: test-only; the audit changed no test                                                                                   |

Commented issues: #231, #251, #254, #255, #256, #257, #259, #260, #263, #270.

## Acceptance criteria confirmation

| AC         | Status | Evidence in this document                                                                                                           |
| ---------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| AC-BI-001  | met    | Scope examined, Method and commands (reproducible `uvx` commands), Findings tables with id, location, severity, evidence, disposition |
| AC-BI-002  | met    | Slice 1: A-S1-01..A-S1-10 with the causing edge and import kind; the broken `entry-layer` contract output                            |
| AC-BI-003  | met    | Package measurements and Slice 2: A-S2-01..A-S2-10 with split or merge recommendations                                               |
| AC-BI-004  | met    | Slice 3: A-S3-01..A-S3-08 each routed to #274, #231 or no action with a reason (R-1: consolidation is a design decision)             |
| AC-BI-005  | met    | Slice 4 one-owner matrix across ps-service, ps-cli, tools and ps-test-support                                                        |
| AC-BI-006  | met    | Splits, moves and consolidations filed as #270-#274 with options and a recommendation; none done inline                              |
| AC-BI-007  | met    | Every findings row ends in `issue #N` or `no action: <reason>`; no row is "fixed in this change" because no inline fix qualified     |
| AC-BI-008  | met    | #270-#274 each carry problem, options, recommendation; all are native sub-issues of #246                                             |
| AC-BI-009  | met    | All filed issues are `refactor(...)` (behaviour-neutral); no shipped behaviour change was found or bundled                           |
| AC-BI-010  | met    | No fix was made, so no root-cause or targeted-test obligation arises; the full suite was not rerun (decision D-10), baseline 8063 passed, 1 skipped at the base commit |
| AC-BI-011  | met    | Nothing became obsolete; see Superseded and removed items (empty change set)                                                         |
| AC-BI-012  | met    | The document and the bodies of #270-#274 were scanned for secret, credential, key, token, exploit and CVE terms: no hits; no vulnerability was found |
| AC-BI-013  | met    | Sibling-issue review above covers #251-#260 and #263-#269 (plus #231) and the five issues filed here                                  |

## Issues filed by this audit

| Issue | Title                                                                                                                  |
| ----- | ---------------------------------------------------------------------------------------------------------------------- |
| #270  | `refactor(ps-service)`: move shared errors, models and write-access helpers out of api                                 |
| #271  | `refactor(ps-service)`: break the config/curated_source and domain_mapper/ingestion package cycles                     |
| #272  | `refactor(mcp_interface)`: split mcp_server.py by tool group                                                           |
| #273  | `refactor(ps-service)`: split api/ingestion_orchestration, main and policy_lifecycle/service                           |
| #274  | `refactor(ps-service)`: decide whether the per-component edge_identity and graph-name helper copies stay independent  |
