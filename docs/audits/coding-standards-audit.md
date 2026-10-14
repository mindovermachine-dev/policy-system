<!-- © 2026 Cartman ApS. All rights reserved. -->

# Code Conformance to Coding Standards Audit - Findings Report (Issue #251, umbrella #246)

## Scope examined

Python in `ps-service/{src,tests}`, `ps-cli/{src,tests}`, `ps-test-support/{src,tests}`, `tools/*`, `scripts/*`, `docs/regulations/*.py` and `conftest.py` files: 1172 tracked `*.py` files. Standards: `docs/coding-standards/level1-coding-principles.md` and `level2-python-instructions.md`. Out of scope: docstring completeness (#240-#245), shell scripts (#257), dependencies (#258), logging content (#253).

## Method and reproduction

Tooling: ruff 0.16.4, basedpyright 1.39.10 (pyright 1.1.412). Machine-checkable rules are run through the existing gates; the remainder is probed with `ast`/`tokenize` scripts kept outside the repository. No dependency, lock or lint-configuration change was made.

```text
uv run ruff check --no-cache .                       # clean
uv run ruff format --check .                         # clean
uv run basedpyright                                  # 0 errors
uv run python scripts/check_mock_boundaries.py ps-service/tests ps-cli/tests   # exit 0
uv run python scripts/check_docstring_provenance.py --tracked                  # exit 0
uv run ruff check --no-cache --isolated --target-version py314 --line-length 100 --statistics --select <RULES> $(git ls-files '<globs>')
uv run ruff check --no-cache --ignore-noqa --statistics .                      # hidden-by-noqa counts
git grep -n "type: ignore" -- '*.py'                  # 20 comment sites plus 2 non-comment text hits
```

Probes (tokenize for comments, ast for calls, raises and handlers over `git ls-files '*.py'`): suppression inventory by kind and tree; `subprocess.run/check_output/check_call` without `timeout=`; generic raises in source; silent handlers (body only `pass`, `continue` or bare `return`); `os.environ`/`getenv` reads outside `config.py`; `cast(` density; function-name prefixes and variable names. Dotted-path resolution of every `approved-mock-boundaries.yaml` entry by import.

A bare `ruff --select RUF100` run reports hundreds of unused `noqa` comments because the other rules are not selected; the real check is the default configuration (RUF100 is inside `ALL`) and is clean. That number is not a finding.

## Severity scale

- High: reachable defect or contract risk.
- Medium: standard contradicted in a way that misleads or costs maintainers.
- Low: cosmetic or documentation drift.

## Checklist (stable ids)

Class: E = enforced by a gate that was run (zero violations or recorded exemption), S = sample-reviewable by probe or reading, N = not checkable.

| id          | rule (source)                                              | class | gate or probe                                           | count                  | disposition                                    |
| ----------- | ---------------------------------------------------------- | ----- | ------------------------------------------------------- | ---------------------- | ---------------------------------------------- |
| L1-01       | single responsibility (L1 :9-13)                           | S     | C901 and PLR09xx as proxy; module size sample           | 0 unsuppressed         | no action: proxies green; splits owned by #273 |
| L1-02       | clarity, descriptive names (L1 :15-19)                     | N     | -                                                       | -                      | no action: not checkable                       |
| L1-03       | naming semantics (L1 :25-42)                               | S     | prefix and variable-name probes                         | see CS-08              | issue #302                                     |
| L1-04       | fail fast at boundaries (L1 :46-53)                        | S     | entry-point sample                                      | 0                      | no action: owned by #252, #255                 |
| L1-05       | DRY (L1 :55)                                               | N     | -                                                       | -                      | issue #302 (ambiguity), #263, #274 own dups    |
| L1-06       | dependency inversion (L1 :60)                              | S     | `FalkorDB(` construction probe                          | 5 tool entry points    | no action: composition roots                   |
| L1-07       | composition over inheritance (L1 :66)                      | S     | read of sampled classes                                 | 0                      | no action                                      |
| L1-08       | test quality and mocks (L1 :71-76)                         | E/S   | `check_mock_boundaries.py`                              | 0                      | no action: gate green                          |
| L1-09       | immutability (L1 :78)                                      | S     | `global` noqa review                                    | 3 PLW0603              | no action: documented process singletons       |
| L1-10       | error handling (L1 :83-87)                                 | S/E   | BLE001, S110, silent-handler probe                      | 3 handlers             | no action (CS-05)                              |
| L1-11       | security (L1 :89)                                          | E     | owned by #252, #253                                     | -                      | no action: cross-reference                     |
| L1-12       | complexity at most 8 (L1 :95)                              | E     | C901 with `--ignore-noqa`                               | 0                      | no action                                      |
| L1-13       | documentation and provenance (L1 :101-102)                 | E/S   | ruff D rules, provenance checker (py), TOML gap         | 0 py; TOML unguarded   | issue #299                                     |
| L2-01/02    | workspace layout, member decoupling (L2 :18-20)            | S     | directory listing                                       | 0                      | no action: owned by #250                       |
| L2-03       | PEP 8 naming (L2 :24)                                      | E     | ruff N rules                                            | 0                      | no action                                      |
| L2-04       | constructor injection (L2 :28-29)                          | S     | `FalkorDB(` probe                                       | 5 tools                | no action: composition roots                   |
| L2-05       | no silent swallow (L2 :33)                                 | S     | handler probe                                           | 3                      | no action (CS-05)                              |
| L2-06       | no `shell=True`, list args, timeouts (L2 :37-38)           | E/S   | S602, S604, S603, timeout probe                         | 0 shell; 5 no timeout  | fixed (CS-03)                                  |
| L2-07       | tests mirror source (L2 :42)                               | S     | directory diff                                          | 5 extra test dirs      | no action (CS-07)                              |
| L2-08       | Detroit and mock boundaries (L2 :43)                       | E     | `check_mock_boundaries.py`, yaml resolution probe       | 0; 1 dead entry        | fixed (CS-06)                                  |
| L2-09       | test names (L2 :44)                                        | S     | AST probe for generic names                             | 0                      | no action                                      |
| L2-10       | secrets, secret logging (L2 :48-49)                        | E     | owned by #252, #253                                     | -                      | no action: cross-reference                     |
| L2-11       | build hygiene (L2 :51-56)                                  | S     | owned by #258                                           | -                      | no action: cross-reference                     |
| L2-12       | ruff contract (L2 :60-65)                                  | S     | config read, isolated rule runs                         | see CS-01, CS-02       | issues #300, #299                              |
| L2-14/15    | docstrings, test and tool exemptions (L2 :64)              | E/S   | ruff D rules; per-file-ignores                          | zero-hit entries       | issue #300                                     |
| L2-16       | typing, no `type: ignore` (L2 :105-116)                    | E/S   | basedpyright strict, token probe                        | 20 sites               | issue #298                                     |
| L2-20/21    | component and package names (L2 :124-128)                  | S     | directory listing vs architecture doc                   | 0 drift found          | no action: doc owned by #254                   |
| L2-22/23    | Pydantic at boundaries, `Field` constraints (L2 :132-135)  | S     | `extra="forbid"` count in `api/models.py`               | 8 of 26 models         | no action (CS-09)                              |
| L2-24/25    | Cypher parameterisation (L2 :139-142)                      | S     | f-string `.query(` grep                                 | 0                      | no action: S608 sites reviewed by #252         |
| L2-26..28   | API patterns, lifespan, health (L2 :146-160)               | S     | grep `on_event`, `HTTPException`                        | 0 `on_event`; 1 router | no action (CS-09)                              |
| L2-29       | logging conventions (L2 :166-172)                          | E     | owned by #253                                           | -                      | no action: cross-reference                     |
| L2-30/31    | MCP tools, untrusted LLM content (L2 :174-182)             | S     | owned by #255, #252                                     | -                      | no action: cross-reference                     |
| L2-32       | domain-specific exceptions (L2 :187)                       | S     | generic-raise probe                                     | 38 raises              | issue #302                                     |
| L2-33       | env var naming and single loader (L2 :192-193)             | S     | environment-read probe                                  | 4 modules              | issue #301                                     |
| L2-40       | thin `__main__` in ps-cli (L2 :203)                        | E     | AST test in `ps-cli/tests/test_cli.py`                  | 0                      | no action: test green                          |
| L2-41..47   | ps-cli patterns (L2 :204-241)                              | S     | dispatch, exit code, silence, config reads              | 0                      | no action (CS-10)                              |
| L2-48       | `integration` marker (L2 :245)                             | S     | marker registered; subprocess users listed              | marker present         | no action                                      |

## Suppression inventory

Comment tokens over `git ls-files '*.py'` (tokenize probe):

| kind                          | total | src | tests | tools and scripts | other |
| ----------------------------- | ----- | --- | ----- | ----------------- | ----- |
| `pyright: ignore`             | 851   | 56  | 790   | 3                 | 2     |
| `noqa`                        | 338   | 158 | 169   | 11                | 0     |
| `type: ignore` (comments)     | 20    | 0   | 20    | 0                 | 0     |
| `pragma: no cover`            | 19    | 10  | 6     | 3                 | 0     |
| `fmt:`                        | 1     | -   | -     | -                 | -     |

Largest `noqa` families in source: PLC0415 46 (deferred imports), BLE001 31, PLR0913 23, TC001 15, S608 9, E402 8. In tests: S603 49, E501 42, F401 25. Justification review: PLC0415 sites defer heavy or optional imports; BLE001 sites are dispatch boundaries; S603 sites use fixed argument lists; PLR0913 sites are the known long constructors. Reasons are missing on 46 `noqa` and 374 `pyright: ignore` comments (mostly `reportPrivateUsage` in tests); see issue #300.

## Findings

| id    | location                                                                 | severity | evidence                                                                                                                                                                                           | disposition                                                                            |
| ----- | ------------------------------------------------------------------------ | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| CS-01 | `pyproject.toml` global `ignore` (12 entries) and per-file-ignores       | Low      | Isolated runs: zero hits for ISC001, TD002, TD003, FIX002, EXE002 globally; zero hits for tests ANN001, ANN201, D105, A006, tools D100-D104 and ANN*, docs scripts ANN*, D100; quoted counts are stale | issue #300 (no configuration change here)                                              |
| CS-02 | `pyproject.toml` comments                                                | Low      | About 25 comment lines carry acceptance-criteria or decision identifiers; checker covers `*.py` only; rule text does not name TOML                                                                  | issue #299                                                                             |
| CS-03 | 5 test calls to `subprocess.run` without `timeout=` (credentials, config key, fake curl x2, model validation) | Low | AST probe: 5 of 52 calls; the 4 `Popen` sites need timed waits, a behaviour change                                                              | fixed in S3 (`timeout=60` added); Popen: no action, a timed wait could add failures    |
| CS-04 | `ps-service/tests` (20 sites)                                            | Medium   | `type: ignore` is prohibited by L2 :115; 5 doubled suppressions were removed (basedpyright 0 errors per file before and after); 20 need typed helpers                                              | fixed (5 doubled sites); issue #298 (20 sites)                                         |
| CS-05 | `fetch.py:180`, `ingestion_runs/dispatch.py:89`, `near_miss_review_orchestration.py:181` | Low | Silent-handler probe lists 3 sites; the first is a reachability probe (any HTTP response means connected), the others are diagnostics-only log handlers                                | fixed (reason comment added at the third); no action otherwise: deliberate             |
| CS-06 | `docs/coding-standards/approved-mock-boundaries.yaml`                    | Low      | 53 entries resolved by import; `ps_service.export.falkordb_connection.raw_connection` no longer exists (symbol moved); 3 tool constants resolve only by path import                                | fixed (dead entry removed); 3 tool entries: no action, false positive of import probe  |
| CS-07 | `ps-service/tests/{deploy_eval,deploy_lib,deploy_llm,deploy_ps,fixtures,release,scripts,tools}` | Low | Test directories without a matching `ps_service` package; they test deploy scripts, release tooling and shared fixtures                                                         | no action: test targets are outside the package                                        |
| CS-08 | production function and variable names                                   | Low      | Prefix counts: read 105, get 30, fetch 15, load 12; `result` assigned 104 times; vocabulary partly follows meaning                                                                                 | issue #302 (settle rule before renaming)                                               |
| CS-09 | `api/models.py`, `auth/protected_resource.py`                            | Low      | `extra="forbid"` on 8 of 26 models (rule applies to request models; split not verified per model); one `HTTPException` in the protected-resource route                                              | no action recorded here: owned by #255 (API contract)                                  |
| CS-10 | ps-cli `except Exception` (4 sites), tools env defaults                  | Low      | One annotated catch-all boundary in `cli.py`; three in `credentials.py` pass BLE001; 7 `tools/` scripts repeat `PS_FALKORDB_HOST`/`PORT` defaults by design (standalone)                           | no action: documented boundary and standalone scripts (see issue #302 item 5)          |
| CS-11 | 4 `ps-service` modules reading `PS_FALKORDB_GRAPH`                       | Medium   | Direct environment reads in `change_monitor`, `company_merge`, `policy_lifecycle` and `mcp_interface` against a single configuration loader                                                      | issue #301                                                                             |
| CS-12 | 38 generic `ValueError`/`TypeError` raises in source                     | Low      | Mostly Pydantic validators and type guards in `graph_gateway`; L2 :187 asks for domain exceptions                                                                                                  | issue #302 (rule ambiguity)                                                            |

## Error handling and configuration access comparison

| package        | error pattern                                                     | configuration pattern                                         | outliers                              |
| -------------- | ----------------------------------------------------------------- | ------------------------------------------------------------- | ------------------------------------- |
| ps-service     | domain exceptions mapped by the API error handlers; MCP returns error values | `ServiceConfig` loader; documented bootstrap reads  | CS-11, CS-12                          |
| ps-cli         | `PsCliError`, one catch-all dispatch boundary, three-way exit codes | default file plus override, one environment override        | none                                  |
| tools, scripts | argparse with environment defaults, exit codes                    | `PS_FALKORDB_*` defaults per script                           | repeated blocks, by design            |

These differ by design in the level 2 text; only the outliers above are findings.

## Superseded and removed

Search recipe: `git grep -n <symbol> -- . ':!docs/audits' ':!.orchestrator'` (the findings documents and the local tracker are the only carve-outs).

| removed or replaced | search | result |
| --- | --- | --- |
| 5 redundant `# type: ignore[method-assign]` and `[attr-defined]` tokens that doubled a `pyright: ignore` | `git grep -nE "type: ignore\[(method-assign\|attr-defined)\]  # pyright" -- . ':!docs/audits' ':!.orchestrator'` | no matches; the remaining 20 are tracked in #298 |
| stale `max-args` comment text in `pyproject.toml` (decision id, residual count, refactor reference) | `git grep -nE "D-6: match max-complexity\|refactored in B12" -- . ':!docs/audits' ':!.orchestrator'` | no matches |
| dead mock-boundary entry `ps_service.export.falkordb_connection.raw_connection` (the symbol now lives in `graph_gateway.key_port`; nothing patches the old path) | `git grep -n "export.falkordb_connection.raw_connection" -- . ':!docs/audits' ':!.orchestrator'` | one match: an inline sample YAML string in `ps-service/tests/scripts/test_check_mock_boundaries.py:41`, a self-contained fixture of the checker's tests, not the repository file; `scripts/check_mock_boundaries.py ps-service/tests ps-cli/tests` exits 0 |

The function `raw_connection` itself is live (`graph_gateway/key_port.py`) and is not removed.

## False positives

- `ruff --select RUF100` alone (see Method).
- `FalkorDB(` hits inside docstrings and `Protocol` stubs in `tools/`.
- Pydantic validator `ValueError` raises counted as generic raises.
- The yaml probe flags three tool constants that resolve only by file path import.
- `type: ignore` text inside `test_base.py` docstring and `test_check_docstring_provenance.py` string: not suppressions.
- `Popen` sites counted as calls without `timeout=`: `Popen` has no such argument; the timed wait is a behaviour change and is left.
- Two of the three silent-handler candidates (a reachability probe and a diagnostics-only log handler) are deliberate, not swallowed errors.

## Sibling issue review

Method: the open sub-issues of #246 (`gh api repos/:owner/:repo/issues/246/sub_issues`, 254 to 260 open, 263 to 302 filed by audits) were enumerated and every body, and for 254, 255, 257, 259, 260 every comment, was read. A comment was posted where scope, ordering or dependencies changed; otherwise the reason for no change is recorded.

| issue | what it covers | decision |
| --- | --- | --- |
| #254 | documentation audit | commented: the standards text is owned by #302 and #299; #251 changed only pyproject comments and one mock-boundary entry |
| #255 | API contract | commented: request-model `extra="forbid"` split (8 of 26) and the one framework `HTTPException` are handed over |
| #257 | configuration hygiene | commented: graph-name environment reads tracked in #301 |
| #259 | observability and resilience | no change needed: no timeout, retry or probe finding in this audit's dimension |
| #260 | guardrails | commented: `type: ignore` ban check, config-comment scope, lint drift, subprocess timeout probe (#298, #299, #300) |
| #263 | symbols reachable only from tests | no change needed: dead-code ownership, no overlap with conformance rules |
| #264, #279, #280 | dependency declarations and bumps | no change needed: dependency edits stay out of the audit; comments in dependency arrays are excluded from #299 |
| #265, #266, #267 | spikes, blame-ignore file, duplicated skill | no change needed: repository hygiene |
| #268, #269 | identical-body tests, mutation gaps | no change needed: different test files from #298 |
| #270, #271 | api and package import cycles | no change needed: boundaries, not coding rules |
| #272 | split of `mcp_server.py` | commented: no enforced module-size rule; sequencing with #301 |
| #273 | split of three large modules | covered by the #301 sequencing comment |
| #274 | per-component helper copies | commented: shares graph-name call sites with #301; decide in order |
| #275 | project licence | no change needed |
| #276, #277, #278 | npm advisories, image pins | no change needed |
| #281 | dev tooling bump | commented: ruff bump changes the rule set that #300 measures |
| #282 to #287 | persistence tests, scripts, indexes, docs | no change needed: no overlap |
| #288 to #292 | security hardening | no change needed: shipped behaviour, owned by the security audit |
| #293 to #297 | log hygiene and audit rows | no change needed: log content is out of scope here |
| #298, #299, #300, #301, #302 | this audit's issues | cross-checked against each other: #300 and #298 depend on the standards decisions in #302 (commented on #300 and #302); #301 sequenced with #274, #272, #273 |

## Issues filed

- #298 `refactor(tests)`: remaining `type: ignore` sites.
- #299 `docs(coding-standards)`: provenance rule scope for config comments.
- #300 `chore(lint)`: prune stale ignores, require suppression reasons.
- #301 `refactor(ps-service)`: resolve the graph name once through configuration.
- #302 `docs(coding-standards)`: settle ambiguous naming, exception and suppression rules.

## Enforcement

Enforced rules fail automatically, in the pre-commit hook on the files they watch and in the CI `trunk-worthy` wave over the whole tree. Review-only rules rely on review. Check ids are the hook and CI ids listed in [CONTRIBUTING.md](../../CONTRIBUTING.md#automated-guardrails).

| Rule | Status | Check id or reason |
| --- | --- | --- |
| Lint and formatting rules of the coding standards | enforced | `ruff-check`, `ruff-format` |
| Strict typing | enforced | `basedpyright` |
| No provenance text in Python docstrings and comments | enforced | `docstring-provenance` |
| Unused-symbol suppressions carry a reason and there is no global name ignore list | enforced | `pytest` (hook configuration test) |
| Other suppression comments carry a reason; stale ignores are pruned | review-only | tracked in #300 |
| Type-ignore comments | review-only | tracked in #298; a ban after it in #327 |
| Provenance text in YAML and TOML comments | review-only | scope decision in #299; check in #328 |
| Ambiguous naming, exception and suppression rules | review-only | tracked in #302 |
| The graph name is resolved once through configuration | review-only | tracked in #301 |
