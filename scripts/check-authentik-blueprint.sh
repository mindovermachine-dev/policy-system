#!/usr/bin/env bash
# Runs the Authentik blueprint check only when a file it depends on changed.
#
#   scripts/check-authentik-blueprint.sh
#
# The files that matter are the git pathspecs in scripts/authentik-blueprint-triggers.txt. They are
# compared with the merge base of HEAD and origin/main. The check runs, and its exit status is this
# script's, when
#   - AUTHENTIK_BLUEPRINT_FORCE is set to a non-empty value,
#   - the merge base cannot be determined (no origin/main, shallow clone), or
#   - HEAD is the merge base itself, so there is no change set to inspect, or
#   - at least one trigger path differs from the merge base.
# Otherwise it prints one "skipped" line naming the reason and exits 0 without starting anything.
# AUTHENTIK_BLUEPRINT_BASE_REF replaces origin/main as the comparison ref. When origin/main is not
# present locally, one best-effort fetch of it is attempted before giving up on the comparison.
# Every decision is one line on stdout, prefixed "authentik-blueprint:". A run also prints a started
# and a finished line with UTC timestamps; the finished line carries the check's exit status.
set -euo pipefail

TRIGGERS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/authentik-blueprint-triggers.txt"
DEFAULT_REF="origin/main"

run_check() {
  echo "authentik-blueprint: running - $1"
  # -s leaves the environment script's phase lines (stderr, elapsed seconds) uncaptured so a
  # timeout kill still leaves them in the log; --durations=0 lists every test's setup and call time.
  local status=0
  echo "authentik-blueprint: started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  uv run pytest -m authentik_blueprint -q -n0 -s --durations=0 || status=$?
  echo "authentik-blueprint: finished $(date -u +%Y-%m-%dT%H:%M:%SZ) status $status"
  exit "$status"
}

read_pathspecs() {
  local line
  while IFS= read -r line; do
    line="${line%%#*}"
    line="${line%"${line##*[![:space:]]}"}"
    [ -n "$line" ] && printf '%s\n' "$line"
  done <"$TRIGGERS"
  return 0
}

ensure_ref() {
  local ref="$1"
  git rev-parse --verify --quiet "$ref^{commit}" >/dev/null && return 0
  if [ "$ref" = "$DEFAULT_REF" ]; then
    echo "authentik-blueprint: $ref missing, fetching it" >&2
    git fetch --quiet --no-tags origin main 2>/dev/null || true
  fi
  return 0
}

main() {
  [ -n "${AUTHENTIK_BLUEPRINT_FORCE:-}" ] && run_check "forced"

  local ref="${AUTHENTIK_BLUEPRINT_BASE_REF:-$DEFAULT_REF}" base line specs changed
  ensure_ref "$ref"
  base="$(git merge-base HEAD "$ref" 2>/dev/null)" || run_check "base unknown"
  [ "$base" = "$(git rev-parse HEAD)" ] && run_check "HEAD is $ref, no comparison base"

  specs=()
  while IFS= read -r line; do specs+=("$line"); done < <(read_pathspecs)
  changed="$(git diff --name-only "$base" -- "${specs[@]}")"
  if [ -z "$changed" ]; then
    echo "authentik-blueprint: skipped - no trigger path changed since $(git rev-parse --short "$base") (AUTHENTIK_BLUEPRINT_FORCE=1 forces the run)"
    exit 0
  fi
  run_check "changed: $(echo "$changed" | tr '\n' ' ')"
}

main "$@"
