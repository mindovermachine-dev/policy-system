<!-- © 2026 Cartman ApS. All rights reserved. -->

# Repository Hygiene Audit - Findings Report (Issue #248, umbrella #246)

## Scope examined

Covered: repository root, `.gitignore`, `.dockerignore`, `.github/`, `logs/`, `node_modules/`, `package.json`, `internal-sources/`, `curated-content/`, `test-data/`, `spikes/CONVENTIONS.md`, plus the tracked history of all branches.

No rule in `docs/coding-standards/` or `docs/architecture/` governs repository layout; none is applicable.

Out of dimension, noted only: workflow security posture (#252), guardrail automation (#260), dead code (#247, delivered in `docs/audits/dead-code-audit.md`).

Facts at audit time: 1548 tracked files, 40 distinct top-level names (`git ls-files | cut -d/ -f1 | sort -u | wc -l`).

`spikes/` is gitignored and untracked (`git ls-files spikes | wc -l` is 0). It was read-only here and nothing in it was deleted or run. Its disposition is recorded in the dead-code audit and tracked in #265. `spikes/CONVENTIONS.md` is untracked and read-only.

## Method and commands

Tools (ephemeral, no dependency or lock change): git 2.54.0, `uvx detect-secrets` 1.5.0. `docker`, `gitleaks` and `trufflehog` are not available on the host, so the container checks are static (see H5).

H1, tracked artifacts and ignore rules:

```bash
git ls-files | grep -E '(^|/)(__pycache__|node_modules|\.venv|dist|build|htmlcov)(/|$)|\.(log|pyc|bak|orig|rdb|aof|tgz)$|\.DS_Store|(^|/)\.coverage|(^|/)\.env'
git ls-files -ci --exclude-standard | wc -l
git ls-files --others --exclude-standard
git ls-files | tr A-Z a-z | sort | uniq -d
git ls-files -s | awk '$1==100755{print $4}'
git ls-files -s | awk '$1==120000{print $4}'
git status --short --ignored ps-test-plugin ; find ps-test-plugin -type f -not -name '*.pyc' | wc -l
```

H2, size and history:

```bash
git ls-files -z | xargs -0 du -k | sort -rn | awk '$1>=200'
git rev-list --objects --all | git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' | awk '$1=="blob" && $3>1000000'
git count-objects -vH
```

H3, secrets:

```bash
git ls-files -z | grep -zvE '(\.pdf|baseline\.json|uv\.lock|package-lock\.json)$' | xargs -0 uvx detect-secrets scan
git grep -I -n -E 'BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{32,}|AccountKey='
git grep -I -c -E '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
git log --all --oneline -G'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|AccountKey=' -- . ':!*test*' ':!docs/audits'
git log --all --diff-filter=D --name-only --format= -- '*.env' '*.pem' '*.key' '*.log'
```

H4, layout coverage (expects no output):

```bash
for e in $(git ls-files | cut -d/ -f1 | sort -u) logs node_modules .venv .falkordb-data spikes; do grep -qF "| \`$e" README.md || echo MISSING $e; done
```

H5, image context, static checks:

```bash
for s in pyproject.toml uv.lock ps-service/pyproject.toml ps-cli/pyproject.toml ps-service/src; do printf '%s: ' $s; grep -qxF "!$s" .dockerignore && echo allowed || echo NOT-ALLOWED; done
grep -n '^members' pyproject.toml ; grep -nE '^(COPY|RUN)' Dockerfile
find ps-service/src -type d -name tests
uv sync --package ps-service --frozen --no-dev --no-install-workspace --dry-run   # run in a scratch copy holding only the allow-listed files
```

H6, CI and tool-config references:

```bash
grep -ohE '(scripts|tools|charts|deploy|docs|ps-[a-z-]+)/[A-Za-z0-9_./-]+' .github/workflows/*.yml .github/actions/*/action.yml .github/scripts/* .github/copilot-instructions.md .pre-commit-config.yaml .insitu.yml .githooks/* .devcontainer/* .vscode/settings.json CONTRIBUTING.md | sort -u   # then test -e each
diff -r .github/skills/reasoning .claude/skills/reasoning
```

The `.gitignore` change is proven behaviour-neutral with a synthetic path corpus: one probe per distinct non-negated rule (rule text turned into a path) plus `scripts/lib/x`, `scripts/release/lib/x`-style negation probes, `logs/.gitkeep`, `logs/x.log`, `ps-cli/logs/x.log`, `.env.example`, `.env`, `lib/x`, `spikes/a/b`, `node_modules/x`, `.venv/x`, `CLAUDE.md` (118 probes). Each probe is checked with `git check-ignore --no-index -q` before and after the edit; the verdict lists are byte-identical (115 ignored; `.env.example`, `logs/.gitkeep` and `scripts/lib/x` stay un-ignored) and `git ls-files -ci --exclude-standard` stays empty.

## Severity scale

- High: tracked secret, data exposure, or a defect in what ships.
- Medium: misleading or costly leftover that a contributor can plausibly act on wrongly.
- Low: inert leftover or documentation gap with no behavioural effect.

No High or Medium finding was made.

## Findings

| id      | location                                                                            | severity | evidence                                                                                                                                                                                                                                                                          | disposition                                                                                                                              |
| ------- | ----------------------------------------------------------------------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| F-H1-01 | `.gitignore` (346 lines)                                                            | Low      | 84 rules appear twice: the language-template block was pasted a second time (the init skill template carries the same block). Probe corpus verdicts identical before and after; 346 lines became 183.                                                                          | Fixed (later exact duplicates and the empty template headings removed; negation lines untouched)                                         |
| F-H1-02 | five tracked Markdown files with mode 100755                                        | Low      | `.claude/skills/container-architecture/SKILL.md`, its template asset, `docs/architecture/ps-solution-architecture.md`, `docs/templates/{container,solution}-architecture-template.md` have no shebang and are documents.                                                          | Fixed (execute bit removed)                                                                                                              |
| F-H1-03 | `ps-service/src/ps_service/mcp_interface/mcp_server.py`, `tools/graph-query/ps.py`   | Low      | Executable bit on Python files; both start with a `#!/usr/bin/env python3` shebang, so the mode matches the content.                                                                                                                                                              | No action: shebang and mode are consistent                                                                                               |
| F-H1-04 | `.gitignore` template rules for tools the repo does not use                         | Low      | Rules for `marimo`, `.streamlit`, `.pdm-python`, `.pixi`, `celerybeat-*`, `mnesia/`, `rabbitmq/`, `activemq-data/` and similar. Removing them changes probe verdicts, so they cannot pass the neutrality proof.                                                                    | No action: the rules protect against future accidental commits and cost nothing                                                          |
| F-H1-05 | `ps-test-plugin/` (untracked, ignored)                                              | Low      | `git status --short --ignored` shows `!! ps-test-plugin/`; the directory holds zero non-`.pyc` files and zero tracked files, so it is ignored only because all its content is.                                                                                                     | No action in the repo: a local leftover; the owner may delete it locally (not deleted here)                                              |
| F-H1-06 | `.claude/worktrees/`, `.claude/scheduled_tasks.lock`                                | Low      | Ignored only by the local `.git/info/exclude` of this checkout, so a fresh clone would show them as untracked.                                                                                                                                                                    | Fixed (two rules added to `.gitignore`; probe corpus unchanged)                                                                          |
| F-H2-01 | `curated-content/*/baseline.json` (8 files of 5 to 52 MB)                           | Low      | Largest: CRA 52 MB, GDPR 17 MB. Shipped product content, regenerated only by the curated export. `git count-objects -vH` reports `size-pack: 43.88 MiB` for all history, so the clone cost is bounded.                                                                            | No action: content is the product, history is compressed to 44 MiB; Git LFS or release assets would change the restore contract         |
| F-H2-02 | history blobs `curated-content/32023R2854/baseline.json`, `32021R0241/baseline.json` | Low      | Present only in history (superseded by `DATAACT-1.0` and `RRF-1.0`); each about 5 MB uncompressed.                                                                                                                                                                                | No action: a history rewrite on a public repo is not warranted for roughly 10 MB uncompressed                                           |
| F-H2-03 | `docs/regulations/*.pdf` (13 files, 16 MB total) and their `.md` conversions        | Low      | Source texts of EU legal acts, converted by `docs/regulations/convert_to_markdown.py`; reuse basis recorded in `curated-content/LICENSING.md` (EU institutions reuse policy).                                                                                                      | No action: provenance and licence already documented                                                                                     |
| F-H2-04 | `test-data/eu-regulations/{cra,gdpr,nis2}.json`, `policy_system_graph.json`         | Low      | Not duplicates of `curated-content/*/native.json` (`cmp` differs). Consumed by `tools/graph-ingestion` and its README; kept out of the image (`ps-service/tests/test_container_image.py`).                                                                                         | No action: distinct, used                                                                                                                |
| F-H3-01 | 147 `detect-secrets` hits (scan excludes PDFs, baselines and lock files)            | Low      | All classified, none a real secret. See the secret-scan triage below.                                                                                                                                                                                                              | No action: false positives                                                                                                               |
| F-H3-02 | history of all branches                                                             | Low      | The private-key, access-key, token and connection-string history search returned zero commits; no `.env`, `.pem`, `.key` or `.log` file was ever deleted from history.                                                                                                              | No action: nothing found                                                                                                                 |
| F-H4-01 | `README.md`                                                                         | Medium   | No repository layout section; 40 top-level names had no documented purpose (0 of 45 layout rows existed).                                                                                                                                                                         | Fixed (Repository layout table added to `README.md`; layout check above is empty)                                                        |
| F-H4-02 | `default.code-workspace`                                                            | Low      | Looked unreferenced, but `.agents/skills/init-takt-devcontainer/SKILL.md` names it as a template target.                                                                                                                                                                          | No action: used by the skill; kept and documented in the layout table                                                                    |
| F-H4-03 | `.git-blame-ignore-revs`                                                            | Low      | Contains only a 40-zero placeholder revision; the header asks for a SHA that was never filled in, and the named commit does not exist in history.                                                                                                                                 | Issue #266                                                                                                                               |
| F-H4-04 | `.gitconfig`, `.insitu.yml`, `.dict`, `internal-sources/`, `deploy/`, `logs/`, `node_modules/` | Low | Each has a consumer: `.insitu.yml` includes `.gitconfig`; `.cspell.jsonc` loads `.dict/repo.dictionary`; `scripts/deploy-ps-eval.sh` uses `deploy/kind/cluster.yaml`; `package.json` pins the pre-commit Node tools restored by `npm ci`. `internal-sources/` is the source of the Engineering Practices instrument. | No action: purpose confirmed and now stated in the layout table                                                                          |
| F-H5-01 | `.dockerignore` vs `Dockerfile`                                                     | Low      | All five paths the Dockerfile copies are allowed (`allowed` for each entry); `find ps-service/src -type d -name tests` is empty, so the `**/tests` rule excludes nothing packaged; `.env`, `.git`, `docs`, `logs`, `spikes`, `.falkordb-data`, `node_modules` fall under the leading `*`. | No action: consistent                                                                                                                    |
| F-H5-02 | `Dockerfile` comment on workspace members; `pyproject.toml` member `ps-test-support` | Low     | `ps-test-support` is a workspace member but not in the build context. A dry-run `uv sync --package ps-service --frozen --no-dev --no-install-workspace` in a scratch copy with only the allow-listed files succeeds, also without `ps-cli/pyproject.toml`; the comment "uv loads every declared workspace member" is therefore imprecise. No image build was possible (no `docker`). | No action: harmless, and a Dockerfile edit changes the shipped build, which needs an owner decision plus a real build                    |
| F-H6-01 | `.claude/skills/reasoning/SKILL.md` and `.github/skills/reasoning/SKILL.md`         | Low      | Byte-identical tracked copies, while `create-gh-issue` uses a symlink.                                                                                                                                                                                                            | Issue #267                                                                                                                               |
| F-H6-02 | workflow, hook and config path references                                           | Low      | Six extracted paths do not exist locally: two are download URLs (`download-actionlint.bash`, `get-helm-3`) and one is a truncated match of `.github/scripts/verify-action-refs.sh`, which exists, one is a placeholder (`path/to/test_file.py`), two are regex or glob fragments of `ps-domain-concepts.md`. No stale path. | No action: false positives                                                                                                               |
| F-H6-03 | uv version pin                                                                      | Low      | `0.12.4` in `Dockerfile`, `.github/actions/prep-runner/action.yml` and `on_semver.yml` (two places).                                                                                                                                                                              | No action: consistent                                                                                                                    |

## Secret-scan triage

Method: `detect-secrets` 1.5.0 over tracked files, a private-key and token regex over the working tree, a UUID pattern, and history searches. Only location and detector type are recorded; no value is printed here or in any issue.

| Class                                              | Location                                                             | Detector                       | Count | Classification                                                                         |
| -------------------------------------------------- | -------------------------------------------------------------------- | ------------------------------ | ----- | -------------------------------------------------------------------------------------- |
| Kubernetes Secret object names and comments         | `charts/policy-system/values.yaml:338`, three `templates/*.yaml` comments, `scripts/deploy-*.sh`, `scripts/ps-upgrade.sh`, `scripts/sync-llm-secrets-to-kind.sh` | Secret Keyword                 | 10    | Names of Secret objects, not secret content                                            |
| Helm unit-test fixtures                            | `charts/policy-system/tests/*_test.yaml`                             | Secret Keyword, Base64 High Entropy | 21 | Synthetic values asserted by chart tests                                               |
| Python and shell test fixtures                     | `ps-service/tests/**`, `ps-cli/tests/**`                              | Secret Keyword, Hex High Entropy, Basic Auth Credentials | 96 | Fake test values, hashes and a redaction test (`user:pw@example.com` style URLs)       |
| Content hashes                                     | `curated-content/*/manifest.json` lines 2 and 7                      | Hex High Entropy               | 20    | SHA-256 checksum fields                                                                |
| Public certificates in test fixtures               | `ps-service/tests/deploy_eval/conftest.py`, `deploy_lib/conftest.py`, `deploy_lib/test_local_tls.py` | regex `BEGIN CERTIFICATE` (not a key) | 3 | Public certificate bodies built for tests; no private key marker in the tree           |
| UUID-shaped strings                                | only under `ps-service/tests/**` (outside baselines)                 | UUID regex                     | n/a   | Test identifiers and fake IdP responses                                                |
| `.env.example`                                     | `.env.example`                                                       | manual                         | 1     | Placeholder keys only; the API key entry has an empty value; real `.env` is untracked and ignored |

Private-key, access-key, personal-access-token and connection-string regexes over the tree returned no matches, and the history searches returned zero commits.

## False positives and reasons

- `default.code-workspace` looks unreferenced; the init skill names it as a template target (F-H4-02).
- Tracked `.env.example` matches the `.env` file pattern; it is the template for the untracked `.env`.
- `logs/.gitkeep` is tracked on purpose; `.gitignore` re-includes it so the directory exists in a fresh clone.
- Six reference-path misses in the CI scan (F-H6-02).
- `ps-test-plugin/` shows as ignored although no rule names it (F-H1-05).

## Superseded and removed items

Removed lines are exact duplicates of earlier `.gitignore` lines or empty template headings; no file, symbol or path was removed. Nothing is referenced elsewhere, because only rule text was deduplicated and the probe corpus shows identical verdicts. The Markdown files whose execute bit was removed keep their content. Proof carve-out: any removal proof in this repo excludes `docs/audits/` (this document), `.orchestrator/` and untracked `spikes/`.

Removal grep (excludes `.git`, `node_modules`, `.venv`, `.orchestrator`, `spikes`, `docs/audits`):

```sh
grep -rn "default.code-workspace" --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv --exclude-dir=.orchestrator --exclude-dir=spikes --exclude-dir=audits .
```

Result: the README layout row and two lines in `.agents/skills/init-takt-devcontainer/SKILL.md` (template source and target). This is the reason the file was kept, not removed. No other token was removed, so no further removal grep applies; the `.gitignore` proof is the probe corpus (identical verdicts before and after, `git ls-files -ci --exclude-standard` empty).

## Sibling issue review

Every other open sub-issue of #246 and every issue filed by earlier audits was reviewed against this audit's outcomes (deduplicated `.gitignore`, five Markdown mode changes, README layout table, issues #266 and #267). #266 and #267 are native sub-issues of #246.

| issue | subject | outcome |
| ----- | ------- | ------- |
| #249 | test suite value | No change needed: no test file touched |
| #250 | component granularity | No change needed |
| #251 | coding standards conformance | No change needed: no source code touched |
| #252 | secure code and deployment | No change needed: history secret search found nothing (F-H3-02); no vulnerability found |
| #253 | logging and data leakage | No change needed: `logs/` handling unchanged |
| #254 | documentation and container architecture | Commented: README now has a layout table; scope and AC unchanged |
| #255 | API and contract consistency | No change needed |
| #256 | data model and persistence | No change needed |
| #257 | configuration, scripts and Helm | No change needed: `.dockerignore`/`Dockerfile` consistent (F-H5-01) |
| #258 | dependencies and licenses | No change needed: no dependency or lockfile change |
| #259 | observability and operability | No change needed |
| #260 | automated guardrails | Commented: three mechanical hygiene checks added as inputs |
| #263 | symbols reachable only from tests | No change needed |
| #264 | declare starlette and redis | No change needed |
| #265 | remove local spike directories | No change needed: `spikes/` stays untouched and is documented in the README layout table |
| #266 | placeholder in `.git-blame-ignore-revs` | Filed by this audit (F-H4-03) |
| #267 | duplicated reasoning skill | Filed by this audit (F-H6-01) |
