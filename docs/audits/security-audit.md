<!-- © 2026 Cartman ApS. All rights reserved. -->

# Secure Code and Deployment Security Audit - Findings Report (Issue #252, umbrella #246)

This repository is public. The report names control categories only: no payload, secret value, route-level mechanism or exploit step appears for any open weakness. Findings that are, or may be, exploitable are handled through a private channel and appear here only as a placeholder row.

## Scope examined

| Area                | Paths                                                                                                                                                                                                              |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Service code        | `ps-service/src/ps_service`: `auth`, `authz`, `passkey_signing`, `invitations`, `mcp_interface`, `api`, `llm_interface`, `graph_gateway`, `persistence`, `ingestion`, `restore`, `export`, `curated_source`, `query_engine` |
| CLI client          | `ps-cli/src/ps_cli` (a REST/MCP client; holds no server authority)                                                                                                                                                 |
| Image and chart     | `Dockerfile`, `.dockerignore`, `charts/policy-system`                                                                                                                                                              |
| Deployment          | `deploy/kind`, `scripts/*.sh`, `scripts/release/*`                                                                                                                                                                 |
| Security docs       | `docs/secops/threat-coverage-map.md`, `docs/secops/baseline.yaml` (read only)                                                                                                                                      |
| Supply chain (pins) | `.github/workflows` (action pinning only)                                                                                                                                                                          |

Out of scope per #252: feature work, behaviour changes (filed as typed issues), and the Azure LLM bootstrap hardening deferral (`docs/architecture/customer-azure-llm-bootstrap.md`, `scripts/deploy-llm.sh`): IP allow-listing and in-cluster RBAC for that flow are not re-proposed.

## Method and reproduction

Run from the repository root; `S` is any scratch directory outside the repository. The tree stayed clean after every tool run (`git status --short` empty).

| Step                | Command                                                                                                                                                                                                                                                         | Result                                                         |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| SAST                | `uvx bandit -r ps-service/src ps-cli/src -f json -q -o $S/bandit.json`                                                                                                                                                                                          | 14 results (9 B608, 2 B405, 2 B105, 1 B101)                    |
| SAST rules          | `uvx semgrep scan --config p/python --config p/security-audit --config p/secrets --metrics=off --quiet --json -o $S/sg.json ps-service/src ps-cli/src` (rules are fetched over the network)                                                                      | 4 results                                                      |
| Dependencies        | `uv export --frozen --no-hashes --all-packages --no-emit-workspace --format requirements-txt -o $S/req.txt` then `uvx pip-audit -r $S/req.txt --no-deps --disable-pip`                                                                                          | no known vulnerabilities                                       |
| Secrets             | `trivy fs --scanners secret --skip-version-check -q --format table <target>`, one call per target: `ps-service/src`, `ps-cli/src`, `charts`, `deploy`, `scripts`, `.github`, `docs`                                                                              | 0 findings in 7 targets                                        |
| Chart render        | `helm template t charts/policy-system --set llm.azure.apiKey=DUMMY --set llm.azure.apiBase=https://example.invalid --set psService.localTestBypass.enabled=true` (default) and the same with `-f values-prod.yaml` plus the issuer, audience and bootstrap-owner values (production) | both render                                                    |
| Chart lint          | `helm lint charts/policy-system` with the default `--set` values                                                                                                                                                                                                | passes (one informational note: icon)                          |
| Chart scan          | `trivy config <render>.yaml --skip-version-check --format json -q`                                                                                                                                                                                              | default render 76 failures, production render 135 failures     |
| Dockerfile scan     | `trivy config Dockerfile --skip-version-check`                                                                                                                                                                                                                  | 1 failure (DS-0026), 26 checks pass                            |
| Shell               | `shellcheck -S warning -f gcc scripts/*.sh scripts/release/*`                                                                                                                                                                                                   | 22 warnings (9 SC2034, 13 SC2155), none injection-related      |
| Entry-point map     | an `ast` script listing `@server.tool` functions in `mcp_interface/mcp_server.py` and their gate calls; `grep -n add_api_route` over `api/routes.py`, `main.py`, `passkey_signing/router.py`                                                                     | 38 tools; 10 + 4 + 6 routes                                    |
| Construction review | `grep -rnE "\.execute\(" ps-service/src`, `grep -rnE "f\"[^\"]*(MATCH\|MERGE\|CREATE\|WHERE\|SET) " ps-service/src`, `grep -rnE "subprocess\|os\.system\|eval\(\|exec\(\|pickle\|yaml\.load\(\|shell=True" ps-service/src ps-cli/src`                              | see S2                                                         |
| Egress review       | `grep -rnE "urlopen\|httpx\.\|requests\.\|aiohttp\|litellm\." ps-service/src ps-cli/src --include='*.py'`                                                                                                                                                       | see S3                                                         |

Tool limitations: no container runtime on the audit host, so no image build and no `trivy image` scan (substitutes: the Dockerfile scan, static review, and the dependency audit); `osv-scanner`, `kubesec`, `kube-linter`, `checkov` and `gitleaks` were not available (substitutes: `trivy config`, `trivy fs --scanners secret`, `semgrep p/secrets`). No cluster and no running service was contacted: the audit is a code and chart review.

## Severity scale

- High: exploitable by a low-privilege caller, secret exposure, authorization bypass, or an injectable query reaching the graph or database. Handled privately, never described here.
- Medium: a missing control that raises risk but is not reachable on its own.
- Low: hygiene, defence in depth, or documentation drift.

## S1 Entry points and their authentication and authorization checks

The bearer-token middleware (`auth/middleware.py:35-38`) is default-deny; the exempt set is `/health`, `/ready`, `/.well-known/*`, `/approvals/*` and the `/mcp` mount (the MCP SDK enforces the same token verifier on that mount). Local-test mode is off in the production values; the chart refuses to render without issuer, audience and bootstrap-owner values unless local-test mode is explicitly enabled (`ps-service-deployment.yaml:8-12`).

Counts: `routes.py` registers 10 routes, `main.py` 4, `passkey_signing/router.py` 6, plus the `/mcp` mount (21 surfaces).

| Surface (REST)                                                  | Authentication                             | Authorization                                                  |
| --------------------------------------------------------------- | ------------------------------------------ | -------------------------------------------------------------- |
| `GET /health`, `GET /ready`                                     | exempt by design (liveness, readiness)     | none; return no graph or configuration content                 |
| `GET /.well-known/oauth-protected-resource` (and the path form) | exempt by design (RFC 9728)                | none                                                           |
| `GET /catalog`                                                  | bearer token                               | authenticated principal                                        |
| `GET /ingestions/{run_id}`                                      | bearer token                               | authenticated principal                                        |
| `POST /restorations`                                            | bearer token                               | `ComplianceOfficer` route dependency                           |
| `POST /restorations/from-catalog`                               | bearer token                               | `ComplianceOfficer` route dependency                           |
| `POST /exports`                                                 | bearer token                               | `ComplianceOfficer` route dependency                           |
| `POST /change-checks`                                           | bearer token                               | `ComplianceOfficer` route dependency                           |
| `GET /near-misses`                                              | bearer token                               | authenticated principal                                        |
| `POST /near-misses/{review_id}/resolve`                         | bearer token                               | authenticated principal; merge needs a signed passkey approval |
| `GET /near-misses/approvals/{pending_approval_id}`              | bearer token                               | authenticated principal                                        |
| `/approvals/*` (6 routes: shell, summary, enroll x2, sign x2)   | exempt from bearer by design (browser UI)  | single-use code, 15-minute expiry, passkey assertion           |
| `/mcp` mount                                                    | MCP SDK `token_verifier` (shared verifier) | per tool, below                                                |

| MCP gate group (38 tools)                                                               | Count | Tools                                                                                                                                                                                                                |
| --------------------------------------------------------------------------------------- | ----- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ComplianceOfficer` (exact match)                                                       | 5     | `ingest_regulation`, `start_ingestion`, `get_ingestion_status`, `check_regulations`, `restore_instrument`                                                                                                            |
| `ComplianceOfficer` (graph cleanup, re-checked at execution)                            | 7     | `find_capability_merge_candidates_tool`, `find_duplicate_obligations_tool`, `merge_capabilities_tool`, `merge_obligations_tool`, `release_capability_governance_tool`, `unmerge_tool`, `check_cleanup_approval_tool` |
| `SystemAdmin` or above                                                                  | 6     | `set_catalog_source`, `reset_catalog_source`, `get_catalog_source`, `invite_user`, `list_audit_events`, `list_access_roles`                                                                                           |
| Role-management RBAC                                                                    | 2     | `grant_access_role`, `revoke_access_role`                                                                                                                                                                            |
| Policy lifecycle (ownership and status rules, `PolicyManager` for approve and reject)   | 11    | `create_policy_draft`, `update_policy_draft`, `add_standard_to_draft`, `update_standard_draft`, `add_control_to_draft`, `update_control_draft`, `get_policy`, `propose_policy`, `approve_policy`, `reject_policy`, `revert_policy_to_draft` |
| Authenticated principal (read tools and approval flow)                                  | 6     | `near_misses_list`, `near_misses_resolve`, `near_misses_check_approval`, `get_catalog_listing`, `check_instrument_ingestion_status`, `cypher`                                                                         |
| Token-verified, serves a static schema                                                  | 1     | `domain_concepts`                                                                                                                                                                                                    |

Total 5 + 7 + 6 + 2 + 11 + 6 + 1 = 38, equal to the `ast` count.

| CLI operation (`ps-cli`)                       | Endpoint or effect                                         |
| ---------------------------------------------- | ---------------------------------------------------------- |
| `get health`                                   | `GET /health`                                              |
| `export instrument`                            | `POST /exports` (`ComplianceOfficer`)                      |
| `auth login`, `auth status`, `auth logout`     | IdP device flow and local encrypted token store            |
| `config set-context`, `use-context`, `get-contexts` | local configuration file only                         |

Settled questions: token verification validates signature, issuer, audience, expiry and not-before, with an algorithm allow-list pinned by `ps-service/tests/auth/test_algorithm_allowlist.py`; the production chart cannot render with local-test mode implicitly on; approvals and invitations use single-use, short-lived artefacts (15 and 30 minutes) with the service's own credential for the IdP admin call (`invitations/client.py`); the closed #143 and #144 fixes are intact (route-level `ComplianceOfficer` dependencies on the four pipeline routes; the chart fails closed without a bootstrap-owner identity).

## S2 Query, shell and path construction

- SQL (9 bandit B608 sites): every statement interpolates only module-level column and table constants; every value goes through named driver parameters. The dynamic filter builder in `audit/store.py` appends fixed literal fragments, with the optional details filter restricted to a fixed allow-list of keys. All sites carry a reasoned `noqa`.
- Cypher: 23 f-string Cypher sites. Interpolated positions are labels and property names taken from the domain schema or a fixed label-to-property mapping; caller values are passed as parameters. The graph write path additionally enforces a label and relationship allow-list in `graph_gateway/label_allow_list.py`. The caller-written `cypher` MCP tool is a read-only query tool guarded by a write-clause rejection, `ro_query`, a timeout and a row cap (pinned by the query-engine unit tests and a live test, not run here).
- Shell and processes: no `subprocess`, `os.system`, `eval`, `exec`, `pickle`, `shell=True` or `yaml.load` in `ps-service/src` or `ps-cli/src` (zero grep hits). Shell scripts: shellcheck reports style warnings only.
- Paths: export scratch directories are created with `tempfile.mkdtemp`; restore and curated-source artefact names are built from validated instrument identifiers; CLI paths come from the local user's own arguments.

## S3 Egress, deserialisation and size limits

| Egress path (server)                         | URL origin                     | Timeout | TLS                                  |
| -------------------------------------------- | ------------------------------ | ------- | ------------------------------------ |
| Cellar fetch (`cellar_eli/fetch.py`)         | fixed constants, `https` guard | yes     | default verification                 |
| Change monitor SPARQL                        | fixed constants, `https` guard | yes     | default verification                 |
| Curated source (`curated_source/http_fetch`) | operator configuration         | yes     | default verification                 |
| IdP discovery (`auth/discovery.py`)          | operator configuration         | yes     | default verification                 |
| IdP key set (`PyJWKClient`)                  | discovered from the issuer     | library | default verification                 |
| IdP admin API (`invitations/client.py`)      | operator configuration         | yes     | default verification                 |
| LLM (`litellm`, 3 call sites)                | operator configuration         | library | library default                      |
| `ps-cli` (`httpx`, 5 modules)                | user configuration             | yes     | `truststore` context                 |

Row count: 8 server egress paths plus the CLI. No `verify=False` anywhere. Request fields never select an outbound URL; the only operator-adjustable URL is the curated-source override, gated by `SystemAdmin`. XML from the EU publications office is parsed with `defusedxml`. Size limiting on inbound and outbound payload paths is not uniform (hardening category): filed as #288.

## S4 Secrets and Kubernetes controls

- No secret-bearing setting has a non-empty default in `config.py` (credentials default to unset and fail closed); `values.yaml` ships an empty LLM key; the chart generates database passwords at install time and supports `existingSecret`; the CLI stores refresh tokens in an encrypted per-context file (`msal_extensions`), not in plain files; the Dockerfile has no `ARG` or `ENV` secret, `.dockerignore` is an allow-list, and the runtime stage copies only the virtual environment. Trivy secret scans and `semgrep p/secrets` report nothing.
- Log content is owned by #253. No secret value is logged by the code paths reviewed here; the content of operator-log records for caught exceptions is a logging-audit question, noted there by comment.

| Workload (production render)     | runAsNonRoot | readOnly root fs | no privilege escalation | caps dropped | seccomp | resource limits | token automount off | NetworkPolicy    |
| -------------------------------- | ------------ | ---------------- | ----------------------- | ------------ | ------- | --------------- | ------------------- | ---------------- |
| ps-service (and init containers) | yes          | no               | no                      | no           | no      | yes (main)      | no                  | none             |
| ps-state-provision job           | yes          | no               | no                      | no           | no      | no              | no                  | none             |
| ps-postgres                      | upstream uid | no               | no                      | no           | no      | no              | no                  | yes              |
| authentik-postgres               | upstream uid | no               | no                      | no           | no      | no              | no                  | yes              |
| falkordb                         | upstream uid | no               | no                      | no           | no      | no              | no                  | yes              |
| authentik server and worker      | subchart     | no               | no                      | no           | no      | no              | no                  | none (subchart)  |

Trivy by workload, production render (135 failures): falkordb 12, ps-postgres 11, authentik-postgres 11, authentik server 11 and worker 11, ps-service 5, provision job 9 and wait-for-postgres 9, authentik Role 5, seccomp class 8, registry class 6, two ConfigMap rows. The default render (76 failures) is the same set without the production-only objects. The classes are one gap (no uniform container hardening baseline): filed as #292. Browser exposure: the production values use `ClusterIP` for the FalkorDB browser and the service; NodePorts exist in the default (local evaluation) values only.

## S5 Dockerfile and image

Non-root user (uid and gid 10001), multi-stage build copying only the virtual environment, exec-form command, no package manager step, pinned `uv` 0.12.4, allow-list `.dockerignore`. The base image uses a floating tag (#278 pins it by digest). The only Trivy finding is DS-0026 (no `HEALTHCHECK`), covered by Kubernetes liveness and readiness probes.

## S6 Error responses

All REST error bodies are minted in `api/error_handlers.py` in one shape. Messages of a fixed set of domain exception types are surfaced after path, host and URL scrubbing; every other exception, and request-validation failures, collapse to a generic message, with the detail written to the operator log only. No stack trace, filesystem path, query or `host:port` token is placed in a response body; the handler tests (`ps-service/tests/api/test_error_handlers*.py`, `test_unhandled_request_error_logging.py`) pin this.

## Findings

| ID        | Location                                           | Severity | Evidence                                                                                | Disposition                                    |
| --------- | -------------------------------------------------- | -------- | --------------------------------------------------------------------------------------- | ---------------------------------------------- |
| SEC-S0-01 | `audit`, `authz`, `ingestion_runs`, `passkey_signing` stores | Low | bandit B608 x9: f-string SQL over module constants, values parameterized, reasoned `noqa` | no action: false positive, see below           |
| SEC-S0-02 | `ingestion/adapters/cellar_eli`                    | Low      | bandit B405 x2 and semgrep use-defused-xml x2: import is type-only (`TYPE_CHECKING`), parsing uses `defusedxml` | no action: false positive                      |
| SEC-S0-03 | `persistence/privileged_migration_runner.py`       | Low      | bandit B105 x2: template placeholder tokens, not credentials                            | no action: false positive                      |
| SEC-S0-04 | `ps-cli/src/ps_cli/credentials.py`                 | Low      | bandit B101: loop-invariant `assert` with a reasoned `noqa`                             | no action: false positive                      |
| SEC-S0-05 | `domain_mapper/identity.py`                        | Low      | semgrep sha1 with `usedforsecurity=False`, documented as a non-security identifier hash | no action: false positive                      |
| SEC-S0-06 | `auth` public metadata                             | Low      | semgrep format-string-return class on a helper; input-validation hardening gap          | issue #289                                     |
| SEC-S0-07 | dependency set (336 pins)                          | Low      | `pip-audit`: no known vulnerabilities                                                   | no action: clean result, scanning owned by #258 |
| SEC-S0-08 | seven scan targets                                 | Low      | `trivy fs --scanners secret`: 0 findings                                                | no action: clean result                        |
| SEC-S0-09 | `scripts/*.sh`, `scripts/release/*`                | Low      | shellcheck: 22 style warnings (SC2034, SC2155), no quoting or injection class           | no action: owned by #257 AC-BI-004             |
| SEC-S0-10 | `Dockerfile`                                       | Low      | trivy DS-0026: no `HEALTHCHECK`                                                         | no action: Kubernetes probes cover liveness and readiness |
| SEC-S1-01 | n/a                                                | High     | High - reported privately                                                               | issue/advisory: reported privately             |
| SEC-S1-02 | n/a                                                | High     | High - reported privately                                                               | issue/advisory: reported privately             |
| SEC-S1-03 | `docs/architecture` MCP Interface rows             | Low      | authorization column of several tool rows differs from the implemented gates            | issue #290                                     |
| SEC-S1-04 | closed #143 title                                  | Low      | title names `PolicyManager`; code uses `ComplianceOfficer` on the pipeline routes       | no action: historical title, gates verified    |
| SEC-S1-05 | `/approvals/*`                                     | Low      | exempt from bearer by design; protected by single-use code, expiry and passkey assertion | no action: documented design (container architecture, Passkey Signing) |
| SEC-S1-06 | service request handling                           | Low      | no general request throttling (threat category, not a defect in a gate)                 | issue #291                                     |
| SEC-S2-01 | Cypher construction (23 sites)                     | Low      | interpolated positions are schema labels or fixed property maps; values parameterized; write gateway allow-lists labels | no action: validated schema constants          |
| SEC-S2-02 | `ps-service/src`, `ps-cli/src`                     | Low      | zero hits for process execution, `eval`, `pickle`, `yaml.load`                           | no action: nothing to fix                      |
| SEC-S3-01 | inbound and outbound payload paths                 | Low      | size limiting is not uniform (request-size limiting hardening)                          | issue #288                                     |
| SEC-S3-02 | curated-source override                            | Low      | operator-configured URL, changeable only by `SystemAdmin`; no request field selects an outbound URL | no action: administrator-controlled configuration |
| SEC-S4-01 | chart workloads                                    | Medium   | no uniform security-context baseline (KSV-0001, 0003, 0004, 0014, 0118 classes), root images on data stores | issue #292                                     |
| SEC-S4-02 | `ps-service` pod                                   | Low      | no NetworkPolicy for the service pod; token automount not disabled                      | issue #292                                     |
| SEC-S4-03 | provision job, data stores, subchart               | Low      | missing resource limits and requests (KSV-0011, 0015, 0016, 0018)                       | issue #292                                     |
| SEC-S4-04 | `charts/policy-system/values.yaml` FalkorDB image  | Low      | mutable `latest` tag (KSV-0013)                                                         | issue #277                                     |
| SEC-S4-05 | all images                                         | Low      | KSV-0125 trusted-registry rule: public registries by design; version pinning tracked by #277 and #278 | no action: by design, pinning tracked          |
| SEC-S4-06 | `ps-postgres-init` ConfigMap                       | Low      | KSV-0109: script reads passwords from the environment and passes them as `psql` variables, no literal | no action: false positive                      |
| SEC-S4-07 | Authentik blueprint ConfigMap                      | Low      | KSV-01010: matches key names only; no credential literal in the blueprint               | no action: false positive                      |
| SEC-S4-08 | Authentik subchart Role                            | Low      | KSV-0048, 0049, 0056, 0113 on the third-party subchart Role                             | issue #292                                     |
| SEC-S4-09 | `.github/workflows`                                | Low      | actions pinned by tag, not commit                                                       | no action: owned by #257 AC-BI-007             |
| SEC-S4-10 | `Dockerfile` comments                              | Low      | issue and criterion references in comments (provenance)                                 | no action: comment-only, routed to #251        |
| SEC-S5-01 | `Dockerfile` base image                            | Low      | floating tag `python:3.14-slim-trixie`                                                  | issue #278                                     |

## False positives and triage notes

- B608 (9): every statement is a module-constant template; user-influenced values are bound parameters.
- B405 and `use-defused-xml` (4): the stdlib import exists only for type annotations; `defusedxml.ElementTree.fromstring` parses the data.
- B105 (2): `{...}` template tokens in a migration runner.
- B101 (1): invariant assertion.
- sha1: used for deterministic identifiers with `usedforsecurity=False`.
- KSV-0109 and KSV-01010: the ConfigMaps hold SQL role-creation statements fed by environment variables and Authentik blueprint key names; no credential value is present.

## Superseded and removed

No source, test or config file was removed. The only text made obsolete is in the threat map: the auth module list that named a hyphenated `protected-resource` module (now `protected_resource`, plus `startup`, `models`, `errors`) and the image-hygiene note that omitted the base-image tag.

| Removed text | Proof (run from the repository root, excluding `docs/audits` and `.orchestrator`) | Result |
| --- | --- | --- |
| auth module list with the hyphenated module name | `git grep -n "discovery\`, \`protected-resource\`" -- . ':!docs/audits'` | one hit in `.agents/skills/ps-pentest/SKILL.md`, a skill listing probe surfaces by name; not a copy of the map text and left unchanged |
| image-hygiene note without the base-image tag | `git grep -n "Image hygiene" -- . ':!docs/audits'` | one hit: the updated map note |

## Reconciliation with `docs/secops`

`docs/secops/threat-coverage-map.md` was updated so each section carries code anchors (module paths), the stale auth file list and image-hygiene note are corrected, surfaces added since the map was written are listed, and a "Not yet covered" section lists threat categories with no probe. The map remains a pentest-coverage model; its anchors were checked against the code on the audit date. `docs/secops/baseline.yaml` and `docs/secops/posture-history.md` were deliberately not touched: the baseline file starts empty on purpose and is a registry of pentest runs, and this audit is not a pentest run.

## Private channel

Two findings are handled outside this repository (rows SEC-S1-01 and SEC-S1-02). No detail is recorded here or in any issue or commit. When fixed, they will be referenced generically.

## Sibling-issue review

Open #246 sub-issues and issues filed by earlier audits were reviewed on the audit date.

| Issue                                                  | Outcome                                                                                              |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| #251 conformance                                       | comment posted: Dockerfile comment provenance                                                        |
| #253 logging                                           | comment posted: operator-log content of caught exceptions is a logging-audit question               |
| #254 documentation                                     | comments posted: authorization-row drift tracked in #290 (docs overlap), threat-map anchors           |
| #255 API contracts                                     | comment posted: #288 may add an over-limit error shape; sequence with any error-body change         |
| #257 configuration and chart                           | comment posted: CI action pinning, chart hardening (#292), render requirements                       |
| #259 observability and resilience                      | comment posted: resource requests and limits are part of #292                                        |
| #260 guardrails                                        | comment posted: candidate checks from #288 to #292 and recurring scans (no scan scope in its body)   |
| #263-#276, #279-#287                                   | no change needed: no security-relevant scope change                                                  |
| #277 and #278                                          | no change needed: consistent with SEC-S4-04 and SEC-S5-01                                            |
| #114, #115, #132, #151                                 | no change needed                                                                                     |
| #288-#292 (filed by this audit)                        | filed and linked as sub-issues of #246                                                               |

## Acceptance criteria confirmation

| AC | Status | Evidence |
| --- | --- | --- |
| AC-BI-001 | met | this document: scope, method and reproduction table, 31 finding rows with id, location, severity, evidence, disposition |
| AC-BI-002 | met | S1: 21 REST surfaces plus the `/mcp` mount, 38 MCP tools (counts equal the `ast` enumeration) and the CLI operations, each mapped to authentication and authorization |
| AC-BI-003 | met | S2: SQL, Cypher, process and path constructions reviewed; no violation |
| AC-BI-004 | met | Method table and false-positive section: bandit, semgrep, pip-audit, trivy (secret, chart, Dockerfile), shellcheck triaged; true positives filed or owned, false positives recorded |
| AC-BI-005 | met | S4 and S5: secrets, image layers, workload settings and network policies checked against the chart; gaps filed (#292, #277, #278) |
| AC-BI-006 | met | S6: error bodies scrubbed and generic, pinned by tests |
| AC-BI-007 | met | Reconciliation section and the threat-map diff: code anchors, corrected statements, new surfaces, "Not yet covered" list |
| AC-BI-008 | met by placeholder | the two private-channel rows are the only record in this repository; the report through the private channel is an action of the repository owner and is pending confirmation |
| AC-BI-009 | met | every row has one disposition (placeholder rows use the private-channel form) |
| AC-BI-010 | met | design-dependent items are issues with problem, options and recommendation (#288 to #292) |
| AC-BI-011 | met | all behaviour-changing items are typed `fix` or `feat` issues; this change is documentation only |
| AC-BI-012 | not applicable | no fix made; no code touched |
| AC-BI-013 | met | Superseded section with grep proof |
| AC-BI-014 | met | this document and issue bodies carry control-category wording only; no payload, secret or reachable-path description of an open item |
| AC-BI-015 | met | sibling-issue review below |

## Result

Findings: 31 rows. Dispositions: 5 filed issues (#288, #289, #290, #291, #292), 2 existing issues (#277, #278), 2 private-channel placeholders, the rest no action with reasons. No code, test or chart file changed; no full suite run (documentation-only change).
