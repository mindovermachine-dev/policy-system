# Contributing

Thank you for considering contributing to this project!

## Table of Contents

- [Workflow](#workflow)
- [Development Setup](#development-setup)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Releasing](#releasing)
  - [How the bump is decided](#how-the-bump-is-decided)
  - [Where a bad header is caught](#where-a-bad-header-is-caught)
  - [What gets synced](#what-gets-synced)
  - [Push races self-heal](#push-races-self-heal)
  - [Verifying a release](#verifying-a-release)
  - [Prereleases](#prereleases)
  - [Manually re-running a release](#manually-re-running-a-release)
  - [ps-cli is released the same way](#ps-cli-is-released-the-same-way)
- [Delivery Process](#delivery-process)
- [Reporting Issues](#reporting-issues)
- [Discussions](#discussions)

## Workflow

Contributions go through `devx-cafe/gh-tt`'s issue-branch workflow: pick up an
issue on a dedicated branch, then hand off to CI, which merges to `main` once
checks pass. No fork and no manual Pull Request in the common case.

`devx-cafe/gh-tt` is installed as part of the devcontainer setup. The workflow is as follows:

1. Start work on an issue -- this creates and checks out an issue branch:

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
- `falkordb` — the community-maintained `falkordb/falkordb:latest` image,
  started automatically alongside `app`.

Reopen the repo in the container (VS Code: "Reopen in Container") and both
services start together.

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
groups, which are opt-out on the command line:

```bash
uv run pytest -m "not integration and not llm_live and not cellar_live and not falkordb_live and not container_image"
```

Every excluded group is registered in the root `pyproject.toml` with the reason it
is slow, and each is run explicitly when you need it. `container_image` builds and
runs the runtime image, so it needs `docker` or `podman` and several minutes on a
cold build:

```bash
uv run pytest ps-service/tests/test_container_image.py -m container_image
```

Run the full local gate exactly as CI does:

```bash
uv sync --group dev
uv run ruff check . && uv run ruff format --check . && uv run basedpyright && \
  uv export --no-emit-workspace --format requirements-txt --no-hashes | uvx pip-audit@2.10.1 -r /dev/stdin
```

(or `uv run pre-commit run --all-files`).

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
- `fix` or `perf` → **patch**
- `docs`, `chore`, `ci`, `test`, `refactor`, `style`, `build` → no bump on their own
- a header that doesn't parse as one of those ten allowed types (`feat fix perf docs
chore ci test refactor style build`) is warned about in the job summary and does
  not bump

When several commits landed since the last tag, the highest bump among them wins.

### Where a bad header is caught

The same `scripts/release/lint-commit-header.sh` runs at three points, earliest first:

- `.githooks/pre-push` — lints the head commit of any push to `ready/*` on your
  machine, so `gh tt deliver` fails locally before CI ever runs. Issue-branch
  (`wrapup`) pushes are not linted; their headers are squashed away.
- `pr-to-ready.yml` — lints the PR title (as `<title> - resolves #N`) before the
  takt action squashes it onto a `ready/*` branch.
- `on_ready.yml` — the CI gate `merge-to-trunk` depends on; the backstop.

When the header's leading token looks like a scope used as a type (`company_merge:
...`, `ps-service/ps-cli: ...`) the lint prints a `hint:` with the `type(scope): ...`
rewrite. Since `gh tt deliver` takes the header from the issue title, fix the _issue
title_ first and deliver again, or the next delivery will fail the same way.

### What gets synced

The computed version is written into every version-lockstep file, in one commit:

- `ps-service/pyproject.toml` — `[project] version`
- `ps-cli/pyproject.toml` — `[project] version`
- `charts/policy-system/Chart.yaml` — `version` and `appVersion`
- `ps-skills/policy-system/.claude-plugin/plugin.json` — `version`

`uv.lock` is re-locked in the same commit so it stays consistent with the two
`pyproject.toml` bumps. The commit, the annotated tag, and the push all land
atomically.

### Push races self-heal

Because the release lands by pushing to `main`, a release push can be rejected if
another push to `main` won the race first. That is expected, not an incident: the
next push to `main` — typically the very commit that won the race — recomputes the
version from the now-current tag state and releases normally. Nothing needs to be
retried, force-pushed, or fixed by hand.

### Verifying a release

```bash
docker buildx imagetools inspect ghcr.io/mindovermachine-dev/ps-service:<tag>
```

Each published tag must resolve to a manifest list containing both `linux/amd64`
and `linux/arm64`. The package also carries `build-amd64` and `build-arm64`
staging tags — these are the per-architecture images the manifest list points at,
and they are expected.

The tag itself must point at a commit that is an ancestor of `main`; the
`verify-tag-on-main` job checks this and fails the release if it does not hold.

### Prereleases

Prerelease-form tags (semver 2.0, e.g. `1.2.4-pre.1`) are never produced by the
automated flow and are not supported by the release pipeline.

### Manually re-running a release

`.github/workflows/on_semver.yml` also accepts a `workflow_dispatch` run against an
existing tag, taking either a bare (`0.12.0`) or `v`-prefixed (`v0.12.0`) tag as
input. This re-runs the image build/publish/GitHub-release steps for a tag that
already exists — it does not cut a new version.

### ps-cli is released the same way

`ps-cli` is released in lockstep with `ps-service` by the same automated job — its
`pyproject.toml` version is one of the synced fields above. The separate CLI tag
procedure is retired: there is no independent tag or release step for `ps-cli`
anymore.

A contributor who needs `ps-cli` built from a branch or commit that has no release
yet — before it's merged to `main`, for example — uses `PS_CLI_REF` with
[`install.sh`](../../ps-cli/install.sh) instead of installing a release wheel:

```bash
curl -fsSL https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/ps-cli/install.sh | PS_CLI_REF=<branch-or-sha> bash
```

This is `install.sh`'s developer path: it skips release resolution and checksum
verification entirely and installs directly via
`uv tool install "git+https://github.com/mindovermachine-dev/policy-system@<branch-or-sha>#subdirectory=ps-cli"`.
Unlike the raw `git+` command, `PS_CLI_REF` accepts either a branch name or a commit
SHA — a SHA pins the exact commit the way a tag cannot.

See also [Run from a repo checkout](#run-from-a-repo-checkout-local-development)
above for invoking `ps-cli` as a module without installing it at all.

## Delivery Process

- Keep each issue branch focused on a single change.
- Give the issue a clear title/description — `gh tt workon` uses it, and
  `gh tt deliver` carries it through to the squashed `ready/*` commit.
- Ensure all checks pass before running `gh tt deliver` (see
  [Getting Started](#getting-started) above).
- Every release adds a `chore(release)` commit to `main` (see
  [Releasing](#releasing) above), so a `ready/**` branch already in flight when
  that happens may need a rebase before its own `gh tt deliver`.
- A rebase that hits conflicts commits the resolution without running the
  pre-commit hook (git's rebase backend never invokes it), so run
  `uv run pre-commit run --all-files` after resolving. `.githooks/pre-push`
  re-runs `ruff check`/`ruff format --check` on every commit you push as a
  safety net, and rejects the push with ruff's own report if either fails.

## Reporting Issues

Please use the `.github/issue_template/standard-mom.md` issue template and make sure to provide high quality acceptance criteria.

## Discussions

Use the GitHub discussions feature to discuss or clarify topics that are not specific TODOs (those belong as Issues in the repo).
