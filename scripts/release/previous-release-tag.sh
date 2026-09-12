#!/usr/bin/env bash
# Find the highest bare-semver git tag strictly below <release_version> (S10, PLAN B.1).
#
# Usage: previous-release-tag.sh <release_version>
#
# Must run with the repository working-tree root as the current directory (same convention as
# the other release scripts). <release_version> is a bare semver (e.g. `0.12.0`) -- callers
# resolving a possibly `v`-prefixed tag (create-github-release.sh) strip the prefix first.
#
# 1. Lists every bare-semver tag (`^[0-9]+\.[0-9]+\.[0-9]+$`; pre-release/rc-prefixed tags are
#    excluded) plus <release_version> itself, sorts with `sort -V`, and takes the tag
#    immediately preceding <release_version> in that order -- the baseline `gh tt semver note`
#    diffs from.
# 2. Prints the baseline to stdout on success.
# 3. Exits 1 (logging + a $GITHUB_STEP_SUMMARY line) when no tag exists below <release_version>
#    -- e.g. the very first release.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/log.sh
source "$script_dir/lib/log.sh"
# shellcheck source=lib/semver.sh
source "$script_dir/lib/semver.sh"

readonly EXIT_USAGE=2
readonly USAGE="usage: $(basename "$0") <release_version>"
readonly BARE_SEMVER_PATTERN='^[0-9]+\.[0-9]+\.[0-9]+$'

# find_highest_tag_below <release_version>: highest bare-semver tag strictly less than it.
find_highest_tag_below() {
  local release_version="$1"
  local ordered
  ordered="$(
    { git tag --list; printf '%s\n' "$release_version"; } \
      | grep -E "$BARE_SEMVER_PATTERN" \
      | sort -V
  )"
  printf '%s\n' "$ordered" | grep -x -F -B1 "$release_version" | head -1
}

report_no_baseline() {
  local release_version="$1"
  release_log error no_baseline_tag outcome=no_baseline release_version="$release_version"
  summary_append "### previous-release-tag: no baseline"
  summary_append "No bare-semver tag below \`$release_version\` exists."
  printf 'no bare-semver tag below "%s" exists\n' "$release_version" >&2
}

main() {
  if [[ $# -ne 1 ]]; then
    printf 'expected exactly one argument, got %s\n%s\n' "$#" "$USAGE" >&2
    exit "$EXIT_USAGE"
  fi
  local release_version="$1"

  if ! validate_semver "$release_version"; then
    release_log error version_invalid release_version="$release_version"
    printf 'release_version "%s" is not a valid semver (expected MAJOR.MINOR.PATCH)\n' \
      "$release_version" >&2
    exit 1
  fi

  local baseline
  baseline="$(find_highest_tag_below "$release_version")"

  if [[ -z "$baseline" || "$baseline" == "$release_version" ]]; then
    report_no_baseline "$release_version"
    exit 1
  fi

  release_log info baseline_found outcome=baseline_found release_version="$release_version" \
    baseline="$baseline"
  printf '%s\n' "$baseline"
}

main "$@"
