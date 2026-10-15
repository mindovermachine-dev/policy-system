#!/usr/bin/env bash
# Create or update the backlog triage labels, then delete the retired ones.
#
#   scripts/github/sync-triage-labels.sh [--repo OWNER/NAME] [--dry-run]
#
# The labels, their meanings and the retired set are defined in docs/policy/triage-policy.md
# (section 6). Colours run from dark (first in the work order or most severe) to light.
#
# Order of work: preflight (gh authenticated, write access to the repository), then create or
# update every policy label, then delete the retired labels. Deletion only starts after every
# create/update succeeded, so a failure never leaves the repository with fewer labels than
# before. Re-running is safe: labels that already match are left untouched.
#
# --repo     Target repository (default: the repository of the current directory).
# --dry-run  Print what would change; create, update and delete nothing.
#
# Exit status: 0 when the repository holds the policy labels and no retired label; 1 on any
# failure, with the reason on stderr.

set -euo pipefail

# Name|colour|description. Descriptions are at most 100 characters (the GitHub limit).
POLICY_LABELS=(
  "group:1|b60205|Group 1 Operational: S0-S2 impact for a customer in production. Worked first"
  "group:2a|d93f0b|Group 2a Internal development work: recurring toil and internal debt. Worked second"
  "group:2b|e99695|Group 2b Customer operations work: recurring toil and customer-visible hygiene"
  "group:3|0e8a16|Group 3 New features not required by any Group 1 or 2 issue. Worked last"
  "P0|0a3069|Priority 0: all customers affected (2a: every change)"
  "P1|0969da|Priority 1: most customers affected (2a: weekly)"
  "P2|54aeff|Priority 2: a distinct subset of customers affected (2a: per release or monthly)"
  "P3|b6e3ff|Priority 3: one or a handful of customers affected (2a: rarely)"
  "S0|3b0764|Severity 0: data loss, data corruption, incorrect results (2a: internal data loss)"
  "S1|7e22ce|Severity 1: cannot use the system or a core function, no workaround (2a: blocks dev)"
  "S2|c084fc|Severity 2: function impaired or degraded, a workaround exists"
  "S3|e9d5ff|Severity 3: cosmetic or negligible"
  "needs-triage|fef2c0|Missing Group, P or S. Triage before picking up any work"
  "blocked|000000|Waiting on an external party, a linked issue or a scheduled window (see comment)"
  "override|5319e7|Feature started ahead of open Group 1/2 work with maintainer approval (see comment)"
)

RETIRED_LABELS=("development" "ad hoc" "bug" "enhancement" "documentation")

repo=""
is_dry_run=0
existing_labels=""

# Print an error to stderr and exit 1.
die() {
  echo "error: $*" >&2
  exit 1
}

# Print the usage block from the header comment.
print_usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

# Set `repo` and `is_dry_run` from the command line; exit on an unknown or incomplete argument.
parse_arguments() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --repo)
        [[ $# -ge 2 ]] || die "--repo needs a value"
        repo="$2"
        shift 2
        ;;
      --dry-run)
        is_dry_run=1
        shift
        ;;
      -h | --help)
        print_usage
        exit 0
        ;;
      *) die "unknown argument: $1" ;;
    esac
  done
}

# Resolve `repo` (current directory's repository when not given) and require OWNER/NAME form.
resolve_repository() {
  if [[ -z "$repo" ]]; then
    repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2> /dev/null)" \
      || die "cannot determine the repository: pass --repo OWNER/NAME"
  fi
  [[ "$repo" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] \
    || die "repository must look like OWNER/NAME, got '${repo}'"
}

# Exit unless gh is authenticated and the token can write labels on `repo`. Runs before any change.
require_label_write_access() {
  command -v gh > /dev/null || die "gh CLI not found"
  gh auth status > /dev/null 2>&1 \
    || die "gh is not authenticated: run 'gh auth login' (needs permission to write repository labels)"
  resolve_repository

  local can_push
  can_push="$(gh api "repos/${repo}" --jq '.permissions.push // false' 2> /dev/null)" \
    || die "cannot read ${repo}: the token lacks access to the repository"
  [[ "$can_push" == "true" ]] \
    || die "the token lacks write access to labels on ${repo} (needs the write role, or Issues write on a fine-grained token)"
}

# Load the repository's labels into `existing_labels`, one name<TAB>colour<TAB>description per line.
load_existing_labels() {
  existing_labels="$(gh label list --repo "$repo" --limit 500 --json name,color,description \
    --jq '.[] | [.name, .color, .description] | @tsv')" \
    || die "cannot list labels on ${repo}"
}

# Succeed when `existing_labels` holds a label with exactly this name, colour and description.
has_matching_label() {
  local name="$1" color="$2" description="$3"
  grep -Fxq -- "$(printf '%s\t%s\t%s' "$name" "$color" "$description")" <<< "$existing_labels"
}

# Succeed when `existing_labels` holds a label with this name.
has_label() {
  awk -F'\t' -v name="$1" '$1 == name { found = 1 } END { exit !found }' <<< "$existing_labels"
}

# Create or update one policy label unless it already matches. Succeeds when the label is correct.
upsert_label() {
  local name="$1" color="$2" description="$3"
  if has_matching_label "$name" "$color" "$description"; then
    echo "ok       ${name}"
  elif ((is_dry_run)); then
    echo "would set ${name}"
  elif gh label create "$name" --repo "$repo" --color "$color" --description "$description" --force > /dev/null; then
    echo "set      ${name}"
  else
    echo "error: failed to create or update label '${name}'" >&2
    return 1
  fi
}

# Create or update every policy label; try them all, then fail if any failed.
sync_policy_labels() {
  local has_failed=0 entry name color description
  for entry in "${POLICY_LABELS[@]}"; do
    IFS='|' read -r name color description <<< "$entry"
    upsert_label "$name" "$color" "$description" || has_failed=1
  done
  ((has_failed == 0)) || die "label create/update failed; no retired label was deleted"
}

# Delete one retired label if it exists.
delete_retired_label() {
  local name="$1"
  if ! has_label "$name"; then
    echo "absent   ${name}"
  elif ((is_dry_run)); then
    echo "would delete ${name}"
  else
    gh label delete "$name" --repo "$repo" --yes > /dev/null || die "failed to delete label '${name}'"
    echo "deleted  ${name}"
  fi
}

# Delete every retired label.
delete_retired_labels() {
  local name
  for name in "${RETIRED_LABELS[@]}"; do
    delete_retired_label "$name"
  done
}

main() {
  parse_arguments "$@"
  require_label_write_access
  load_existing_labels
  sync_policy_labels
  delete_retired_labels
}

main "$@"
