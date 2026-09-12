#!/usr/bin/env bash
# Write release_version into the version-lockstep fields across four files (AC-BI-012).
#
# Usage: sync-version-files.sh <release_version>
#
# Must be run with the repository working-tree root as the current directory -- matches how
# release.sh invokes it and how a workflow step's `run:` body executes. Writes:
#   ps-service/pyproject.toml                          version      (uv version --package, +uv.lock)
#   ps-cli/pyproject.toml                              version      (uv version --package, +uv.lock)
#   charts/policy-system/Chart.yaml                    version, appVersion
#   ps-skills/policy-system/.claude-plugin/plugin.json version
#
# `charts/policy-system/values.yaml` is deliberately NOT synced (issue #80 AC-BI-012 amendment):
# the chart template's `psService.image.tag | default .Chart.AppVersion` fallback means
# `Chart.yaml`'s `appVersion` (synced above) is the single source of the image version.
#
# Exit 1 (error log + message on stderr) when <release_version> is not a valid semver
# (lib/semver.sh) -- re-validated here as the security-sink layer (L1 Fail Fast at Boundaries)
# because this value is interpolated into `sed`/`uv` command arguments. Exit 2 on a usage error.
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
readonly PLUGIN_JSON_PATH="ps-skills/policy-system/.claude-plugin/plugin.json"

# sync_uv_package_version <package_name> <release_version>: rewrites <package_name>/pyproject.toml
# `version` and re-locks uv.lock (PLAN A-13) -- never --frozen, which would desync uv.lock (A-14).
sync_uv_package_version() {
  local package_name="$1"
  local release_version="$2"
  uv version --package "$package_name" --no-sync "$release_version" >/dev/null
}

# sync_chart_yaml <release_version>: rewrites Chart.yaml's top-level `version:` (unquoted) and
# `appVersion:` (quoted) fields -- one line each, no other field touched.
sync_chart_yaml() {
  local release_version="$1"
  sed -i -E "s/^version: .*/version: ${release_version}/" "$CHART_YAML_PATH"
  sed -i -E "s/^appVersion: .*/appVersion: \"${release_version}\"/" "$CHART_YAML_PATH"
}

# sync_plugin_json <release_version>: rewrites the single `"version": "..."` field.
sync_plugin_json() {
  local release_version="$1"
  sed -i -E "s/^(  \"version\": \")[^\"]*(\",)/\1${release_version}\2/" "$PLUGIN_JSON_PATH"
}

# assert_files_exist: fail fast, naming the missing path, before any sed/uv sink runs.
assert_files_exist() {
  local path
  for path in "$CHART_YAML_PATH" "$PLUGIN_JSON_PATH"; do
    if [[ ! -f "$path" ]]; then
      release_log error file_missing path="$path"
      printf 'expected "%s" to exist relative to the current directory (run from the repo root)\n' \
        "$path" >&2
      exit 1
    fi
  done
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

  assert_files_exist

  sync_uv_package_version ps-service "$release_version"
  sync_uv_package_version ps-cli "$release_version"
  sync_chart_yaml "$release_version"
  sync_plugin_json "$release_version"

  release_log info files_synced outcome=files_synced release_version="$release_version"
  summary_append "Synced version fields to \`$release_version\`."
}

main "$@"
