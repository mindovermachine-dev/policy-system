#!/usr/bin/env bash
# Reads the three Azure LLM credentials scripts/deploy-llm.sh wrote into Key Vault and writes
# them into the active local kind cluster as a Kubernetes Secret, ready for charts/policy-system
# to consume (issue #105). See docs/architecture/customer-azure-llm-bootstrap.md for the full
# design; docs/coding-standards/level1-coding-principles.md for the conventions below.
#
# Usage:
#   scripts/sync-llm-secrets-to-kind.sh
#
# Exit codes: 1 preflight/business failure, 0 success. No flags are accepted, so a usage error
# (exit 2, per scripts/deploy-llm.sh's convention) does not apply to this script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/deploy-llm-common.sh
source "${SCRIPT_DIR}/lib/deploy-llm-common.sh"

readonly EXIT_FAILURE=1
readonly SECRET_NAME="policy-system-llm-credentials"

# require_kind_context: aborts before any other call (Azure or kubectl) unless the active
# kubectl context matches kind-* (AC-BI-015) -- protects against writing Azure credentials into
# the wrong cluster if the evaluator has multiple kube contexts configured. Runs first, before
# az account show / az keyvault secret show / any kubectl write (Fail Fast at Boundaries).
require_kind_context() {
  local context
  context="$(kubectl config current-context)"
  if [[ "$context" != kind-* ]]; then
    printf 'Current kubectl context "%s" is not a kind-* context. Switch to your kind cluster and re-run.\n' \
      "$context" >&2
    exit "$EXIT_FAILURE"
  fi
}

# fetch_subscription_id: prints the signed-in az session's subscription id -- the same
# computation deploy-llm.sh uses (scripts/lib/deploy-llm-common.sh), so this script finds the
# same deterministically-named Key Vault a prior deploy-llm.sh run created.
fetch_subscription_id() {
  az account show --query id -o tsv
}

# require_secret_value <vault_name> <secret_name>: prints the secret's currently-stored value, or
# fails clearly if it does not exist yet (deploy-llm.sh has not been run against this
# subscription). Never printed/logged by any caller -- passed straight to kubectl (Security by
# Design: never log secrets, AC-BI-013).
require_secret_value() {
  local vault_name="$1" secret_name="$2"
  local secret_json
  if ! secret_json="$(az keyvault secret show --vault-name "$vault_name" --name "$secret_name" \
      2>/dev/null)"; then
    printf 'Secret %s not found in Key Vault %s. Run scripts/deploy-llm.sh first.\n' \
      "$secret_name" "$vault_name" >&2
    exit "$EXIT_FAILURE"
  fi
  jq -r '.value' <<< "$secret_json"
}

# apply_credentials_secret <api_key> <api_base> <api_version>: writes/updates the Secret via the
# create --dry-run=client -o yaml | apply -f - idiom (idempotent in-place update on rerun,
# AC-BI-017) -- deliberately never passes -n/--namespace to either half; "the active namespace"
# is whatever the current context already resolves to (design doc). Values are only ever passed
# as arguments/piped, never printed (AC-BI-013).
apply_credentials_secret() {
  local api_key="$1" api_base="$2" api_version="$3"
  kubectl create secret generic "$SECRET_NAME" \
    --from-literal="AZURE_API_KEY=$api_key" \
    --from-literal="AZURE_API_BASE=$api_base" \
    --from-literal="AZURE_API_VERSION=$api_version" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

main() {
  require_kind_context

  local subscription_id vault_name
  subscription_id="$(fetch_subscription_id)"
  vault_name="$(llm_keyvault_name "$subscription_id")"

  local api_key api_base api_version
  api_key="$(require_secret_value "$vault_name" "AZURE-API-KEY")"
  api_base="$(require_secret_value "$vault_name" "AZURE-API-BASE")"
  api_version="$(require_secret_value "$vault_name" "AZURE-API-VERSION")"

  apply_credentials_secret "$api_key" "$api_base" "$api_version"

  printf 'Synced %s into the active namespace.\n' "$SECRET_NAME"
}

main "$@"
