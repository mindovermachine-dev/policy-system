#!/usr/bin/env bash
# Commit, tag and atomically push a release (S8), handling a push race per D-03 (S9).
#
# Usage: publish-release.sh <release_version> <major|minor|patch>
#
# Must run with the repository working-tree root as the current directory (same convention as
# sync-version-files.sh/verify-version-files.sh), after those two scripts have already synced and
# verified the five version-file paths -- this script only commits, tags and pushes them.
#
# 1. Exports GIT_AUTHOR_NAME/EMAIL and GIT_COMMITTER_NAME/EMAIL to the D-06 bot identity for the
#    whole script, so both the release commit and the annotated tag's tagger use it (PLAN A-04).
# 2. Commits exactly the five version-file paths S7 writes, as `chore(release): <release_version>`.
# 3. Runs `${PS_RELEASE_GH:-gh} tt semver bump --<bump_size>`, which tags HEAD (PLAN A-03).
# 4. Asserts the tag resolves to the release commit: `git rev-list -n 1 <release_version> == HEAD`
#    (defense in depth before pushing, AC-BI-015).
# 5. `git push --atomic origin main <release_version>` -- a single atomic push of both refs.
# 6. On rejection (non-fast-forward: `main` moved upstream since checkout, PLAN A-30): exit 1, no
#    retry, no force -- the summary states `main` moved and that the next push to `main`
#    recomputes and self-heals (D-03, AC-BI-019). On success: summary "released".
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/log.sh
source "$script_dir/lib/log.sh"
# shellcheck source=lib/semver.sh
source "$script_dir/lib/semver.sh"

readonly EXIT_USAGE=2
readonly USAGE="usage: $(basename "$0") <release_version> <major|minor|patch>"

# D-06: the bot identity for both the release commit and the annotated tag's tagger (A-04).
export GIT_AUTHOR_NAME="github-actions[bot]"
export GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

# The five version-file paths S7's sync-version-files.sh writes (PLAN A-18/A-20, IMPL_SLICE_4).
# `charts/policy-system/values.yaml` is deliberately excluded (issue #80 AC-BI-012 amendment) --
# it is no longer synced, see sync-version-files.sh's header.
readonly VERSION_FILE_PATHS=(
  "ps-service/pyproject.toml"
  "ps-cli/pyproject.toml"
  "uv.lock"
  "charts/policy-system/Chart.yaml"
  "ps-skills/policy-system/.claude-plugin/plugin.json"
)

# commit_release_files <release_version>: stage exactly the five version-file paths and commit.
commit_release_files() {
  local release_version="$1"
  git add -- "${VERSION_FILE_PATHS[@]}"
  git commit --quiet -m "chore(release): $release_version"
}

# create_release_tag <bump_size>: run gh-tt's bump subcommand, which tags HEAD (PLAN A-03).
create_release_tag() {
  local bump_size="$1"
  "${PS_RELEASE_GH:-gh}" tt semver bump "--$bump_size" >/dev/null
}

# assert_tag_points_at_head <release_version>: defense-in-depth before pushing (AC-BI-015).
assert_tag_points_at_head() {
  local release_version="$1"
  local tag_commit head_commit
  tag_commit="$(git rev-list -n 1 "$release_version")"
  head_commit="$(git rev-parse HEAD)"
  if [[ "$tag_commit" != "$head_commit" ]]; then
    release_log error tag_mismatch outcome=tag_mismatch release_version="$release_version" \
      tag_commit="$tag_commit" head_commit="$head_commit"
    printf 'tag "%s" points at %s, expected HEAD (%s)\n' \
      "$release_version" "$tag_commit" "$head_commit" >&2
    exit 1
  fi
}

report_push_rejected() {
  local release_version="$1"
  release_log error push_rejected outcome=push_rejected release_version="$release_version"
  summary_append "### publish-release: push rejected"
  summary_append \
    "\`main\` moved upstream since checkout; the next push to \`main\` recomputes and self-heals."
  printf 'push rejected: main moved upstream since checkout; the next push to main recomputes and self-heals\n' >&2
}

report_released() {
  local release_version="$1"
  release_log info push_succeeded outcome=released release_version="$release_version"
  summary_append "### publish-release: released"
  summary_append "Released \`$release_version\`."
}

main() {
  if [[ $# -ne 2 ]]; then
    printf 'expected exactly two arguments, got %s\n%s\n' "$#" "$USAGE" >&2
    exit "$EXIT_USAGE"
  fi
  local release_version="$1"
  local bump_size="$2"

  case "$bump_size" in
    major | minor | patch) ;;
    *)
      printf 'bump size must be one of major|minor|patch, got "%s"\n%s\n' "$bump_size" "$USAGE" >&2
      exit "$EXIT_USAGE"
      ;;
  esac

  if ! validate_semver "$release_version"; then
    release_log error version_invalid release_version="$release_version"
    printf 'release_version "%s" is not a valid semver (expected MAJOR.MINOR.PATCH)\n' \
      "$release_version" >&2
    exit 1
  fi

  commit_release_files "$release_version"
  release_log info release_committed outcome=release_committed release_version="$release_version"

  create_release_tag "$bump_size"
  release_log info tag_created outcome=tag_created release_version="$release_version"

  assert_tag_points_at_head "$release_version"

  if ! git push --atomic origin main "$release_version"; then
    report_push_rejected "$release_version"
    exit 1
  fi

  report_released "$release_version"
}

main "$@"
