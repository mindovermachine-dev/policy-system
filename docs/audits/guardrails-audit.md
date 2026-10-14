<!-- © 2026 Cartman ApS. All rights reserved. -->

# Automated Guardrails Audit - Findings Report (Issue #260, umbrella #246)

The report covers the guardrails added by this audit, the guardrails it harvested from the other audits and why some are deferred, the sibling-issue review, and the measured runtime.

## Scope examined

Mechanical checks that keep the cleanup of the earlier audits from regressing: unused symbols, import layering, configuration drift (environment variables and Helm values against the chart and documentation), documentation links, stray `print` calls and repository hygiene. In scope as named by the issue: CI workflows, `scripts/check_*.py`, `pyproject.toml` lint and type configuration, pre-commit and `CONTRIBUTING.md`. Paths: `ps-service/src`, `ps-cli/src`, `ps-test-support/src`, `scripts`, `charts/policy-system`, `docs`, `.env.example`, `pyproject.toml`, `.pre-commit-config.yaml`, `.insitu.yml`.

## Tooling and reproduction

| Guardrail | Mechanism | Local command | CI id |
| --- | --- | --- | --- |
| unused symbols | vulture 2.16 through a pinned pre-commit environment, `[tool.vulture]` in `pyproject.toml`, confidence 80 | `uv run pre-commit run unused-symbols --all-files` | `unused-symbols` |
| import layering | import-linter 2.3 through a pinned pre-commit environment, `[tool.importlinter]` in `pyproject.toml` | `uv run pre-commit run import-layering --all-files` | `import-layering` |
| configuration drift | `scripts/check_config_drift.py` (rules `env-var-unlisted`, `helm-value-unused`, `helm-prod-orphan`, `helm-value-undocumented`) | `uv run python scripts/check_config_drift.py` | `config-drift` |
| documentation links | `scripts/check_doc_links.py` (rules `doc-link-missing`, `doc-link-anchor`, `doc-path-missing`) | `uv run python scripts/check_doc_links.py --tracked` | `doc-links` |
| stray print | ruff rule `T201` from `select = ["ALL"]`, allow-list pinned by `ps-service/tests/scripts/test_ruff_print_gate.py` | `uv run ruff check` | `ruff-check` |
| repository hygiene | `scripts/check_repo_hygiene.py` (rules `tracked-backup-file`, `exec-without-shebang`, `tracked-but-ignored`, `duplicate-gitignore-rule`, `readme-layout-missing-row`) | `uv run python scripts/check_repo_hygiene.py` | `repo-hygiene` |
| CI replica | `scripts/run_ci_guardrails.py` replays the CI commands against an export of the working tree | `UV_NO_SYNC=1 uv run python scripts/run_ci_guardrails.py` | not in CI |

Reproduction of the measurements behind the findings:

- Unused symbols before suppression: `uvx vulture==2.16 ps-service/src ps-cli/src ps-test-support/src scripts --min-confidence 80` reported 15 hits; the same command reports 0 after the per-site suppressions. At `--min-confidence 60` it reports 255 hits.
- Layering baseline: `PYTHONPATH=ps-service/src:ps-cli/src:ps-test-support/src uvx --from import-linter==2.3 lint-imports --no-cache` with the `entry-layer` contract and no `ignore_imports` reports 26 import lines (15 module pairs) from components into `ps_service.api`.
- Links: `git ls-files '*.md'` minus the excluded prefixes; with the exclusions in `scripts/check_doc_links.py` the tracked tree reports 0 problems, and without excluding `docs/templates/` and the skill `assets/` directories it reports 26, all template placeholders.
- Stray print: `echo 'print(1)' | uv run ruff check --stdin-filename ps-service/src/x.py --select T201 -` exits 1 and names `T201`; the same call with `ps-cli/src/x.py` exits 0; `scripts/x.py` also exits 1, so the new scripts write through `sys.stdout.write`.
- Hygiene probes: `git ls-files | grep -E '\.(bak|orig|rej|swp)$'`, `git ls-files -ci --exclude-standard`, `git ls-files -s | awk '$1==100755'` with a first-bytes check, and duplicate lines in each `.gitignore`: all empty on `main` before the check was written.
- Values documentation: `uv run python scripts/check_config_drift.py` on the tree before the reference document was edited reported 7 `helm-value-undocumented` findings.

## Harvested guardrails

The candidates come from the guardrail comments posted on this issue by the other audits (and the audit documents). Each has one disposition.

| id | source | candidate | disposition |
| --- | --- | --- | --- |
| G-1 | #247 | unused symbols (vulture) | fixed in this change: `unused-symbols`; confidence-60 and tests-only reachability ratchet: issue #324 |
| G-2 | #250 | import layering contracts | fixed in this change: `import-layering`; baseline removal belongs to #270 (comment posted) |
| G-3 | #257 | environment variable cross-reference | fixed in this change: `config-drift` (`env-var-unlisted`) |
| G-4 | #254 | Helm values cross-reference (values, production values, reference document) | fixed in this change: `config-drift` (three rules) |
| G-5 | #254 | documented config versus code | fixed in this change: `config-drift` (`helm-value-undocumented`); `.env.example` names covered by `env-var-unlisted` |
| G-6 | #253 | stray `print` in service code | fixed in this change: ruff `T201` already gates; allow-list pinned by a test that runs ruff |
| G-7 | #255 | OpenAPI and MCP surface drift | no action for detection: the structure test already fails CI; regeneration recipe documented in CONTRIBUTING.md; script, snapshot rename and responses test: issue #326 (comment on #303) |
| G-8 | #254 | link, anchor and path checker for markdown | fixed in this change: `doc-links` |
| G-9 | #254 | INV-005 component check against the solution architecture | issue #325 |
| G-10 | #248 | repository hygiene rules | fixed in this change: `repo-hygiene` |
| G-11 | #253 | log-leakage canary | no action: exists as `ps-service/tests/logging/test_log_leakage_canary.py`, runs in `pytest` |
| G-12 | #253 | closed log outcome vocabulary | issue #327, ordered after #295 |
| G-13 | #251 | `# type: ignore` ban | issue #327, ordered after #298 (21 sites re-measured) |
| G-14 | #251 | `subprocess` without `timeout=` in tests | issue #328 |
| G-15 | #251, #249 | provenance in YAML and TOML comments | issue #328, ordered after the scope decision in #299 |
| G-16 | #249 | stale mock-boundary entries | issue #328, ordered after #268 and #269 |
| G-17 | #257 | shellcheck over `scripts/` | no new issue: #310 clears the findings first and carries a comment that the gate is ready to add |
| G-18 | #257, #252 | action and tool pinning | no action: owned by #313; `action-refs` and `actionlint` already run |
| G-19 | #258 | deptry, scheduled pip-audit, npm audit, licence allowlist, update automation | issue #329 (needs a repository-level decision) |
| G-20 | #252 | chart misconfiguration scan, recurring secret scan | issue #329, ordered after #292 |
| G-21 | #259 | chart workloads declare probes and resources | issue #330, ordered after #319 and #320 |
| G-22 | #256 | migration-apply tests per component, backup verifier coverage | issue #330, ordered after #282, #283 and #284 |
| G-23 | #258, #255 | nothing beyond G-19 and G-7 | no action: recorded |
| G-24 | #249 | duplicate test-body detector | issue #328, ordered after #268 |
| G-25 | #247 | removal-proof search recipe | no action beyond documentation: review-only, recipe in CONTRIBUTING.md |

## Severity scale

- High: a defect or contract breach the guardrail would have caught.
- Medium: drift between code, deployment and documentation.
- Low: noise that must be suppressed or excluded so the guardrail can run clean.

## Findings

| id | location | severity | evidence | disposition |
| --- | --- | --- | --- | --- |
| F-S1-01 | `__exit__(self, *exc_info)` in five modules (`auth/discovery.py`, `change_monitor/cellar_consolidated.py`, `curated_source/http_fetch.py`, `ingestion/adapters/cellar_eli/fetch.py`, `invitations/client.py`) | Low | vulture reports the variadic parameter unused; the signature is fixed by the context-manager protocol | no action: per-site `# noqa: V107` with the reason on the same line |
| F-S1-02 | Protocol parameters in `graph_gateway/key_port.py` (3 sites) and `graph_gateway/read_only.py`; `write(self, s, /)` in `logging/emitter.py` | Low | the parameter names are the documented call contract of the Protocol or file-like method; implementations never read them in the protocol body | no action: per-site `# noqa: V107` with the reason on the same line |
| F-S1-03 | `LiteralString` under `TYPE_CHECKING` in `audit/store.py`, `passkey_signing/migration_runner.py`, `persistence/migration_runner.py` | Low | the name is used only inside a string cast, which vulture cannot see | no action: per-site `# noqa: V104` with the reason on the same line |
| F-S1-04 | `pyproject.toml` `[tool.vulture]` | Low | confidence 60 reports 255 hits (163 variables that are pydantic fields, 45 functions, 28 methods): too noisy to gate | issue (ratchet follow-up, filed with the remaining guardrail issues) |
| F-S1-05 | `pyproject.toml` `[tool.ruff.lint]` | Low | ruff reports a vulture code in a suppression comment as an invalid rule code unless it is declared external | fixed in this change: `external = ["V104", "V107"]` |
| F-S2-01 | `ps_service.<component>` importing `ps_service.api.*` (15 module pairs, 26 import lines) | Medium | `entry-layer` contract broken without a baseline; the other three contracts are kept | issue #270 (existing): the 15 pairs are the `ignore_imports` baseline of `entry-layer`, each group commented with the owning issue; remove the baseline when #270 lands |
| F-S2-02 | `graph_cleanup.executors -> authz.service`, `policy_lifecycle.service -> authz.service`, `state_migrations -> authz` and related chains | Low | chain links that disappear once `authz.service -> api.errors` is resolved | no action: not baselined; they stop being reported when the direct edge is ignored |
| F-S3-01 | `docs/artifacts/helm-chart-values-reference.md` rows for `psService.resources.*`, `psService.probes.*`, `psService.authzBootstrapOwner.issuer` | Low | keys documented only through shorthand (`/ .memory`), so they cannot be matched to the values file by name | fixed in this change: full dotted names in the rows |
| F-S3-02 | reference document versus values: "documented but absent" candidates | Low | 10 candidates are literals or prose (`tls.crt`, `ca.crt`, `values.yaml`, `host.containers.internal`) or documented under a longer prefix; zero true positives | no action: the stale-key rule is not built |
| F-S3-03 | `authentik.global.env` in `values-prod.yaml` | Low | key passes through to the Authentik sub-chart, which defines it | no action: listed in `PROD_ORPHAN_BASELINE` with that reason |
| F-S3-04 | every `PS_*` name in code | Low | all appear in the chart, scripts, CI configuration, `.env.example` or documentation; audit reports are excluded from the listing so a finding cannot satisfy its own rule | no action: the rule passes without a baseline |
| F-S4-01 | `docs/templates/` and `.claude/skills/**/assets/` | Low | 26 link problems, all template placeholders | no action: excluded by prefix in `scripts/check_doc_links.py` |
| F-S4-02 | deleted markdown file | Low | the pre-commit hook receives only staged existing files, so deleting a linked-to file trips only CI | no action: `uv run python scripts/run_ci_guardrails.py` runs the tracked mode locally |
| F-S6-01 | `pyproject.toml` `[tool.ruff.lint.per-file-ignores]` | Low | `T201` is ignored only for `ps-cli/src/**`, `tools/**` and `docs/regulations/*.py`; `scripts/` is not exempt, so a stray `print` in service or script code already fails ruff | fixed in this change: `test_ruff_print_gate.py` runs the locked ruff against the real configuration and pins the allow-list |
| F-S7-01 | tracked files | Low | no backup file, no executable bit without shebang, no tracked-but-ignored file, no repeated ignore rule and every top-level entry has a README row on `main`, so the check passes without a baseline | fixed in this change: `repo-hygiene` keeps it that way |
| F-S7-02 | `scripts/` | Low | the guardrail scripts themselves must not use `print` (not exempt from `T201`) | fixed in this change: scripts write through `sys.stdout.write` |
| F-G-01 | `# type: ignore` comments | Low | 21 sites (all in `ps-service/tests`), the issue for them (#298) says 20 | issue #298 (comment with the re-measured count); ban in #327 |
| F-G-02 | API surface | Low | the structure test fails on drift but names no rule and no regeneration step | fixed in this change: recipe in CONTRIBUTING.md (verified to reproduce the snapshot byte for byte); script in #326 |
| F-G-03 | `docs/audits/dependencies-audit.md` | Low | its guardrail table pointed at #260 for scheduled scans and the licence allowlist | fixed in this change: now points at #329; `pip-audit` runs in every CI run through `.insitu.yml`, which that audit's "no workflow" wording does not mention |

## False positives and exclusions

- vulture: the 15 hits above are all false positives; none is dead code.
- links: `docs/audits/`, `docs/regulations/`, `docs/templates/`, `test-data/`, skill `assets/` directories, `node_modules/`, `.venv/`, `.orchestrator/` and `spikes/` are out of scope.
- values documentation: the 10 "documented but absent" candidates of the earlier audit tooling are prose or literals, so no stale-key rule was built.
- import-linter: the chain links reported behind the 15 direct edges are not findings of their own.

## Suppressions and baselines

Every suppression on `main` carries its reason at the site (AC-BI-005):

| Mechanism | Count | Reason recorded |
| --- | --- | --- |
| `# noqa: V107` (unused argument) | 11 | protocol, override or context-manager signature, same line |
| `# noqa: V104` (unused import) | 3 | name used only in a type-checking-only cast, same line |
| `ignore_imports` of the `entry-layer` contract | 15 module pairs | each group commented with the owning open issue (#270); removed when it lands |
| `PROD_ORPHAN_BASELINE` in `scripts/check_config_drift.py` | 1 | passed through to the Authentik sub-chart |
| ruff `external = ["V104", "V107"]` | 2 codes | declares the vulture codes so ruff does not call them invalid |

There is no global `ignore_names`, and a test fails if one is added or a suppression has no reason.

## Superseded and removed

Nothing existing was obsolete: the guardrails are additions. The only edits to existing content are the seven shorthand rows of the values reference (replaced by full dotted keys), the vulture and ruff configuration, and the pointers in `docs/audits/dependencies-audit.md` that named this issue for work now tracked in #329.

Proof that nothing refers to names this audit considered and did not create (`git grep -n 'check_unused_symbols\|check_import_layering\|scripts/api_surface' -- . ':!docs/audits' ':!.orchestrator'`): no match. The surface snapshot keeps its name; its only references are the structure test and CONTRIBUTING.md.

## Sibling-issue review

Method: `gh api --paginate repos/:owner/:repo/issues/246/sub_issues` lists 75 sub-issues (13 audits #247 to #259 closed, this issue, and the issues filed by the audits, #263 to #323, all open); every body was read with `gh issue view`. Issues #324 to #330 are filed by this audit. A comment was posted only where scope, acceptance criteria, dependencies or order changed.

| Issue | Read result | Outcome |
| --- | --- | --- |
| #247 to #259 | closed audits | Their findings docs now end with an Enforcement table (AC-BI-002) |
| #263 | 19 symbols reachable only from tests | Comment: the reachability guardrail #324 depends on its per-symbol decisions |
| #264 | declare two imported packages | No change needed; the declared-versus-used check in #329 is ordered after it |
| #265 | spike directories | No change needed; untracked, outside every repository check |
| #266 | placeholder blame revision | No change needed |
| #267 | duplicated skill copy | No change needed |
| #268 | identical-body tests | No change needed; duplicate-body check in #328 is ordered after it |
| #269 | mutation gaps | No change needed |
| #270 | components import the entry layer | Comment: layering check landed, the 15-pair baseline is deleted with this issue |
| #271 | package cycles | No change needed; a cycle contract needs this issue first (review-only today) |
| #272 | split mcp_server.py | No change needed |
| #273 | split mixed-responsibility modules | No change needed |
| #274 | per-component helper copies | No change needed |
| #275 | declare the licence | No change needed; the licence allowlist in #329 is ordered after it |
| #276 | npm advisories | No change needed; npm scan in #329 is ordered after it |
| #277, #278 | image tag and digest pins | No change needed |
| #279 to #281 | dependency bumps | No change needed; the unused-symbol, layering and drift checks are configuration-pinned and do not block a bump |
| #282 | migration tests | No change needed; structural migration tests in #330 are ordered after it |
| #283, #284 | backup verifier | No change needed; ordered before #330 |
| #285, #286 | index decisions | No change needed |
| #287 | persistence descriptions | No change needed |
| #288, #289, #291 | request limits, input validation, throttling | No change needed |
| #290 | MCP authorization rows | No change needed |
| #292 | pod security baseline | No change needed; chart scan in #329 is ordered after it |
| #293, #294 | log hygiene | No change needed |
| #295 | log outcome vocabulary | Comment: closed-set check in #327 is ordered after it; no scope change |
| #296, #297 | request logs, audit records | No change needed |
| #298 | type-ignore removal | Comment: re-measured 21 sites (body says 20); ban in #327 is ordered after it |
| #299 | provenance in config comments | Comment: the layering baseline comments name #270 and are the single exception to carry as reasons; check in #328 follows the decision |
| #300 | stale lint ignores | No change needed; this audit added only the two external vulture codes, each with a reason |
| #301, #302 | graph name once, naming rules | No change needed |
| #303 | OpenAPI declarations | Comment: documented regeneration recipe for the snapshot; script and responses test in #326 follow it |
| #304 to #308 | MCP naming, error bodies, pagination, validation, CLI outcome | No change needed; a surface change regenerates the snapshot with the recipe |
| #309 | chart tunables | No change needed; the new values need a reference row, which `config-drift` now enforces |
| #310 | shellcheck findings | Comment: the shellcheck gate is ready to add in the same change |
| #311, #312 | shell function and defaults duplication | No change needed |
| #313 | workflow supply chain | No change needed; `action-refs` and `actionlint` already run |
| #314 to #318 | timeouts, retry settings, reconciliation, shutdown, readiness | No change needed |
| #319, #320 | chart probes and resources | No change needed; structural tests in #330 are ordered after them |
| #321, #322, #323 | metrics, tool wording, domain-concepts provenance | No change needed |
| #324 to #330 | filed by this audit | Created as sub-issues of #246 with problem, options and recommendation |

## Acceptance criteria confirmation

| AC | Result | Evidence |
| --- | --- | --- |
| AC-BI-001 | met | this document: scope, tooling, reproduction commands, findings with id, location, severity, evidence and disposition |
| AC-BI-002 | met | an `## Enforcement` table in each of the 13 audit documents, listing each rule as enforced (with its check id) or review-only (with the reason or issue), consistent across documents |
| AC-BI-003 | met | injected-violation tests per rule name an unused symbol, a layering violation, an unlisted environment variable, an unused or undocumented Helm value and a stray `print` (`T201`), and the failure output names the rule |
| AC-BI-004 | partly met | documented configuration versus code is checked by `config-drift`; the OpenAPI and MCP surface is checked by the existing structure test in `pytest`, which already fails CI, with the regeneration recipe documented; a dedicated check and a declared-versus-actual responses test are tracked in #326 |
| AC-BI-005 | met | every new check passes on `main` (replica run, pre-commit on all files); suppressions are listed above, each with its reason |
| AC-BI-006 | met | see the runtime section: added CI time is about 1.1 s of checks plus about 2.3 s of tests against a stated budget of 15 s |
| AC-BI-007 | met | CONTRIBUTING.md `## Automated guardrails`: the checks, how to run each locally including the replica, the review-only rules, and the cost; `test_contributing_guardrails.py` ties the listed commands to the configuration |
| AC-BI-008 | met | every finding and harvested guardrail above has one disposition: fixed, issue number, or no action with a reason |
| AC-BI-009 | met | #324 to #330 carry problem, options and recommendation and are sub-issues of #246 |
| AC-BI-010 | met | no shipped behaviour changed; the filed issues are typed `chore`, `test` and `ci` because they add checks only |
| AC-BI-011 | met | the root cause (no automation) is addressed by checks with red-then-green tests; full-suite result in the runtime section |
| AC-BI-012 | met | nothing became obsolete; the search proof is in the superseded section |
| AC-BI-013 | met | this document, CONTRIBUTING.md and the seven issue bodies carry names of variables, keys and rules only; the scan result is recorded below |
| AC-BI-014 | met | sibling-issue review above; comments on #263, #270, #295, #298, #299, #303 and #310 |

## Runtime of the new checks

Measured on the development host with a warm cache. The first run of a pre-commit hook builds its pinned environment once and needs network access.

| Check | Wall time |
| --- | --- |
| `unused-symbols` | 0.46 s |
| `import-layering` | 0.20 s |
| `config-drift` | 0.15 s |
| `doc-links` | 0.20 s |
| `repo-hygiene` | 0.06 s |
| serial total of the five checks | about 1.1 s |
| replica of all five including the export | about 3.5 s |
| new test files (`-n0`, seven files plus the contributing test) | about 2.3 s of test time |

Stated budget: at most 15 s added to the CI `trunk-worthy` wave (the checks run in parallel beside `pytest`), at most 5 s added to a typical commit (hooks run only when matching files are staged; `repo-hygiene` always runs), at most 10 s of added test time. Measured: about 1.1 s of checks and 2.3 s of tests, inside every limit.

Full suite (`uv run pytest --cov`, run once): 8158 passed, 1 skipped in 100 s. Expected: the 8066 passed of the base plus 92 new tests (68 from the first part: 13 config drift, 19 documentation links, 9 script wiring, 19 tool wiring, 8 replica; 24 from the second part: 6 print gate, 10 hygiene, 4 added to script wiring, 4 contributing). No difference to explain, no failure, nothing skipped or marked expected-fail.

## Leak scan

A search of this document, CONTRIBUTING.md, the thirteen Enforcement sections and the seven new issue bodies for credential words, key and token patterns, hosts and addresses finds only variable names, rule names and chart value names. No secret, exploit text or vulnerability detail is present.
