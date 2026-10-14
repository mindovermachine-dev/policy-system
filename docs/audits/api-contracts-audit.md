<!-- © 2026 Cartman ApS. All rights reserved. -->

# API and Contract Consistency Audit - Findings Report (Issue #255, umbrella #246)

This repository is public. Wording on anything security-relevant is generic. Authentication in the tables is reduced to `bearer` (token required) or `none` (exempt path).

## Scope examined

| Area            | Paths                                                                                                                     |
| --------------- | ------------------------------------------------------------------------------------------------------------------------- |
| REST            | `ps-service/src/ps_service/api` (`routes.py`, `models.py`, `error_handlers.py`), `main.py` probes, served OpenAPI document |
| Response owners | `auth/middleware.py`, `auth/protected_resource.py`, `passkey_signing/router.py` (they own response shapes)                |
| MCP             | `ps-service/src/ps_service/mcp_interface` (38 registered tools)                                                           |
| Schemas         | `docs/artifacts/schemas` and its packaged copies in `ps-service` and `ps-cli`                                             |
| CLI             | `ps-cli/src/ps_cli` (`http_client.py`, `models.py`, `modules/parser.py`)                                                  |
| Consumers       | `ps-skills/`, `ps-test-plugin/` (tool names and REST paths)                                                               |

Out of scope: feature work; contract changes (filed as typed issues); authorization-doc rows (#290), size limits (#288), public-metadata input validation (#289), throttling (#291).

## Method and reproduction

Run from the repository root; `S` is a scratch directory outside the repository. `git status --short` stayed empty after every tool run. No server was started and no network was used.

| Step         | Command                                                                                                                                                | Result                                       |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------- |
| Surface dump | `PYTHONDONTWRITEBYTECODE=1 uv run --frozen python -B dump_surface.py $S` (script in the appendix; imports `create_app` with the local-test bypass)     | 19 operations, 38 tools                      |
| Naming       | `tail -n +2 $S/mcp.tsv \| cut -f1 \| grep -c -e '-'` and `grep -c '_'`                                                                                 | 26 kebab, 10 snake, 2 bare                   |
| Error bodies | `sed -n` over `error_handlers.py`, `middleware.py`, `main.py`, `protected_resource.py`, `passkey_signing/router.py` (static; cites in S3)              | five shapes                                  |
| Schemas      | `diff -q` of the docs schema against the `ps-service` and `ps-cli` copies                                                                              | identical                                    |
| CLI          | `grep -nE '_(INGESTIONS\|EXPORTS\|HEALTH\|READY)_PATH' ps-cli/src/ps_cli/http_client.py`; `grep -rnE '"/[a-z]' ps-cli/src \| grep -v 'https\?://'`      | four paths, no others                        |
| Consumers    | per-tool `grep -rlF -e <tool> ps-skills ps-test-plugin`                                                                                                | see S2                                       |
| Siblings     | `gh api repos/:owner/:repo/issues/246/sub_issues` then `gh issue view` of every open one                                                               | 44 reviewed (S7)                             |
| Tests        | one file per command: `test_no_drift_surface.py`, `test_openapi_provenance_free.py`, both schema-packaging tests, `test_freshness.py`, `test_http_client.py` | all pass (S5, S6)                            |

Tool limits: the dump tool does not expand `oneOf` bodies (`POST /ingestions` shows its union in the table by hand); `security` is `-` for every operation because authentication is middleware, so the Auth column is derived from the middleware exempt set.

## Severity scale

High: exploitable weakness (handled privately, not described). Medium: a contract inconsistency that clients must special-case. Low: cosmetic or documented-by-design deviation.

## S1 Surface inventory

### REST (19 operations)

| Method | Path | Request body | Success | Declared statuses | Auth |
| --- | --- | --- | --- | --- | --- |
| GET | `/.well-known/oauth-protected-resource` | - | `ProtectedResourceMetadata` | 200 | none |
| GET | `/approvals/{pending_approval_id}` | - | (no schema) | 200, 422 | none |
| POST | `/approvals/{pending_approval_id}/enroll/options` | `_ApprovalCodeRequest` | (no schema) | 200, 422 | none |
| POST | `/approvals/{pending_approval_id}/enroll/verify` | `_EnrollVerifyRequest` | (no schema) | 200, 422 | none |
| POST | `/approvals/{pending_approval_id}/sign/options` | `_ApprovalCodeRequest` | (no schema) | 200, 422 | none |
| POST | `/approvals/{pending_approval_id}/sign/verify` | `_SignVerifyRequest` | (no schema) | 200, 422 | none |
| POST | `/approvals/{pending_approval_id}/summary` | `_ApprovalCodeRequest` | (no schema) | 200, 422 | none |
| GET | `/catalog` | - | `CuratedCatalogResponse` | 200 | bearer |
| POST | `/change-checks` | - | `ChangeCheckResponse` | 200 | bearer |
| POST | `/exports` | `ExportRequest` | `ExportAcceptedResponse` | 200, 422 | bearer |
| GET | `/health` | - | (no schema) | 200 | none |
| POST | `/ingestions` | `IngestionRequest` (oneOf catalog/internal) | `IngestionAcceptedResponse` | 200, 422 | bearer |
| GET | `/ingestions/{run_id}` | - | `IngestionStatusResponse` | 200, 422 | bearer |
| GET | `/near-misses` | - | `PendingReviewListResponse` | 200 | bearer |
| GET | `/near-misses/approvals/{pending_approval_id}` | - | `PendingApprovalStatusResponse` | 200, 422 | bearer |
| POST | `/near-misses/{review_id}/resolve` | `ResolveReviewRequest` | `ResolveReviewResponse` | 200, 422 | bearer |
| GET | `/ready` | - | (no schema) | 200 | none |
| POST | `/restorations` | `RestorationRequest` | `RestorationAcceptedResponse` | 200, 422 | bearer |
| POST | `/restorations/from-catalog` | `CatalogRestorationRequest` | `RestorationAcceptedResponse` | 200, 422 | bearer |

Observed REST convention (majority): kebab-case plural nouns (`/ingestions`, `/restorations`, `/exports`, `/change-checks`, `/near-misses`, `/catalog`); verbs through the HTTP method; action sub-resources only where no method fits (`/near-misses/{review_id}/resolve`, `/restorations/from-catalog`). Every `POST` is registered with explicit status 200. No operation accepts a query parameter. Only `200` and `422` are declared (`/catalog`, `/change-checks`, `/near-misses` declare `200` only).

### MCP (38 tools)

| Tool | Style | Parameters (`*` required) |
| --- | --- | --- |
| `add-control-to-draft` | kebab | `standard_id*,title*,control_type,fields` |
| `add-standard-to-draft` | kebab | `policy_id*,title*,fields` |
| `approve-policy` | kebab | `policy_id*` |
| `check-cleanup-approval` | kebab | `pending_approval_id*` |
| `check_instrument_ingestion_status` | snake | `celex_ids*` |
| `check_regulations` | snake | - |
| `create-policy-draft` | kebab | `title*,standards,supersedes_policy_id,capability_ids` |
| `cypher` | bare | `query*` |
| `domain_concepts` | snake | - |
| `find-capability-merge-candidates` | kebab | `min_similarity` |
| `find-duplicate-obligations` | kebab | `role_id` |
| `get-catalog-listing` | kebab | - |
| `get-catalog-source` | kebab | - |
| `get-policy` | kebab | `policy_id*` |
| `get_ingestion_status` | snake | `run_id*` |
| `grant-access-role` | kebab | `principal_subject*,access_role*` |
| `ingest_regulation` | snake | `celex*,short_name*` |
| `invite-user` | kebab | `email*` |
| `list-access-roles` | kebab | - |
| `list-audit-events` | kebab | `actor_subject,actor_issuer,resource_type,resource_id,action,occurred_from,occurred_to,cursor,page_size,details` |
| `merge-capabilities` | kebab | `survivor_id*,absorbed_id*,acknowledge_governance_change` |
| `merge-obligations` | kebab | `survivor_id*,absorbed_id*` |
| `near_misses_check_approval` | snake | `pending_approval_id*` |
| `near_misses_list` | snake | - |
| `near_misses_resolve` | snake | `review_id*,decision*` |
| `propose-policy` | kebab | `policy_id*` |
| `reject-policy` | kebab | `policy_id*` |
| `release-capability-governance` | kebab | `capability_id*` |
| `reset-catalog-source` | kebab | - |
| `restore_instrument` | snake | `instrument_id*` |
| `revert-policy-to-draft` | kebab | `policy_id*` |
| `revoke-access-role` | kebab | `principal_subject*,access_role*` |
| `set-catalog-source` | kebab | `url*` |
| `start_ingestion` | snake | `celex*,short_name*` |
| `unmerge` | bare | `merged_id*` |
| `update-control-draft` | kebab | `control_id*,fields` |
| `update-policy-draft` | kebab | `policy_id*,fields` |
| `update-standard-draft` | kebab | `standard_id*,fields` |

## S2 Naming, verbs and status codes

| Id        | Location                                                  | Severity | Evidence                                                                                                                                                                                                                                              | Disposition                                                                                                                                                                |
| --------- | --------------------------------------------------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| API-S2-01 | `mcp_interface/mcp_server.py` (38 `@server.tool(` sites)  | Medium   | Tool names use three styles: 26 kebab-case, 10 snake_case, 2 bare. REST resources are kebab-case. Plugin skills reference most names (for example `create-policy-draft` 8 files, `cypher` 7, `revert-policy-to-draft` 7), so a rename is breaking. | issue #304                                                                                                                                                                 |
| API-S2-02 | REST vs MCP                                               | Low      | Same capability, different verbs: `POST /ingestions` vs `ingest_regulation` / `start_ingestion` / `get_ingestion_status`; `/near-misses/{review_id}/resolve` vs `near_misses_resolve`.                                                                | issue #304                                                                                                                                                                 |
| API-S2-03 | `api/models.py:146-150` vs `api/models.py:179`            | Low      | Two models for one concept: REST `IngestionStatusResponse` (`run_id`, `stage`) and MCP `IngestionRunStatusResponse` (`run_id`, `status`, `stage`, `result`, `error`).                                                                                   | issue #304                                                                                                                                                                 |
| API-S2-04 | `api/routes.py:769,776,783,790,797,805`                   | Low      | Create-style `POST`s return 200 rather than 201/202.                                                                                                                                                                                                  | no action: each `POST` runs the pipeline to completion before replying and returns the result body, so 200 is accurate; 201/202 would imply a created or queued resource. |
| API-S2-05 | `/near-misses/{review_id}/resolve`, `/restorations/from-catalog` | Low | Action sub-resources next to plural nouns.                                                                                                                                                                                                            | no action: no HTTP method expresses these; they are the minority pattern and consistent with each other.                                                                   |

Tool names not referenced by any skill or plugin file: `get-catalog-source`, `reset-catalog-source`, `set-catalog-source`, `near_misses_check_approval` (0 files each). `grep -rnoE "(GET|POST) /[a-z/{}_.-]+" ps-skills ps-test-plugin --include='*.md'` finds no REST path references, so no consumer names a removed endpoint.

## S3 Error shapes and declared responses

Cites re-opened on 2026-10-14 (`sed -n`): `error_handlers.py:229` (`_API_ERROR_SPECS`), `:442-451` (422 envelope), 500 returns at `:380,404,428,481`, generic 500 constants `:185-186`; `auth/middleware.py:185-192`; `main.py:209-214`, `:1106-1112`, `:1003`; `auth/protected_resource.py:112`; `passkey_signing/router.py:702,719,723,726,765`.

| Shape | Where                                                                 | Body                                                                      |
| ----- | --------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| 1     | typed errors, 401 (`middleware.py:185-192`), 422, generic 500         | `{"error": {"code", "message", "failing_stage"}, "run_id"}`               |
| 2     | `/ready` 503 (`main.py:1106-1112`), replay gate 503 (`main.py:209-214`) | `{"status", "unhealthy_dependencies", ...}`                               |
| 3     | `/.well-known/oauth-protected-resource` 404                           | `{"detail": "Not Found"}`                                                 |
| 4     | approval ceremony routes                                              | `{"error": "<string>"}` (same key, string instead of object)              |
| 5     | MCP tools                                                             | plain `"error: ..."` text (65 return sites in `mcp_server.py`, 2 in `query_engine/cypher_query.py`) or SDK-formatted raised errors |

| Id        | Location                                           | Severity | Evidence                                                                                                                                                              | Disposition |
| --------- | -------------------------------------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| API-S3-01 | served OpenAPI, 422 on 13 operations               | Medium   | Declared as `HTTPValidationError`; actual body is shape 1 (`error_handlers.py:442-451`).                                                                              | issue #303  |
| API-S3-02 | served OpenAPI responses                           | Medium   | Only 200/422 declared; `_API_ERROR_SPECS` maps 400/401/403/404/409/413/422/502/503 and a generic 500 that no operation declares.                                       | issue #303  |
| API-S3-03 | `main.py:1003`, `components.schemas`               | Low      | `info.title` is `FastAPI`, version `0.1.0`; no security scheme; `_ApprovalCodeRequest`, `_EnrollVerifyRequest`, `_SignVerifyRequest` exposed as component names.        | issue #303  |
| API-S3-04 | five shapes above                                  | Medium   | `error` is an object in shape 1 and a string in shape 4; 503 has two bodies; 404 has a third.                                                                          | issue #305  |
| API-S3-05 | `api/routes.py:733`, `api/models.py:146-150`       | Low      | `GET /ingestions/{run_id}` answers 200 for any id, including unknown ones.                                                                                            | no action: documented design (best-effort live-progress read, no 404 case). |

## S4 Pagination and request validation

| Id        | Location                                                                                       | Severity | Evidence                                                                                                                                                                       | Disposition |
| --------- | ---------------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------- |
| API-S4-01 | `GET /catalog`, `GET /near-misses`; MCP `get-catalog-listing`, `near_misses_list`, `list-access-roles`, `find-capability-merge-candidates`, `find-duplicate-obligations` | Medium | Only `list-audit-events` paginates (`cursor`, `page_size` 1..100, default 25, `next_cursor`, `mcp_server.py:2689-2690,2719`). The others return the whole set; no `LIMIT` or slice bound found in the readers. | issue #306  |
| API-S4-02 | `mcp_server.py:2784-2830, 2853-2930`                                                           | Medium   | `fields: dict` and `standards: list` validated by hand (`_parse_patch_fields`, `_parse_control_draft_input`, `_parse_standard_draft_input`, `_parse_policy_draft_standards`).   | issue #307  |
| API-S4-03 | `routes.py:733` and the `review_id`, `pending_approval_id` path parameters                     | Low      | Bare `str`, no length or pattern, while request-model `run_id` has `^[A-Za-z0-9_-]{1,64}$` (`models.py` `CatalogIngestionRequest.run_id`).                                      | issue #307  |
| API-S4-04 | `RestorationRequest` blobs                                                                     | Low      | Only `min_length=1`; base64 validity checked in the handler (`RestoreArtifactRejectedError`). Size limits are #288's.                                                          | issue #307  |
| API-S4-05 | `models.py:81-90`                                                                              | Low      | `InternalIngestionRequest.content` validated by the adapter's JSON Schema, not a `Field()`.                                                                                    | no action: documented design; the schema is the single source of truth for the intake format. |

Declared-in-model examples (compliant): `CatalogIngestionRequest.celex` length and pattern, `short_name` pattern, `run_id` pattern, `ResolveReviewRequest.decision` literal, `similarity` bounds, MCP `celex` pattern (`mcp_server.py:1058,1381`), `min_similarity` bounds (`:3955`), `page_size` bounds.

## S5 Spec and schemas versus behaviour

| Id        | Location                                                 | Severity | Evidence                                                                                                                                                                                                                                                                  | Disposition |
| --------- | -------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| API-S5-01 | OpenAPI vs handler behaviour                             | Medium   | Existing tests pin the spec structure and MCP tool schemas to a snapshot (`tests/api/test_no_drift_surface.py`), route allow-list (`test_route_allowlist_audit.py`), app wiring and the schema-copy byte identity. None compares declared responses with actual handler behaviour (API-S3-01, -02 pass undetected). | issue #260 (existing guardrail issue; #303 fixes the content first) |
| API-S5-02 | `docs/artifacts/schemas/internal-regulation-intake.v1.schema.json` | Low | Identical to the `ps-service` and `ps-cli` packaged copies (`diff -q` silent); top-level `nodes` and `edges` required, `additionalProperties: false`, matching `InternalIngestionRequest.content` ("nodes and edges"). Pinned by two packaging tests and `test_freshness.py`. | no action: schema documents current behaviour; any edit would change three synchronised copies (contract). |
| API-S5-03 | `api/models.py`                                          | Low      | Models used only by MCP (`StartIngestionResponse`, `IngestionRunStatusResponse`) are referenced at `mcp_server.py:83-84,1361,1367`; no unused model found.                                                                                                                | no action: nothing dead. |

No source or test file was added or changed: the existing drift tests already fail on structural spec drift, and a test for declared-versus-actual responses would fail today (API-S3-01), so it is left to #260 after #303.

## S6 CLI versus API

| CLI command                                           | HTTP call (`http_client.py`)         | Live route                    | Fields compared                                                                                  |
| ----------------------------------------------------- | ------------------------------------ | ----------------------------- | ------------------------------------------------------------------------------------------------ |
| `get health`                                          | `GET /health` (`:617,636`), `GET /ready` (`:659`) | `/health`, `/ready` | `status`, `unhealthy_dependencies` match                                                         |
| `export instrument`                                   | `POST /exports` (`:698`)             | `POST /exports`               | `instrument_id`, `manifest`, `baseline_blob_base64`, `native_blob_base64`, `stages` match `ExportAcceptedResponse` |
| `ingest document`                                     | `POST /ingestions` (`:739`)          | `POST /ingestions`            | `run_id`, `regulatory_instrument_id`, `source`, `stages` match; `outcome` not read (API-S6-01)   |
| `config set-context`, `use-context`, `get-contexts`   | none (local file)                    | n/a                           | n/a                                                                                              |
| `auth login`, `status`, `logout`                      | identity provider only               | n/a                           | n/a                                                                                              |

| Id        | Location                                      | Severity | Evidence                                                                                                         | Disposition |
| --------- | --------------------------------------------- | -------- | ---------------------------------------------------------------------------------------------------------------- | ----------- |
| API-S6-01 | `ps-cli/src/ps_cli/models.py`, `http_client.py` | Low    | `IngestionAcceptedResponse.outcome` (`fresh`/`already_ingested`) is ignored by the CLI.                          | issue #308  |
| API-S6-02 | `http_client.py:59-62`                        | Low      | Four path constants, all present in the REST table; no other string path in `ps-cli/src`. No CLI path uses a removed endpoint. | no action: all 9 commands map to a live endpoint or are local. |

## False positives

- `POST /change-checks` has no request body schema: it takes no body by design.
- `POST /ingestions` shows an empty body in the dump: the tool does not expand the `oneOf` union (catalog/internal), which is declared.
- `ps-cli/tests/test_grant_access_role_client.py` is not a REST path: it tests a script helper that resolves a bearer token.
- OIDC discovery and device-flow URLs in the CLI go to the identity provider, not PS Service.

## Superseded and removed

Nothing was made obsolete: this change adds only this document. Proof:

| Check                                                                                    | Command                                                                           | Result                                   |
| ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- | ---------------------------------------- |
| No source, test, config or schema file changed                                           | `git status --short -- . ':!docs/audits'`                                         | empty                                    |
| No removed symbol to search for                                                          | `git diff --stat HEAD -- . ':!docs/audits' ':!.orchestrator'`                     | empty                                    |
| CLI paths still resolve to live routes (no stale reference to a removed endpoint)        | `grep -rnE '"/(health\|ready\|exports\|ingestions)"' ps-cli/src ps-service/src` | four CLI constants, four live registrations |

The repository-wide search required by AC-BI-011 therefore has no removed symbols to match.

## Acceptance criteria confirmation

| AC        | Status | Evidence                                                                                                                                              |
| --------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| AC-BI-001 | met    | Scope examined, Method and reproduction (commands), findings tables S2-S6 each with id, location, severity, evidence and disposition.                  |
| AC-BI-002 | met    | S1 inventory tables; deviations in S2 (naming, verbs, status codes), S3 (error shapes), S4 (pagination).                                               |
| AC-BI-003 | met    | No non-breaking deviation was fixable inline; breaking or contract-changing ones are filed with options: #303, #304, #305, #306, #307, #308.          |
| AC-BI-004 | partial | Mismatches between spec and behaviour are recorded (API-S3-01, -02, -03) and filed as #303; they are not corrected in this change because that changes shipped output. The drift-failing test for declared versus actual responses is owned by #260 (API-S5-01); existing tests already fail on structural drift. |
| AC-BI-005 | partial | Every endpoint and tool was reviewed (S1, S4); placement deviations are recorded (API-S4-02, -03, -04) and filed as #307; moving validation into models is not done inline. |
| AC-BI-006 | met    | S6: each CLI command maps to a live route or is local; no CLI path uses a removed endpoint (API-S6-02; Superseded and removed).                         |
| AC-BI-007 | met    | Every finding row carries exactly one disposition: issue number, `no action` with reason; none fixed inline.                                          |
| AC-BI-008 | met    | Contract-touching findings are in #303-#308, not fixed inline.                                                                                        |
| AC-BI-009 | met    | Behaviour-changing work filed with typed titles (`fix`, `feat!`, `fix!`, `feat`, `fix`, `feat(ps-cli)`); only this document changed.                    |
| AC-BI-010 | met (vacuous) | No fix was made; targeted tests in Method and reproduction pass.                                                                          |
| AC-BI-011 | met (vacuous) | Superseded and removed: nothing obsolete, proof table.                                                                                            |
| AC-BI-012 | met    | Document and issue bodies #303-#308 scanned for secrets, credentials, payloads and endpoints; generic wording only; no vulnerability found.         |
| AC-BI-013 | met    | Sibling-issue review: all open #246 sub-issues (#254-#308) reviewed, comments posted on #260, #254, #272, #290, #270.                                  |

## Dispositions summary

Filed: #303 (OpenAPI content), #304 (MCP naming and status models), #305 (error body shapes), #306 (pagination and bounds), #307 (validation placement), #308 (CLI `outcome`). Existing: #260 (guardrail). Fixed in this change: none (docs only). Authorization findings: none to report in this audit.

## Sibling-issue review

Open sub-issues of #246 were listed with `gh api repos/:owner/:repo/issues/246/sub_issues` and every body was read (44 excluding #255). Comments posted where scope, order or dependencies changed: #260, #254, #272, #290, #270.

| Issue    | Review result |
| -------- | ------------- |
| #254     | comment posted: naming and error-shape convention should cite #304/#305 instead of declaring a convention |
| #257     | no change needed: config and deploy scripts, no API contract |
| #259     | no change needed: probes (`/health`, `/ready`) body shape noted in #305 without changing its scope |
| #260     | comment posted: owns the CI check for OpenAPI drift and declared-vs-actual responses; sequence after #303 |
| #263     | no change needed: test-only symbols, no API surface |
| #264     | no change needed: dependency declarations |
| #265     | no change needed: local spike directories |
| #266     | no change needed: blame-ignore file |
| #267     | no change needed: duplicated skill file |
| #268     | no change needed: identical-body tests, none in the API drift files |
| #269     | no change needed: mutation gaps in graph_cleanup test doubles |
| #270     | comment posted: #303 and #305 touch `api/error_handlers.py` and `api/models.py` that this issue moves |
| #271     | no change needed: config and curated_source cycles |
| #272     | comment posted: #304 and #307 touch the tool definitions and `_parse_*` helpers it splits |
| #273     | no change needed: splits `api/ingestion_orchestration`, `main`, `policy_lifecycle/service`; no route or model change by this audit |
| #274     | no change needed: edge_identity helper copies |
| #275     | no change needed: project licence |
| #276     | no change needed: npm advisories in dev tooling |
| #277     | no change needed: FalkorDB image tag |
| #278     | no change needed: Python base image digest |
| #279     | no change needed: runtime dependency bumps (fastapi bump may alter OpenAPI output; #303 regenerates the snapshot at its own time) |
| #280     | no change needed: openai major version |
| #281     | no change needed: dev tooling bumps |
| #282     | no change needed: migration tests |
| #283     | no change needed: backup-restore e2e |
| #284     | no change needed: backup verifier counts |
| #285     | no change needed: graph indexes |
| #286     | no change needed: signing database indexes |
| #287     | no change needed: persistence description docs |
| #288     | no change needed: size limits stay here; API-S4-04 defers to it |
| #289     | no change needed: public metadata input validation stays here; #307 excludes it |
| #290     | comment posted: tool-to-role table should tolerate #304 renames |
| #291     | no change needed: throttling, no contract overlap |
| #292     | no change needed: chart hardening |
| #293     | no change needed: log hygiene for exception text |
| #294     | no change needed: user identifiers in logs |
| #295     | no change needed: outcome vocabulary is log labels, not API bodies (#305 notes it) |
| #296     | no change needed: server request log |
| #297     | no change needed: audit records |
| #298     | no change needed: type-ignore suppressions in tests |
| #299     | no change needed: provenance in config comments |
| #300     | no change needed: lint ignores |
| #301     | no change needed: graph name configuration |
| #302     | no change needed: naming rules for code; API naming is #304 (noted in the #254 comment) |
| #303-#308 | filed by this audit, linked as sub-issues of #246 |

## Appendix: dump_surface.py usage

The surface dump script is kept with the audit tooling; reproduce its output by importing `create_app` (`ps-service/src/ps_service/main.py`) with the local-test bypass configuration as `ps-service/tests/api/test_openapi_provenance_free.py` does, reading `app.openapi()`, and listing tools with `server.list_tools()` as `ps-service/tests/api/test_no_drift_surface.py` does.

## Enforcement

Enforced rules fail automatically, in the pre-commit hook on the files they watch and in the CI `trunk-worthy` wave over the whole tree. Review-only rules rely on review. Check ids are the hook and CI ids listed in [CONTRIBUTING.md](../../CONTRIBUTING.md#automated-guardrails).

| Rule | Status | Check id or reason |
| --- | --- | --- |
| The OpenAPI document and the MCP tool list match the checked-in snapshot | enforced | `pytest` (`ps-service/tests/api/test_no_drift_surface.py`); the regeneration recipe is in CONTRIBUTING.md |
| Domain schema artifacts match the code-defined schema | enforced | `verify-domain-schema-freshness` |
| Declared responses match what handlers return | review-only | content fixed in #303; a test in #326 |
| One JSON error body shape across REST, approval routes and MCP | review-only | tracked in #305 |
| MCP tool naming convention and status models | review-only | tracked in #304 |
| Pagination, bounds and request validation | review-only | tracked in #306 and #307 |
| The CLI and the API agree on paths and shapes | review-only | no contract test across the two |
