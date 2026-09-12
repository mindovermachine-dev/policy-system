#!/usr/bin/env bash
# Lint commit headers against the conventional-commit rule (AC-BI-024, AC-BI-025, AC-BI-026).
#
# Usage:
#   lint-commit-header.sh --header "<header text>"        one header (on_ready.yml: ready HEAD)
#   lint-commit-header.sh --range "<before>..<after>"     every commit in the range (on_main.yml)
#
# Range mode lints only <after> (HEAD when <after> is empty) whenever <before> is empty, the
# all-zero SHA GitHub sends for a first push, or unknown to this checkout (workflow_dispatch,
# force-push). Exit 0 when every header parses; exit 1 listing each offending `sha header`
# and the expected form; exit 2 on a usage error. Structured logs go to stderr, a human
# summary to $GITHUB_STEP_SUMMARY (lib/log.sh).
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/log.sh
source "$script_dir/lib/log.sh"
# shellcheck source=lib/conventional-header.sh
source "$script_dir/lib/conventional-header.sh"

readonly ZERO_SHA="0000000000000000000000000000000000000000"
readonly EXIT_USAGE=2
readonly USAGE="usage: $(basename "$0") --header \"<text>\" | --range \"<before>..<after>\""

offending_headers=()
offending_hints=()

usage_error() {
  local reason="$1"
  printf '%s\n%s\n' "$reason" "$USAGE" >&2
  exit "$EXIT_USAGE"
}

# lint_one_header <sha-or-empty> <header>: records the header when it does not parse.
lint_one_header() {
  local sha="$1"
  local header="$2"
  if parse_conventional_header "$header"; then
    release_log info header_accepted sha="$sha" type="$HEADER_TYPE" breaking="$HEADER_BREAKING"
    return 0
  fi
  release_log error header_unparsed sha="$sha" header="$header"
  offending_headers+=("${sha:+$sha }$header")
  local hint
  if hint="$(scope_hint_for_header "$header")"; then
    offending_hints+=("$hint")
  fi
}

# scope_hint_for_header <header>: prints a rewrite suggestion when the header's leading
# token reads like a scope used as the type (`company_merge: ...`, the #32 failure). Only
# the shape `<word>: <description>` qualifies -- the word must not be an allowed type
# (that case is a genuine parse failure elsewhere in the header) and must be a single
# token without spaces or parentheses. Returns 1 when there is nothing to suggest.
scope_hint_for_header() {
  local header="$1"
  if [[ ! "$header" =~ ^([A-Za-z0-9_./-]+):\ (.+)$ ]]; then
    return 1
  fi
  local token="${BASH_REMATCH[1]}"
  local description="${BASH_REMATCH[2]}"
  if [[ "$token" =~ ^($CONVENTIONAL_HEADER_TYPES)$ ]]; then
    return 1
  fi
  printf "'%s' is not a type; if it is the scope, write e.g. 'fix(%s): %s'\n" \
    "$token" "$token" "$description"
}

# is_known_commit <ref>: true when <ref> resolves to a commit in this checkout.
is_known_commit() {
  local ref="$1"
  git cat-file -e "$ref^{commit}" 2>/dev/null
}

# list_range_commits <before> <after>: prints `sha header` per commit, newest first.
list_range_commits() {
  local before="$1"
  local after="${2:-HEAD}"
  if [[ -z "$before" || "$before" == "$ZERO_SHA" ]] || ! is_known_commit "$before"; then
    release_log warn range_fallback_to_head before="$before" after="$after"
    git log -1 --format='%H %s' "$after"
    return 0
  fi
  git log --format='%H %s' "$before..$after"
}

# lint_range "<before>..<after>": lints every commit the range yields.
lint_range() {
  local range="$1"
  local before="${range%%..*}"
  local after="${range#*..}"
  local sha header
  while IFS=' ' read -r sha header; do
    lint_one_header "$sha" "$header"
  done < <(list_range_commits "$before" "$after")
}

report_failure() {
  local offender_count="${#offending_headers[@]}"
  local offender
  printf 'Non-conventional commit header(s):\n'
  printf '  %s\n' "${offending_headers[@]}"
  printf 'expected form: %s\n' "$CONVENTIONAL_HEADER_EXPECTED_FORM"
  local hint
  for hint in "${offending_hints[@]}"; do
    printf 'hint: %s\n' "$hint"
  done
  summary_append "### lint-commit-header: failed"
  summary_append "Non-conventional commit header(s) -- expected form \`$CONVENTIONAL_HEADER_EXPECTED_FORM\`:"
  for offender in "${offending_headers[@]}"; do
    summary_append "- \`$offender\`"
  done
  for hint in "${offending_hints[@]}"; do
    summary_append "- hint: $hint"
  done
  release_log error lint_failed outcome=failed offenders="$offender_count"
}

report_success() {
  summary_append "### lint-commit-header: passed"
  summary_append "Every linted commit header is conventional."
  release_log info lint_passed outcome=passed
}

main() {
  local mode="${1:-}"
  local value="${2:-}"
  if [[ $# -ne 2 ]]; then
    usage_error "expected exactly two arguments, got $#"
  fi
  case "$mode" in
    --header) lint_one_header "" "$value" ;;
    --range)
      [[ "$value" == *..* ]] || usage_error "--range needs the form <before>..<after>"
      lint_range "$value"
      ;;
    *) usage_error "unknown option '$mode'" ;;
  esac
  if (( ${#offending_headers[@]} > 0 )); then
    report_failure
    exit 1
  fi
  report_success
}

main "$@"
