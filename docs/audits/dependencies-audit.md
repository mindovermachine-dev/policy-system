<!-- © 2026 Cartman ApS. All rights reserved. -->

# Dependencies, Licences and Base Images Audit - Findings Report (Issue #258, umbrella #246)

Audit date 2026-10-14, tree at `main` 7dfea7a. No dependency, lockfile, chart or `Dockerfile` file was changed: each such edit alters a shipped artefact and is filed as a typed issue instead (see "Disposition rule").

## Scope examined

| Area           | Paths                                                                                                                                              |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Python         | `pyproject.toml` (workspace root, dev group), `ps-service/pyproject.toml`, `ps-cli/pyproject.toml`, `ps-test-support/pyproject.toml`, `uv.lock`    |
| Non-member code | imports in `tools/` and `scripts/` (no `pyproject.toml` of their own)                                                                             |
| npm            | `package.json`, `package-lock.json`, `.pre-commit-config.yaml` (the consumer of the npm tools), `.cspell.jsonc`                                    |
| Images/charts  | `Dockerfile`, `.dockerignore`, `charts/policy-system/Chart.yaml`, `Chart.lock`, `values.yaml`, `values-prod.yaml`, `.github/actions/prep-runner/action.yml`, workflows |

## Method and reproduction

All tools run ephemerally (`uvx` or `uv run --frozen --with <tool>`); `git status --short` was empty after every run. Never run `uv sync`, `uv lock`, `npm ci`, `npm install` or `npm audit fix` for this audit (they mutate the lock, venv or `node_modules`). `$S` is any scratch directory outside the repository.

| Step              | Command (repository root)                                                                                                                                                         |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| declared vs used  | `(cd <member> && uv run --frozen --with deptry deptry src tests --no-ansi)` for `ps-service`, `ps-cli`, `ps-test-support`                                                        |
| non-member code   | stdlib `ast` scan of `tools` and `scripts` for non-stdlib top-level imports, compared with the workspace dependencies                                                             |
| lock sync         | `uv lock --check` (115 packages resolved)                                                                                                                                         |
| runtime closure   | `uv export --frozen --no-hashes --no-dev --package ps-service --no-emit-workspace -o $S/rt.txt` (87 pins), same with `--package ps-cli` (27 pins); all groups, dev included: `--all-packages --all-groups` (112 pins; the export file is 336 lines because each pin is followed by `# via` comment lines) |
| Python advisories | `uvx pip-audit -r <export> --no-deps --disable-pip` for the three exports                                                                                                         |
| licences          | `uv run --frozen --with pip-licenses pip-licenses --from=mixed --format=csv --output-file=$S/lic.csv`, filtered by the runtime export names                                       |
| outdated          | `uv tree --frozen --outdated --depth 1`; `npm outdated`                                                                                                                           |
| npm               | `npm audit --package-lock-only --json`, `npm ls smol-toml katex`, `git ls-files node_modules`                                                                                     |
| chart             | `(cd charts/policy-system && helm dependency list && helm lint .)`                                                                                                                |
| image             | no `docker` on the audit host; the `Dockerfile` context was simulated: copy the `.dockerignore` allow-list (`.dockerignore:8-12`) into `$S/ctx` and run `uv sync --package ps-service --frozen --no-dev --no-install-workspace --dry-run` with `UV_PROJECT_ENVIRONMENT` pointing inside `$S/ctx` |
| support dates     | `https://endoflife.date/api/{postgresql,python,debian}.json` and the Docker Hub tags API, fetched 2026-10-14                                                                       |

Tool notes: plain `uvx deptry <member>` is unusable (thousands of false DEP001 because module names and local imports do not resolve outside the project environment), hence the in-environment run. `deptry` cannot scan `tools/` and `scripts/` (no project file), hence the import scan. `osv-scanner` is not installable here (not on PyPI, no binary), so `pip-audit` and `npm audit` were used instead. `trivy` was not used (no supported lock input for `uv.lock`, image scanning belongs to #252).

## Severity scale

- High: a vulnerability or licence conflict reachable in shipped code.
- Medium: a shipped artefact is not reproducible or not pinned, or a declaration contradicts the code.
- Low: hygiene with no behavioural effect.

## Disposition rule

Per AC-BI-008 each row has exactly one of: fixed in this change, `issue #N`, or `no action: <reason>`. Every version change in `uv.lock`, a `pyproject.toml`, `package.json`/`package-lock.json`, `Chart.yaml` or the `Dockerfile` changes a shipped artefact, so none was made inline; AC-BI-002 ("fixed in every `pyproject.toml`") is satisfied through issue #264 and #279 per AC-BI-008 and AC-BI-010. No fix was made, so no test was required (AC-BI-011).

Ownership: this audit owns version pins, declared-vs-used, licences and image tag facts; #252 owns scanner triage on the image and chart; #257 owns Helm values hygiene; #260 owns CI guardrails.

## Findings

| id       | location                                                                                                       | severity | evidence                                                                                                                                                                                                                                               | disposition                                                                                         |
| -------- | -------------------------------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- |
| D-S1-01  | `ps-service/pyproject.toml:10` `hiredis==3.4.2`                                                                | Low      | deptry DEP002 (declared, never imported). It is the optional parser accelerator that `redis` loads when installed, so removal is a performance decision, not dead code; `ps-service/tests/graph_gateway/test_reply_parser.py` asserts that the parser in use is the hiredis one.                                                                                | issue #264 (comment: keep via `redis[hiredis]` or drop)                                             |
| D-S1-02  | `ps-service` imports of `redis` (14 sites) and `starlette` (`auth/middleware.py:21`, `main.py:25,26`)         | Low      | deptry DEP003 (18 issues in total with D-S1-01): imported directly, installed only transitively through `falkordb` and `fastapi`.                                                                                                                         | issue #264                                                                                          |
| D-S1-03  | `ps-test-support/src/ps_test_support/mock_oidc_provider.py:47` imports `cryptography`                         | Low      | deptry DEP003, 1 issue. Reconciliation: dead-code audit F-S8-02 recorded `no action` because `cryptography` is present through the declared `pyjwt[crypto]` extra; deptry reports it because it is imported directly and not declared (`ps-test-support/pyproject.toml:6-9`). Both statements are true; this row does not rewrite the earlier document. | issue #264 (comment asks it to decide: declare explicitly or accept the transitive reliance)        |
| D-S1-04  | `ps-cli`                                                                                                       | n/a      | deptry: no dependency issues.                                                                                                                                                                                                                          | no action: clean                                                                                    |
| D-S1-05  | `tools/` and `scripts/` imports                                                                                | n/a      | Third-party imports are `falkordb` (declared by `ps-service`), `ps_service`/`ps_cli` (workspace), `httpx` (root dev group `pyproject.toml:7`, also a `ps-cli` dependency) and `yaml` (root dev `pyyaml`, `pyproject.toml:12`). All are provided by the workspace environment the scripts run in. | no action: every import is declared or provided                                                     |
| D-S2-01  | root dev group `pyproject.toml:5-14`                                                                           | n/a      | Each entry has a consumer: `httpx` and `pytest-cov`/`pytest-xdist` by tests and `addopts` (`pyproject.toml:49`), `pyyaml` by `scripts/check_mock_boundaries.py`, `ruff`/`basedpyright`/`pre-commit` by the hooks. `ps-service` and `ps-cli` dev groups contain only `ps-test-support`.                                                                  | no action: classification correct (AC-BI-003)                                                       |
| D-S2-02  | runtime image contents                                                                                         | n/a      | The runtime export (`--no-dev`, 87 pins for `ps-service`, 27 for `ps-cli`) contains none of `pytest`, `ruff`, `basedpyright`, `pre-commit`, `coverage`, `pytest-cov`, `pytest-xdist`, `ps-test-support` (grep count 0). `Dockerfile:31,37` use `--no-dev --frozen`; the runtime stage copies the virtual environment only.                                | no action: image holds runtime dependencies only                                                    |
| D-S2-03  | `ps-test-support/pyproject.toml:8` `pytest>=8.0.0`                                                             | n/a      | A runtime dependency of the test-support wheel: the package ships a pytest plugin importing `_pytest` (`serialize_live_markers.py:23`). The package is dev-only and never in the image.                                                                    | no action: justified                                                                                |
| D-S2-04  | `ps-cli` build context vs workspace members                                                                    | n/a      | `Dockerfile` copies `ps-cli/pyproject.toml` but not the `ps-test-support` member, yet the context simulation resolves. Frozen no-dev sync does not need the missing member directory.                                                                  | no action: false positive (see below)                                                               |
| D-S3-01  | `uv.lock`                                                                                                      | n/a      | `uv lock --check` passes (115 packages resolved); `grep -E '^name = ' uv.lock \| sort \| uniq -d` empty, so no package is locked in two versions.                                                                                                        | no action: lock in sync, no duplicates                                                              |
| D-S3-02  | `ps-service/pyproject.toml:18` `structlog>=26.1.0`                                                             | Low      | The only runtime dependency declared as a range; the other 13 are exact pins. Reproducible through `uv.lock`, but inconsistent with the pin policy.                                                                                                      | issue #279                                                                                          |
| D-S3-03  | dev ranges: root `pyproject.toml:7,9-12` (`httpx`, `pytest`, `pytest-cov`, `pytest-xdist`, `pyyaml`), `ps-test-support/pyproject.toml:8` | Low | Lower-bound ranges on dev/test packages, resolved to concrete versions by `uv.lock`; nothing ships.                                                                                                                                                     | no action: dev ranges are lock-pinned                                                               |
| D-S3-04  | `requires-python`                                                                                              | n/a      | `>=3.14` in all three members (`pyproject.toml` of each, `:5`), `target-version = "py314"` (`pyproject.toml:70`) and `python:3.14-slim-trixie` in the `Dockerfile` agree.                                                                              | no action: consistent                                                                               |
| D-S4-01  | runtime packages `psycopg` 3.3.6, `psycopg-binary` 3.3.6                                                       | Low      | LGPL-3.0-only; `psycopg-binary` ships `LICENSE.txt` in its `dist-info`. Used unmodified as a dynamically imported library and redistributed unmodified in the image, which the licence permits; the licence text is carried with the wheel.             | no action: LGPL library used unmodified                                                             |
| D-S4-02  | runtime packages `certifi` 2026.7.22 (MPL-2.0), `tqdm` 4.70.0 (MPL-2.0 AND MIT)                              | Low      | File-level copyleft; both unmodified.                                                                                                                                                                                                                  | no action: MPL-2.0 unmodified                                                                       |
| D-S4-03  | workspace members `ps-service`, `ps-cli`, `ps-test-support`; repository root                                  | Medium   | Licence UNKNOWN in the listing: no `license` field in any `pyproject.toml`, no `LICENSE`/`COPYING` file at the root, while `README.md` calls the project open source and source headers say "All rights reserved". Needs an owner decision.             | issue #275                                                                                          |
| D-S4-04  | licence coverage gap                                                                                           | Low      | 4 runtime-export names are not installed on this host because their environment markers exclude it (`colorama`, `pywin32`, `tzdata`: `sys_platform == 'win32'`; `httpx2-jsfetch`: `sys_platform == 'emscripten'`), so their licences were not read from the environment. None applies to the Linux image.          | no action: Windows/emscripten-only markers; a licence allowlist check belongs to #260 |
| D-S5-01  | Python advisories                                                                                              | n/a      | `pip-audit`: no known vulnerabilities for all groups (112 pins), the `ps-service` runtime export (87) and the `ps-cli` runtime export (27).                                                                                                              | no action: clean                                                                                    |
| D-S5-02  | npm advisories (`package-lock.json`)                                                                           | Low      | 5 high-severity advisories, 0 critical, in transitive dev-tooling packages of the markdown linter. Not shipped in any image or chart; not reachable through untrusted input (the pre-commit hook runs with `--no-globs`, `.pre-commit-config.yaml:24`, on repository-owned files). The only fix `npm audit` offers is a major downgrade of the linter, which is not a remedy. A fix needs a lockfile change. | resolved by #276 (not exploitable here)                                                                   |
| D-S5-03  | scanner coverage                                                                                               | n/a      | `osv-scanner` unavailable; image and chart scanning is #252's scope; no real exploitable High found in shipped code, so no private advisory is needed.                                                                                                 | no action: documented limitation                                                                    |
| D-S6-01  | `package.json` devDependencies `cspell`, `@cspell/dict-da-dk`, `markdownlint-cli2`, `prettier`                 | n/a      | All four are used: `.pre-commit-config.yaml:12-15` states the Node tools are resolved from `node_modules` with `npx --no-install`; hooks at `:22-24` (markdownlint), `:28-30` (prettier), `:33-35` (cspell); the Danish dictionary is imported at `.cspell.jsonc:4`. CI sets up Node 24 with an npm cache keyed on `package-lock.json` (`.github/actions/prep-runner/action.yml:23-39`); contributors run `npm ci` once (`.pre-commit-config.yaml:12-15`). | no action: justified by scripts and hooks (AC-BI-006)                                               |
| D-S6-02  | `package.json:10-13` overrides `smol-toml` 1.9.0, `katex` 0.18.2                                               | Low      | `package-lock.json` carries `node_modules/smol-toml` 1.9.0 (`:2396`) and `node_modules/katex` 0.18.2 (`:1502`). The local `node_modules` holds older versions (`npm ls` reports them invalid), which is an untracked local-environment state.     | no action: lock correct; `node_modules` untracked (`git ls-files node_modules` = 0), `npm ci` restores it from the lock |
| D-S6-03  | `cspell` 10.2.0 -> 10.4.0, `@cspell/dict-da-dk` 4.1.2 -> 4.1.3, `prettier` 3.9.6 -> 3.9.10                    | Low      | `npm outdated`; changes `package-lock.json` and may change formatting.                                                                                                                                                                                  | resolved by #276                                                                                       |
| D-S7-01  | `charts/policy-system/values.yaml:10` `falkordb/falkordb` tag `latest`                                         | Medium   | Unpinned server image while the client is pinned (`falkordb==1.7.1`); the live read-only guard test documents behaviour observed on `latest` (`test_ro_query_rejects_write_live.py:3`). Same literal in `ps-service/tests/test_container_image.py:287,622`, `charts/policy-system/tests/falkordb-deployment_test.yaml:9,13`, `tools/graph-ingestion/load_graph.py:28`, `tools/graph-ingestion/load_all.sh:4`, `.devcontainer/docker-compose.yml:37`, `CONTRIBUTING.md:64`. A versioned tag exists (`v4.22.0`, 2026-09-30). #257 and #252 were read first: neither lists image tags. | issue #277 (`fix(chart)`)                                                                           |
| D-S7-02  | `Dockerfile:12,40` `python:3.14-slim-trixie`                                                                   | Medium   | Floating tag, no digest, both stages; moved last on 2026-10-09. Supported: Python 3.14 security support to 2030-10-31, Debian 13 to 2030-06-30 (endoflife.date, fetched 2026-10-14). No test pins the base image reference.                           | issue #278 (`fix(docker)`)                                                                          |
| D-S7-03  | `Dockerfile:16` `ghcr.io/astral-sh/uv:0.12.4`                                                                  | Low      | Exact tag, matches the CI pin (`.github/actions/prep-runner/action.yml:50`); newer upstream release exists.                                                                                                                                            | no action: exact tag; refresh belongs to dependency-update automation (#260)                        |
| D-S7-04  | `values.yaml:171-173,249-251` `postgres:16-alpine`                                                             | Low      | Major version pinned; PostgreSQL 16 upstream support to 2028-11-09 (endoflife.date, fetched 2026-10-14); tag rebuilt 2026-09-21. Also in `ps-service/tests/test_container_image.py:304`, `.devcontainer/docker-compose.yml:66`.                         | no action: major-pinned, patch updates wanted                                                       |
| D-S7-05  | `charts/policy-system/Chart.yaml:9` authentik `2026.8.3`, `Chart.lock`                                         | n/a      | Exact version plus lock digest; `helm dependency list` status `ok`; newest in the upstream index at audit time; `helm lint .` passes (only "icon is recommended"). Packaged `charts/*/charts/*.tgz` is gitignored (`.gitignore:13`), so `Chart.lock` is the tracked pin. | no action: pinned and current                                                                       |
| D-S7-06  | `ps-service` image tag `values.yaml:23-25` (`tag: ""`)                                                         | n/a      | Defaults to the chart `appVersion`, so released images are version-pinned by the release process.                                                                                                                                                      | no action: pinned by chart version                                                                  |
| D-S7-07  | GitHub Actions refs (`actions/checkout@v6`, `docker/*@v4/v7`, `azure/setup-helm@v4`, `devx-cafe/takt-actions/...@experimental`, `@v1`) | Low | Major-version or branch refs, not commit SHAs. Supply-chain hardening is a security/CI decision.                                                                                                                                                       | issue #257 (comment; its AC-BI-007 owns action pinning, #252 keeps the hardening view)              |
| D-S8-01  | direct runtime dependencies behind latest within their major: `falkordb`, `fastapi`, `litellm`, `mcp`, `pydantic`, `uvicorn`, `pyjwt`, `truststore` | Low | `uv tree --frozen --outdated --depth 1`; `pip-audit` is clean, so these are maintenance, not security.                                                                                                                                                  | issue #279                                                                                          |
| D-S8-02  | `openai` 2.54.0 (`ps-service/pyproject.toml:14`) vs 3.x                                                        | Low      | Major version behind; `litellm` also depends on `openai`, so the move is constrained.                                                                                                                                                                  | issue #280                                                                                          |
| D-S8-03  | dev tooling `ruff` 0.16.4, `basedpyright` 1.39.10, `pre-commit` 4.6.2 behind latest                            | Low      | Strict rule sets can add findings on bump.                                                                                                                                                                                                             | issue #281                                                                                          |
| D-S8-04  | update automation and CI audit                                                                                 | Medium   | No Dependabot/Renovate config; `pip-audit` runs only as a local pre-commit hook on lock change (`.pre-commit-config.yaml:62-67`); no workflow in this repository runs `pip-audit`, `npm audit`, a licence check or `deptry`.                          | issue #260 (comment; its method already lists dependency hygiene)                                   |
| D-S9-01  | provenance in dependency-related files: `pyproject.toml` (about 20 comment lines citing AC, decision or issue IDs, e.g. `:18`, `:35`, `:69`, `:70`, `:99`, `:155`), `Dockerfile:5,34,53`, `.github/actions/prep-runner/action.yml:50,51` | Low | Comments cite acceptance-criterion and decision IDs. Provenance is #251's dimension, and editing the `Dockerfile` changes the shipped image.                                                                                                              | issue #251 (comment)                                                                                |

## False positives and reasons

- Plain `uvx deptry <member>`: thousands of DEP001 because module names and workspace-local imports do not resolve outside the project environment; superseded by the in-environment run above.
- `pyjwt` DEP002 in a plain run: the module is imported as `jwt` (`ps-service/src/ps_service/auth/verifier.py`); with the project environment it is not reported.
- `ps-cli` build context lacking the `ps-test-support` member: not an error, the frozen no-dev sync resolves (simulation above).
- `npm outdated`/`npm ls` "invalid" for `smol-toml` and `katex`: stale untracked `node_modules`, lock is correct.
- `pip-licenses` full-text MIT in `tiktoken`: the licence field holds the MIT text, not a copyleft licence.
- `npm audit` suggested fix: a major downgrade of the linter, not a real remedy.

## Superseded and removed

No dependency, test, config or doc was removed or made obsolete (no inline change, AC-BI-012). Removal proof not applicable: `git diff --stat` shows only this document.

## Superseded: removal proof

Nothing was removed or made obsolete, so there are no removed symbols to search for (AC-BI-012). Proof that the tree holds no other change: `git diff --stat` is empty and `git status --short` lists only this document (`?? docs/audits/dependencies-audit.md`). Recheck for every finding that proposes a removal: `hiredis` (D-S1-01) is only a candidate inside #264 and the same grep (`--include='*.py' --include='*.toml'`, excluding `docs/audits`, `.orchestrator`, `node_modules`, `.venv`, `spikes`) still matches `ps-service/pyproject.toml:10` and `ps-service/tests/graph_gateway/test_reply_parser.py`, which is expected because nothing was dropped.

## Guardrail status of the rules found

| rule | enforced automatically today | review-only |
| ---- | ---------------------------- | ----------- |
| runtime image holds no dev tools | yes, structurally (`Dockerfile:31,37` `--no-dev --frozen`) | |
| lock in sync with manifests | yes, frozen installs fail on drift | |
| Python advisories | partly: `pip-audit` pre-commit hook on `pyproject.toml`/`uv.lock` change (`.pre-commit-config.yaml:62-67`), no scheduled CI job | scheduled scan (#329) |
| declared vs used (`deptry`) | no | review-only until #329 |
| licence allowlist | no | review-only until #329 |
| npm advisories | no | review-only until #329 |
| base image and tag pinning | no | review-only (#277, #278) |

## Generic-wording gate (AC-BI-013)

On the final run, a grep of this document for advisory identifiers, the words exploit, payload, password, secret and key, digests, and the names of the affected npm packages matched only the generic sentences of D-S5-02 and D-S5-03 (the words "exploitable"/"exploitable High"); no advisory identifiers, payload text or fix detail are listed, and the affected package names appear in no row. The same grep over the bodies and comments of issues #275 to #281 matches only the sentence in #276 saying the advisories are "not exploitable here". No real exploitable High finding exists in shipped code, so no private security advisory was raised.

## Sibling-issue review

Open sub-issues of the umbrella (#251 to #257, #259, #260) and every issue filed by earlier audits (#263 to #274) were read with `gh issue view`; issues #275 to #281 were filed by this audit and are linked as sub-issues of #246.

| issue | outcome                                                                                                                                    |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| #251  | comment: AC/decision-ID comments in `pyproject.toml`, `Dockerfile`, `prep-runner/action.yml` are in its dimension (D-S9-01)                |
| #252  | comment: dependency scan counts (no duplicate scan), image/chart scanning stays here; later correction: action pinning is owned by #257 (D-S7-07) |
| #253  | no change needed                                                                                                                           |
| #254  | no change needed (versions are not named in any scope item)                                                                                |
| #255  | no change needed                                                                                                                           |
| #256  | no change needed                                                                                                                           |
| #257  | comment: `values.yaml:10` `latest` tag is filed as #277; `postgres:16-alpine` no action (D-S7-01, D-S7-04); second comment: its AC-BI-007 owns action pinning (D-S7-07) |
| #259  | no change needed                                                                                                                           |
| #260  | two comments: no update automation or CI dependency checks exist, candidate checks, digest refresh need (D-S8-04); order note that #278, #279, #281 assume automation but ship independently, `deptry` check after #264 |
| #263  | no change needed                                                                                                                           |
| #264  | comment: `cryptography` reconciliation with dead-code audit F-S8-02 and `hiredis` wording; coordinate with #279 on the same lock (D-S1-03)  |
| #265  | no change needed                                                                                                                           |
| #266  | no change needed                                                                                                                           |
| #267  | no change needed                                                                                                                           |
| #268  | no change needed                                                                                                                           |
| #269  | no change needed                                                                                                                           |
| #270  | no change needed (api-layer moves, no dependency impact)                                                                                    |
| #271  | no change needed                                                                                                                           |
| #272  | no change needed                                                                                                                           |
| #273  | no change needed                                                                                                                           |
| #274  | no change needed (edge_identity copies, no dependency impact)                                                                               |

## Issues filed by this audit

| issue | title                                                                                         |
| ----- | --------------------------------------------------------------------------------------------- |
| #275  | `docs(licence)`: declare the project licence                                                  |
| #276  | `chore(deps)`: clear markdown-tooling npm advisories and refresh npm dev dependencies         |
| #277  | `fix(chart)`: pin the FalkorDB image tag instead of latest                                    |
| #278  | `fix(docker)`: pin the Python base image by digest                                            |
| #279  | `fix(deps)`: bump ps-service runtime dependencies within their current major and pin structlog |
| #280  | `fix(deps)`: decide when to move openai to the next major version                              |
| #281  | `chore(deps)`: bump Python dev tooling (ruff, basedpyright, pre-commit)                        |

## Acceptance-criteria map

AC-BI-001: scope, method and findings above. AC-BI-002: D-S1-01 to D-S1-05 (#264). AC-BI-003: D-S2-01 to D-S2-04. AC-BI-004: D-S4-01 to D-S4-04. AC-BI-005: D-S5-01 to D-S5-03, D-S8-01. AC-BI-006: D-S6-01 to D-S6-03. AC-BI-007: D-S7-01 to D-S7-07. AC-BI-008 to AC-BI-010: disposition column and issues table. AC-BI-011/012: no fix, no removal. AC-BI-013: generic-wording gate. AC-BI-014: sibling-issue review.

## Enforcement

Enforced rules fail automatically, in the pre-commit hook on the files they watch and in the CI `trunk-worthy` wave over the whole tree. Review-only rules rely on review. Check ids are the hook and CI ids listed in [CONTRIBUTING.md](../../CONTRIBUTING.md#automated-guardrails).

| Rule | Status | Check id or reason |
| --- | --- | --- |
| Runtime image holds no development tools | enforced | structurally, by the frozen `--no-dev` install in the `Dockerfile` |
| Lock file in sync with the manifests | enforced | frozen installs fail on drift |
| Known advisories in locked Python dependencies | enforced | `pip-audit` in the commit hook when `pyproject.toml` or `uv.lock` changes and in every CI run; a scheduled scan between pushes is tracked in #329 |
| Declared dependencies match imports | review-only | tracked in #264 and #329 |
| Licence allowlist | review-only | tracked in #275 and #329 |
| Advisories in npm development dependencies | review-only | tracked in #276 and #329 |
| Base image and image tag pinning | review-only | tracked in #277 and #278 |
| Dependency update proposals | review-only | needs a decision on update automation; tracked in #329 |
