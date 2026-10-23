<!-- © 2026 Cartman ApS. All rights reserved. -->

# Documentation Audit - Findings Report (Issue #254, umbrella #246)

## Scope examined

In scope: `docs/architecture/*`, `docs/artifacts/*`, `README.md`, `CONTRIBUTING.md`, per-package READMEs, `helm-chart-values-reference.md`, `ps-solution-architecture.md` and `ps-service-container-architecture.md`, plus the deployable units enumerated from the `Dockerfile`, the Helm chart and the packaging metadata.

Examined in this slice group (units and architecture documents): the unit enumeration, the new `ps-cli-container-architecture.md`, `ps-solution-architecture.md` and `ps-service-container-architecture.md` against the code (components, flows, configuration variables, REST routes, MCP tools, data stores named in the text), and the INV-005 and INV-011 checks.

Examined in the second slice group: the remaining PS Service container architecture sections, MCP authorization, Logging, the operations, installation and user guides, `helm-chart-values-reference.md`, READMEs, `CONTRIBUTING.md` and the coding standards' ps-cli and logging sections.

Examined in the third slice group: the history and iteration narrative sweep over every in-scope document (issue numbers, acceptance-criterion ids, removed components, history wording), link and path fixes, and the closing review. The limits that remain are recorded in the acceptance criteria table and in issue #323.

## Method and commands

Tool versions: `uv run --frozen python -B --version` 3.14.6, `helm version --short` v4.3.0, `gh --version` 2.102.0, repository at `e4ba098` plus the uncommitted audit edits. All commands are read-only; `git status --short` shows only the intended document and test edits.

- Unit enumeration: `grep -n 'kind:\|image:' charts/policy-system/templates/*.yaml`, `Chart.yaml`, `Dockerfile` (one image, two stages), `ps-cli/pyproject.toml`, `ls scripts/*.sh | wc -l` (11), `ls ps-skills/ps-plugin/skills` (14 skills), `cat .claude-plugin/marketplace.json`, `ls tools .github .claude/skills .agents/skills docs/architecture/ps-client-mockup`.
- ps-cli surface: `uv run --frozen python -B -m ps_cli --help` and `... <group> <command> --help` for every command (`get health`, `ingest document`, `export instrument`, `config set-context|use-context|get-contexts`, `auth login|status|logout`); modules read under `ps-cli/src/ps_cli/`.
- INV-005: `uv run --frozen python -B .orchestrator/tracker/i254/tools/inv_check.py` compares the domain path of every row of each container architecture's "C4 Component Overview" with the solution architecture's "Component Breakdown".
- MCP tools: `server.list_tools()` of `ps_service.mcp_interface.mcp_server` (38 tools) compared by name with the container architecture text.
- Configuration variables: `grep -rhoE 'PS_[A-Z0-9_]+'` over `ps-service/src charts scripts deploy ps-cli/src` compared with the same extraction over the two architecture documents.
- REST routes: `GET|POST /...` strings in the two architecture documents compared with `add_api_route` registrations in `ps-service/src/ps_service`.
- Readiness model: `_GATING_DEPENDENCIES`, `_READY_DEPENDENCIES`, `_check_dependencies_at_startup`, `_retry_gating_dependencies` and `ready()` in `ps-service/src/ps_service/main.py`; constants in `dependency_health/registry.py`.
- MCP authorization: `server.list_tools()` descriptions and the `TOOL_GATES` declarations in `mcp_interface/tool_gates.py` and the `check_role` calls in `policy_lifecycle/service.py` and `graph_cleanup/executors.py`, with the rules in `authz/service.py`, `authz/request_gate.py` and `authz/rules.py`.
- Identifier and path check: every backticked identifier of the container architecture searched in `ps-service/src charts scripts ps-cli/src ps-skills deploy` (only action names remain unmatched); every `*.py`, `*.sql`, `*.yaml`, `*.sh` file name resolved with `git ls-files`; every `ps_service.*` module resolved to a file or package.
- Registration coverage: every `ps-service/src/ps_service/**/*.py` path searched in the container architecture.
- Values reference: `python3 .orchestrator/tracker/i254/tools/values_keys.py` against `values.yaml` and `values-prod.yaml`.
- Commands and flags: every `--flag` and `scripts/*.sh` named in the three guides searched in `scripts`, `ps-cli/install.sh` and `ps-cli/src`; every `ps-cli <group> <command>` string in the documents compared with `uv run --frozen python -B -m ps_cli --help`.
- Links: `python3 .orchestrator/tracker/i254/tools/linkcheck.py` (100 files; 26 problems, all template placeholder anchors, none in a document of this audit).
- Narrative sweep: `.orchestrator/tracker/i254/tools/narrative_sweep.sh` for density per file, then a context scan for `issue #N`, `#N`, `AC-*` ids and `DG-`/`PS-ADR-` ids over every in-scope document (colour codes in diagram styles excluded), then `grep -E '(formerly|previously|used to|originally|walking skeleton|was removed|no longer|renamed from)'` read hit by hit. Terms verified as live and therefore excluded: `PS Question Skill`, `Policy Editor`, `PS_MCP_URL`.
- Issue and release state used to test cited issues: `gh issue view <n> --json state`, `git tag --contains <commit>`, `gh release list`.
- Documents: `uv run pre-commit run --files <changed documents>` (prettier, cspell).
- Tests pinning architecture wording, run one file per command: `graph_gateway/test_docs_content.py` (115 passed after the update), `change_monitor/test_reingestion_docs_content.py` (11), `domain_schema/test_documentation_consistency.py` (6), `release/test_docs_mcp_connector_rule.py` (31), `release/test_docs_plugin_names.py` (8), `mcp_interface/test_ps_ingest_regulation_skill_content.py` (8).

## Severity scale

- High: the document tells an operator or contributor to do something that fails, or hides behaviour with security or data impact. None found in this slice group.
- Medium: a materially wrong or missing statement about a user-facing surface, a contract or a deployable unit.
- Low: a naming, wording or completeness defect with an obvious correction.

## Deployable units (AC-BI-002)

| Unit | Evidence | Container architecture |
| ---- | -------- | ---------------------- |
| PS Service image (API, MCP, provision job, init containers) | one `Dockerfile` image with two stages; used by the Deployment, two init containers and the provision job | exists: `ps-service-container-architecture.md` |
| ps-cli | `ps-cli/pyproject.toml` entry point, `ps-cli/install.sh` | created: `ps-cli-container-architecture.md` |
| FalkorDB | chart workload from `falkordb.image`; `falkordb-browser-service.yaml` is a Service only | none, reason recorded in the solution architecture |
| PS Postgres | chart workload plus init ConfigMap SQL | none, reason recorded |
| Authentik and its Postgres | subchart dependency plus chart-owned blueprint | none, reason recorded |
| Policy System Plugin and `.claude-plugin/marketplace.json` | 14 skills, rubrics, one connector declaration; no executable code | none, reason recorded |
| `curated-content/` | data artefact | none, reason recorded |
| Helm chart | packaging | none, reason recorded |
| `tools/*`, `scripts/*` (11 shell scripts plus `lib`, `release`, Python helpers), `.github/` actions and scripts, `.claude/skills`, `.agents/skills` | maintainer, operator and CI tooling | none, reason recorded |
| `ps-test-support`, `ps-test-plugin` | test-only packages | none, reason recorded |
| `docs/architecture/ps-client-mockup/` | static mockup | none, reason recorded |

## INV-005 and INV-011 (AC-BI-003)

INV-005: 24 PS Service components and 5 PS-Cli components, every domain path found in the solution architecture's Component Breakdown (`inv_check.py`: 0 misses). The reverse direction found one solution-architecture component without a section (Domain Schema, DOC-S2-05).

INV-011: the PS-Cli document names one entity, Regulatory instrument, which is a heading in `ps-domain-concepts.md`. In the PS Service document every entity heading is either a heading of `ps-domain-concepts.md` (the compliance-spine nodes and the "Graph Write Gateway and Mutation Log" entities) or an operational record. The operational records AccessRole assignment, Signing credential and Pending approval, and Ingestion run already carried a statement that they are not domain concepts; Audit event, Runtime config key and the adapter-defined native structural graph did not (DOC-S2-08). `ps-domain-concepts.md` was not edited.

## Findings

| id | location | severity | evidence | disposition |
| -- | -------- | -------- | -------- | ----------- |
| DOC-S1-01 | repository, `ps-cli/` | Medium | the only distributed client with its own code (5 components, 4 REST contracts, config and credential files, exit codes) had no container architecture | fixed in this change: `docs/architecture/ps-cli-container-architecture.md` created from the code |
| DOC-S1-02 | `ps-solution-architecture.md` Container and Component Breakdown | Medium | PS-Cli was a diagram-only external client; no Container Breakdown row and no Component Breakdown rows, so INV-005 had nothing to check | fixed in this change: PS-Cli container row (`ps.cli`) and five component rows added; container diagram shows PS-Cli inside the system boundary |
| DOC-S1-03 | `ps-solution-architecture.md` | Low | no record of why FalkorDB, PS Postgres, Authentik, the plugin, `curated-content/`, the chart, tooling, test packages and the mockup have no container architecture | fixed in this change: "Units without a Container Architecture" table with reason and contract location per unit |
| DOC-S2-01 | `ps-solution-architecture.md` Overview, both diagrams, System Context Breakdown, User Role Mapping, Use Case rows | Medium | PS-Cli described as doing Cellar/ELI regulation selection and curated-artifact restore; `ps-cli` offers `get health`, `ingest document` (internal intake JSON only), `export instrument`, `config` and `auth`; regulation selection and restore are the `ps-ingest-regulation` and `ps-restore-instrument` skills over MCP | fixed in this change |
| DOC-S2-02 | `ps-service-container-architecture.md` Export section (implementation guidance and action row) | Medium | stated "never exposed via ps-cli" and "not network-reachable"; `POST /exports` exists behind the `ComplianceOfficer` access role (`api/routes.py:787-792`) and `ps-cli export instrument` calls it | fixed in this change |
| DOC-S2-03 | `ps-solution-architecture.md` Export, Restore and PS Service rows | Medium | Export "never exposed via REST or PS-Cli"; Restore and the PS Service row load from a "`curated-content` git folder"; Restore takes an uploaded artifact (`POST /restorations`) or fetches from the configured curated content source (`POST /restorations/from-catalog`) | fixed in this change |
| DOC-S2-04 | `ps-solution-architecture.md` Dependency Health and Process Harness rows; `ps-service-container-architecture.md` overview rows and the Dependency Health and Process Harness sections | Medium | documents named FalkorDB, LLM Interface and Cellar/ELI and "self-healing"; code gates `/ready` on FalkorDB, the state Postgres and the graph replay (`main.py:250`), tracks six dependencies (`dependency_health/registry.py:29-34`), probes five at startup, re-probes only the gating ones while not ready, and does not reset ready afterwards | fixed in this change, describing today's behaviour only; the recovery gap after a mid-run outage stays with issue #318 |
| DOC-S2-05 | `ps-service-container-architecture.md` (H-4) | Medium | solution architecture lists a Domain Schema component (`ps.service.domainschema`); the container architecture had no overview row or section although it cites `ps_service.domain_schema` | fixed in this change: overview row and component section added |
| DOC-S2-06 | `ps-service-container-architecture.md` MCP Interface actions | Low | registered tool `check_instrument_ingestion_status` had no action row | fixed in this change |
| DOC-S2-07 | both architecture documents | Low | the MCP tool is registered as `invite-user`; the text said `invite_user` | fixed in this change |
| DOC-S2-08 | `ps-service-container-architecture.md` entity sections | Low | INV-011: Audit event, Runtime config key and the native structural graph are not in the domain concepts and were not marked as non-domain | fixed in this change: one-line statement added, matching the existing operational entities |
| DOC-S2-09 | `ps-solution-architecture.md` Overview, container diagram edge, footnote under the Component Breakdown | Low | issue-number provenance in text edited by this slice | fixed in this change in the text touched; the remaining narrative is examined by the narrative slice |
| DOC-S2-10 | `ps-service/tests/graph_gateway/test_docs_content.py` | Low | three tests pinned issue numbers in solution architecture text (`Policy Lifecycle (#211)`, `Change Monitor (#213)`, `Graph Cleanup (#212)`) | fixed in this change: the tests now pin the present-tense fact (the writers named on the gateway edge) and keep the negative assertions; the assertions on `ps-domain-concepts.md` are unchanged |
| DOC-H-04 | harvest H-4 | Medium | see DOC-S2-05 | fixed in this change |
| DOC-H-05 | harvest H-5 (domain path against package name) | Low | domain paths follow the skill's lowercase dot rule and need not equal package spelling; a package-name column would add mechanism to a WHAT table | no action: container-architecture skill rule (WHAT, not HOW) |
| DOC-H-06 | harvest H-6 | Low | `mcp_server.py`, `main.py` and entry-layer rows describe today's modules; refactors #270, #272, #273 are open | no action: documents describe today's code; the closing run comments on those issues |
| DOC-H-07 | harvest H-7 | Low | operational labels `PendingReview`, `ReingestProgress`, `GraphReplayState` are named by the code, not by the domain concepts | no action: owned by #287; the container architecture does not name them as domain entities |
| DOC-H-11 | harvest H-11 | Low | neither architecture document states an MCP naming or error-body convention | no action: nothing to correct; #304 and #305 own the convention |
| DOC-S3-01 | `ps-service-container-architecture.md` Restore (design record) | Medium | a 90-line block of design decisions, a 14-step read classification, retired and rewritten test tables (ten test files that no longer exist) and a list of behaviour differences against a previous path; it described the history of the migration, not the component, and named files absent from `ps-service/tests/restore` | fixed in this change: replaced by present-tense "Guarantees" and "Observable behaviour" sections; the terminal audit-row table is kept |
| DOC-S3-02 | `ps-service-container-architecture.md` MCP Interface actions | Medium | six action rows (`IngestRegulation`, `CheckRegulations`, `RestoreInstrumentFromCatalog`, `ListNearMisses`, `ResolveNearMiss`, `GetCatalogListing`) said "no role/scope model exists"; code gates the first three on `ComplianceOfficer` (`mcp_server.py:1113`, `:1162`, `:1583`, `:2205`) and the other three on authentication only (harvest H-8, #290) | fixed in this change |
| DOC-S3-03 | `ps-service-container-architecture.md` `ResolveNearMiss` row | Medium | row said a merge executes at the call after an `elicitation/create` confirmation and removes the loser atomically; code returns a pending passkey approval and writes nothing until the approval is signed; no `elicit` call exists in `mcp_server.py` | fixed in this change |
| DOC-S3-04 | `ps-service-container-architecture.md` `GrantAccessRole`, `RevokeAccessRole` | Medium | rows listed three grantable roles; `_GRANT_RBAC` and `_REVOKE_RBAC` (`authz/service.py:86-104`) manage four, `ComplianceOfficer` included, and granting it needs `SystemOwner` or `SystemAdmin` | fixed in this change |
| DOC-S3-05 | `ps-service-container-architecture.md` Policy Lifecycle (`ApprovePolicy`, `RejectPolicy`, owner-authored row) | Medium | "`PolicyManager` or above"; `require_role` extends only a `SystemAdmin` minimum to `SystemOwner` (`authz/service.py:326`), so approval needs `PolicyManager` exactly | fixed in this change |
| DOC-S3-06 | `ps-service-container-architecture.md` MCP Interface | Medium | no single reference for which tool needs which role (issue #290) | fixed in this change: "MCP tool authorization" table covering all 38 tools, derived from the registered tool descriptions and the gates in code |
| DOC-S3-07 | `ps-service-container-architecture.md` MCP Interface registration row, action list | Low | one 2,500-character row with change-record text, and 4 registered tools without an `Implements` entry | fixed in this change: row rewritten, `Implements` completed |
| DOC-S3-08 | `ps-service-container-architecture.md` Logging (harvest H-9) | Medium | said `run_id` is bound only for MCP query calls and that Ingestion is "not yet wired"; code binds per MCP tool call, per REST request with the run-id dependency (`api/dependencies.py:178`) and in ingestion, restore, change-check and monitor runs; also "under exploration" notes for rotation and multi-process writes | fixed in this change: present-tense guidance (no rotation, one writer thread per process) |
| DOC-S3-09 | `ps-service-container-architecture.md` Implementation Registration tables | Low | 14 modules present in code were in no registration table (audit `actor`/`emit`, invitations `service`/`audit_actions`, restore `audit_actions`, Company Merge `live_merge_approval`/`preconditions`, Domain Schema `json_layout`/`errors`, four internal-seed adapter modules) | fixed in this change: rows added |
| DOC-S3-10 | `ps-service-container-architecture.md` Authorization actions, Process Harness | Low | removed table `access_role_grant_events` named with an issue number; no statement of the request size limit | fixed in this change |
| DOC-S3-11 | `docs/coding-standards/level2-python-instructions.md` ps-cli verb vocabulary | Medium | listed `get catalog`, `ingest regulation`, `restore instrument`, `check regulations` and guidance for commands that do not exist; `ps-cli --help` offers `get health`, `ingest document`, `export instrument`, `config`, `auth` | fixed in this change |
| DOC-S3-12 | `docs/coding-standards/level2-python-instructions.md` Logging Conventions (harvest H-9) | Low | compared with `ps_service.logging` (`models.py` field set, `facade.py` stderr mirror of `failed`): consistent | no action: matches the code |
| DOC-S3-13 | `ps-service/tests/graph_gateway/test_docs_content.py` | Low | nine assertions pinned the removed design record (decision ids, step table, test lists, delivery notes) and `check regulations` in the operations guide | fixed in this change: tests now pin the present-tense guarantees; test-only edit |
| DOC-S4-01 | `operations-guide.md` Troubleshooting (harvest H-13 item 1) | Medium | `get health` row named FalkorDB, LLM Interface and Cellar/ELI and said config-incomplete or FalkorDB gate `not_ready`; code gates on FalkorDB, state Postgres and the graph replay (`main.py:250`, `:1103`) and lists six names in `unhealthy_dependencies` | fixed in this change |
| DOC-S4-02 | `operations-guide.md` Troubleshooting (H-13 items 2, 3, 4) | Medium | no rows for `/ready` staying 503 after a mid-run outage (open #318), a restart during `start_ingestion` (open #316), passkey-signing Postgres down, identity provider unreachable, Cellar/ELI down, `OOMKilled` | fixed in this change: six rows describing today's behaviour and remedy (restart, start again); none promises recovery |
| DOC-S4-03 | `operations-guide.md` Troubleshooting | Medium | rows named `ingest regulation` and `check regulations` as `ps-cli` commands | fixed in this change: they name the `ingest_regulation`, `start_ingestion` and `check_regulations` tools and `ingest document` |
| DOC-S4-04 | `operations-guide.md` (harvest H-13 item 5) | Low | `:568` Postgres hard-dependency paragraph | no action: correct |
| DOC-S4-05 | `scripts/ps-reset-data.sh`, `scripts/grant-access-role.sh` (harvest H-2) | Medium | evaluator helpers named by no guide | fixed in this change: two sections in the operations guide |
| DOC-S4-06 | `scripts/verify-chart-independence.sh` | Low | static check run by tests and CI, not an operator step | no action: CI helper |
| DOC-S4-07 | `helm-chart-values-reference.md` vs `values.yaml` and `values-prod.yaml` (`values_keys.py`) | Medium | undocumented: `psService.resources.*`, `psService.probes.*`, `authentik.postgres.*`, `authentik.server.service.type`, `authentik.postgresql.enabled`, `authentik.authentik.postgresql.*`; two statements said readiness reflects FalkorDB only, one named `ps-cli ingest regulation` / `check regulations` | fixed in this change: rows added, statements corrected; remaining tool output is abbreviation style (`.periodSeconds`) of documented keys |
| DOC-S4-08 | `helm-chart-values-reference.md` (harvest H-1) | Medium | `PS_SERVICE_MAX_REQUEST_BODY_BYTES` documented nowhere; no chart value sets it (#309 open) | fixed in this change: documented in the values reference (default 104857600, 413) and in the Process Harness section |
| DOC-S4-09 | `user-guide.md` | Medium | readiness described as "FalkorDB only" in two places; no section for `ps-list-ingested`, `ps-near-miss-review`, `ps-author-policy` | fixed in this change |
| DOC-S4-10 | `installation-guide.md`, `operations-guide.md`, `user-guide.md` | Low | every `--flag` named in the three guides exists in the scripts or `ps-cli` source or belongs to an external tool (`podman`, `az`); every `scripts/*.sh` path exists | no action: consistent |
| DOC-S4-11 | `curated-content/README.md` | Medium | named `ps-cli get catalog` and `ps-cli restore instrument` (removed) and said the internal baseline is not curated (`ENGPRAC-1.0` is in `catalog.json`) | fixed in this change |
| DOC-S4-12 | `ps-skills/readme.md` | Medium | listed 8 of the 14 plugin skills | fixed in this change: the six missing skills added |
| DOC-S4-13 | `CONTRIBUTING.md` | Low | `.github/issue_template/standard-mom.md`; the tracked path is `.github/ISSUE_TEMPLATE/standard-mom.md` (fails on a case-sensitive file system) | fixed in this change |
| DOC-S4-14 | `README.md` | Low | repository layout table matches `git ls-files` top level exactly; the passkey feature row named only near-miss merges | fixed in this change: row names Graph Cleanup actions too |
| DOC-S4-15 | `.agents/skills/ps-pentest/SKILL.md:58` | Low | broken path `charts/.../falkordb-browser-service.yaml` (the one real link problem in the baseline) | fixed in this change |
| DOC-S4-16 | `grant-access-role` and `revoke-access-role` tool descriptions (`mcp_server.py`) | Low | descriptions say a caller may never grant or revoke their own role; `block_self_target` exempts a `SystemOwner` | issue #322 |
| DOC-S5-01 | `ps-service-container-architecture.md` (about 140 hits) | Medium | issue numbers (`Implemented (#208)`, `since #215`, `issue #133`, `issue #194`), acceptance-criterion ids (`AC-BI-002`, `AC-RD-009`) and comparisons with a retired writer ("Observable differences from the direct writes", "the old writer", "used to") in the gateway, the six gateway-writer sections, Authorization, Ingestion Runs and the MCP action rows; information kept, provenance dropped | fixed in this change: rewritten as present-tense description ("Write behaviour:"); a deviation note about a closed issue's deliverable became "No own migration runner" |
| DOC-S5-02 | `ps-solution-architecture.md` Security rows, gateway footnote | Medium | "originally out of scope", "originally required only", issue and criterion ids; the `AccessRole` list named three roles plus the implicit one while the code manages four (`ComplianceOfficer`) | fixed in this change |
| DOC-S5-03 | `customer-azure-deployment.md` | Medium | 17 issue references, 10 criterion ids, a "capability regression" note about a removed flag, "previously ran after" and "renamed for this issue" wording | fixed in this change: present-tense procedure; the manual rotation section states that no script flag exists |
| DOC-S5-04 | `customer-azure-llm-bootstrap.md` | Medium | "Depends on #103" and "the chart still passes the old hardcoded name" while #103 is closed and `llm.model`/`llm.embedModel` exist (`_helpers.tpl`); the Helm section was written as a plan ("changes from", "add a `fail()` guard", "breaking change") although the guard exists (`templates/secret.yaml:17`) and the default provider is `azure` | fixed in this change: section renamed "Helm chart behaviour" and rewritten as description |
| DOC-S5-05 | `idp-configuration-contract.md` | Low | "As of issue #129/#140/#165", "#128's spike", criterion ids in the verification excerpt | fixed in this change |
| DOC-S5-06 | `installation-guide.md` Verification status and surface table | Medium | said the invitation-link support (`PS_AUTHENTIK_PUBLIC_URL`) is in no published release and "the latest release, 3.11.0" lacks it, and that the published chart has "the same limit"; it is in 3.12.0 and later (`git tag --contains`), the latest release is 4.1.4 and `deploy-ps-prod.sh` sets `psService.authentik.publicUrl`; also cited closed issues #237 and #239 as the place where verification is tracked (#239 is the hostname change, closed) | fixed in this change: statements rewritten; the evidence template no longer asks for a comment on a closed issue; the evaluator hostname claim stays marked unverified |
| DOC-S5-07 | `helm-chart-values-reference.md` | Medium | named `ps-cli ingest regulation` and `check regulations` as pre-flight callers (removed commands); issue and criterion ids on four rows | fixed in this change |
| DOC-S5-08 | `user-guide.md`, `internal-regulation-intake-format.md` | Low | links to closed issues #54, #57, #58; "eventual companion JSON Schema" although `schemas/internal-regulation-intake.v1.schema.json` exists | fixed in this change: schema linked, history links dropped |
| DOC-S5-09 | `ps-client-ux-design.md`, `ps-native-client-exploration.md` | Low | `(#173)`, `#174 removed ...`, `(#199, ...)` provenance | fixed in this change |
| DOC-S5-10 | `ps-domain-concepts.md` and its shipped copy | Low | a paragraph naming issue numbers per gateway writer, two `issue #134` mentions (one generated from `domain_schema/definition.py:37`), one criterion id | issue #323 (a shipped file and generated tables; pinned by tests) |
| DOC-S5-11 | operations guide (#151, #235), README (#234) | Low | pointers to open issues that track a documented limitation | no action: a limitation is stated with its tracker; the pointer is current, not history |
| DOC-S5-12 | `ps-service/tests/**/test_*docs*.py` (four files) | Low | assertions pinned removed narrative: issue numbers, `Implemented (#N)`, `Observable differences ... (#N)`, "GO (#172)", "#239 pending" | fixed in this change: test-only; the assertions now pin the present-tense fact and keep the behavioural ones |
| DOC-S5-13 | `.py:NNN` citations in documents | Low | no checker built; citations in architecture text are re-opened by hand only | no action: stated limit of AC-BI-007; #260 lists the link and path checker as a guardrail candidate |
| DOC-S5-14 | prose algorithms in Domain Mapper, Company Merge, Ingestion; installation guide prose; design documents with their own id scheme | Low | verified for named modules, tables, configuration and removed components only | issue #323 |

## Coverage of the PS Service container architecture (AC-BI-004)

| Section group | Named modules, config, routes and tools checked | State |
| ------------- | ------------------------------------------------ | ----- |
| Overview, component overview and mapping tables | domain paths (INV-005), PS_* variables, route strings | checked |
| Dependency Health, Process Harness | `main.py`, `dependency_health/registry.py` | checked |
| Export, Domain Schema, MCP Interface action list | routes, tool registrations, package files | checked |
| Entity sections | headings against `ps-domain-concepts.md` (INV-011) | checked |
| Every other component section (Ingestion, Domain Mapper, Company Merge, Restore, Curated Source, Invitations, Query Engine, Authentication, Authorization, Policy Lifecycle, Graph Cleanup, Regulatory Change Monitor, LLM Interface, Logging, Passkey Signing, Audit, Graph Write Gateway, Persistence, Runtime Config, Ingestion Runs) | PS_* variable names, route strings and MCP tool names verified document-wide (no unknown variable, no unknown route); every backticked identifier resolves in the source tree or is a named action; every file path and module named exists; defaults of `PS_*` variables match `config.py`; sequence-diagram participants and calls exist; registration tables list every module (14 added); MCP Interface, Authorization, Policy Lifecycle, Logging and Restore were read claim by claim | checked; prose describing algorithms inside Domain Mapper, Company Merge and Ingestion was read only where it names modules, tables or configuration (not re-derived from code) |

## False positives and reasons

- `PS_MCP_URL` and `PS_LLMINTERFACE_`: connector variable and prefix, not stale names (`release/test_docs_mcp_connector_rule.py`, `test_plugin_manifests.py`).
- `PS Question Skill` and `Policy Editor` in the solution architecture are live and exploratory clients, not removed components.
- `rotate-authentik-secrets` and `access_role_grant_events` still appear in tests that assert their absence (`deploy_ps/test_no_authentik_or_signing_keyvault_calls.py`, `authz/test_store_transactional_audit.py`); they are guards, not references.
- Open-issue pointers (#151, #234, #235) in the operations guide and README, and the NobodyWho issue #111 link in `ps-native-client-exploration.md`, are current tracker links, not history.
- Hex colour codes in diagram `style` lines (`#333`, `#90CAF9`) match an issue-number pattern and are excluded.
- `CUC-NN` ids in the client design documents are the documents' own use-case ids, not acceptance criteria.
- Snake-case names such as `propose_policy` in the Policy Lifecycle registration table are Python function names, not MCP tool names.

## Superseded and removed items

No code symbol was deleted. Removed statements and the repository-wide search for each (excluding `docs/audits` and `.orchestrator`):

| Removed text | Search | Result |
| ------------ | ------ | ------ |
| `ps-cli get catalog`, `ps-cli restore instrument` (curated-content README) | `grep -rIl 'ps-cli get catalog\|ps-cli restore instrument'` | no match |
| `ps-cli check regulations`, `ps-cli ingest regulation` as live commands | `grep -rIl 'ps-cli check regulations\|ps-cli ingest regulation'` | no match after the values reference fix |
| `--rotate-authentik-secrets` described as a flag | `grep -rIl 'rotate-authentik-secrets'` | one test that asserts the flag is rejected |
| "Observable differences from the direct writes" | `grep -rIl 'Observable differences from the direct writes'` | no match |
| "Depends on #103", "unreleased PS Service image", `issue_template/standard-mom` | `grep -rIl` for each | no match |
| `access_role_grant_events` as a live table | `grep -rIl 'access_role_grant_events'` | tests that assert the table is gone |

## Issues filed

- #322 `docs(ps-service)`: self-grant and self-revoke wording in two MCP tool descriptions (code text, shipped); sub-issue of #246.
- #323 `docs(domain-concepts)`: remove issue-number provenance from the domain concepts (shipped copy, generated tables) and finish the prose verification left open here; sub-issue of #246.

Every other mismatch was a documentation error with the code correct.

## Acceptance criteria confirmation

| AC | State | Evidence |
| -- | ----- | -------- |
| AC-BI-001 | met | this document: scope, paths, tooling, commands, findings with id, location, severity, evidence and disposition |
| AC-BI-002 | met | deployable units table; `ps-cli-container-architecture.md` created; the solution architecture records a reason for every unit without a document |
| AC-BI-003 | met | `inv_check.py`: INV-005 0 misses for both documents; INV-011 reviewed (see section above) |
| AC-BI-004 | partial | every named module, variable, route, tool, path and table checked and corrected; prose that describes algorithms in Domain Mapper, Company Merge and Ingestion was not re-derived from code; tracked in #323 |
| AC-BI-005 | partial | every documented key, flag, command and script path exists and the undocumented user-facing ones were added (DOC-S4-01 to DOC-S4-10, DOC-S5-06, DOC-S5-07); installation guide prose beyond commands, flags, paths and release claims was not re-read sentence by sentence; tracked in #323 |
| AC-BI-006 | partial | no issue number, criterion id or removed-component reference remains in the architecture documents, guides, READMEs or coding standards except: `ps-domain-concepts.md` and its shipped copy (#323), and current tracker links (DOC-S5-11); history wording was swept by pattern and read hit by hit, not every sentence of the design documents |
| AC-BI-007 | partial | `linkcheck.py`: 100 files, 26 problems, all template placeholder anchors; `.py:NNN` citations are not machine-checked (DOC-S5-13) |
| AC-BI-008 | met | every finding row carries exactly one disposition |
| AC-BI-009 | met | decisions and shipped-file edits filed with problem, options and recommendation: #322, #323 |
| AC-BI-010 | met | no shipped behaviour changed; `git diff --stat -- charts ps-service/src ps-cli/src` is empty |
| AC-BI-011 | met | the four test files that pinned removed narrative were updated test-only; 11 further documentation-reading test files pass, each run alone, and the full suite passes (see Verification evidence) |
| AC-BI-012 | met | removed statements searched repository-wide (section above) |
| AC-BI-013 | met | leak scan of this document, the changed documents and the bodies of #322 and #323: no credential, token, key, address or vulnerability detail |
| AC-BI-014 | met | sibling review below, from the issue bodies; 12 comments posted |

## Sibling issue review

Enumerated with `gh api --paginate repos/:owner/:repo/issues/246/sub_issues` (14 sub-issues; #260 is the only other open one) and `gh issue list --state all` for every issue from #263 to #323. Each row was written after reading the issue body with `gh issue view`. Twelve comments were posted; every other row records no change needed.

| Issue | Body says | Effect of this audit | Action |
| ----- | --------- | -------------------- | ------ |
| 247-259 (except 254, 260) | closed sub-issues of #246 (dead code, hygiene, tests, boundaries, standards, security, logging, API, data, configuration, dependencies, observability) | outcomes delivered; their findings are inputs read for the harvest H-1 to H-16; nothing to update | no change needed |
| 260 | automated guardrails for the cleanup | docs-versus-code drift is one of the rules it may automate; this audit's link, INV-005 and values-key checks are candidates | comment posted |
| 263 | 19 production symbols reachable only from tests | documents name modules, not these symbols | no change needed |
| 264 | declare starlette and redis as direct dependencies | documents list no dependency set | no change needed |
| 265 | remove local spike directories | the documents reference no spike directory as a source | no change needed |
| 266 | placeholder revision in `.git-blame-ignore-revs` | the README layout row lists the file only | no change needed |
| 267 | single-source the duplicated reasoning skill | documents do not describe the skill copies | no change needed |
| 268 | identical-body tests with distinct claims | test-only | no change needed |
| 269 | mutation-testing gaps | test-only | no change needed |
| 270 | move shared contracts out of `api` | the container architecture calls `api/` implementation wiring and states a no-API-import rule for one component; those lines change with the move | comment posted |
| 271 | break two package cycles | documents describe no import cycle | no change needed |
| 272 | split `mcp_server.py` by tool group | the container architecture row for `mcp_server.py` describes today's file; the issue assumes this audit updates it | comment posted (the split updates the row) |
| 273 | split three mixed-responsibility modules | documents name those module paths today | comment posted (the split updates the rows) |
| 274 | per-component `edge_identity` copies | documents describe the per-component graph submission modules as they are | no change needed |
| 275 | declare the project licence | the README wording about the licence is part of that decision, not a documentation error | no change needed |
| 276 | markdown-tooling npm advisories | tooling only | no change needed |
| 277 | pin the FalkorDB image tag | documents name the image through the chart value, not a tag | no change needed |
| 278 | pin the Python base image | documents name no base image digest | no change needed |
| 279 | bump runtime dependencies | documents list no versions | no change needed |
| 280 | `openai` major version | no document names it | no change needed |
| 281 | bump dev tooling | no document names versions | no change needed |
| 282 | migration-apply tests | test-only | no change needed |
| 283 | run the backup-restore verifier end to end | the operations guide describes the verifier; its unverified end-to-end state is already stated there | no change needed |
| 284 | extend verifier counts to every state table | the operations guide names the three compared tables in three passages | comment posted |
| 285 | graph indexes for celex and status | documents state no index set beyond the id index | no change needed |
| 286 | unused signing database indexes | documents name no signing index | no change needed |
| 287 | align persistence descriptions | point 2 asks this audit for a pointer in the domain concepts; not done, a shipped copy | comment posted; remains open with #287 |
| 288 | request-size limiting hardening | the values reference and Process Harness text document today's single limit and its 413 | no change needed (becomes current behaviour on delivery) |
| 289 | input validation of public metadata endpoints | no document describes this validation | no change needed |
| 290 | reconcile MCP authorization rows | done by this audit (DOC-S3-02 to DOC-S3-06) | comment posted; close at delivery of #254 |
| 291 | request throttling | the solution architecture lists rate limiting as open | no change needed |
| 292 | pod security-context and network-policy baseline | the values reference documents no such value | no change needed |
| 293 | log hygiene: exception text | the Logging section states the field set, not call-site hygiene | no change needed |
| 294 | log hygiene: user identifiers | as 293 | no change needed |
| 295 | unify the log outcome vocabulary | the Logging section lists `failed` mirrored to stderr, which is today's behaviour | no change needed |
| 296 | structured server request logs | documents do not describe the server access log | no change needed |
| 297 | audit records for more actions | the Audit section lists the registered actions as they are | no change needed |
| 298 | remove test type-ignore suppressions | test-only | no change needed |
| 299 | provenance rules for config-file comments | coding-standards wording is that issue's decision; this audit edited only the ps-cli verb list | no change needed |
| 300 | prune lint ignores | configuration only | no change needed |
| 301 | resolve the graph name once | the container architecture names `PS_FALKORDB_GRAPH` as it is read today | no change needed |
| 302 | settle ambiguous coding-standards rules | different rules from the ps-cli verb list this audit corrected | no change needed |
| 303 | OpenAPI document completeness | documents do not restate the OpenAPI document | no change needed |
| 304 | MCP tool naming convention | the authorization table uses today's registered names; a rename updates it | no change needed (breaking change carries its own docs work) |
| 305 | converge JSON error body shapes | documents state no common shape | no change needed |
| 306 | pagination and bounds for list operations | the documents describe `list-audit-events` pagination only | no change needed |
| 307 | declare request validation in models | internal | no change needed |
| 308 | show fresh or already ingested in ps-cli | the ps-cli container architecture lists output fields as they are | no change needed |
| 309 | chart values for service tunables | the values reference documents the variables as environment-only | comment posted |
| 310 | shellcheck findings | scripts only | no change needed |
| 311 | duplicated deploy shell functions | documents name scripts, not functions | no change needed |
| 312 | single source for Azure LLM defaults | the Azure documents state both default files; a merge changes them | no change needed (the change owns its documents) |
| 313 | supply-chain hardening of workflows | documents do not describe workflow pinning | no change needed |
| 314 | bound connection and socket waits | documents state no connect timeout | no change needed |
| 315 | explicit LLM retry and key-set timeouts | as 314 | no change needed |
| 316 | reconcile interrupted ingestion runs at startup | the operations guide and Ingestion Runs text describe poll-driven reconciliation | comment posted |
| 317 | align the shutdown sequence | documents state the configured window only | no change needed |
| 318 | `/ready` recovery | the operations guide and Dependency Health text describe today's behaviour with a restart remedy | comment posted |
| 319 | startup and readiness probes | the values reference documents the existing probe keys | comment posted |
| 320 | resource requests and limits | the values reference documents the existing resource keys | comment posted |
| 321 | operational metrics | the operations guide documents them (a Metrics section) | documented by #321 |
| 322 | self-grant wording in tool descriptions | filed by this audit | no change needed |
| 323 | domain concepts provenance and remaining prose checks | filed by this audit | no change needed |

## Verification evidence

- Full suite, run once in the background: `uv run pytest --cov` gave 8066 passed, 1 skipped in 117 s (log `.orchestrator/tracker/i254/logs/full_suite.log`). The baseline at `main` is 8069 passed, 1 skipped; the difference of three is the net removal of tests that pinned the retired Restore design record (six removed, three present-tense replacements), all in `graph_gateway/test_docs_content.py`. No test was skipped or weakened to pass; the behavioural assertions are kept.
- Documentation-reading test files, each run alone: `graph_gateway/test_docs_content.py` 115, `change_monitor/test_reingestion_docs_content.py` 11, `ingestion/test_dg008_docs_content.py` 4, `domain_schema/test_doc_generation.py` 27, `domain_schema/test_documentation_consistency.py` 6, `domain_schema/test_slim_contract.py` 7, `release/test_docs_plugin_names.py` 8, `release/test_docs_mcp_connector_rule.py` 31, `release/test_docs_installation_hostname.py` 13, `scripts/test_coding_standards_documentation.py` 8, `tools/test_tools_help_provenance_free.py`; all pass.
- `uv run ruff check .`, `uv run ruff format --check .` and `uv run basedpyright` clean; `scripts/check_docstring_provenance.py --tracked` and `scripts/check_mock_boundaries.py` pass.
- `uv run pre-commit run --files <every file of git status --short>` passes (ruff, basedpyright, markdownlint, prettier, cspell, provenance, mock boundaries).
- `git diff --stat -- charts ps-service/src ps-cli/src` is empty: no shipped file changed. The only code-tree edits are four test files.

## Enforcement

Enforced rules fail automatically, in the pre-commit hook on the files they watch and in the CI `trunk-worthy` wave over the whole tree. Review-only rules rely on review. Check ids are the hook and CI ids listed in [CONTRIBUTING.md](../../CONTRIBUTING.md#automated-guardrails).

| Rule | Status | Check id or reason |
| --- | --- | --- |
| Relative links, anchors and repository paths in markdown resolve | enforced | `doc-links` |
| Markdown style, formatting and spelling | enforced | `markdownlint`, `prettier`, `cspell` |
| Every Helm value and environment variable is documented | enforced | `config-drift` |
| Generated domain documents and the packaged copy match the canonical source | enforced | `verify-domain-schema-freshness`, `verify-domain-concepts-doc` |
| Every top-level entry has a README layout row | enforced | `repo-hygiene` (`readme-layout-missing-row`) |
| Container architecture components match the solution architecture (INV-005) | review-only | tracked in #325 |
| Prose matches the code (flows, components, routes, tools) | review-only | judgement at review |
| No history or issue-number wording in documents | review-only | tracked in #323; Python code text is covered by `docstring-provenance` |
