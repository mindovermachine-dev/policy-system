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

# require_release_auth_issuer: refuses to upgrade a release that has no stored OIDC issuer.
# Production values are reused from the release, so a missing issuer means there is nothing to
# reuse (never deployed, or deployed without the deploy script) and the upgrade would silently
# fall back to chart defaults.
require_release_auth_issuer() {
  local issuer
  issuer="$(helm get values "$RELEASE_NAME" -o json | jq -r '.psService.auth.issuer')"
  if [[ -z "$issuer" || "$issuer" == "null" ]]; then
    print_error 'Could not read "%s" from the current release'"'"'s values -- refusing to upgrade\n' \
      ".psService.auth.issuer"
    exit "$EXIT_FAILURE"
  fi
}

upgrade_local_test() {
  log_step "Upgrading Policy System (Evaluator/local-test)"
  helm upgrade --install "$RELEASE_NAME" "$CHART_REF" \
    --reset-then-reuse-values \
    --set llm.existingSecret="$LLM_SECRET" --wait --wait-for-jobs
}

upgrade_production() {
  log_step "Checking the current release's auth values"
  require_release_auth_issuer

  log_step "Upgrading Policy System (Production)"
  helm upgrade --install "$RELEASE_NAME" "$CHART_REF" \
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
