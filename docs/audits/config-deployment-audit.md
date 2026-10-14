<!-- © 2026 Cartman ApS. All rights reserved. -->

# Configuration, Deployment Scripts and Helm Chart Hygiene Audit - Findings Report (Issue #257, umbrella #246)

## Scope examined

Covered: `ps-service/src/ps_service/config.py` and the other environment reads in `ps-service`, `ps-cli` and `ps-test-support`; `scripts/*`, `scripts/lib`, `scripts/release` and `scripts/release/lib`; `charts/policy-system` (templates, `values.yaml`, `values-prod.yaml`, chart unit tests); `deploy/kind`; `.github/workflows`, `.github/actions` and `.github/scripts`; `.env.example`; `.insitu.yml` for CI tool installation.

Not examined: anything that needs a cluster, Docker or the network beyond read-only GitHub API calls. No `kubectl`, `helm install` or upgrade, `az` or container command was run. Dependency version changes belong to the dependencies audit.

## Method and commands

Tooling used: Helm v4.3.0 (local; CI pins v3.18.4), helm-unittest plugin 1.1.2, shellcheck 0.11.0, actionlint 1.7.12, `gh` for read-only API calls, `uv run --frozen python -B` for cross-reference scripts. Nothing was installed and no dependency or lock file changed.

Rendering with fake values (the chart fails closed without LLM credentials, an auth issuer and a bootstrap owner, which is by design):

```sh
C=charts/policy-system
helm template r $C --set llm.existingSecret=x --set psService.localTestBypass.enabled=true
helm template r $C -f $C/values-prod.yaml --set llm.existingSecret=x \
  --set psService.auth.issuer=https://i.example --set psService.auth.audience=a \
  --set psService.authzBootstrapOwner.subject=s \
  --set psService.authzBootstrapOwner.issuer=https://i.example
helm lint $C --strict --set llm.existingSecret=x --set psService.localTestBypass.enabled=true
helm lint $C -f $C/values-prod.yaml --strict --set llm.existingSecret=x \
  --set psService.auth.issuer=https://i.example --set psService.auth.audience=a \
  --set psService.authzBootstrapOwner.subject=s \
  --set psService.authzBootstrapOwner.issuer=https://i.example
helm unittest $C
scripts/verify-chart-independence.sh
```

Scripts and workflows:

```sh
shellcheck -P SCRIPTDIR -f gcc scripts/*.sh scripts/lib/*.sh scripts/release/*.sh scripts/release/lib/*.sh
actionlint .github/workflows/*.yml
.github/scripts/verify-action-refs.sh
grep -rhoE 'uses: *[^ ]+' .github | sort | uniq -c | sort -rn
```

Environment variables, Helm values and shell duplication were cross-referenced with three read-only scripts (`envxref.py`, `valuesxref.py`, `fndup.py`). They walk the repository, write only to a scratch directory and are described below so a reviewer can rewrite them:

- `envxref.py`: collect every `PS_*` token and record the areas it appears in: code (`ps-service/src`, `ps-cli/src`, `ps-test-support/src`), tests, chart (templates, values, files), scripts (`scripts`, `deploy`, `.github`) and docs (`docs`, `README.md`, `ps-skills`). Result: 142 tokens, 126 after dropping bare prefixes used in prose.
- `valuesxref.py`: flatten the leaves of `values.yaml` and `values-prod.yaml` and search the templates for each.
- `fndup.py`: parse shell function definitions in `scripts/*.sh`, group by name and compare bodies byte for byte.

Dispositions: `fixed in this change`, `issue #N`, or `no action: <reason>`. Severity: High is a reachable defect or exposure, Medium a misleading or costly inconsistency, Low an inert one. No High candidate was found.

## Results at a glance

| check                                      | result                                                                                    |
| ------------------------------------------ | ----------------------------------------------------------------------------------------- |
| `helm lint` default and prod, `--strict`   | 0 failed; one INFO line (`icon is recommended`); no warnings                              |
| `helm template` default and prod           | pass with the fake values above (759 and 2002 lines); bare defaults fail by design        |
| `helm unittest`                            | 33 suites, 255 tests passed, 0 snapshots                                                  |
| `verify-chart-independence.sh`             | 3 PASS lines                                                                              |
| `shellcheck -P SCRIPTDIR`                  | 22 findings: SC2155 x13, SC2034 x5, SC2059 x2, SC2015 x2 (plus 2 SC1091 path notes)       |
| `actionlint`                               | no findings                                                                               |
| `verify-action-refs.sh`                    | every `uses:` reference resolves                                                          |
| `values.yaml` keys with no template reader | 0                                                                                         |
| `.env.example` vs `ServiceConfig`          | 28 variables read by `config.py` were missing; now present (commented, with the default)  |

## Acceptance criteria confirmation

Status: met, partial (the audit part is met, the remediation is tracked in an issue because the rule forbids doing it inline), or not applicable.

| AC           | status                      | evidence                                                                                                                                                                                                                                                                                  |
| ------------ | --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| AC-BI-001    | met                         | This document: "Scope examined", "Method and commands" (exact commands, tool versions), "Findings" with id, location, severity, evidence and disposition.                                                                                                                                  |
| AC-BI-002    | partial                     | `envxref.py` (126 names) and `valuesxref.py` (0 unreferenced leaves) cross-referenced code, chart, scripts and docs. The 28 variables missing from `.env.example` were added (CFG-S2-01). Variables settable only by editing the template are tracked in #309 (eight listed, `PS_LOGGING_DIR` deliberately without a chart value) and #301 (`PS_FALKORDB_GRAPH`), because changing the chart surface is a `feat`. |
| AC-BI-003    | met                         | "Environment variable naming" section: convention `PS_<COMPONENT>_<FIELD>` from the Python standard, exceptions listed (CFG-S2-05, CFG-S2-06 are no action with a reason).                                                                                                               |
| AC-BI-004    | partial                     | shellcheck run: 22 findings (SC2155 x13, SC2034 x5, SC2059 x2, SC2015 x2). Fixing them and moving the 27 identical functions edit runtime scripts exercised by tests, so they are #310 and #311 (CFG-S3-01 to CFG-S3-06), not done inline. Nothing is suppressed or fixed in this change.   |
| AC-BI-005    | met, with a caveat          | `helm lint --strict` default and prod: 0 failed, one INFO line (`icon is recommended`), no warning. `helm template` default and prod pass with fake values (the chart fails closed without them by design, CFG-S1-02). `helm unittest`: 255 passed. Run with local Helm v4.3.0; CI pins v3.18.4 (CFG-S1-10). |
| AC-BI-006    | partial                     | Dev-only defaults are each overridden or fail closed in the prod render (CFG-S1-09). Contradictions found and tracked: FalkorDB `latest` (CFG-S1-03, #277), chat capacity 300 against 200 (CFG-S3-07, #312). No chart value was changed.                                                  |
| AC-BI-007    | partial                     | `actionlint` clean, `verify-action-refs.sh` resolves every `uses:`, every referenced script exists, all workflows declare permissions (CFG-S4-02). No action is pinned to a commit SHA and one ref is a branch name (CFG-S4-01), tracked in #313.                                         |
| AC-BI-008    | met                         | Every finding row has exactly one disposition: fixed in this change (CFG-S2-01), `issue #N`, or `no action` with a reason.                                                                                                                                                                |
| AC-BI-009    | met                         | #309 to #313 each carry problem, options and a recommendation and are not fixed inline; findings with design decisions point to existing issues (#277, #292, #301, #299).                                                                                                                |
| AC-BI-010    | met                         | Filed with typed titles: #309 `feat(chart)`, #310 `fix(scripts)`, #311 `refactor(scripts)`, #312 `fix(scripts)`, #313 `ci`. The only inline change is additive comments in `.env.example`, which no code reads.                                                                              |
| AC-BI-011    | met (nothing to fix)        | The one fix, `.env.example`, addresses the cause (the example was never extended when `ServiceConfig` grew). No code or test file changed, so there are no touched tests; `pre-commit` passes on both files.                                                                              |
| AC-BI-012    | not applicable              | Nothing became obsolete and nothing was deleted; proof under "Removal proof" and "Superseded and removed items".                                                                                                                                                                          |
| AC-BI-013    | met                         | Variable names only, no values: the example lines are empty or carry the public defaults of `config.py`; the render commands use placeholder values. The supply-chain issue #313 is written generically. No vulnerability candidate arose in this audit, so no private report was needed. |
| AC-BI-014    | met                         | "Sibling issue review": 64 rows from the live sub-issue list, every body read; comments posted on #254, #259, #260, #284, #288, #299 and #301, and an addition on #310; the rest record why no change was needed.                                                                        |

## Findings

| id        | location                                                                                                         | severity | evidence                                                                                                                                                                                                 | disposition                                                                                                              |
| --------- | ---------------------------------------------------------------------------------------------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| CFG-S1-01 | `charts/policy-system/Chart.yaml`                                                                                | Low      | `helm lint` prints the INFO line `icon is recommended`; no warning                                                                                                                                       | no action: INFO-level and cosmetic; an icon changes the packaged chart                                                   |
| CFG-S1-02 | `charts/policy-system/templates/secret.yaml`                                                                     | Low      | `helm template` with bare defaults fails: the LLM credential guard requires `llm.existingSecret` or credentials                                                                                           | no action: deliberate fail-closed guard; the audit renders with fake values                                              |
| CFG-S1-03 | `charts/policy-system/values.yaml:10`                                                                            | Medium   | FalkorDB image `tag: latest`; the prod render uses `falkordb/falkordb:latest` (`values-prod.yaml` does not override it)                                                                                   | issue #277                                                                                                               |
| CFG-S1-04 | `charts/policy-system/values.yaml:173,251`                                                                       | Low      | PS Postgres and Authentik Postgres use `16-alpine`, a major-pinned supported tag                                                                                                                         | no action: major-pinned and supported (assessed in #277)                                                                 |
| CFG-S1-05 | prod render: FalkorDB and Authentik Postgres Deployments; PS Postgres Deployment                                | Medium   | the first two have no readiness or liveness probe and no resources; PS Postgres has probes but no limits; only ps-service and the Authentik server and worker set resources                              | no action: chart probes and resources are in the scope of #259; comment posted there                                     |
| CFG-S1-06 | `charts/policy-system/values.yaml` (30 lines), `values-prod.yaml` (7 lines), chart template comments           | Low      | comments cite issue numbers, acceptance-criteria ids and plan sections; the provenance rule names only Python comments                                                                                   | issue #299 (comment posted with the sites; `values*.yaml` comment edits are render-neutral)                              |
| CFG-S1-07 | all chart workloads                                                                                              | Medium   | only the ps-service containers set `runAsNonRoot`; no uniform container hardening baseline                                                                                                               | no action: owned by #292                                                                                                 |
| CFG-S1-08 | `charts/policy-system/tests/__snapshot__`                                                                        | Low      | empty directory on disk, no tracked file (`git ls-files` returns nothing), snapshot count 0                                                                                                              | no action: not part of the repository                                                                                    |
| CFG-S1-09 | `values.yaml:31,62,231` vs `values-prod.yaml`                                                                    | Low      | development-only defaults (`service.type: NodePort`, FalkorDB browser enabled, Authentik disabled, `localTestBypass.enabled: false`) are each overridden or fail closed in the prod render; no hostPath, no privileged container, no NodePort or LoadBalancer in the prod render | no action: prod overrides verified in the render; the bypass is off by default and its render guard fails closed         |
| CFG-S1-10 | local tooling vs CI                                                                                              | Low      | local Helm is v4.3.0 while CI installs v3.18.4; all three CI places (`on_semver.yml:191`, `prep-runner/action.yml:55-62`, `.insitu.yml:73`) agree on v3.18.4                                              | no action: the CI version is authoritative; the same renders and 255 tests pass locally; bumps follow #281               |
| CFG-S2-01 | `.env.example`                                                                                                   | Medium   | 28 variables read by `config.py` and the graph readers were absent from the example file (all authentication, both Postgres groups, query limits, curated source, bootstrap owner, identity provider, run concurrency)            | fixed in this change: commented additive lines with the code default and meaning                                         |
| CFG-S2-02 | `ps-service-deployment.yaml` env block vs `config.py`                                                            | Medium   | `PS_SERVICE_MAX_REQUEST_BODY_BYTES`, `PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS`, `PS_QUERY_TIMEOUT_MS`, `PS_QUERY_ROW_CAP`, `PS_INGESTIONRUNS_MAX_IN_FLIGHT`, `PS_CURATEDSOURCE_URL`, `PS_CURATEDSOURCE_ALLOW_INSECURE_HTTP`, `PS_LOGGING_DIR` are read but have no chart value and no extra-env value | issue #309                                                                                                               |
| CFG-S2-03 | `change_monitor/falkordb_client.py:131`, `policy_lifecycle/graph_submission.py:188`, `company_merge/falkordb_client.py:161`, `mcp_interface/mcp_server.py:351` | Low      | `PS_FALKORDB_GRAPH` is read directly from `os.environ` in four modules although `config.py:6` says components should not read the environment independently                                             | issue #301                                                                                                               |
| CFG-S2-04 | `ps-service/src/ps_service/logging/facade.py:67`                                                                 | Low      | `PS_LOGGING_DIR` is resolved both in `ServiceConfig` and in the logging facade                                                                                                                           | no action: the facade also computes a repository-relative default when the variable is unset; recorded on #301                                |
| CFG-S2-05 | `PS_SIGNING_*`, `PS_STATE_DATABASE`, `PS_STATE_USER` (chart provisioning job) vs `PS_PASSKEYSIGNING_POSTGRES_*`, `PS_STATE_POSTGRES_*` (service) | Low      | job-only names have no Python reader; the service names follow `PS_<COMPONENT>_<FIELD>`                                                                                                                  | no action: documented naming exception; renaming changes rendered output and the operator contract                      |
| CFG-S2-06 | `PS_QUERY_TIMEOUT_MS` vs `*_SECONDS` names                                                                       | Low      | unit suffix is mixed (`_MS` once, `_SECONDS` elsewhere)                                                                                                                                                  | no action: renaming a shipped variable is a contract change with no benefit; exception listed in the naming section      |
| CFG-S2-07 | script-only variables (`PS_ROLLOUT_TIMEOUT`, `PS_PORT_FORWARD_*`, `PS_EVAL_*`, `PS_BLUEPRINT_*` ...)           | Low      | read only by scripts, no Python reader                                                                                                                                                                   | no action: live script knobs; already recorded as live in `dead-code-audit.md`                                           |
| CFG-S2-08 | `charts/policy-system/values*.yaml`                                                                              | Low      | `valuesxref.py` finds 0 `values.yaml` leaves without a template reader; `values-prod.yaml` overrides 15 leaves, all present in `values.yaml` except the subchart-owned `authentik.global.env`            | no action: consistent                                                                                                    |
| CFG-S3-01 | `scripts/deploy-llm.sh:46`, `scripts/ps-aks.sh:36`, 11 files under `scripts/release/`                          | Low      | SC2155 x13, each `readonly USAGE="usage: ... $(basename "$0") ..."`; `basename` cannot fail, so a split is behaviour-neutral, but every script is exercised by tests                                     | issue #310                                                                                                               |
| CFG-S3-02 | `scripts/release/sync-version-files.sh:31,32`                                                                    | Low      | two unused variables (the verify script has its own copies)                                                                                                                                              | issue #310                                                                                                               |
| CFG-S3-03 | `scripts/release/lib/conventional-header.sh:31,43,45`                                                            | Low      | SC2034 on globals consumed by the sourcing scripts (`lint-commit-header.sh:46`, `bump-from-commits.sh:28`)                                                                                               | issue #310 (suppression with reason)                                                                                     |
| CFG-S3-04 | `scripts/deploy-llm.sh:95`, `scripts/ps-aks.sh:41`                                                               | Low      | SC2059: colour-code variables inside the printf format are intentional                                                                                                                                   | issue #310 (suppression with reason)                                                                                     |
| CFG-S3-05 | `scripts/release/sync-version-files.sh:52,62`                                                                    | Low      | SC2015 `A && B \|\| C` where the `\|\|` branch is meant to run on either failure                                                                                                                         | issue #310                                                                                                               |
| CFG-S3-06 | `scripts/deploy-llm.sh`, `scripts/deploy-ps-prod.sh` and five more scripts                                      | Medium   | 54 function names defined in 2 or more scripts: 27 byte-identical, 27 differing; `print_error` has 7 copies. The deploy tests copy a fixed file list (`deploy_ps/conftest.py:200-205`, `deploy_llm/conftest.py:72`) so a new library file needs test setup changes | issue #311                                                                                                               |
| CFG-S3-07 | `scripts/llm-defaults.conf:7`, `scripts/ps-defaults.conf:6`                                                     | Medium   | chat model capacity 300 vs 200; SKU keys only in `ps-defaults.conf`; region candidates and model names repeated                                                                                          | issue #312                                                                                                               |
| CFG-S3-08 | `.insitu.yml`, `.pre-commit-config.yaml`                                                                         | Medium   | no shell lint gate runs `shellcheck` over `scripts/**`                                                                                                                                                   | no action: guardrails belong to #260; comment posted there                                                               |
| CFG-S3-09 | `scripts/lib/*.sh`                                                                                               | Low      | three library files lack `set -euo pipefail` and are mode 644                                                                                                                                           | no action: sourced libraries inherit the caller's options and are never executed                                         |
| CFG-S3-10 | `scripts/*.sh`, `scripts/release/*.sh`                                                                           | Low      | every executable script has `set -euo pipefail`; none is tracked with a stale `__pycache__`                                                                                                              | no action: clean                                                                                                         |
| CFG-S4-01 | `.github/workflows/*.yml`, `.github/actions/prep-runner/action.yml`                                              | Medium   | no action is pinned to a commit SHA; `pr-to-ready.yml:61` uses the branch-like ref `@experimental`; a non-GitHub-owned action uses `@v1`; CI-time tool installers are read from a moving branch and a Helm plugin install has no version (`.insitu.yml:54,73,76`) | issue #313                                                                                                               |
| CFG-S4-02 | `.github/workflows/*.yml`                                                                                        | Low      | every referenced script exists (`lint-commit-header.sh`, `release.sh`, `create-github-release.sh`, `test_container_image.py`); all six workflows declare job-level `permissions`; `actionlint` passes   | no action: consistent                                                                                                    |
| CFG-S4-03 | `.github/actions/prep-runner/action.yml:50-51`                                                                   | Low      | comments carry decision-id tags (`D-4`)                                                                                                                                                                  | issue #299                                                                                                               |

## Environment variable naming (AC-BI-003)

The convention is `PS_<COMPONENT>_<FIELD>`, upper case, with the component in the same spelling as the `ServiceConfig` section (the convention is stated in `docs/coding-standards/level2-python-instructions.md`, Configuration and Secrets) (for example `PS_STATE_POSTGRES_HOST`, `PS_AUTH_ISSUER`, `PS_QUERY_ROW_CAP`). Of the 126 distinct names found in code, scripts, chart and docs, 41 are test seams (`PS_TEST_*`) and 11 are `ps-cli` names; the service and chart names follow the convention. Exceptions, all listed here:

| exception                                                                    | reason                                                                     |
| ---------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `PS_SIGNING_DATABASE`, `PS_SIGNING_USER`, `PS_STATE_DATABASE`, `PS_STATE_USER` | read by the provisioning job, never by Python; service-side names differ   |
| `PS_QUERY_TIMEOUT_MS`                                                        | unit suffix `_MS`; every other duration uses `_SECONDS`                    |
| `LLM_*`, `AUTHZ_OWNER_EMAIL`, `TLS_CONTACT_EMAIL` in `scripts/*.conf`        | script configuration, not service environment                              |
| `HELM_BIN`, `AUTHENTIK_*`                                                    | upstream tool or subchart names                                            |
| `PS_TEST_*`, `PS_CLI_FAKE_*`                                                 | test seams, set only by tests                                              |

## Reconciliation done in this change

`.env.example` is documentation only: it is referenced by `README.md`, `CONTRIBUTING.md` and comments in `tools/*.py`, and no code or test reads it (`scripts/ps-service.sh` sources the untracked `.env`). Commented, additive lines were added for the variables listed in CFG-S2-01, each with the default taken from `config.py` and no credential values. The stray `PS_SERVICE_` text in that file is a section heading, not a stub. After the edit, every variable `config.py` reads appears in the file; `PS_FALKORDB_GRAPH` is included although it is read outside `config.py`.

## False positives and reasons

- SC2034 on `scripts/release/lib/conventional-header.sh` (CFG-S3-03): the globals are read by the sourcing scripts, which shellcheck does not follow without `-x`.
- SC1091 "not following" notes (2 with `-P SCRIPTDIR`, 24 without): path-resolution artefacts of the invocation form, not findings. Without `-P SCRIPTDIR` one SC2153 note also appears (`scripts/release/bump-from-commits.sh:96`, a possible variable misspelling); it is outside the 22 counted findings and is included in #310 for review.
- The planning assumption that `azure/setup-helm@v4` has no Helm version is wrong: `on_semver.yml:191` sets `v3.18.4`.

## Removal proof

No file, symbol or key was removed by this change, so a repo-wide removal search is not applicable. Proof that nothing was removed:

```sh
git diff --diff-filter=D --name-only          # no deleted file
git diff -U0 .env.example | grep -c '^-[^-]'  # 0 removed lines
```

## Superseded and removed items

None. The only edit is additive documentation in `.env.example` (91 lines added, none removed), so nothing became obsolete. Search proof that no symbol is left dangling, excluding the audit documents and tracker:

```sh
git grep -n -e 'config-deployment-audit' -- . ':!docs/audits' ':!.orchestrator'   # no match
git diff -U0 .env.example | grep '^-[^-]'                                          # no match
```

Every variable name added to `.env.example` is read by code (see "Verification evidence").

## Sibling issue review

Reviewed against the outcomes above: 64 issues, being every sub-issue of #246 except #257 itself, read from the live sub-issue list (10 closed, including the earlier audits #247 to #256 and #258, and 54 open). The body of every one was fetched and read; each row records what that body says and what it means for this audit. Comments were posted on #254, #259, #260, #284, #288, #299 and #301, where scope, dependencies or order changed; every other row records that no change was needed and why.

| issue | decision                                                                                                                                                                                                         |
| ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| #247  | closed; the dead-code audit scoped Helm values and env vars and found them live; the cross-reference here agrees (0 unreferenced `values.yaml` leaves). No change needed.                                        |
| #248  | closed; repository hygiene (tracked artefacts, root files). The `.env.example` edit adds no file. No change needed.                                                                                              |
| #249  | closed; test value and mock boundaries. The deploy script tests are the regression net for #310 to #312 and stay green. No change needed.                                                                         |
| #250  | closed; package boundaries and import layering. No config or deploy item. No change needed.                                                                                                                      |
| #251  | closed; Python standards conformance. Shell scripts are outside its scope; the env var convention it cites (`level2-python-instructions.md`, Configuration and Secrets) is the one applied in AC-BI-003. No change needed. |
| #252  | closed; security audit. Its container-hardening and request-size findings are #292 and #288; the chart security rows here either defer to #292 or are not security items. No change needed.                       |
| #253  | closed; logging. `PS_LOGGING_DIR` is only documented here and not changed. No change needed.                                                                                                                     |
| #254  | open; documentation audit owns `helm-chart-values-reference.md` and deployment docs. Comment posted: #309 and #312 will require matching doc updates.                                                              |
| #255  | closed; API and contract consistency, no env or chart item. No change needed.                                                                                                                                    |
| #256  | closed; persistence. Its verifier script is outside the shellcheck findings (its function copies are mentioned under #284). No change needed.                                                                      |
| #258  | closed; dependency and image versions, no ownership of workflow actions (stated in its own doc); pinning of actions is #313. No change needed.                                                                     |
| #259  | open; owns probes, resources, shutdown and timeouts. Comment posted: FalkorDB, Authentik Postgres and PS Postgres gaps found in the prod render, plus pointer to #309.                                             |
| #260  | open; guardrails. Comment posted: no shell lint gate exists (fix #310 first), and no check that actions are pinned (#313). Its existing note that action pinning is owned here is honoured by #313.                 |
| #263  | open; Python symbols reachable only from tests. No config or script symbol in its list. No change needed.                                                                                                        |
| #264  | open; declare `starlette` and `redis` in `ps-service/pyproject.toml`. Python dependencies only; no overlap with chart or scripts. No change needed.                                                              |
| #265  | open; local spike directories under `spikes/`, untracked, never touched by this audit. No change needed.                                                                                                          |
| #266  | open; blame ignore file. No change needed.                                                                                                                                                                       |
| #267  | open; duplicated reasoning skill file. No change needed.                                                                                                                                                         |
| #268  | open; identical-body tests in `ps-service/tests`; none of the listed files is a deploy or chart test. No ordering change.                                                                                         |
| #269  | open; mutation survivors; its `scripts/check_mock_boundaries.py` row is Python. No change needed.                                                                                                                |
| #270  | open; moves shared errors and models out of `api`; touches no config or env name. No change needed.                                                                                                              |
| #271  | open; breaks the `config` and `curated_source` import cycle in code. Env names in #309 (`PS_CURATEDSOURCE_*`) are unaffected by a code move. No change needed.                                                  |
| #272  | open; splits `mcp_server.py`; one of the four `PS_FALKORDB_GRAPH` readers is there, already sequenced by the note on #301. No change needed.                                                                     |
| #273  | open; splits `main.py` and orchestration modules; no variable or chart value changes. No change needed.                                                                                |
| #274  | open; per-component helper copies; the graph-name copies overlap #301 only through the same variable, and #301 already carries the sequencing note. No change needed.                                            |
| #275  | open; project licence declaration. No change needed.                                                                                                                                                             |
| #276  | open; npm advisories in dev tooling. No change needed.                                                                                                                                                           |
| #277  | open; pins the FalkorDB image (`values.yaml:10`, matches finding CFG-S1-03) and its test and docs literals. #309 edits other `psService.*` values, so the two only need a rebase. No change needed.            |
| #278  | open; Dockerfile base image digest, relies on the update automation proposed on #260; #313 relies on the same automation and does not conflict. No change needed.                                                 |
| #279  | open; runtime dependency bumps; `pyproject.toml` and `uv.lock` only. No change needed.                                                                                                                            |
| #280  | open; openai major version decision. No change needed.                                                                                                                                                           |
| #281  | open; Python dev tooling bumps; the Helm, actionlint and shellcheck versions this audit used are CI-pinned elsewhere (CFG-S1-10). No change needed.                                                              |
| #282  | open; migration tests for authz and runtime_config. No change needed.                                                                                                                                            |
| #283  | open; runs the backup-restore verifier end to end; this audit ran no cluster command, so nothing to add. No change needed.                                                                                       |
| #284  | open; extends verifier counts and asks for coordination with #257. Comment posted: backup scope is unchanged here, Authentik Postgres gaps routed to #259, and #311 edits `print_error` and `require_command` copies in the same script, so the later change rebases. |
| #285  | open; graph indexes. No change needed.                                                                                                                                                                           |
| #286  | open; signing database indexes. No change needed.                                                                                                                                                                |
| #287  | open; edits the header comment of the chart init script (renders into a ConfigMap) and a domain doc pointer; no chart value or env var involved, disjoint from #309. No change needed.                            |
| #288  | open; request-size limiting. Comment posted: `PS_SERVICE_MAX_REQUEST_BODY_BYTES` has no chart value (#309); update #309 if a different mechanism is chosen.                                                       |
| #289  | open; validation of public metadata endpoints against deployment configuration. Validation of request inputs, no existing variable from this audit involved. No change needed.                              |
| #290  | open; reconciles MCP authorization rows in the architecture doc. No change needed.                                                                                                                               |
| #291  | open; throttling design decision (application or edge). Edge throttling would be chart work, but no decision exists yet. No change needed.                                                                       |
| #292  | open; container hardening baseline. It edits security contexts of the same Deployment templates as #309 but different blocks (securityContext against env). Owns CFG-S1-07. No change needed.                    |
| #293  | open; exception text in logs. No change needed.                                                                                                                                                                  |
| #294  | open; user identifiers in logs. No change needed.                                                                                                                                                                |
| #295  | open; log outcome vocabulary. No change needed.                                                                                                                                                                  |
| #296  | open; request logs in structured format. Its log destination depends on #259; no env var from this audit involved. No change needed.                                                                              |
| #297  | open; audit rows for security actions. No change needed.                                                                                                                                                         |
| #298  | open; type-ignore removal in tests. No change needed.                                                                                                                                                            |
| #299  | open; provenance rule for config-file comments. Comment posted listing the chart values, template, `scripts/*.conf` and workflow action comment sites; `values*.yaml` comment edits are render-neutral.         |
| #300  | open; prunes stale lint ignores for Python. No change needed.                                                                                                                                                    |
| #301  | open; resolve `PS_FALKORDB_GRAPH` once through `ServiceConfig`. Comment posted: the only other direct reads are `PS_LOGGING_DIR` and the provisioning job; the variable is excluded from #309 so #301 owns it.      |
| #302  | open; coding standard wording (function names, suppressions). Env var naming is not among its five points. No change needed.                                            |
| #303  | open; OpenAPI document. No change needed.                                                                                                                                                                        |
| #304  | open; MCP tool names. No change needed.                                                                                                                                                                          |
| #305  | open; error body shapes. No change needed.                                                                                                                                                                       |
| #306  | open; pagination of list operations. No change needed.                                                                                                                                                           |
| #307  | open; request validation in models. No change needed.                                                                                                                                                            |
| #308  | open; `ps-cli` output of ingest outcome. No change needed.                                                                                                                                                       |
| #309  | filed by this audit (`feat(chart)`); body re-read after filing: eight variables, none owned elsewhere; native sub-issue of #246.                                                                                  |
| #310  | filed by this audit (`fix(scripts)`); 24 shellcheck notes; native sub-issue of #246.                                                                                                                              |
| #311  | filed by this audit (`refactor(scripts)`); 54 duplicated function names; native sub-issue of #246.                                                                                                                |
| #312  | filed by this audit (`fix(scripts)`); chat capacity 300 against 200; native sub-issue of #246.                                                                                                                    |
| #313  | filed by this audit (`ci`); generic supply-chain wording, no exploit detail; native sub-issue of #246.                                                                                                            |

## Verification evidence

- Chart: the rendered output was not changed (no chart file edited); `helm lint`, `helm template` and `helm unittest` results are in the table above. No `values*.yaml` comment was edited because the cleanup depends on the wording decision in #299.
- Scripts: no script, test or Python file changed, so the full test suite is skipped; the tree code is identical to the base commit.
- `.env.example`: all 28 added names were checked against `ps-service/src` (each is read in `config.py`, except `PS_FALKORDB_GRAPH`, read in four component modules) and each stated default equals the code default (for example `_DEFAULT_QUERY_TIMEOUT_MS = 5000`, `_DEFAULT_MAX_REQUEST_BODY_BYTES = 104_857_600`, `_DEFAULT_GRAPH_DRIFT_CHECK_INTERVAL_SECONDS = 3600`, ports 5432). Credential-bearing names are empty and commented out. Every `PS_*` name read by `config.py` is present in the file.
- Repository: the working tree differs from the base commit by this document and `.env.example` only; `uv run pre-commit run --files` passes on both.
- Public hygiene: the document and the filed issues contain variable names only, no values, credentials or exploit detail; the supply-chain item is described generically.
- Issues filed by this audit: #309 (`feat(chart)`), #310 (`fix(scripts)`), #311 (`refactor(scripts)`), #312 (`fix(scripts)`), #313 (`ci`); each is a native sub-issue of #246 and carries "Part of #246" and "Related: #257".
