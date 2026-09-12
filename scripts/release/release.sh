#!/usr/bin/env bash
# Drive the release decision on every push to `main` (AC-BI-001..004, AC-BI-013).
#
# Usage: release.sh   (no arguments; PS_RELEASE_GH overrides the `gh` binary for tests)
#
# 1. Read the baseline release tag via `gh tt semver` and validate it (lib/semver.sh) BEFORE any
#    git command runs (CHANGES X-01) -- an invalid baseline fails fast (exit 1) with no git
#    command issued and no file written.
# 2. Classify every commit since the baseline (`git log -z ... | bump-from-commits.sh`).
# 3. bump=none -> summary "no release", exit 0. No commit, no tag, no version files touched.
# 4. bump!=none -> compute the next release version (next-version.sh), sync the version-lockstep
#    fields (sync-version-files.sh), verify them (verify-version-files.sh), then commit, tag and
#    atomically push the release (publish-release.sh, S8). A push rejected because `main` moved
#    upstream (D-03/S9) exits this driver non-zero with no retry -- the next push to `main`
#    recomputes from the new tip and self-heals.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/log.sh
source "$script_dir/lib/log.sh"
# shellcheck source=lib/semver.sh
source "$script_dir/lib/semver.sh"

readonly EXIT_USAGE=2
readonly USAGE="usage: $(basename "$0")  (no arguments; PS_RELEASE_GH optional)"

# read_baseline_tag: the highest bare-semver release tag, via gh-tt (PLAN A-01).
read_baseline_tag() {
  "${PS_RELEASE_GH:-gh}" tt semver
}

# classify_commits_since <baseline_tag>: prints exactly one line, `bump=<size>`.
classify_commits_since() {
  local baseline_tag="$1"
  git log -z --format='%H%n%s%n%b' "$baseline_tag..HEAD" | "$script_dir/bump-from-commits.sh"
}

# bump_size_from_classification <classification-line>: strips the `bump=` prefix.
bump_size_from_classification() {
  local classification="$1"
  printf '%s' "${classification#bump=}"
}

fail_invalid_baseline() {
  local baseline_tag="$1"
  release_log error version_invalid outcome=invalid_version baseline="$baseline_tag"
  summary_append "### release: failed"
  summary_append "Baseline \`$baseline_tag\` from \`gh tt semver\` is not a valid semver (outcome=invalid_version)."
  printf 'baseline "%s" is not a valid semver (expected MAJOR.MINOR.PATCH)\n' "$baseline_tag" >&2
}

report_no_release() {
  local baseline_tag="$1"
  summary_append "### release: no release"
  summary_append "No release-worthy commits since \`$baseline_tag\`."
  release_log info no_release outcome=no_release baseline="$baseline_tag"
}

report_release_computed() {
  local baseline_tag="$1"
  local bump_size="$2"
  local release_version="$3"
  summary_append "### release: $release_version"
  summary_append "Bump \`$bump_size\` from \`$baseline_tag\` to \`$release_version\`."
  release_log info release_computed outcome=version_computed baseline="$baseline_tag" bump="$bump_size" release_version="$release_version"
}

main() {
  if [[ $# -ne 0 ]]; then
    printf 'expected no arguments, got %s\n%s\n' "$#" "$USAGE" >&2
    exit "$EXIT_USAGE"
  fi

  local baseline_tag
  baseline_tag="$(read_baseline_tag)"
  if ! validate_semver "$baseline_tag"; then
    fail_invalid_baseline "$baseline_tag"
    exit 1
  fi
  release_log info baseline_read baseline="$baseline_tag"

  local classification bump_size
  classification="$(classify_commits_since "$baseline_tag")"
  bump_size="$(bump_size_from_classification "$classification")"

  if [[ "$bump_size" == "none" ]]; then
    report_no_release "$baseline_tag"
    exit 0
  fi

  local release_version
  release_version="$("$script_dir/next-version.sh" "$baseline_tag" "$bump_size")"
  "$script_dir/sync-version-files.sh" "$release_version"
  "$script_dir/verify-version-files.sh" "$release_version"
  "$script_dir/publish-release.sh" "$release_version" "$bump_size"
  report_release_computed "$baseline_tag" "$bump_size" "$release_version"
}

main "$@"
