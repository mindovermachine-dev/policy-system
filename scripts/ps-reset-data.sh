#!/usr/bin/env bash
# Evaluator/test helper, NOT a production feature: wipes all Policy System data so the next
# `scripts/ps-upgrade.sh` starts from an empty store. Intended flow when testing a new release:
#
#   scripts/ps-reset-data.sh && scripts/ps-upgrade.sh
#
# Deletes the PS Service, FalkorDB and PS Postgres deployments and the FalkorDB and PS Postgres
# data volumes: the graph, the graph mutation log, ingestion runs, audit events, access-role
# grants, the catalog source setting and the passkey signing data. Authentik (users, passkeys,
# the OIDC application) is kept so you do not have to log in again. The upgrade then recreates
# the deleted resources: Helm creates missing objects, Postgres runs its initdb on the empty
# volume, and the provisioning Job reapplies the migrations.
#
# Refuses to run unless the active kubectl context is kind-* (same convention as ps-upgrade.sh).
#
# Usage:
#   scripts/ps-reset-data.sh [--yes]
#
# Options:
#   --yes   Skip the confirmation prompt.
#
# Exit codes: 1 preflight/business failure, 0 success.
set -euo pipefail

if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
  readonly COLOR_RED=$'\033[31m'
  readonly COLOR_RESET=$'\033[0m'
else
  readonly COLOR_RED=""
  readonly COLOR_RESET=""
fi

print_error() {
  local format="$1"
  shift
  # shellcheck disable=SC2059 # the caller supplies the printf format; the colour codes wrap it
  printf "${COLOR_RED}${format}${COLOR_RESET}" "$@" >&2
}

log_step() {
  printf '==> %s\n' "$1" >&2
}

readonly EXIT_FAILURE=1
readonly RELEASE_NAME="policy-system"
readonly DEPLOYMENTS=(
  "${RELEASE_NAME}-ps-service"
  "${RELEASE_NAME}-falkordb"
  "${RELEASE_NAME}-ps-postgres"
)
readonly PVCS=(
  "${RELEASE_NAME}-falkordb-data"
  "${RELEASE_NAME}-ps-postgres-data"
)
readonly DELETE_TIMEOUT="120s"

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    print_error 'This script requires "%s" on PATH.\n' "$1"
    exit "$EXIT_FAILURE"
  }
}

require_local_test() {
  local context
  context="$(kubectl config current-context)"
  if [[ "$context" != kind-* ]]; then
    print_error 'Refusing to reset data: kubectl context "%s" is not a kind-* cluster\n' "$context"
    exit "$EXIT_FAILURE"
  fi
}

confirm() {
  local context answer
  context="$(kubectl config current-context)"
  printf 'This permanently deletes ALL Policy System data in context "%s":\n' "$context" >&2
  printf '  graph, audit events, ingestion runs, access-role grants, passkey signing data.\n' >&2
  printf 'Authentik is kept. Type "yes" to continue: ' >&2
  read -r answer
  if [[ "$answer" != "yes" ]]; then
    print_error 'Aborted\n'
    exit "$EXIT_FAILURE"
  fi
}

reset_data() {
  log_step "Deleting deployments"
  kubectl delete deployment "${DEPLOYMENTS[@]}" --ignore-not-found --wait --timeout="$DELETE_TIMEOUT"

  log_step "Deleting data volumes"
  kubectl delete pvc "${PVCS[@]}" --ignore-not-found --wait --timeout="$DELETE_TIMEOUT"
}

main() {
  local assume_yes=false
  case "${1:-}" in
    "") ;;
    --yes) assume_yes=true ;;
    *)
      print_error 'Unknown argument "%s"\nUsage: scripts/ps-reset-data.sh [--yes]\n' "$1"
      exit "$EXIT_FAILURE"
      ;;
  esac

  require_command kubectl
  require_local_test

  if [[ "$assume_yes" == false ]]; then
    confirm
  fi

  reset_data
  log_step "Data reset. Run scripts/ps-upgrade.sh to recreate the stores"
}

main "$@"
