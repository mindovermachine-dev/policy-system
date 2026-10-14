<!-- © 2026 Cartman ApS. All rights reserved. -->

# Dead Code and Leftovers Audit - Findings Report (Issue #247, umbrella #246)

## Scope examined

Covered: `ps-service/src`, `ps-cli`, `ps-test-support`, `ps-question-skill`, `ps-test-plugin`, `tools`, `scripts`, `charts`, `deploy`, config keys and environment variables.

| Path                | Tracked files | Files on disk |
| ------------------- | ------------- | ------------- |
| `ps-service/src`    | 282           | 282           |
| `ps-cli`            | 52            | 53            |
| `ps-test-support`   | 7             | 7             |
| `ps-question-skill` | 1             | 1             |
| `ps-test-plugin`    | 0             | 0             |

Counts: `git ls-files <dir> | wc -l` (tracked) and `find <dir> -type f -not -path '*/__pycache__/*' -not -name .DS_Store | wc -l` (on disk). The extra `ps-cli` file on disk is an ignored `.coverage` data file.

## Method and commands

- Tools run ephemerally, no dependency or lock change: `uvx vulture` 2.16, `uvx deptry` 0.25.1.
- Two-pass reachability: `uvx vulture <src> --min-confidence 60 --sort-by-size`, then the same with the package's `tests` directory appended. A symbol flagged only in the first pass is reachable from tests alone.
- Per candidate: word-boundary `grep -rnw` over source, tests, scripts, tools, docs, charts and skills, plus string-form search for decorator, registry and MCP tool names.
- Reflection search: `grep -rn "getattr(\|importlib\|__import__" ps-service/src ps-cli/src`.
- Removal proof: `grep -rn "<symbol>" --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv --exclude-dir=.orchestrator --exclude-dir=spikes --exclude-dir=audits --exclude-dir=__pycache__ .` returns nothing. The proof excludes `docs/audits/` (this document), `.orchestrator/` and untracked `spikes/`.
- Public contract source for REST routes and MCP tools: `docs/architecture/ps-service-container-architecture.md`.

## Severity scale

- High: reachable defect or contract risk caused by the leftover (wrong behaviour, security exposure).
- Medium: misleading or costly leftover (dead code that a reader can mistake for live behaviour, orphaned config).
- Low: inert leftover with no behavioural effect.

## Findings

| id       | location                                                                | severity | evidence                                                                                                                                                      | disposition                                                                                            |
| -------- | ----------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| F-S1-01  | `ps-service/src/ps_service/api/models.py` `ErrorBody`, `ErrorDetail`    | Low      | Zero references in source, tests, docs; `ErrorDetail` used only by `ErrorBody`; not a route response model.                                                   | fixed (both classes deleted)                                                                           |
| F-S1-02  | `ps-service/src/ps_service/graph_gateway/index_manager.py` `ensure_id_indexes` | Low | Zero references anywhere; thin wrapper over `IdIndexes(...).ensure`, which stays in use.                                                                  | fixed (function deleted)                                                                               |
| F-S1-03  | `ps-service/src/ps_service/company_merge/models.py` `CanonicalNodeProperties` | Low | Type alias with zero uses; only a docstring mentioned it.                                                                                               | fixed (alias and docstring mention deleted)                                                            |
| F-S1-04  | `ps-service/tests/graph_gateway/test_digest_order.py.bak`               | Low      | Tracked backup copy of the live `test_digest_order.py` (older text, same tests); not collected by pytest.                                                      | fixed (file deleted)                                                                                   |
| F-S1-05  | 19 production symbols reachable only from tests (graph_gateway, change_monitor, company_merge, domain_schema, ingestion_runs, passkey_signing, api/catalog) plus the unread `policy_governor` index in `internal_seed/persist.py` | Medium | Second vulture pass removes them; grep finds no caller outside `ps-service/tests`. Each is a candidate seam or dead code and needs a per-component decision. | issue #263                                                                                             |
| F-S1-06  | `mcp_server.py` tool functions (`ingest_regulation`, `near_misses_*`, `*_tool`, ...) | n/a | Registered by decorator; names are the public MCP contract in the container architecture document.                                                    | no action: reachability false positive (decorator registration, public contract)                      |
| F-S1-07  | `__call__` on `Protocol` classes (`cellar_eli`, `curated_source`, `auth/discovery`, `invitations/client`, `company_merge/governance_import`, `change_monitor`) | n/a | Protocol members implemented by callables injected at wiring time.                                                         | no action: protocol members                                                                            |
| F-S1-08  | Pydantic `model_config`, model fields, `_check_*` validators in `api`, `audit`, `domain_mapper`, `graph_cleanup`, `graph_gateway`, `ingestion`, `ingestion_runs` | n/a | Read by Pydantic at validation time (`runtime-evaluated-base-classes` in `pyproject.toml`).                                                  | no action: framework-read fields and validators                                                        |
| F-S1-09  | `Gateway.recover`/`check_drift`/`is_reconciling` family, `graph_gateway/store.py` `read_groups_by_audit_event` | Low | Overlaps the open graph-handle protocol consolidation (#231) and graph-state backup rework (#202).                                          | issue #263 (cross-reference only; not touched here)                                                    |
| F-S2-01  | `ps-cli/src/ps_cli/http_client.py` `PsServiceClient._post`              | Low      | Zero callers in source or tests; `_authenticated_post` is the only POST path used.                                                                            | fixed (method deleted)                                                                                 |
| F-S2-02  | `ps-cli/src/ps_cli/toml_writer.py` `format_flat_table`                  | Low      | Only referenced by its own three tests and two docstrings; no writer calls it.                                                                                | fixed (function, its three tests and docstring mentions deleted)                                       |
| F-S2-03  | `ps-cli/src/ps_cli/models.py` nine `*Body` TypedDicts                   | Low      | Unused by any code; two named in parser docstrings. They document the wire shapes the dataclass parsers consume; the coding standard prescribes `TypedDict` for dictionary schemas. | no action: wire-shape documentation types per level 2 standard                                         |
| F-S2-04  | `ps-cli/src/ps_cli/intake_validation.py` `Any`, `ValidationError` imports | n/a | Used inside `cast("...")` string types; ruff and basedpyright clean.                                                                                     | no action: false positive                                                                              |
| F-S2-05  | `ps-test-support` `MockOidcProvider` handlers, device-flow helpers, `jwks_request_count`, `last_token_request_form`, `rotate_key`, `mock_oidc_provider_fixture`, `REQUIRED_STARTUP_ENV`, `serialize_live_markers` hooks | n/a | HTTP handler overrides, pytest fixtures and plugin hooks; each helper is used by tests in `ps-service` or `ps-cli`.                    | no action: test-support helpers and pytest plugin hooks                                                |
| F-S2-06  | `ps-cli/tests` HTTP handler classes and fixtures (`do_GET`, `log_message`, `wrong_host_server`, ...) | n/a | Overrides of `BaseHTTPRequestHandler` and pytest fixtures resolved by name.                                                                           | no action: false positive                                                                              |
| F-S2-07  | `ps-question-skill/.gitkeep`                                            | Low      | Directory holds only an empty placeholder; owning issue #53 is closed (delivered as `ps-skills/ps-plugin`). Only reference was a scope sentence in the level 2 coding standard. | fixed (placeholder deleted; standard sentence updated)                                              |
| F-S2-08  | `ps-test-plugin/`                                                       | Low      | No tracked files, no files on disk apart from empty cache directories; reported ignored by `git status --ignored` although `git check-ignore -v` names no rule. | no action: local environment leftover; owner may delete the empty directory locally                   |
| F-S3-01  | `tools/graph-ingestion/load_all.sh`                                     | Medium   | Last `load` call pointed at `test-data/engineering-practices/...`, a directory that does not exist; the seed lives under `internal-sources/engineering-practices/`. The script (named in `tools/graph-ingestion/README.md`) would fail at its last step. Seed JSON has the `nodes`/`edges` shape `load_graph.py` reads. | fixed (path corrected; `bash -n` clean)                                                                |
| F-S3-02  | `scripts/ps-reset-data.sh`                                              | Low      | Only reference is its own header. Self-described evaluator helper that pairs with `scripts/ps-upgrade.sh`; no doc names it.                                  | no action: manual operator entry point; documenting it is docs work (#254)                             |
| F-S3-03  | `tools/` and `scripts/` files with callers only in tests or docs (`graph_digests.py`, `seed_content.py`, `seed_grants.py`, `ps.py`, `measure_skills.py`, `grant_access_role_client.py`, `company_merge_similarity_sweep.py`, `exclusion_audit.py`, `tools/skills/reasoning.md`, `*.conf`, `release/*.sh`, `lib/*`) | n/a | Each is named by a CI workflow, hook, `.insitu.yml`, another live script, a runbook, a skill that reads it (`reasoning.md`), or a CLI test pinned in `ps-service/tests/{tools,scripts,release,deploy_*}`. | no action: live or operator entry points                                                               |
| F-S3-04  | vulture on `scripts tools`: `score_pair`, `select_sample_units`, `capture_unit_record`, `propose_classification`, `selection_tier`, `tier_b_match_text`, `_FalkorDB`, `q` | n/a | Functions are imported by tests by path; `_FalkorDB`/`q` are typing stubs for an optional import. | no action: reachability false positives                                                                |
| F-S4-01  | `charts/policy-system/values.yaml`, `values-prod.yaml` (every key path outside the `authentik` subchart block) | n/a | Each key path is referenced by a template or file through `.Values.<path>`; `psPostgres.provisioning.connectTimeoutSeconds` is read through the `$provisioning` alias. `values-prod.yaml` overrides only existing keys plus the subchart `authentik.global`. | no action: no unused value                                                                             |
| F-S4-02  | `charts/policy-system/templates/_helpers.tpl` defines                   | n/a | Every `define` has at least one `include` consumer.                                                                                                           | no action                                                                                              |
| F-S4-03  | Templates, `files/*`, `tests/*`, `deploy/kind/cluster.yaml`             | n/a | All 25 templates have a gate or consumer; 33 helm-unittest suites present, none orphaned; `files/*` are embedded by templates; `cluster.yaml` is used by the installation guide (`kind create cluster --config`) and a structural test. Empty untracked `tests/__snapshot__` ignored. | no action                                                                                              |
| F-S5-01  | `PS_SERVICE_MAX_REQUEST_BODY_BYTES`                                     | Low      | Read by `config.py`, enforced by middleware and tested, but set in no chart or script and absent from the docs (undocumented knob).                           | no action: live; documentation gap owned by #254                                                       |
| F-S5-02  | Env vars read only by scripts (`PS_SERVICE_STARTUP_TIMEOUT_SECONDS`, `PS_SERVICE_SHUTDOWN_TIMEOUT_SECONDS`, `PS_CLI_CONTEXT`, `PS_SERVICE_LOCAL_URL`, `PS_HOSTS_FILE`, `PS_*_PYPROJECT_PATH`, `PS_POSTGRES_ADMIN_USER`, `PS_RELEASE_GH`, `PS_TLS_*`, `PS_BLUEPRINT_*`, `PS_PORT_FORWARD_*`, `PS_EVAL_LEAF_VALID_DAYS`) | n/a | Script-local test and override knobs read by the named scripts (several pinned by script tests).                    | no action: script-internal overrides                                                                   |
| F-S5-03  | Env vars set in chart templates (`PS_*`, `AUTHENTIK_*`, ...)            | n/a | Every `PS_*` name set in a template is read in `ps-service/src`; `POSTGRES_DB`, `POSTGRES_PASSWORD` are consumed by the Postgres image.                      | no action: no dead config                                                                              |
| F-S5-04  | `ServiceConfig` fields (36) and `runtime_config` registry (one key, `curated_source`) | n/a | Every field has at least one consumer outside `config.py`; the single registered key is consumed by the curated-source component.             | no action                                                                                              |
| F-S5-05  | Documentation names `PS_AUTH_`, `PS_LLMINTERFACE_`, `PS_STATE_POSTGRES_`, `PS_PASSKEYSIGNING_POSTGRES_` and test-only `PS_STATE_*`, `PS_SIGNING_*`, `PS_CONTAINER_*`, `PS_TEST_POSTGRES_SUPERUSER_DSN`, `PS_MCP_URL` | n/a | Prefix wildcards and test, CI or plugin-manifest variables; no doc names a variable absent from code. | no action: no documentation drift found                                                                |
| F-S6-01  | `tools/graph-ingestion/merge_capabilities.py` usage docstring           | Low      | Example path `docs/test-data/eu-regulations/capability_merges.json` does not exist; the file lives at `test-data/eu-regulations/capability_merges.json` (as `tools/graph-ingestion/README.md` says). | fixed (path corrected)                                                                                 |
| F-S6-02  | `ps-skills/ps-plugin` (14 skills, 10 rubrics and templates, 18 reference files, `.mcp.json`, `plugin.json`) | n/a | Skills are discovered by directory; every reference file is named by its `SKILL.md`; every rubric is named by `scoring-model.md`, `dataset-gates.md` or a sibling; no relative markdown link is broken. | no action: all live                                                                                    |
| F-S6-03  | `ps-skills/ps-plugin/rubrics/{scoring-model,dataset-gates}.md` mention the retired `test-data/rubrics/...strict-rubrics.md` | n/a | Both sentences state that the file was retired and why; they are history, not live pointers.                                                                 | no action: intentional historical statement                                                            |
| F-S6-04  | `curated-content/` (10 instrument directories, `catalog.json`, `README.md`, `LICENSING.md`) | n/a | `catalog.json` lists exactly the 10 directories; each holds `manifest.json`, `baseline.json`, `native.json`; consumed by the curated-source component, export and restore tools and many tests. Persistent restore data. | no action: live data, kept                                                                             |
| F-S6-05  | `test-data/eu-regulations/` (`cra.json`, `nis2.json`, `gdpr.json`, `policy_system_graph.json`, `capability_merges.json`, three methodology notes, README) | n/a | JSON files are loaded by `tools/graph-ingestion/load_all.sh`, `load_graph.py`, `exclusion_audit.py` and the tool README; methodology notes are listed in the directory README and cross-reference each other. | no action: all referenced                                                                              |
| F-S7-01  | `spikes/eval-install-path`                                              | Low      | Untracked and gitignored (0 tracked files). Owner #165 (closed).                                                                                             | issue #265 |
| F-S7-02  | `spikes/remote-mcp-authentik-login`, `spikes/remote-mcp-authentik-plugin-http` | Low | Untracked and gitignored. Owners #167 and #168 (both closed).                                                                                        | issue #265 |
| F-S7-03  | `spikes/remote-mcp-entra-login`                                         | Low      | Untracked and gitignored. README references #53, #57, #58; the matching spike issue is #65 (closed).                                                         | issue #265 |
| F-S7-04  | `spikes/deploy-ps-azure`                                                | Medium   | Untracked and gitignored. README warns of live billable resources, not torn down; promoted into a supported script by #111 (closed); no open owning issue verified. | issue #265 |
| F-S7-05  | Open spike issues #233 and #238                                         | n/a      | Both open; no matching directory exists under `spikes/` yet.                                                                                                  | no action: active, work not yet started locally                                                        |
| F-S8-01  | `ps-service` `starlette` and `redis` imported directly but undeclared (transitive via `fastapi`, `falkordb`) | Low | deptry run via `uvx`: 3 and 14 import sites; `pyjwt` DEP002 is a scan artifact (imported as `jwt`). | issue #264                                                                                             |
| F-S8-02  | `ps-cli` and `ps-test-support` dependencies                             | n/a      | `ps-cli`: no unused or undeclared dependency. `ps-test-support`: `cryptography` comes from the declared `pyjwt[crypto]` extra; `_pytest` from declared `pytest`. | no action                                                                                              |
| F-S8-03  | `ps-service/pyproject.toml` `hiredis==3.4.2` | Low | Declared and locked, never imported; deptry DEP002. It is the optional C parser accelerator that `redis` loads implicitly when installed, so removal is a performance decision, not dead code. | issue #264 (comment recommends the `redis[hiredis]` extra) |

## False positives and reasons

Classes confirmed reachable and kept: decorator-registered MCP tools, Protocol members, Pydantic fields and validators, `BaseHTTPRequestHandler` overrides, pytest fixtures and plugin hooks, string-typed `cast` imports. Reflection call sites are listed by the reflection search above.

## Removed-symbol proof

Each removed symbol was searched with the removal-proof recipe above and returned no matches: `ErrorBody`, `ErrorDetail`, `ensure_id_indexes`, `CanonicalNodeProperties`, `test_digest_order.py.bak`, `format_flat_table`, `ps-question-skill`, and the `PsServiceClient._post` method. The only remaining hit for the name `_post` is an unrelated test-local helper.

## Superseded and removed items

Removed in this change and proven absent repo-wide by the removal-proof recipe (see the previous section): `ErrorBody`, `ErrorDetail`, `ensure_id_indexes`, `CanonicalNodeProperties`, `format_flat_table`, `PsServiceClient._post`, `test_digest_order.py.bak`, `ps-question-skill/.gitkeep`. The `ps-question-skill` scope sentence in `docs/coding-standards/level2-python-instructions.md` was updated in the same change.

## Sibling issue review

Reviewed against the outcomes above; issue numbers are the open sub-issues of the umbrella and the issues filed by this audit.

| issue | outcome                                                                                              |
| ----- | ---------------------------------------------------------------------------------------------------- |
| #248  | comment: backup file already deleted here; re-scan for other stray artifacts independently           |
| #249  | comment: dependency satisfied; coordinate with #263 on tests of test-only symbols                    |
| #250  | comment: dependency satisfied; #263 seam decisions belong to the boundary review                     |
| #251  | comment: dependency satisfied, no scope change                                                       |
| #252  | no change needed                                                                                     |
| #253  | no change needed                                                                                     |
| #254  | comment: owns docs for `PS_SERVICE_MAX_REQUEST_BODY_BYTES` and `scripts/ps-reset-data.sh`            |
| #255  | no change needed                                                                                     |
| #256  | no change needed                                                                                     |
| #257  | comment: `load_all.sh` repaired here; no dead chart config found                                    |
| #258  | comment: dependency findings handed over as #264; licence and base images remain in full             |
| #259  | no change needed                                                                                     |
| #260  | comment: candidate guardrails (tracked backup files, periodic vulture and deptry, removal-proof grep) |
| #263  | filed by this audit; linked as sub-issue of the umbrella                                             |
| #264  | filed by this audit; comment adds the `hiredis` decision; linked as sub-issue                        |
| #265  | filed by this audit (local spike directories); linked as sub-issue                                   |

## Verification evidence (slices S0-S5)

- S4: `helm unittest charts/policy-system` 33 suites, 255 tests pass; `helm template` output unchanged (no chart file touched).
- S6-S8: grep consumer search per file; `uvx deptry` per package (logs under `.orchestrator/tracker/i247/logs/deptry-*.log`); spike owners checked with `gh issue view`.
- S3: `bash -n tools/graph-ingestion/load_all.sh` clean.

- `uv run basedpyright`: 0 errors, 0 warnings.
- `ruff check` and `ruff format --check` clean on every touched file.
- Targeted tests passed: `ps-cli/tests/test_toml_writer.py` (7), `ps-cli/tests/test_http_client.py` (49), `ps-cli/tests/test_targets.py` (13), `ps-service/tests/graph_gateway/test_digest_order.py` (6), `ps-service/tests/graph_gateway/test_gateway_indexes.py` (7), `ps-service/tests/api/test_models_export.py` (13), `ps-service/tests/company_merge/test_models.py`.

## Enforcement

Enforced rules fail automatically, in the pre-commit hook on the files they watch and in the CI `trunk-worthy` wave over the whole tree. Review-only rules rely on review. Check ids are the hook and CI ids listed in [CONTRIBUTING.md](../../CONTRIBUTING.md#automated-guardrails).

| Rule | Status | Check id or reason |
| --- | --- | --- |
| Unused functions, arguments, variables and imports in production code (confidence 80) | enforced | `unused-symbols` |
| Unused imports and local variables | enforced | `ruff-check` |
| Helm values that no template reads | enforced | `config-drift` (`helm-value-unused`) |
| Environment variables used in code but listed nowhere | enforced | `config-drift` (`env-var-unlisted`) |
| Tracked backup and merge leftovers | enforced | `repo-hygiene` (`tracked-backup-file`) |
| Symbols below confidence 80 and symbols reachable only from tests | review-only | each needs a per-symbol decision first; tracked in #263 and #324 |
| Environment variables listed but no longer read by code | review-only | no tool compares in that direction |
| Removal proof: a repository-wide search for a removed symbol returns nothing | review-only | a reviewer reruns the search; the recipe is in CONTRIBUTING.md |
| Tests left behind by removed code | review-only | a failing import is caught by `pytest`, a test of a removed behaviour is not |
