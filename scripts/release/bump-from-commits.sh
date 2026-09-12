#!/usr/bin/env bash
# Classify the commits since the baseline tag into one bump size (AC-BI-005..AC-BI-011).
#
# Usage:
#   git log -z --format='%H%n%s%n%b' "<baseline_tag>..HEAD" | bump-from-commits.sh
#
# stdin  NUL-separated records `sha<LF>header<LF>body` (PLAN A-33).
# stdout exactly one line: bump=<major|minor|patch|none>.
# stderr structured logs (lib/log.sh): header_classified, header_unparsed, bump_decided.
# $GITHUB_STEP_SUMMARY gets a table of every commit (sha, type, bump, warning); a header that
# does not parse gets a warning row naming its SHA and counts as no bump (AC-BI-010).
#
# Rules: the type is read from the header only (PLAN A-32); `!` after type/scope or a body line
# `BREAKING CHANGE: ` / `BREAKING-CHANGE: ` -> major; feat -> minor; fix|perf -> patch; the
# other seven allowed types -> none; the highest bump across all commits wins (AC-BI-009).
# Empty input -> bump=none. Exit 0 in every classified case; exit 2 on a usage error.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/log.sh
source "$script_dir/lib/log.sh"
# shellcheck source=lib/conventional-header.sh
source "$script_dir/lib/conventional-header.sh"

readonly BREAKING_CHANGE_FOOTER_REGEX='^BREAKING[ -]CHANGE: '
readonly EXIT_USAGE=2
readonly USAGE="usage: git log -z --format='%H%n%s%n%b' <range> | $(basename "$0")"
readonly UNPARSED_WARNING="header does not parse as \`$CONVENTIONAL_HEADER_EXPECTED_FORM\`"

highest_bump_size="none"
classified_count=0
unparsed_count=0

# bump_rank <bump_size>: none=0 < patch=1 < minor=2 < major=3.
bump_rank() {
  local bump_size="$1"
  case "$bump_size" in
    major) printf '3' ;;
    minor) printf '2' ;;
    patch) printf '1' ;;
    *) printf '0' ;;
  esac
}

# raise_highest_bump <bump_size>: keeps the highest bump seen so far (AC-BI-009).
raise_highest_bump() {
  local bump_size="$1"
  if (( $(bump_rank "$bump_size") > $(bump_rank "$highest_bump_size") )); then
    highest_bump_size="$bump_size"
  fi
}

# has_breaking_change_footer <body>: true when any body line is a BREAKING CHANGE footer.
has_breaking_change_footer() {
  local body="$1"
  printf '%s\n' "$body" | grep -qE "$BREAKING_CHANGE_FOOTER_REGEX"
}

# bump_size_for <type> <is_breaking>: the bump one parsed header contributes.
bump_size_for() {
  local header_type="$1"
  local is_breaking="$2"
  if [[ "$is_breaking" == "true" ]]; then
    printf 'major'
  elif [[ "$header_type" == "feat" ]]; then
    printf 'minor'
  elif [[ "$header_type" == "fix" || "$header_type" == "perf" ]]; then
    printf 'patch'
  else
    printf 'none'
  fi
}

# escape_table_cell <text>: markdown table cells cannot contain a bare pipe.
escape_table_cell() {
  local text="$1"
  printf '%s' "${text//|/\\|}"
}

# classify_commit <sha> <header> <body>: logs, records a summary row and raises the bump.
classify_commit() {
  local sha="$1"
  local header="$2"
  local body="$3"
  local is_breaking="false"
  local bump_size
  if ! parse_conventional_header "$header"; then
    release_log warn header_unparsed sha="$sha" header="$header"
    summary_append "| \`$sha\` | - | none | :warning: $UNPARSED_WARNING: \`$(escape_table_cell "$header")\` |"
    unparsed_count=$((unparsed_count + 1))
    return 0
  fi
  if [[ "$HEADER_BREAKING" == "true" ]] || has_breaking_change_footer "$body"; then
    is_breaking="true"
  fi
  bump_size="$(bump_size_for "$HEADER_TYPE" "$is_breaking")"
  release_log info header_classified sha="$sha" type="$HEADER_TYPE" breaking="$is_breaking" bump="$bump_size"
  summary_append "| \`$sha\` | $HEADER_TYPE | $bump_size | |"
  classified_count=$((classified_count + 1))
  raise_highest_bump "$bump_size"
}

# split_record <record>: sets record_sha, record_header, record_body from `sha<LF>header<LF>body`.
split_record() {
  local record="$1"
  local after_sha
  record_sha="${record%%$'\n'*}"
  after_sha="${record#*$'\n'}"
  record_header="${after_sha%%$'\n'*}"
  record_body="${after_sha#*$'\n'}"
  if [[ "$record_body" == "$after_sha" ]]; then
    record_body=""
  fi
}

# classify_stream: reads every NUL-terminated record on stdin (tolerating a missing final NUL).
classify_stream() {
  local record record_sha record_header record_body
  while IFS= read -r -d '' record || [[ -n "$record" ]]; do
    split_record "$record"
    classify_commit "$record_sha" "$record_header" "$record_body"
    record=""
  done
}

write_summary_header() {
  summary_append "### bump-from-commits"
  summary_append ""
  summary_append "| sha | type | bump | warning |"
  summary_append "| --- | --- | --- | --- |"
}

write_summary_footer() {
  summary_append ""
  summary_append "Bump: **$highest_bump_size** ($classified_count classified, $unparsed_count unparsed)"
}

main() {
  if [[ $# -ne 0 ]]; then
    printf 'expected no arguments, got %s\n%s\n' "$#" "$USAGE" >&2
    exit "$EXIT_USAGE"
  fi
  write_summary_header
  classify_stream
  write_summary_footer
  release_log info bump_decided bump="$highest_bump_size" classified="$classified_count" unparsed="$unparsed_count"
  printf 'bump=%s\n' "$highest_bump_size"
}

main "$@"
