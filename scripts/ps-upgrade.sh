#!/usr/bin/env bash
# Combines the operations guide's "Updating to the latest version" steps -- for both Evaluator
# (local-test) and Production -- into one command (see docs/artifacts/operations-guide.md).
# Manually, production's step requires reading back 5 fields from `helm get values` and
# hand-copying them into the next `helm upgrade` invocation so they aren't reset to chart
# defaults; this script does that read-back and re-supply itself. Environment is detected from
# the active kubectl context, same kind-* convention scripts/sync-llm-secrets-to-kind.sh already
# uses to distinguish evaluator from a real cluster.
#
# Usage:
#   scripts/ps-upgrade.sh
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
  printf "${COLOR_RED}${format}${COLOR_RESET}" "$@" >&2
}

log_step() {
  printf '==> %s\n' "$1" >&2
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

readonly EXIT_FAILURE=1
readonly RELEASE_NAME="policy-system"
readonly CHART_REF="oci://ghcr.io/mindovermachine-dev/charts/policy-system"
readonly LLM_SECRET="policy-system-llm-credentials"
readonly PROD_VALUES_FILE="${REPO_ROOT}/charts/policy-system/values-prod.yaml"

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    print_error 'This script requires "%s" on PATH.\n' "$1"
    exit "$EXIT_FAILURE"
  }
}

is_local_test() {
  [[ "$(kubectl config current-context)" == kind-* ]]
}

update_client() {
  log_step "Updating ps-cli client"
  "${REPO_ROOT}/ps-cli/install.sh"
}

# read_auth_value <jq filter>: prints one field from the currently-deployed release's values,
# failing fast if it's missing/null -- silently --set'ing an empty string would wipe that field
# from the release instead of preserving it (the exact hazard the manual guide step works
# around by hand).
read_auth_value() {
  local filter="$1"
  local value
  value="$(jq -r "$filter" <<<"$current_values")"
  if [[ -z "$value" || "$value" == "null" ]]; then
    print_error 'Could not read "%s" from the current release'"'"'s values -- refusing to upgrade\n' "$filter"
    exit "$EXIT_FAILURE"
  fi
  printf '%s' "$value"
}

upgrade_local_test() {
  log_step "Upgrading Policy System (Evaluator/local-test)"
  helm upgrade --install "$RELEASE_NAME" "$CHART_REF" \
    --set llm.existingSecret="$LLM_SECRET" --wait
}

upgrade_production() {
  log_step "Reading back current auth values"
  local current_values issuer audience cli_client_id scopes
  current_values="$(helm get values "$RELEASE_NAME" -o json)"
  issuer="$(read_auth_value '.psService.auth.issuer')"
  audience="$(read_auth_value '.psService.auth.audience')"
  cli_client_id="$(read_auth_value '.psService.auth.cliClientId')"
  scopes="$(read_auth_value '.psService.auth.scopes')"

  log_step "Upgrading Policy System (Production)"
  helm upgrade --install "$RELEASE_NAME" "$CHART_REF" \
    -f "$PROD_VALUES_FILE" \
    --set llm.existingSecret="$LLM_SECRET" \
    --set psService.auth.issuer="$issuer" \
    --set psService.auth.audience="$audience" \
    --set psService.auth.cliClientId="$cli_client_id" \
    --set psService.auth.scopes="$scopes" \
    --wait
}

verify_pods() {
  log_step "Verifying pods"
  kubectl get pods -l app.kubernetes.io/component=ps-service \
    -o custom-columns='NAME:.metadata.name,IMAGE:.spec.containers[0].image,STATUS:.status.phase'
}

main() {
  require_command kubectl
  require_command helm
  require_command jq

  update_client

  if is_local_test; then
    upgrade_local_test
  else
    upgrade_production
  fi

  verify_pods
}

main "$@"
