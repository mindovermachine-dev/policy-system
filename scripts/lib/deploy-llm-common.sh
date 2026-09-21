# shellcheck shell=bash
# Shared naming helpers for scripts/deploy-llm.sh and scripts/sync-llm-secrets-to-kind.sh
# (issue #105) -- both scripts compute the same subscription-hash-derived resource names
# (docs/architecture/customer-azure-llm-bootstrap.md#naming--idempotency).
#
# Sourced, never executed (mode 100644):
#   source "$script_dir/lib/deploy-llm-common.sh"

if [[ -n "${DEPLOY_LLM_COMMON_LIB_LOADED:-}" ]]; then
  return 0
fi
DEPLOY_LLM_COMMON_LIB_LOADED=1

readonly LLM_RESOURCE_GROUP_NAME="rg-policy-system-llm"

# subscription_hash8 <subscription_id>: prints the first 8 hex chars of sha256(subscription_id).
# `printf '%s'`, never `echo`, so no trailing newline is hashed (PLAN.md §0.4) -- this is what
# an evaluator would independently reproduce with `printf '%s' "$SUB_ID" | sha256sum`.
# Picks whichever of `sha256sum` (Linux) / `shasum -a 256` (macOS) this machine actually has,
# same portable pattern as ps-cli/install.sh (AC-BI-008).
subscription_hash8() {
  local subscription_id="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$subscription_id" | sha256sum | cut -c1-8
  else
    printf '%s' "$subscription_id" | shasum -a 256 | cut -c1-8
  fi
}

# llm_account_name <subscription_id>: prints the deterministic AIServices account name.
llm_account_name() {
  local subscription_id="$1"
  printf 'policy-system-llm-%s' "$(subscription_hash8 "$subscription_id")"
}

# llm_keyvault_name <subscription_id>: prints the deterministic Key Vault name.
llm_keyvault_name() {
  local subscription_id="$1"
  printf 'kv-ps-llm-%s' "$(subscription_hash8 "$subscription_id")"
}
