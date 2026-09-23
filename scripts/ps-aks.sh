#!/usr/bin/env bash
# Starts or stops the AKS cluster provisioned by scripts/deploy-ps.sh -- `az aks stop` pauses
# node compute billing without deleting anything (the FalkorDB PVC, Key Vault, and Entra app
# registrations are untouched); `az aks start` resumes it. See
# docs/artifacts/operations-guide.md#start-and-stop-the-aks-cluster for the manual command
# sequence this wraps, and its caveats (PS Service/ps-cli unreachable while stopped, don't stop
# mid-ingestion) -- this script doesn't check ingestion state, that's still the operator's call.
#
# Reuses RESOURCE_GROUP_NAME and aks_cluster_name() from lib/deploy-llm-common.sh so the
# resolved cluster name always matches what deploy-ps.sh itself provisioned -- no `az aks list`
# guessing.
#
# Usage:
#   scripts/ps-aks.sh start
#   scripts/ps-aks.sh stop
#
# Exit codes: 2 usage error, 1 failure (e.g. cluster not found), 0 success.
set -euo pipefail

# Same COLOR_RED/print_error/log_step discipline as scripts/deploy-ps.sh -- small and
# self-contained enough that every script here copies it rather than sharing it (PLAN.md §0.10).
if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
  readonly COLOR_RED=$'\033[31m'
  readonly COLOR_RESET=$'\033[0m'
else
  readonly COLOR_RED=""
  readonly COLOR_RESET=""
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/deploy-llm-common.sh
source "${SCRIPT_DIR}/lib/deploy-llm-common.sh"

readonly EXIT_USAGE=2
readonly EXIT_FAILURE=1
readonly USAGE="usage: $(basename "$0") {start|stop}"

print_error() {
  local format="$1"
  shift
  printf "${COLOR_RED}${format}${COLOR_RESET}" "$@" >&2
}

log_step() {
  printf '==> %s\n' "$1" >&2
}

# fetch_subscription_id: prints the signed-in az session's subscription id -- same call as
# deploy-ps.sh's own, needed to derive the same deterministic cluster name.
fetch_subscription_id() {
  az account show --query id -o tsv
}

# require_cluster_exists <cluster_name>: fails clearly (not on az aks stop/start's own error
# text) if deploy-ps.sh hasn't provisioned a cluster yet.
require_cluster_exists() {
  local cluster_name="$1"
  if ! az aks show --name "$cluster_name" --resource-group "$RESOURCE_GROUP_NAME" >/dev/null 2>&1; then
    print_error 'AKS cluster %s not found in resource group %s. Run scripts/deploy-ps.sh first.\n' \
      "$cluster_name" "$RESOURCE_GROUP_NAME"
    exit "$EXIT_FAILURE"
  fi
}

cmd_start() {
  local cluster_name="$1"
  log_step "Starting AKS cluster $cluster_name"
  az aks start --name "$cluster_name" --resource-group "$RESOURCE_GROUP_NAME"
  log_step "AKS cluster $cluster_name started -- allow a minute or two before pods report Ready"
}

cmd_stop() {
  local cluster_name="$1"
  log_step "Stopping AKS cluster $cluster_name"
  az aks stop --name "$cluster_name" --resource-group "$RESOURCE_GROUP_NAME"
  log_step "AKS cluster $cluster_name stopped -- PS Service and ps-cli are unreachable until started again"
}

main() {
  if [[ $# -ne 1 ]]; then
    print_error '%s\n' "$USAGE"
    exit "$EXIT_USAGE"
  fi

  local action="$1"
  case "$action" in
    start|stop) ;;
    *)
      print_error 'unknown action: %s\n%s\n' "$action" "$USAGE"
      exit "$EXIT_USAGE"
      ;;
  esac

  local subscription_id cluster_name
  subscription_id="$(fetch_subscription_id)"
  cluster_name="$(aks_cluster_name "$subscription_id")"

  require_cluster_exists "$cluster_name"

  case "$action" in
    start) cmd_start "$cluster_name" ;;
    stop) cmd_stop "$cluster_name" ;;
  esac
}

main "$@"
