# Contributing

Thank you for considering contributing to this project!

## Table of Contents

- [Workflow](#workflow)
- [Development Setup](#development-setup)
- [Dependency updates](#dependency-updates)
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
   full `trunk-worthy test audit` gate, then auto-merges to `main` -- no manual Pull
   Request needed. Once merged, `ps-service`, `policy-service plugin` and `ps-cli`
   releases are cut automatically -- see [Releasing](#releasing) below.

## Development Setup

Requires VSCode with the Dev Containers extension.

The devcontainer (`.devcontainer/`) is a Docker Compose setup with two
services:

- `app` — the dev/backend container VS Code attaches to.
- `falkordb` — the community-maintained `falkordb/falkordb:6.0.3` image
  (pinned in `charts/policy-system/values.yaml` as `falkordb.image.tag`),
  started automatically alongside `app`.

Reopen the repo in the container (VS Code: "Reopen in Container") and both
services start together.

### Bumping the FalkorDB image

Never use `latest`. Tags are exact versions as the registry publishes them: the 4.x line carries a
`v` prefix (`v4.22.3`) and the 6.x line does not (`6.0.3`); `falkordb_image_tag()` and the pin
guard accept both. To move to a new tag:

1. Change `falkordb.image.tag` in `charts/policy-system/values.yaml`. It is the
   only source the chart and the Python tests read.
2. Update the literals the guard test reports: the compose file, the line above,
   the two tool usage lines under `tools/graph-ingestion/`, the
   `falkordb.image.tag` row in `docs/artifacts/helm-chart-values-reference.md`,
   and the image tag and observation date in the docstring of
   `ps-service/tests/query_engine/test_ro_query_rejects_write_live.py`.
3. Start a throwaway container of the new tag on a spare port and run every FalkorDB-only live
   test against it. The marker expression overrides the default deselection while the repository
   `addopts` stay in place, which the live-marker serialisation needs:
   `PS_TEST_FALKORDB_PORT=<port> PS_FALKORDB_PORT=<port> uv run pytest ps-service/tests -m "falkordb_live and not postgres_live and not llm_live and not cellar_live and not integration" -rA`.
   The run must end in `N passed` with `N` equal to the `--collect-only` count and no skipped
   tests. Run it on the previous tag first, so a failure on the new tag is a comparison.
4. Run the smoke tests:
   `uv run pytest ps-service/tests/test_container_image.py -m container_image -rA`.
5. Re-check that `redis-cli` (FalkorDB) and `pg_isready` (authentik Postgres) exist in
   the new image: the chart probes call them.
6. Prove the data survives the change on a real volume:
   `tools/falkordb-upgrade-proof/run_on_kind.sh --from-tag <previous-tag>` seeds a known graph
   with the previous image on a PersistentVolumeClaim of the local kind cluster, opens it with the
   chart's image, then opens it again with the previous image. It exits 1 unless the per label
   node counts, per type edge counts, graph list and index definitions are identical in all three
   legs. Without a cluster, repeat the same three legs with `podman run -v <dir>:/var/lib/falkordb/data`
   on `<previous-tag>`, the new tag and `<previous-tag>` again (running `SAVE` before each stop)
   and compare the same queries by hand.
7. Run `helm unittest charts/policy-system` and
   `uv run pytest ps-service/tests/test_falkordb_image_pin.py`.
8. Record the new tag and the evidence from steps 3 to 7 on the GitHub issue for the
   bump (or in the wrapup commit message).

Since 6.0.2 the image starts as root, hands the data directory to uid 999 and runs the server as
that user. This only happens when the directory holds nothing but `*.rdb`, `nodes*.conf` and
the append-only file directory; otherwise the server keeps running as root. The chart needs no change: an existing
volume keeps loading, and the previous image reads a volume the new one has written because it runs
as root. In the devcontainer the bind-mounted `.falkordb-data` directory can be re-owned by the first
start of a 6.x image; under rootless podman that uid maps to a sub-uid, so the host user may then
need `podman unshare rm -rf .falkordb-data` to reset it. The image has a documented switch that keeps
the server running as root; set it on the compose service if the reset is not acceptable.

### Rolling back a FalkorDB image

On a deployed cluster, take a backup before the upgrade (operations guide, Backup). To roll back:

1. Set `falkordb.image.tag` back to the previous tag and `helm upgrade`. The previous image opens
   the volume the newer image wrote; `run_on_kind.sh` (step 6 above) exercises exactly this leg.
2. If the graph is empty or damaged afterwards, PS Service rebuilds it from the graph mutation log
   when it starts (operations guide, Graph replay at startup). If PS Postgres is lost as well,
   restore from the backup taken before the upgrade (operations guide, Restore).

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

## Dependency updates

[`.github/dependabot.yml`](.github/dependabot.yml) proposes weekly updates for uv, npm, Docker,
Helm and GitHub Actions. Dependabot only opens pull requests; nothing merges automatically.

How a Dependabot pull request lands:

1. Review the pull request and approve it. The approving review triggers `pr-to-ready.yml`, which
   squashes it onto a `ready/*` branch, and `on_ready.yml` then runs the full gate and merges to
   `main`, as for any other branch. `pr-to-ready.yml` needs the `READY_PUSHER` secret, which for
   Dependabot-triggered runs must be set as a Dependabot secret.
2. The pull request title must pass `scripts/release/lint-commit-header.sh`, or `pr-to-ready`
   stops it. The `commit-message` prefixes in the config guarantee that for version updates and
   security updates alike.
3. The prefix says what ships: `fix(deps)` for runtime Python dependencies, the Docker image and
   the Authentik chart (a patch release), `build(deps)` for dev Python dependencies and npm,
   `ci(deps)` for Actions (no release). Minor and patch updates are grouped; a major update
   always gets its own pull request.
4. The Python base image is pinned by its multi-arch digest in the `base` stage of the
   `Dockerfile` (`FROM python:<tag>@sha256:<digest> AS base`). Dependabot's docker ecosystem
   refreshes that digest through a `fix(deps)` pull request, which ships as a patch release.
   Never edit the digest by hand. The interpreter's major and minor version stay ignored, so
   moving to a new Python minor is a deliberate manual change of the tag.

Dependabot cannot see the following, so update them by hand:

- **The uv version.** It is pinned in six places that must agree, plus one comment: `Dockerfile`
  (the `COPY --from=ghcr.io/astral-sh/uv:` line and the comment above it),
  `.github/actions/prep-runner/action.yml`, `.github/workflows/backup-restore-e2e.yml`,
  `.github/workflows/on_semver.yml` (two steps) and `.devcontainer/devcontainer.json`.
  `test_dependabot_config.py` fails when they disagree. The uv image is excluded from Docker
  updates for this reason.
- **The npm `overrides` block** in `package.json` (katex and re2js). Dependabot does not update it.
- **kubectl and the devcontainer Node version.**
- **The FalkorDB tag** in `charts/policy-system/values.yaml`; follow
  [Bumping the FalkorDB image](#bumping-the-falkordb-image).

An Authentik chart bump is a trigger, not a finished change. The pull request as opened must also
get `charts/policy-system/values-prod.yaml`, `charts/policy-system/templates/_helpers.tpl` and
`docs/architecture/customer-azure-deployment.md` updated before it is approved.

### Pinned actions

Every non-local `uses:` in `.github/workflows` and `.github/actions` names a full commit SHA with
the version it stands for in a trailing comment, so a re-pointed upstream tag or branch cannot
change what the release pipeline runs:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
```

To add an action, resolve the tag to its commit
(`gh api repos/<owner>/<repo>/commits/<tag> --jq .sha`), write the SHA and keep the
`# vX.Y.Z` comment in that form, because it is what Dependabot reads. Dependabot then proposes
the SHA and the comment together as a reviewed pull request; do not edit the pins by hand to
upgrade. The installers in `.insitu.yml` read their scripts from a version tag and install
helm-unittest with an explicit `--version`.
`test_workflow_action_pins.py` fails on a tag, a branch or a short SHA, with the file and line.

## Coding Standards

See [`docs/coding-standards/level2-python-instructions.md`](docs/coding-standards/level2-python-instructions.md)
and [`docs/coding-standards/level1-coding-principles.md`](docs/coding-standards/level1-coding-principles.md).

Python code is linted with **ruff** (`select = ["ALL"]` minus a documented opt-out
list), formatted with **ruff format**, type-checked with **basedpyright** in strict
mode, and its dependencies audited with **pip-audit**. Config lives in the root
`pyproject.toml`. `on_ready.yml` runs `gh insitu run trunk-worthy test audit` and `on_dev.yml` runs
`gh insitu run trunk-worthy`: ruff check and ruff format run in the `trunk-worthy` wave, which is also the git commit hook; basedpyright runs in the
`test` wave and pip-audit in the `audit` wave. A violation blocks the commit or fails CI.

## Testing

When implementing code use TDD as the default way to ensure appropriate test coverage.

Run the default suite — everything except the slow, environment-dependent marker
groups. A bare `pytest` already does this and runs in parallel (`pytest-xdist`, configured
once in `addopts` in the root `pyproject.toml`, the same setting the `trunk-worthy` gate
uses). The worker count is one per core, capped by available memory at one worker per
800 MiB; the run's first line says the count and which limit set it. An explicit `-n N`
is used unchanged:

```bash
uv run pytest
```

The default suite includes the chart workload guard, which renders the chart, so it needs `helm`
and the packaged Authentik chart (`helm dependency build charts/policy-system`, or
`gh insitu run post-create-deps`). It fails, and never skips, without them.

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

Run the live suites against throwaway FalkorDB and Postgres containers, never a shared or dev
graph. Point the tests at them with `PS_TEST_FALKORDB_HOST`/`PS_TEST_FALKORDB_PORT` (read by
`falkordb_endpoint()` in `ps-service/tests/graph_gateway/live_endpoints.py`) and
`PS_FALKORDB_HOST`/`PS_FALKORDB_PORT` (read by tests that build their configuration with
`load_config()`). Besides the superuser DSN, some `postgres_live` tests read the ambient
`PS_STATE_POSTGRES_*` and `PS_PASSKEYSIGNING_POSTGRES_*` variables (`_HOST`, `_PORT`, `_DATABASE`,
`_USER`, `_PASSWORD`) through `load_config()`, so those databases and roles must exist first:

```bash
psql "$PS_TEST_POSTGRES_SUPERUSER_DSN" \
  -c "create role ps_state login password '<state-password>'" \
  -c "create role ps_signing login password '<signing-password>'" \
  -c "create database ps_state owner ps_state" \
  -c "create database ps_signing owner ps_signing"
```

Keep the repository `addopts` when selecting live markers across packages, for example
`uv run pytest -m "falkordb_live or postgres_live"`: they set `--import-mode=importlib`, without
which same-named test files in different packages collide at collection. `-o addopts=""` is only
safe for a single test file.

Run the full local gate exactly as CI does:

```bash
uv sync --group dev
gh insitu run trunk-worthy test audit
```

The waves run in order and a failing wave stops the later ones. `trunk-worthy` is the short
wave that also runs in the git commit hook; `test` holds pytest and basedpyright; `audit`
holds pip-audit and the action-reference check.

## Automated guardrails

Mechanical rules from the audits in [`docs/audits/`](docs/audits/) are checked automatically so
the cleanup does not regress. Each check is defined once in `.insitu.yml` and runs over the whole
tree in the `trunk-worthy` wave: in the git commit hook (`.githooks/pre-commit`) and on every push to an issue branch (`on_dev.yml`). The
hook checks the working tree, not the index, so a commit passes or fails on what CI will see.
There is no push-time hook; `on_dev.yml` runs only `trunk-worthy` and `on_ready.yml` runs all three
waves before the merge, so run `gh insitu run trunk-worthy test audit` locally before `gh tt deliver`. A failure prints
`RULE <rule-id>: <file>:<line>: <what>. How to fix: <one line>` for the scripts, and the tool's
own message for the third-party checks.

| Check (`.insitu.yml` id)   | Rules it enforces                                                                                                             | Run it locally                                                                 |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `unused-symbols`           | unused functions, variables, arguments and imports in production code (vulture, confidence 80)                                | `uv run vulture`                                                               |
| `import-layering`          | package layering contracts in `[tool.importlinter]` (import-linter)                                                           | `uv run lint-imports`                                                          |
| `config-drift`             | `env-var-unlisted`, `helm-value-unused`, `helm-prod-orphan`, `helm-value-undocumented`                                        | `uv run python scripts/check_config_drift.py`                                  |
| `doc-links`                | `doc-link-missing`, `doc-link-anchor`, `doc-path-missing`                                                                     | `uv run python scripts/check_doc_links.py --tracked`                           |
| `repo-hygiene`             | `tracked-backup-file`, `exec-without-shebang`, `tracked-but-ignored`, `duplicate-gitignore-rule`, `readme-layout-missing-row` | `uv run python scripts/check_repo_hygiene.py`                                  |
| `docstring-provenance`     | no issue, AC or plan references in Python docstrings and comments                                                             | `uv run python scripts/check_docstring_provenance.py --tracked`                |
| `mock-boundary-guardrail`  | mocks only at the boundaries listed for the tests                                                                             | `uv run python scripts/check_mock_boundaries.py ps-service/tests ps-cli/tests` |
| `ruff-check` (rule `T201`) | no `print` in service code                                                                                                    | `uv run ruff check .`                                                          |

Run the whole set the way the commit hook does with `gh insitu run trunk-worthy`. Because it
checks the whole working tree, deleting or renaming a file that others link to trips `doc-links`
at commit time.

### Structural tests in the default `pytest` run

Four guards are ordinary tests in the default `uv run pytest`, so they also run in CI's `pytest`
check. They need no live service and use only dummy values. They fail, and never skip, when
`helm` or the Authentik chart archive is missing. Each exemption a guard allows is stated, with its
reason, in the guard's module docstring.

| Guard                     | What it enforces                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | Run it locally                                                                | Where to edit                                                                         |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| chart workloads           | renders `charts/policy-system` with the default values and with `authentik.enabled=true`; `Deployment`, `StatefulSet` and `DaemonSet` main containers declare `readinessProbe`, and `startupProbe` if and only if `livenessProbe`; every main container of those and of `Job` and `CronJob` sets `requests.cpu`, `requests.memory` and `limits.memory`; Jobs are exempt from the probe rule and plain init containers from both, while an init container with `restartPolicy: Always` counts as a main container | `uv run pytest ps-service/tests/guardrails/test_chart_workloads.py`           | the chart templates and values                                                        |
| migration coverage        | every directory of `NNNN_*.sql` files under `ps-service/src/ps_service` has an entry naming its `postgres_live` empty-apply and N-1→N upgrade tests that run, and its database; the entry records the newest migration                                                                                                                                                                                                                                                                                           | `uv run pytest ps-service/tests/guardrails/test_migration_coverage.py`        | `ps-service/tests/guardrails/migration_registry.py`                                   |
| verifier tables           | every table a `ps_state` or `ps_signing` migration creates is declared seeded by a tool in `tools/backup-restore-verify` that the verifier runs, or is on `SEED_EMPTY_EXCEPTIONS`; no list names a table no migration creates                                                                                                                                                                                                                                                                                    | `uv run pytest ps-service/tests/guardrails/test_verifier_tables.py`           | `ps-service/tests/guardrails/verifier_seeding.py`, `scripts/verify-backup-restore.sh` |
| script comment provenance | no comment line in `scripts/deploy-ps-eval.sh`, `scripts/deploy-ps-prod.sh` or `scripts/lib/authentik-owner.sh` carries an acceptance-criterion id or an issue number; string literals the code needs are not comments                                                                                                                                                                                                                                                                                           | `uv run pytest ps-service/tests/guardrails/test_script_comment_provenance.py` | the comments of those scripts                                                         |

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
- Persistence: the registered migration tests pass against a live Postgres
  (`uv run pytest -m postgres_live`), which CI does not run yet.
- Deployment: probe timings and resource sizes fit the workload; workflow actions and images are
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
budget of 15 seconds for the added CI time and 5 seconds for a typical commit. The commit hook
checks the working tree, so changes left unstaged with `git add -p` are checked too.

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
