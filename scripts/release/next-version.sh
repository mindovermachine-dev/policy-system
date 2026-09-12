#!/usr/bin/env bash
# Compute the next release version from a baseline semver and a bump size (AC-BI-006/007/008).
#
# Usage:
#   next-version.sh <baseline_tag> <major|minor|patch>
#
# stdout exactly one line: the computed release version.
# Exit 1 (error log + message on stderr) when <baseline_tag> is not a valid semver
# (^[0-9]+\.[0-9]+\.[0-9]+$, lib/semver.sh) or when the arithmetic result somehow fails the same
# check (defense in depth at this sink, AC-BI-013). Exit 2 on a usage error.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/log.sh
source "$script_dir/lib/log.sh"
# shellcheck source=lib/semver.sh
source "$script_dir/lib/semver.sh"

readonly EXIT_USAGE=2
readonly USAGE="usage: $(basename "$0") <baseline_tag> <major|minor|patch>"

# increment_semver <baseline_tag> <bump_size>: prints the raw arithmetic result (unvalidated).
increment_semver() {
  local baseline_tag="$1"
  local bump_size="$2"
  local major minor patch
  IFS=. read -r major minor patch <<<"$baseline_tag"
  case "$bump_size" in
    major) printf '%s.0.0' "$((major + 1))" ;;
    minor) printf '%s.%s.0' "$major" "$((minor + 1))" ;;
    patch) printf '%s.%s.%s' "$major" "$minor" "$((patch + 1))" ;;
  esac
}

fail_invalid() {
  local event="$1"
  local field_name="$2"
  local field_value="$3"
  release_log error "$event" "$field_name=$field_value"
  printf '%s "%s" is not a valid semver (expected MAJOR.MINOR.PATCH)\n' "$field_name" "$field_value" >&2
  exit 1
}

main() {
  if [[ $# -ne 2 ]]; then
    printf 'expected exactly two arguments, got %s\n%s\n' "$#" "$USAGE" >&2
    exit "$EXIT_USAGE"
  fi
  local baseline_tag="$1"
  local bump_size="$2"

  case "$bump_size" in
    major | minor | patch) ;;
    *)
      printf 'bump size must be one of major|minor|patch, got "%s"\n%s\n' "$bump_size" "$USAGE" >&2
      exit "$EXIT_USAGE"
      ;;
  esac

  if ! validate_semver "$baseline_tag"; then
    fail_invalid version_invalid baseline "$baseline_tag"
  fi

  local release_version
  release_version="$(increment_semver "$baseline_tag" "$bump_size")"

  if ! validate_semver "$release_version"; then
    fail_invalid version_invalid computed "$release_version"
  fi

  release_log info version_computed baseline="$baseline_tag" bump="$bump_size" release_version="$release_version"
  printf '%s\n' "$release_version"
}

main "$@"
