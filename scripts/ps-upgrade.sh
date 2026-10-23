#!/usr/bin/env bash
# Combines the operations guide's "Updating to the latest version" steps -- for both Evaluator
# (local-test) and Production -- into one command (see docs/artifacts/operations-guide.md).
# Both branches upgrade with `--reset-then-reuse-values`: the new chart's defaults apply, then the
# values the release already has (everything the deploy script or an operator supplied at install:
# auth, bootstrap owner, Authentik URLs, local-TLS settings) are kept, then the flags below
# override. A plain `helm upgrade` would reset every value not re-supplied to the chart default.
# Environment is detected from the active kubectl context, same kind-* convention
# scripts/sync-llm-secrets-to-kind.sh already uses to distinguish evaluator from a real cluster.
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
  # shellcheck disable=SC2059 # the caller supplies the printf format; the colour codes wrap it
  printf "${COLOR_RED}${format}${COLOR_RESET}" "$@" >&2
}

log_step() {
  printf '==> %s\n' "$1" >&2
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=lib/chart-version.sh
# shellcheck source-path=SCRIPTDIR
source "${SCRIPT_DIR}/lib/chart-version.sh"
chart_version_args=()

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

# require_release_value <jq path>: refuses to upgrade a release that lacks a stored value.
# Production values are reused from the release, so a missing value means there is nothing to
# reuse (never deployed, or deployed without the deploy script) and the upgrade would silently
# fall back to chart defaults.
require_release_value() {
  local path="$1" value
  value="$(helm get values "$RELEASE_NAME" -o json | jq -r "$path")"
  if [[ -z "$value" || "$value" == "null" ]]; then
    print_error 'Could not read "%s" from the current release'"'"'s values -- refusing to upgrade. Fix: run scripts/deploy-ps-prod.sh first; it sets every value the new chart requires.\n' \
      "$path"
    exit "$EXIT_FAILURE"
  fi
}

upgrade_local_test() {
  log_step "Upgrading Policy System (Evaluator/local-test)"
  set_chart_version_args "$SCRIPT_DIR" "$CHART_REF"
  helm upgrade --install "$RELEASE_NAME" "$CHART_REF" \
    ${chart_version_args[@]+"${chart_version_args[@]}"} \
    --reset-then-reuse-values \
    --set llm.existingSecret="$LLM_SECRET" --wait --wait-for-jobs
}

upgrade_production() {
  log_step "Checking the current release's auth values"
  require_release_value '.psService.auth.issuer'
  require_release_value '.psService.publicUrl'

  log_step "Upgrading Policy System (Production)"
  set_chart_version_args "$SCRIPT_DIR" "$CHART_REF"
  helm upgrade --install "$RELEASE_NAME" "$CHART_REF" \
    ${chart_version_args[@]+"${chart_version_args[@]}"} \
    -f "$PROD_VALUES_FILE" \
    --reset-then-reuse-values \
    --set llm.existingSecret="$LLM_SECRET" \
    --wait --wait-for-jobs
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
