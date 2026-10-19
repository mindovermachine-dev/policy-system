# Contributing

Thank you for considering contributing to this project!

## Table of Contents

- [Workflow](#workflow)
- [Development Setup](#development-setup)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Automated guardrails](#automated-guardrails)
- [Releasing](#releasing)
  - [How the bump is decided](#how-the-bump-is-decided)
  - [What gets synced](#what-gets-synced)
  - [Push races self-heal](#push-races-self-heal)
- [Reporting Issues](#reporting-issues)
  - [Triage and backlog order](#triage-and-backlog-order)
- [Discussions](#discussions)

## Workflow

Contributions go through `devx-cafe/gh-tt`'s issue-branch workflow: pick up an
issue on a dedicated branch, then hand off to CI, which merges to `main` once
checks pass. No fork and no manual Pull Request in the common case.

The workflow is as follows:

1. Pick the next issue according to the
   [triage policy](docs/policy/triage-policy.md) (see
   [Triage and backlog order](#triage-and-backlog-order)), then start work on it
   -- this creates and checks out an issue branch:

   ```bash
   gh tt workon -i <issue-number>
   ```

   The issue title must carry a conventional-commit type (`feat: ...`, `fix: ...`,
   etc. -- see [Releasing](#releasing) for the full allow-list)

2. Make your changes. Run tests (see [Testing](#testing) below).

3. Commit and push your progress to the issue branch as you go:

   ```bash
   gh tt wrapup "<commit message>"
   ```

   Repeat steps 2-3 as many times as needed.

4. When the issue is done run:

   ```bash
   gh tt deliver
   ```

   Pushing a `ready/*` branch triggers `on_ready.yml`: it runs the
   `trunk-worthy` check suite, then auto-merges to `main` -- no manual Pull
   Request needed. Once merged, `ps-service`, `policy-service plugin` and `ps-cli`
   releases are cut automatically -- see [Releasing](#releasing) below.

## Development Setup

Requires VSCode with the Dev Containers extension.

The devcontainer (`.devcontainer/`) is a Docker Compose setup with two
services:

- `app` — the dev/backend container VS Code attaches to.
- `falkordb` — the community-maintained `falkordb/falkordb:v4.22.1` image
  (pinned in `charts/policy-system/values.yaml` as `falkordb.image.tag`),
  started automatically alongside `app`.

Reopen the repo in the container (VS Code: "Reopen in Container") and both
services start together.

### Bumping the FalkorDB image

Never use `latest`; tags are `vMAJOR.MINOR.PATCH`. To move to a new tag:

1. Change `falkordb.image.tag` in `charts/policy-system/values.yaml`. It is the
   only source the chart and the Python tests read.
2. Update the literals the guard test reports: the compose file, the line above,
   the two tool usage lines under `tools/graph-ingestion/`, the
   `falkordb.image.tag` row in `docs/artifacts/helm-chart-values-reference.md`,
   and the image tag and observation date in the docstring of
   `ps-service/tests/query_engine/test_ro_query_rejects_write_live.py`.
3. Start a throwaway container of the new tag and run the live read-only test:
   `PS_TEST_FALKORDB_PORT=<port> uv run pytest ps-service/tests/query_engine/test_ro_query_rejects_write_live.py -m falkordb_live -p no:xdist -o addopts=""`.
4. Run the smoke tests:
   `uv run pytest ps-service/tests/test_container_image.py -m container_image -p no:xdist -o addopts=""`.
5. Run `helm unittest charts/policy-system` and
   `uv run pytest ps-service/tests/test_falkordb_image_pin.py`.
6. Record the new tag and the evidence from steps 3 to 5 on the GitHub issue for the
   bump (or in the wrapup commit message).

`devx-cafe/gh-tt` must be installed as part of the devcontainer setup. Once the devcontainer is up you must run the following:

```bash
gh auth login
gh ext install devx-cafe/gh-insitu
gh insitu run post-create post-create-deps
```

Running PS Service against your own state Postgres (`scripts/ps-service.sh start`): the
immutable graph log tables are created by a privileged step, not by PS Service, so startup fails
closed until you run `python -m ps_service.graph_gateway.provision` once with admin credentials
(`PS_STATE_ADMIN_POSTGRES_USER`, `PS_STATE_ADMIN_POSTGRES_PASSWORD`, plus the usual
`PS_STATE_POSTGRES_*`). The same command first applies the ordinary `ps_state` migrations as the
application role, so it also works against an empty database. The admin role must be able to
`SET ROLE` to the application role. See the [Operations Guide](docs/artifacts/operations-guide.md#upgrading-to-the-graph-mutation-log).

## Coding Standards

See [`docs/coding-standards/level2-python-instructions.md`](docs/coding-standards/level2-python-instructions.md)
and [`docs/coding-standards/level1-coding-principles.md`](docs/coding-standards/level1-coding-principles.md).

Python code is linted with **ruff** (`select = ["ALL"]` minus a documented opt-out
list), formatted with **ruff format**, type-checked with **basedpyright** in strict
mode, and its dependencies audited with **pip-audit**. Config lives in the root
`pyproject.toml`. All four run in CI (`trunk-worthy` wave) and in the pre-commit
hook; a violation blocks the commit and fails CI.

## Testing

When implementing code use TDD as the default way to ensure appropriate test coverage.

Run the default suite — everything except the slow, environment-dependent marker
groups. A bare `pytest` already does this and runs in parallel across all cores
(`pytest-xdist`, configured once in `addopts` in the root `pyproject.toml`, the same
setting the `trunk-worthy` gate uses):

```bash
uv run pytest
```

Pass `-n0` to run serially in a single process, for example under `pdb` or with `-s`:

```bash
uv run pytest -n0 -s ps-service/tests/path/to/test_file.py
```

Selecting a live marker group with `-m` (for example `-m falkordb_live`) runs in a single
process automatically, because those tests share external services and are not safe to run
concurrently. Passing `-n` explicitly together with a live marker overrides that and is
unsupported.

A test that needs several external services carries every one of their markers (for example
`falkordb_live` and `llm_live` on the real-socket startup test, which also provisions a scratch
Postgres through a fixture), so a run selecting one marker picks only tests whose sole external service is that one.
Run a multi-service test with the conjunction, for example `-m "integration and falkordb_live"`.

The live suites are self-contained: each seeds (and removes) its own graphs, so none depends on
ambient dev data. A full live run needs a reachable FalkorDB, a Postgres superuser DSN in
`PS_TEST_POSTGRES_SUPERUSER_DSN` (the tests provision a scratch cluster), network access to
`publications.europa.eu` for `cellar_live`, and the Azure OpenAI settings of `.env.example`
(`PS_LLMINTERFACE_MODEL`, `PS_LLMINTERFACE_EMBED_MODEL`, `AZURE_API_BASE`, `AZURE_API_VERSION` and
`AZURE_API_KEY`) for `llm_live`. The `live_native_graphs` and `live_baseline_graphs` fixtures
(`ps-service/tests/ingestion/live_native.py`) delete and rebuild the per-instrument
native and baseline graphs, so do not run those suites against a FalkorDB holding data you keep.

Run the full local gate exactly as CI does:

```bash
uv sync --group dev
uv run ruff check . && uv run ruff format --check . && uv run basedpyright && \
  uv export --no-emit-workspace --format requirements-txt --no-hashes | uvx pip-audit@2.10.1 -r /dev/stdin
```

(or `uv run pre-commit run --all-files`).

## Automated guardrails

Mechanical rules from the audits in [`docs/audits/`](docs/audits/) are checked automatically so
the cleanup does not regress. Each check runs as a pre-commit hook on the files it watches and
again in CI (`trunk-worthy` wave) over the whole tree. A failure prints
`RULE <rule-id>: <file>:<line>: <what>. How to fix: <one line>` for the scripts, and the tool's
own message for the third-party checks.

| Check (hook and CI id)     | Rules it enforces                                                                                                             | Run it locally                                                                 |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `unused-symbols`           | unused functions, variables, arguments and imports in production code (vulture, confidence 80)                                | `uv run pre-commit run unused-symbols --all-files`                             |
| `import-layering`          | package layering contracts in `[tool.importlinter]` (import-linter)                                                           | `uv run pre-commit run import-layering --all-files`                            |
| `config-drift`             | `env-var-unlisted`, `helm-value-unused`, `helm-prod-orphan`, `helm-value-undocumented`                                        | `uv run python scripts/check_config_drift.py`                                  |
| `doc-links`                | `doc-link-missing`, `doc-link-anchor`, `doc-path-missing`                                                                     | `uv run python scripts/check_doc_links.py --tracked`                           |
| `repo-hygiene`             | `tracked-backup-file`, `exec-without-shebang`, `tracked-but-ignored`, `duplicate-gitignore-rule`, `readme-layout-missing-row` | `uv run python scripts/check_repo_hygiene.py`                                  |
| `docstring-provenance`     | no issue, AC or plan references in Python docstrings and comments                                                             | `uv run python scripts/check_docstring_provenance.py --tracked`                |
| `mock-boundary-guardrail`  | mocks only at the boundaries listed for the tests                                                                             | `uv run python scripts/check_mock_boundaries.py ps-service/tests ps-cli/tests` |
| `ruff-check` (rule `T201`) | no `print` in service code                                                                                                    | `uv run ruff check`                                                            |

Run the whole set the way CI does, against an export of your working tree, with:

```bash
UV_NO_SYNC=1 uv run python scripts/run_ci_guardrails.py
```

Use it before delivering. A hook sees only staged files, so deleting or renaming a file that
others link to trips `doc-links` only in CI and in this command.

### Fixing a failure and recording an exception

- Fix the cause first. Add an exception only when the code is correct as written.
- Unused symbol that is part of a fixed signature (a protocol, a context-manager method, a
  type-only import): put `# noqa: V107` (argument) or `# noqa: V104` (import) on the line with the
  reason after it. There is no global name ignore list.
- Import layering: an entry in `ignore_imports` of the `entry-layer` contract needs a comment
  naming the open issue that removes it. Do not add new ones for new code.
- Config drift: document the value or variable where the rule points. The only baseline is
  `PROD_ORPHAN_BASELINE` in `scripts/check_config_drift.py`, with a reason per entry.
- OpenAPI and MCP surface: the structure test
  `ps-service/tests/api/test_no_drift_surface.py` fails when either differs from the snapshot in
  `ps-service/tests/fixtures/api-surface/`. After a deliberate change, regenerate the snapshot
  and format it:

  ```bash
  UV_NO_SYNC=1 uv run python -c "import json, sys; sys.path.insert(0, 'ps-service/tests/api'); import test_no_drift_surface as t; t._SNAPSHOT.write_text(json.dumps(t._current_surface(), indent=2, sort_keys=True) + chr(10), encoding='utf-8')"
  npx --no-install prettier --write ps-service/tests/fixtures/api-surface/service-surface-before-drift-detector.json
  ```

### Review-only rules

No tool checks these yet; reviewers look for them. The `## Enforcement` table of each audit in
`docs/audits/` says which rules are enforced and which are review-only.

- Design judgement: component granularity, new cross-package dependencies, naming and
  public-contract changes, and whether a new test adds value over existing ones.
- Security: authorisation of new routes and tools, input bounds, and secrets handling.
- Logging: no secret or personal data in log entries, and a single outcome vocabulary.
- Persistence: migrations apply from the previous schema and backups cover every state table.
- Deployment: chart workloads declare probes and resources; workflow actions and images are
  pinned; shell scripts pass shellcheck.
- Dependencies: direct imports are declared, licences are allowed, and advisories are triaged.
- Documentation: component documents match the solution architecture, and removed symbols no
  longer appear anywhere. To prove a removal, search the repository excluding `docs/audits/`,
  for example `git grep -n <symbol> -- . ':!docs/audits'`.
- Provenance text in config-file comments (YAML, TOML) and duplicated test bodies.

Checks for part of this list are planned in the guardrail issues linked from the umbrella
issue for the cleanup.

### Cost of the checks

All five new checks together take about 1 second warm on a development host, against a stated
budget of 15 seconds for the added CI time and 5 seconds for a typical commit. The first run of
a pre-commit hook builds its pinned tool environment once and needs network access.

## Releasing

A release is cut automatically. The `release` job in
`.github/workflows/on_main.yml` runs on every push to `main`, except a push whose
head commit message starts with `chore(release):` (that guard stops the job from
releasing off its own release commit). It classifies the commits landed since the
last release tag, computes the next version, writes it into the synced files below,
and commits/tags/pushes the result as `github-actions[bot]` — no one runs a release
command by hand.

### How the bump is decided

The job reads each commit's conventional-commit header
(`type(scope)!: description`) and classifies it:

- `feat!` (or any type with `!` after it), or a `BREAKING CHANGE:` /
  `BREAKING-CHANGE:` footer in the body → **major**
- `feat` → **minor**
- `fix`, `perf` or `refactor` → **patch** (a refactor ships because releases are the only way to deploy and verify internal changes)
- `docs`, `chore`, `ci`, `test`, `style`, `build` → no bump on their own
- a header that doesn't parse as one of those ten allowed types (`feat fix perf docs
chore ci test refactor style build`) is warned about in the job summary and does
  not bump

When several commits landed since the last tag, the highest bump among them wins.

### What gets synced

The computed version is written into every version-lockstep file, in one commit:

- `ps-service/pyproject.toml` — `[project] version`
- `ps-cli/pyproject.toml` — `[project] version`
- `charts/policy-system/Chart.yaml` — `version` and `appVersion`
- `ps-skills/ps-plugin/.claude-plugin/plugin.json` — `version`

`uv.lock` is re-locked in the same commit so it stays consistent with the two
`pyproject.toml` bumps. The commit, the annotated tag, and the push all land
atomically.

### Push races self-heal

Because the release lands by pushing to `main`, a release push can be rejected if
another push to `main` won the race first. That is expected, not an incident: the
next push to `main` — typically the very commit that won the race — recomputes the
version from the now-current tag state and releases normally. Nothing needs to be
retried, force-pushed, or fixed by hand.

## Reporting Issues

Please use the `.github/ISSUE_TEMPLATE/standard-mom.md` issue template and make sure to provide high quality acceptance criteria.

### Triage and backlog order

Every issue is triaged and worked on according to the
[triage policy](docs/policy/triage-policy.md). In short:

- **Classify when you create the issue.** Give it exactly one Group label
  (`1-Prod-Impact`, `2a-Toil-Debt-Hygiene`, `2b-CustOps-Hygiene`, `3-Backlog-Feature`), one Priority label (`P0`-`P3`:
  how many customers are affected) and one Severity label (`S0`-`S3`: how bad it
  is for them). If you can't, label it `needs-triage`. The issue type stays in
  the conventional-commit title prefix, which is authoritative; type labels such as
  `bug` do not affect ordering.
- **Work in group order.** Group 1 (operational: S0-S2 impact in production)
  before Group 2 (repeating manual work, technical debt and hygiene) before
  Group 3 (new features). Within a group: severe-and-widespread first, then
  Severity, then Priority.
- **Never pick up a `needs-triage` issue.** Triage it first.
- **Blocked is only for a good reason** (an external party, a linked issue or a
  scheduled window). Label it `blocked` and say in a comment what it is waiting on.
- **Debt you knowingly create while building a feature** is filed as a Group 2
  issue before the feature is done.
- **Feature work ahead of open Group 1/2 work** needs an `override` approved by
  the primary maintainer and recorded on the issue.

The policy has the full definitions, edge cases and the reasoning behind each rule.

## Discussions

Use the GitHub discussions feature to discuss or clarify topics that are not specific TODOs (those belong as Issues in the repo).
