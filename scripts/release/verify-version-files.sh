#!/usr/bin/env bash
# Read back the version-lockstep fields and fail if any does not equal release_version --
# defense-in-depth before any commit (AC-BI-012/014). Read-only: this script never writes.
#
# Usage: verify-version-files.sh <release_version>
#
# Must be run with the repository working-tree root as the current directory (same convention
# as sync-version-files.sh). Exit 0 iff every field below reads back as <release_version>;
# otherwise exit 1, logging each mismatched field (event=field_stale) and printing every
# offender with its actual value. Exit 2 on a usage error.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/log.sh
source "$script_dir/lib/log.sh"
# shellcheck source=lib/semver.sh
source "$script_dir/lib/semver.sh"

readonly EXIT_USAGE=2
readonly USAGE="usage: $(basename "$0") <release_version>"

readonly PS_SERVICE_PYPROJECT_PATH="ps-service/pyproject.toml"
readonly PS_CLI_PYPROJECT_PATH="ps-cli/pyproject.toml"
readonly CHART_YAML_PATH="charts/policy-system/Chart.yaml"
readonly VALUES_YAML_PATH="charts/policy-system/values.yaml"
readonly PLUGIN_JSON_PATH="ps-skills/policy-system/.claude-plugin/plugin.json"

# read_pyproject_version <path>: the `[project]` `version = "..."` value.
read_pyproject_version() {
  local path="$1"
  sed -nE 's/^version = "([^"]*)"$/\1/p' "$path" | head -n1
}

# read_chart_field <field_name> <path>: a Chart.yaml top-level scalar, quotes stripped.
read_chart_field() {
  local field_name="$1"
  local path="$2"
  sed -nE "s/^${field_name}: \"?([^\"]*)\"?\$/\1/p" "$path" | head -n1
}

# read_values_ps_service_image_tag <path>: `psService.image.tag`, via the same block-tracking
# state machine sync-version-files.sh writes with (read-only here).
read_values_ps_service_image_tag() {
  local path="$1"
  awk '
    BEGIN { in_ps_service = 0; in_image = 0 }
    {
      if ($0 ~ /^[A-Za-z]/) {
        in_ps_service = ($0 ~ /^psService:/) ? 1 : 0
        in_image = 0
      } else if (in_ps_service && $0 ~ /^  [A-Za-z]/) {
        in_image = ($0 ~ /^  image:/) ? 1 : 0
      }
      if (in_ps_service && in_image && $0 ~ /^    tag: /) {
        line = $0
        sub(/^    tag: "/, "", line)
        sub(/"$/, "", line)
        print line
      }
    }
  ' "$path"
}

# read_plugin_json_version <path>: the single `"version": "..."` field.
read_plugin_json_version() {
  local path="$1"
  sed -nE 's/^  "version": "([^"]*)",?$/\1/p' "$path" | head -n1
}

# collect_stale_fields <release_version>: prints one "path:field=actual" line per field that
# does not equal <release_version>; prints nothing when all fields match.
collect_stale_fields() {
  local release_version="$1"
  local actual

  actual="$(read_pyproject_version "$PS_SERVICE_PYPROJECT_PATH")"
  [[ "$actual" == "$release_version" ]] ||
    printf '%s:version=%s\n' "$PS_SERVICE_PYPROJECT_PATH" "$actual"

  actual="$(read_pyproject_version "$PS_CLI_PYPROJECT_PATH")"
  [[ "$actual" == "$release_version" ]] ||
    printf '%s:version=%s\n' "$PS_CLI_PYPROJECT_PATH" "$actual"

  actual="$(read_chart_field version "$CHART_YAML_PATH")"
  [[ "$actual" == "$release_version" ]] ||
    printf '%s:version=%s\n' "$CHART_YAML_PATH" "$actual"

  actual="$(read_chart_field appVersion "$CHART_YAML_PATH")"
  [[ "$actual" == "$release_version" ]] ||
    printf '%s:appVersion=%s\n' "$CHART_YAML_PATH" "$actual"

  actual="$(read_values_ps_service_image_tag "$VALUES_YAML_PATH")"
  [[ "$actual" == "$release_version" ]] ||
    printf '%s:psService.image.tag=%s\n' "$VALUES_YAML_PATH" "$actual"

  actual="$(read_plugin_json_version "$PLUGIN_JSON_PATH")"
  [[ "$actual" == "$release_version" ]] ||
    printf '%s:version=%s\n' "$PLUGIN_JSON_PATH" "$actual"
}

report_stale_fields() {
  local release_version="$1"
  local stale_fields="$2"
  release_log error field_stale outcome=stale_field release_version="$release_version"
  printf 'stale version field(s), expected "%s":\n%s\n' "$release_version" "$stale_fields" >&2
  summary_append "### verify-version-files: failed"
  summary_append "Stale field(s) (expected \`$release_version\`):"
  local field
  while IFS= read -r field; do
    [[ -n "$field" ]] && summary_append "- \`$field\`"
  done <<<"$stale_fields"
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

  local stale_fields
  stale_fields="$(collect_stale_fields "$release_version")"

  if [[ -n "$stale_fields" ]]; then
    report_stale_fields "$release_version" "$stale_fields"
    exit 1
  fi

  release_log info files_verified outcome=verified release_version="$release_version"
}

main "$@"
