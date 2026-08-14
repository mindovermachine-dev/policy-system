---
description: Initialize a minimal standalone devcontainer with TakT tooling.  TakT is a set of DevX tooling bundled into a conceptual unity, although it spawns both GitHub CLI extensions, GitHub Actions, Project and Issue templates, and other DevX tooling.  This skill is used to initialize a repository with devcontainer with TakT tooling. Later you can apply more specific skills to add specific tools stack awareness, suchs as Astro, Jekyll, GoLang, Python etc.
metadata:
  github-path: skills/init-takt-devcontainer
  github-ref: refs/heads/main
  github-repo: https://github.com/mindovermachine-dev/how-we-work
  github-tree-sha: 4aac0e5e4a1f9d3a5e3be48c706e929e176575f1
name: init-takt-devcontainer
---

# Init TakT Devcontainer

Use this skill when a repository needs a standalone devcontainer with minimal
dependencies and TakT-compatible GitHub CLI tooling.

## Use when

- As an initial basic TakT setup
- `cspell`, `prettier` and `markdownlint-cli2` as the only verification methods.
- No build or tools stack support yet.
- The repo should uses a single standard Ubuntu LTS devcontainer

## Files created or updated

- `.devcontainer/devcontainer.json`
- `.insitu.yml`
- `.gitignore` (append if it already exists)
- `.gitconfig`
- `.githooks/pre-commit`

## Implementation steps

In the following steps that name both a _source_ and a _target-, Create the _target_ using the _source_ as an offset. If the _target_ files already exists, it's likely that the skill has already run befoe and that this is an update run. You shall then attemptto merge the content of the _source_ file into the existing file. Use a standard three-way-merge approach where _change lead_. If a clean merge seems impossible, mark the conflict it as a standard merge conflict and let the user resolve it manually.

If changes are insignificant, you should assess wether it makes more sense to act _idempotently_ and leave the file as is. If you are unsure, ask the user for guidance.

Do not stage or commit any files to the repository. The user will be responsible for staging and committing the changes after reviewing them.

### Devcontainer

source: `templates/.devcontainer/devcontainer.json`
target: `.devcontainer/devcontainer.json`

### Insitu

source: `templates/insitu.yml`
target: `.insitu.yml`

### GitHub Issue Template

source: `templates/.github/ISSUE_TEMPLATE/standard-mom.md`
target: `.github/ISSUE_TEMPLATE/standard-mom.md`

### Git Config

source: `templates/.gitconfig`
target: `.gitconfig`

### Git ignore

source: `templates/.gitignore`
target: `.gitignore`

### Git Hooks

source: `templates/.githooks/pre-commit`
target: `.githooks/pre-commit`

### cSpell

source: `templates/.cspell.jsonc`
target: `.cspell.jsonc`

Source: `templates/.dict/repo.dictionary`
target: `.dict/repo.dictionary`

### markdownlint

source: `templates/.markdownlint-cli2.jsonc`
target: `.markdownlint-cli2.jsonc`

### VS Code

source: `templates/.vscode/settings.json`
target: `.vscode/settings.json`

source: `templates/default.code-workspace`
target: `default.code-workspace`

### Actions

source: `templates/.github/actions/prep-runner/action.yml`
target: `.github/actions/prep-runner/action.yml`

### Workflows

source: `templates/.github/workflows/copilot-setup-steps.yml`
target: `.github/workflows/copilot-setup-steps.yml`

source: `templates/.github/workflows/on_dev.yml`
target: `.github/workflows/on_dev.yml`

source: `templates/.github/workflows/on_main.yml`
target: `.github/workflows/on_main.yml`

source: `templates/.github/workflows/on_ready.yml`
target: `.github/workflows/on_ready.yml`

source: `templates/.github/workflows/on_semver.yml`
target: `.github/workflows/on_semver.yml`

source: `templates/.github/workflows/pr-to-ready.yml`
target: `.github/workflows/pr-to-ready.yml`

### Copilot instructions

source: `templates/.github/copilot-instructions.md`
target: `.github/copilot-instructions.md`

## verification steps

The verification is to use the VS Code feature `Dev Containers: Rebuild and Reopen in Container` after the devcontainer is built and openened the workspace should be loaded.

the verification steps below only makes sense if you have a devcontainer already running and you are inside the container. The use may reinvoke this skill after first run, just to run the verification steps. If that is the case then you should jump straight to verification.

The pre-commit hook will be automatically installed and run on every commit. The hook will run the `trunk-worthy` insitu command, which will verify that the repository is ready for trunk development.

You can run it manually by running the following command:

```bash
.githooks/pre-commit
```

or alternatively you can run the following command:

````bash
gh insitu run trunk-worthy
```bash

It's likely that the first run of the pre-commit hook will fail, because the spelling and linting tools will find issues in the repository. You can offer to fix those issues. but if you do, leave the changed files unstaged and uncommitted for the user to review and commit.

The command:

```bash
gh insitu run fix-all
````

will attempt to fix all issues found by bot prettier and markdownlint-clli2. But you may need to help the user by addin folders to ignore to `.markdownlint-cli2.jsonc` and `.prettierignore` if the repository has a lot of files that are not relevant to the project or apper to be autogenreated or imported from somewhere else.

The spelling can be executer simply by running the following command:

```bash
cspell
```

Iw words are valid and should be added to the dictionary, you can add them to the `.dict/repo.dictionary` keet the file sorted alphabetically. If you find any misspelled words you can correct them. If you are unsure about a word, you can ask the user for guidance.

Leave the changed files unstaged and uncommitted for the user to review and commit.

## Notes

- Keep runtime minimal; avoid app-framework dependencies unless explicitly needed.
- Prefer markdown/doc quality tooling over build stacks for docs-first repos.
- Install lint tools through devcontainer features and verify them in insitu.
