#!/usr/bin/env bash
# Provisions the customer-managed Azure LLM backend used by ps-service (issue #105):
# resource group, AIServices account, two model deployments, and a Key Vault holding the
# resulting credentials. See docs/architecture/customer-azure-llm-bootstrap.md for the full
# design; docs/coding-standards/level1-coding-principles.md for the conventions below.
#
# Usage:
#   scripts/deploy-llm.sh [--yes] [--rotate-key]
#
#   --yes         Skip the "Proceed with these values? [Y/n]" prompt (the table still prints).
#   --rotate-key  Rotate the Azure Cognitive Services API key currently NOT stored in Key Vault
#                 (the "inactive" slot) and write its new value back. Branches immediately after
#                 flag parsing -- skips config validation, the confirmation table, RBAC
#                 preflight, and region/quota verification entirely (none of those matter for
#                 rotating an already-provisioned account's key).
#
# Exit codes: 2 usage error, 1 validation/preflight/business failure, 0 success -- including
# the evaluator declining at the confirmation prompt and a fully-idempotent no-op rerun.
set -euo pipefail

# Every hard-stop failure message goes through print_error (below), which is red only when
# stderr is a terminal -- piping to a file/CI log leaves plain text, no stray ANSI codes
# (respects NO_COLOR, https://no-color.org).
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
readonly CONFIG_FILE="${SCRIPT_DIR}/llm-defaults.conf"
readonly CONFIG_FILE_DISPLAY_PATH="scripts/llm-defaults.conf"
readonly SUPPORTED_REGIONS=(swedencentral francecentral westeurope germanywestcentral)
readonly POSITIVE_INTEGER_PATTERN='^[1-9][0-9]*$'
readonly CHAT_MODEL_SKU="GlobalStandard"
readonly EMBED_MODEL_SKU="DataZoneStandard"
readonly AZURE_API_VERSION_LITERAL="preview"
readonly USAGE="usage: $(basename "$0") [--yes] [--rotate-key]"

skip_confirmation=false
rotate_key=false
# account_endpoint / made_changes are process-wide state written by ensure_account/ensure_*
# below. They are set via plain assignment inside functions that are always called as a plain
# statement, never wrapped in a `$(...)` command substitution -- that would fork a subshell
# whose variable writes never reach the caller (the exact bug S6 fixed in select_region; see
# IMPL_SLICE_6.md). Functions that only need to return a string and never mutate other state
# (e.g. fetch_account_key1) are still called via `$(...)` as usual.
account_endpoint=""
made_changes=false

# parse_args <args...>: sets skip_confirmation/rotate_key from CLI flags; fails fast otherwise.
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes) skip_confirmation=true ;;
      --rotate-key) rotate_key=true ;;
      *)
        print_error 'unknown flag: %s\n%s\n' "$1" "$USAGE"
        exit "$EXIT_USAGE"
        ;;
    esac
    shift
  done
}

# load_config: sources the checked-in defaults file, failing fast if it is missing.
load_config() {
  if [[ ! -f "$CONFIG_FILE" ]]; then
    print_error '%s: file not found\n' "$CONFIG_FILE_DISPLAY_PATH"
    exit "$EXIT_FAILURE"
  fi
  # shellcheck source=llm-defaults.conf
  source "$CONFIG_FILE"
}

# print_error <format> [args...]: like `printf <format> >&2`, wrapped in COLOR_RED/COLOR_RESET
# (empty strings when stderr isn't a terminal, so this degrades to plain printf). Every hard-stop
# failure message in this script goes through this instead of a bare `printf ... >&2`.
print_error() {
  local format="$1"
  shift
  printf "${COLOR_RED}${format}${COLOR_RESET}" "$@" >&2
}

# log_step <message>: prints a "==> <message>" progress line to stderr. Called right before each
# major phase starts (not after it finishes), so if the script crashes mid-step -- e.g. inside an
# `az` call -- the last line printed names the step that was running, not just a bash line number.
log_step() {
  printf '==> %s\n' "$1" >&2
}

# join_comma_space <items...>: prints items comma-and-space joined, in argument order.
join_comma_space() {
  local joined="" item
  for item in "$@"; do
    if [[ -n "$joined" ]]; then
      joined+=", "
    fi
    joined+="$item"
  done
  printf '%s' "$joined"
}

# fail_validation <message>: prints a field+file-scoped config error and exits (AC-BI-001).
fail_validation() {
  local message="$1"
  print_error '%s: %s\n' "$CONFIG_FILE_DISPLAY_PATH" "$message"
  exit "$EXIT_FAILURE"
}

# is_supported_region <region>: true if region is one of the four vetted EU candidates.
is_supported_region() {
  local region="$1"
  local candidate
  for candidate in "${SUPPORTED_REGIONS[@]}"; do
    [[ "$region" == "$candidate" ]] && return 0
  done
  return 1
}

# validate_region_field <field_name> <region>: fails unless <region> is one of SUPPORTED_REGIONS
# (shared by LLM_REGION and every LLM_REGION_CANDIDATES entry).
validate_region_field() {
  local field_name="$1" region="$2"
  if ! is_supported_region "$region"; then
    fail_validation \
      "$field_name \"$region\" is not one of the supported EU regions: $(join_comma_space "${SUPPORTED_REGIONS[@]}")"
  fi
}

# validate_region_candidates: every LLM_REGION_CANDIDATES entry must be a supported EU region.
validate_region_candidates() {
  local index
  for index in "${!LLM_REGION_CANDIDATES[@]}"; do
    validate_region_field "LLM_REGION_CANDIDATES[$index]" "${LLM_REGION_CANDIDATES[$index]}"
  done
}

# validate_non_empty <field_name> <value>: fails when value is empty after trimming whitespace.
validate_non_empty() {
  local field_name="$1"
  local value="$2"
  local trimmed="${value#"${value%%[![:space:]]*}"}"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
  if [[ -z "$trimmed" ]]; then
    fail_validation "$field_name must not be empty"
  fi
}

# validate_positive_integer <field_name> <value>: fails unless value matches ^[1-9][0-9]*$.
validate_positive_integer() {
  local field_name="$1"
  local value="$2"
  if [[ ! "$value" =~ $POSITIVE_INTEGER_PATTERN ]]; then
    fail_validation "$field_name \"$value\" must be a positive integer"
  fi
}

# validate_config: runs every AC-BI-001 validation rule against the loaded config, in order.
validate_config() {
  validate_region_field "LLM_REGION" "$LLM_REGION"
  validate_region_candidates
  validate_non_empty "LLM_CHAT_MODEL_NAME" "$LLM_CHAT_MODEL_NAME"
  validate_non_empty "LLM_EMBED_MODEL_NAME" "$LLM_EMBED_MODEL_NAME"
  validate_positive_integer "LLM_CHAT_MODEL_CAPACITY" "$LLM_CHAT_MODEL_CAPACITY"
  validate_positive_integer "LLM_EMBED_MODEL_CAPACITY" "$LLM_EMBED_MODEL_CAPACITY"
}

# fetch_subscription_id: prints the signed-in az session's subscription id. Called exactly once
# by main() -- both resource names below derive from this single value (S2 exit criterion: no
# repeated subscription lookups).
fetch_subscription_id() {
  az account show --query id -o tsv
}

# print_confirmation_table <account_name> <vault_name>: prints every resolved value (AC-BI-002).
# The region row shows LLM_REGION -- the single region that will actually be used, not probed
# in any order -- plus the LLM_REGION_CANDIDATES pool that's only consulted for a suggestion if
# LLM_REGION itself turns out not to work (PLAN.md §0.2).
print_confirmation_table() {
  local account_name="$1"
  local vault_name="$2"

  printf 'The following Azure LLM resources will be used:\n\n'
  printf '  Region:                       %s\n' "$LLM_REGION"
  printf '  Fallback candidates:          %s\n' "$(join_comma_space "${LLM_REGION_CANDIDATES[@]}")"
  printf '  Resource group:               %s\n' "$RESOURCE_GROUP_NAME"
  printf '  AIServices account:           %s\n' "$account_name"
  printf '  Chat deployment:              %s (%s, capacity %s)\n' \
    "$LLM_CHAT_MODEL_NAME" "$CHAT_MODEL_SKU" "$LLM_CHAT_MODEL_CAPACITY"
  printf '  Embedding deployment:         %s (%s, capacity %s)\n' \
    "$LLM_EMBED_MODEL_NAME" "$EMBED_MODEL_SKU" "$LLM_EMBED_MODEL_CAPACITY"
  printf '  Key Vault:                    %s\n\n' "$vault_name"
  printf 'Proceed with these values? [Y/n] '
}

# confirm_or_exit: reads the [Y/n] prompt's answer unless --yes was given; on "N"/"n", prints
# where to edit and exits 0 without doing anything else (AC-BI-003) -- nothing past this point
# in main() runs.
confirm_or_exit() {
  if [[ "$skip_confirmation" == true ]]; then
    return 0
  fi
  local confirmation_answer=""
  read -r confirmation_answer || true
  if [[ "$confirmation_answer" =~ ^[Nn] ]]; then
    printf 'No changes made. Edit %s and re-run when ready.\n' "$CONFIG_FILE_DISPLAY_PATH"
    exit 0
  fi
}

# fetch_signed_in_user_upn: prints the signed-in az session's UPN (used as the RBAC preflight's
# --assignee and in its failure message).
fetch_signed_in_user_upn() {
  az account show --query user.name -o tsv
}

# fetch_role_assignments <upn> <subscription_id>: prints the newline-separated role names
# assigned to <upn> at subscription scope.
fetch_role_assignments() {
  local upn="$1" subscription_id="$2"
  az role assignment list --assignee "$upn" --scope "/subscriptions/$subscription_id" \
    --query "[].roleDefinitionName" -o tsv
}

# has_sufficient_role <roles>: true if the newline-separated <roles> contains Owner or
# Contributor (AC-BI-004).
has_sufficient_role() {
  local roles="$1"
  local role
  while IFS= read -r role; do
    [[ "$role" == "Owner" || "$role" == "Contributor" ]] && return 0
  done <<< "$roles"
  return 1
}

# rbac_preflight <subscription_id>: hard-stops unless the signed-in user has Owner or
# Contributor at subscription scope, with an actionable fix command (AC-BI-004). Runs before
# region verification (PLAN.md §0.1 step 5).
rbac_preflight() {
  local subscription_id="$1"
  local upn roles
  upn="$(fetch_signed_in_user_upn)"
  roles="$(fetch_role_assignments "$upn" "$subscription_id")"
  if ! has_sufficient_role "$roles"; then
    print_error 'RBAC preflight failed: %s has neither Owner nor Contributor at subscription scope.\n' \
      "$upn"
    print_error 'Fix: az role assignment create --assignee %s --role Contributor --scope /subscriptions/%s\n' \
      "$upn" "$subscription_id"
    exit "$EXIT_FAILURE"
  fi
}

# model_generally_available <model_list_json> <model_name> <sku>: true if <model_name> is
# GenerallyAvailable at <sku> in <model_list_json>.
model_generally_available() {
  local model_list="$1" model_name="$2" sku="$3"
  jq -e --arg name "$model_name" --arg sku "$sku" \
    'any(.[]; .model.name == $name and .model.lifecycleStatus == "GenerallyAvailable"
      and any(.model.skus[]?; .name == $sku))' \
    <<< "$model_list" >/dev/null
}

# both_models_generally_available <model_list_json>: true if the configured chat and embed
# models are both GenerallyAvailable at their required SKU, per that region's `model list`
# response.
both_models_generally_available() {
  local model_list="$1"
  model_generally_available "$model_list" "$LLM_CHAT_MODEL_NAME" "$CHAT_MODEL_SKU" \
    && model_generally_available "$model_list" "$LLM_EMBED_MODEL_NAME" "$EMBED_MODEL_SKU"
}

# capacity_in_range <model_list_json> <model_name> <sku> <capacity>: true if <capacity> falls
# within <model_name>'s <sku> SKU's [minimum, maximum] range, per <model_list_json>. Boolean
# sibling of validate_model_capacity below -- shared with region_is_viable's fallback probe,
# which needs a plain true/false with no message/exit side effect.
capacity_in_range() {
  local model_list="$1" model_name="$2" sku="$3" capacity="$4"
  local range minimum maximum
  range="$(model_capacity_range "$model_list" "$model_name" "$sku")"
  minimum="${range%% *}"
  maximum="${range##* }"
  (( capacity >= minimum && capacity <= maximum ))
}

# quota_sufficient <usage_json> <usage_key> <capacity>: true if <usage_key>'s remaining quota in
# <usage_json> covers <capacity>. Boolean sibling of validate_model_quota below -- same reuse
# rationale as capacity_in_range.
quota_sufficient() {
  local usage="$1" usage_key="$2" capacity="$3"
  local remaining
  remaining="$(model_remaining_quota "$usage" "$usage_key")"
  (( remaining >= capacity ))
}

# region_is_viable <region>: true only if <region> passes all three checks LLM_REGION itself
# must pass -- both models Generally Available, configured capacities within their SKU ranges,
# and enough remaining quota for both. Used exclusively by fail_region_not_viable's fallback
# probe below, never for LLM_REGION itself (that path needs per-check messages, not a bool).
region_is_viable() {
  local region="$1"
  local model_list usage
  model_list="$(az cognitiveservices model list --location "$region")"
  both_models_generally_available "$model_list" || return 1
  capacity_in_range "$model_list" "$LLM_CHAT_MODEL_NAME" "$CHAT_MODEL_SKU" \
    "$LLM_CHAT_MODEL_CAPACITY" || return 1
  capacity_in_range "$model_list" "$LLM_EMBED_MODEL_NAME" "$EMBED_MODEL_SKU" \
    "$LLM_EMBED_MODEL_CAPACITY" || return 1
  usage="$(az cognitiveservices usage list --location "$region")"
  quota_sufficient "$usage" "chat" "$LLM_CHAT_MODEL_CAPACITY" || return 1
  quota_sufficient "$usage" "embed" "$LLM_EMBED_MODEL_CAPACITY" || return 1
  return 0
}

# fail_region_not_viable <excluded_region>: shared tail call for every way LLM_REGION can fail
# (not Generally Available, capacity out of range, insufficient quota). Probes every OTHER
# LLM_REGION_CANDIDATES entry for full viability (region_is_viable) and reports which ones would
# actually work, instead of the script silently picking one (AC-BI-005/008 superseded: LLM_REGION
# is never auto-switched). Always exits -- callers append no code after invoking this.
fail_region_not_viable() {
  local excluded_region="$1"
  local candidate working=()
  for candidate in "${LLM_REGION_CANDIDATES[@]}"; do
    [[ "$candidate" == "$excluded_region" ]] && continue
    if region_is_viable "$candidate"; then
      working+=("$candidate")
    fi
  done
  if [[ "${#working[@]}" -gt 0 ]]; then
    print_error 'Regions that would work instead: %s\n' "$(join_comma_space "${working[@]}")"
  else
    print_error 'No other candidate region in LLM_REGION_CANDIDATES currently works either.\n'
  fi
  exit "$EXIT_FAILURE"
}

# verify_target_region <region>: checks only that both configured models are Generally
# Available in <region> -- LLM_REGION is used as configured, never auto-switched (AC-BI-005
# superseded). Prints the `model list` response on success so a caller capturing this via
# command substitution (a subshell -- a plain variable set here would not survive back to the
# caller) can reuse it for validate_capacity_range instead of re-querying the same region. Fails
# via fail_region_not_viable, which reports working alternatives, if the models aren't GA.
verify_target_region() {
  local region="$1"
  local model_list
  model_list="$(az cognitiveservices model list --location "$region")"
  if ! both_models_generally_available "$model_list"; then
    print_error 'Region %s does not have both %s (%s) and %s (%s) Generally Available.\n' \
      "$region" "$LLM_CHAT_MODEL_NAME" "$CHAT_MODEL_SKU" "$LLM_EMBED_MODEL_NAME" "$EMBED_MODEL_SKU"
    fail_region_not_viable "$region"
  fi
  printf '%s' "$model_list"
}

# model_capacity_range <model_list_json> <model_name> <sku>: prints "<minimum> <maximum>" for
# <model_name>'s <sku> SKU, per that region's `model list` response. Azure reports
# capacity.minimum as JSON null for SKUs like GlobalStandard/DataZoneStandard (no lower bound
# beyond the positive-integer check validate_config already runs) -- `// 0` / `// 999999999999`
# substitute a real number so the bash arithmetic in capacity_in_range never sees the literal
# string "null" (which crashes under set -u: bash treats an unquoted non-numeric arithmetic
# operand as a variable name, and `null` is never a shell variable).
model_capacity_range() {
  local model_list="$1" model_name="$2" sku="$3"
  jq -r --arg name "$model_name" --arg sku "$sku" \
    '.[] | select(.model.name == $name) | .model.skus[]? | select(.name == $sku)
      | "\(.capacity.minimum // 0) \(.capacity.maximum // 999999999999)"' \
    <<< "$model_list" | head -n1
}

# validate_model_capacity <model_list_json> <field_name> <model_name> <sku> <capacity> <region>:
# hard stops unless <capacity> falls within the [minimum, maximum] range <model_name>'s <sku>
# SKU reports in <model_list_json> (AC-BI-006). Reports working alternative regions via
# fail_region_not_viable rather than a bare exit, since this is one of the three ways LLM_REGION
# itself can fail.
validate_model_capacity() {
  local model_list="$1" field_name="$2" model_name="$3" sku="$4" capacity="$5" region="$6"
  if ! capacity_in_range "$model_list" "$model_name" "$sku" "$capacity"; then
    local range minimum maximum
    range="$(model_capacity_range "$model_list" "$model_name" "$sku")"
    minimum="${range%% *}"
    maximum="${range##* }"
    print_error '%s: %s "%s" is outside the allowed range for %s (%s) in %s: %s-%s\n' \
      "$CONFIG_FILE_DISPLAY_PATH" "$field_name" "$capacity" "$model_name" "$sku" "$region" \
      "$minimum" "$maximum"
    fail_region_not_viable "$region"
  fi
}

# validate_capacity_range <model_list_json> <region>: validates the configured chat/embed
# capacities against <region>'s live-reported ranges (AC-BI-006) -- reuses the `model list`
# response verify_target_region already fetched, no second call for the same region.
validate_capacity_range() {
  local model_list="$1" region="$2"
  validate_model_capacity "$model_list" "LLM_CHAT_MODEL_CAPACITY" "$LLM_CHAT_MODEL_NAME" \
    "$CHAT_MODEL_SKU" "$LLM_CHAT_MODEL_CAPACITY" "$region"
  validate_model_capacity "$model_list" "LLM_EMBED_MODEL_CAPACITY" "$LLM_EMBED_MODEL_NAME" \
    "$EMBED_MODEL_SKU" "$LLM_EMBED_MODEL_CAPACITY" "$region"
}

# model_remaining_quota <usage_json> <usage_key>: prints the remaining quota (limit minus
# current usage) for <usage_key> ("chat" or "embed") in <usage_json>.
model_remaining_quota() {
  local usage="$1" usage_key="$2"
  local current limit
  current="$(jq -r --arg key "$usage_key" \
    '.[] | select(.name.value == $key) | .currentValue' <<< "$usage")"
  limit="$(jq -r --arg key "$usage_key" \
    '.[] | select(.name.value == $key) | .limit' <<< "$usage")"
  printf '%s' "$((limit - current))"
}

# validate_model_quota <usage_json> <usage_key> <field_name> <requested_capacity> <region>
# <account_name>: hard stops with a quota-increase message unless <usage_key>'s remaining quota
# covers <requested_capacity> (AC-BI-007). Reports working alternative regions via
# fail_region_not_viable rather than a bare exit, since this is one of the three ways LLM_REGION
# itself can fail. Also prints the delete/purge commands for <account_name> -- a prior deploy left
# soft-deleted keeps reserving its models' capacity against the subscription's regional quota
# until purged (same gotcha as the Key Vault soft-delete purge documented for teardown), and this
# script's account name is deterministic, so the exact remedy command is always knowable even
# though the script itself only detects quota exhaustion, never resolves it automatically.
validate_model_quota() {
  local usage="$1" usage_key="$2" field_name="$3" requested_capacity="$4" region="$5"
  local account_name="$6"
  local remaining
  remaining="$(model_remaining_quota "$usage" "$usage_key")"
  if (( remaining < requested_capacity )); then
    print_error '%s: insufficient Azure quota for %s: requested %s, only %s remaining in %s.\n' \
      "$CONFIG_FILE_DISPLAY_PATH" "$field_name" "$requested_capacity" "$remaining" "$region"
    print_error 'Request a quota increase for %s, or use one of the working alternatives below.\n' \
      "$region"
    print_error \
      'If a previous deploy left %s soft-deleted, it may still be reserving this quota -- free it with:\n' \
      "$account_name"
    print_error '  az cognitiveservices account delete --name %s --resource-group %s\n' \
      "$account_name" "$RESOURCE_GROUP_NAME"
    print_error '  az cognitiveservices account purge --name %s --resource-group %s --location %s\n' \
      "$account_name" "$RESOURCE_GROUP_NAME" "$region"
    fail_region_not_viable "$region"
  fi
}

# check_quota <region> <account_name>: hard-stops if either model's remaining quota at <region>
# is less than its configured capacity (AC-BI-007) -- runs once, against LLM_REGION only; a
# failure reports working alternatives instead of trying one automatically. <account_name> is
# only used to print the delete/purge remedy in validate_model_quota's failure message.
check_quota() {
  local region="$1" account_name="$2"
  local usage
  usage="$(az cognitiveservices usage list --location "$region")"
  validate_model_quota "$usage" "chat" "LLM_CHAT_MODEL_CAPACITY" "$LLM_CHAT_MODEL_CAPACITY" \
    "$region" "$account_name"
  validate_model_quota "$usage" "embed" "LLM_EMBED_MODEL_CAPACITY" "$LLM_EMBED_MODEL_CAPACITY" \
    "$region" "$account_name"
}

# resource_group_exists: true if the fixed-name resource group already exists.
resource_group_exists() {
  az group show --name "$RESOURCE_GROUP_NAME" >/dev/null 2>&1
}

# ensure_resource_group <region>: create-if-absent (AC-BI-009, AC-BI-010) -- the first link in
# the provisioning chain. Sets made_changes=true only when a create actually happened; this is
# the check-before-act idiom AC-BI-011's idempotent rerun depends on.
ensure_resource_group() {
  local region="$1"
  if resource_group_exists; then
    return 0
  fi
  az group create --name "$RESOURCE_GROUP_NAME" --location "$region" >/dev/null
  made_changes=true
}

# fetch_account_endpoint <account_json>: prints .properties.endpoint from an account
# show/create response.
fetch_account_endpoint() {
  jq -r '.properties.endpoint' <<< "$1"
}

# ensure_account <account_name> <region>: create-if-absent, kind AIServices, SKU S0 (design doc
# step 6). Writes the resolved endpoint into the process-wide `account_endpoint` (top-of-file
# note) rather than returning it on stdout, since this function must also set `made_changes`.
ensure_account() {
  local account_name="$1" region="$2"
  local account_json
  if account_json="$(az cognitiveservices account show --name "$account_name" \
      --resource-group "$RESOURCE_GROUP_NAME" 2>/dev/null)"; then
    account_endpoint="$(fetch_account_endpoint "$account_json")"
    return 0
  fi
  account_json="$(az cognitiveservices account create --name "$account_name" \
    --resource-group "$RESOURCE_GROUP_NAME" --location "$region" --kind AIServices \
    --sku S0 --custom-domain "$account_name" --yes)"
  account_endpoint="$(fetch_account_endpoint "$account_json")"
  made_changes=true
}

# deployment_exists <account_name> <deployment_name>: true if it already exists.
deployment_exists() {
  local account_name="$1" deployment_name="$2"
  az cognitiveservices account deployment show --name "$account_name" \
    --resource-group "$RESOURCE_GROUP_NAME" --deployment-name "$deployment_name" \
    >/dev/null 2>&1
}

# ensure_deployment <account_name> <deployment_name> <sku> <capacity>: create-if-absent, using
# the deployment name as the model name (design doc's own reference deployment does the same,
# e.g. "Deployment: gpt-5.4-mini").
ensure_deployment() {
  local account_name="$1" deployment_name="$2" sku="$3" capacity="$4"
  if deployment_exists "$account_name" "$deployment_name"; then
    return 0
  fi
  az cognitiveservices account deployment create --name "$account_name" \
    --resource-group "$RESOURCE_GROUP_NAME" --deployment-name "$deployment_name" \
    --model-name "$deployment_name" --model-format OpenAI --sku-name "$sku" \
    --sku-capacity "$capacity" >/dev/null
  made_changes=true
}

# keyvault_exists <vault_name>: true if it already exists.
keyvault_exists() {
  local vault_name="$1"
  az keyvault show --name "$vault_name" >/dev/null 2>&1
}

# ensure_keyvault <vault_name> <region>: create-if-absent, standard SKU, access-policy based
# (enableRbacAuthorization false), matching the reference vault (design doc step 8).
ensure_keyvault() {
  local vault_name="$1" region="$2"
  if keyvault_exists "$vault_name"; then
    return 0
  fi
  az keyvault create --name "$vault_name" --resource-group "$RESOURCE_GROUP_NAME" \
    --location "$region" --sku standard --enable-rbac-authorization false >/dev/null
  made_changes=true
}

# fetch_signed_in_user_object_id: prints the signed-in identity's object id (the Key Vault
# access policy's --object-id).
fetch_signed_in_user_object_id() {
  az ad signed-in-user show --query id -o tsv
}

# grant_keyvault_access <vault_name>: grants the deploying identity get/list/set on secrets,
# scoped to this vault only (AC-BI-012). Azure's set-policy is itself idempotent and there is no
# "read current policy" call to check against first, so this always runs on every provisioning
# pass -- it is not a "create" and does not affect AC-BI-011's zero-create-calls proof.
grant_keyvault_access() {
  local vault_name="$1"
  local object_id
  object_id="$(fetch_signed_in_user_object_id)"
  az keyvault set-policy --name "$vault_name" --object-id "$object_id" \
    --secret-permissions get list set >/dev/null
}

# fetch_account_key1 <account_name>: prints the account's current key1 value. Pure read, no
# state mutation -- safe to call via `$(...)`.
fetch_account_key1() {
  local account_name="$1"
  local keys_json
  keys_json="$(az cognitiveservices account keys list --name "$account_name" \
    --resource-group "$RESOURCE_GROUP_NAME")"
  jq -r '.key1' <<< "$keys_json"
}

# read_secret_value <vault_name> <secret_name>: prints the currently-stored value, or an empty
# string if the secret does not exist yet. Never printed/logged by any caller -- passed straight
# into the next `az`/comparison (Security by Design: never log secrets).
read_secret_value() {
  local vault_name="$1" secret_name="$2"
  local secret_json
  if secret_json="$(az keyvault secret show --vault-name "$vault_name" --name "$secret_name" \
      2>/dev/null)"; then
    jq -r '.value' <<< "$secret_json"
  else
    printf ''
  fi
}

# write_secret_if_changed <vault_name> <secret_name> <desired_value>: write-if-changed, never
# unconditional -- `az keyvault secret set` always creates a new version even when the value is
# unchanged, so this read-before-write comparison is what makes an unchanged rerun a true no-op
# (AC-BI-011, PLAN.md §0.5). <desired_value> is never printed or logged, only compared and
# forwarded to `az` (AC-BI-013).
write_secret_if_changed() {
  local vault_name="$1" secret_name="$2" desired_value="$3"
  local current_value
  current_value="$(read_secret_value "$vault_name" "$secret_name")"
  if [[ "$current_value" == "$desired_value" ]]; then
    return 0
  fi
  az keyvault secret set --vault-name "$vault_name" --name "$secret_name" \
    --value "$desired_value" >/dev/null
  made_changes=true
}

# provision_resources <region> <account_name> <vault_name>: the full create-if-absent chain
# (resource group -> account -> both deployments -> Key Vault + access policy) followed by the
# write-if-changed three-secret step (AC-BI-009, AC-BI-010, AC-BI-011, AC-BI-012).
provision_resources() {
  local region="$1" account_name="$2" vault_name="$3"
  local key1

  log_step "Ensuring resource group $RESOURCE_GROUP_NAME"
  ensure_resource_group "$region"
  log_step "Ensuring AIServices account $account_name"
  ensure_account "$account_name" "$region"
  log_step "Ensuring chat deployment $LLM_CHAT_MODEL_NAME"
  ensure_deployment "$account_name" "$LLM_CHAT_MODEL_NAME" "$CHAT_MODEL_SKU" \
    "$LLM_CHAT_MODEL_CAPACITY"
  log_step "Ensuring embedding deployment $LLM_EMBED_MODEL_NAME"
  ensure_deployment "$account_name" "$LLM_EMBED_MODEL_NAME" "$EMBED_MODEL_SKU" \
    "$LLM_EMBED_MODEL_CAPACITY"
  log_step "Ensuring Key Vault $vault_name"
  ensure_keyvault "$vault_name" "$region"
  log_step "Granting Key Vault access to the signed-in identity"
  grant_keyvault_access "$vault_name"

  log_step "Writing secrets to $vault_name"
  key1="$(fetch_account_key1 "$account_name")"
  write_secret_if_changed "$vault_name" "AZURE-API-BASE" "$account_endpoint"
  write_secret_if_changed "$vault_name" "AZURE-API-KEY" "$key1"
  write_secret_if_changed "$vault_name" "AZURE-API-VERSION" "$AZURE_API_VERSION_LITERAL"
}

# print_provisioning_summary: evaluator-visible closing message -- names which secrets now exist
# (never their values, AC-BI-013) and whether anything actually changed (AC-BI-011).
print_provisioning_summary() {
  if [[ "$made_changes" == true ]]; then
    printf 'Azure LLM resources provisioned. Wrote secrets: AZURE-API-BASE, AZURE-API-KEY, AZURE-API-VERSION.\n'
  else
    printf 'Azure LLM resources already up to date -- no changes made.\n'
  fi
}

# require_account_exists <account_name>: fails clearly ("run deploy-llm.sh first") if the
# AIServices account has not been provisioned yet -- --rotate-key has nothing to rotate
# otherwise (PLAN.md §0.6).
require_account_exists() {
  local account_name="$1"
  if ! az cognitiveservices account show --name "$account_name" \
      --resource-group "$RESOURCE_GROUP_NAME" >/dev/null 2>&1; then
    print_error 'Azure AIServices account %s not found. Run scripts/deploy-llm.sh first.\n' \
      "$account_name"
    exit "$EXIT_FAILURE"
  fi
}

# require_keyvault_exists <vault_name>: fails clearly if the Key Vault has not been created yet.
require_keyvault_exists() {
  local vault_name="$1"
  if ! keyvault_exists "$vault_name"; then
    print_error 'Key Vault %s not found. Run scripts/deploy-llm.sh first.\n' "$vault_name"
    exit "$EXIT_FAILURE"
  fi
}

# active_key_slot <stored_value> <key2_value>: prints "key2" if <stored_value> (the currently
# persisted AZURE-API-KEY secret) equals the account's current key2 value, "key1" otherwise --
# including when neither key matches (a from-scratch rotation, PLAN.md §0.6), matching
# deploy-llm.sh's own initial secret write, which always writes key1's value first.
active_key_slot() {
  local stored_value="$1" key2_value="$2"
  if [[ "$stored_value" == "$key2_value" ]]; then
    printf 'key2'
  else
    printf 'key1'
  fi
}

# inactive_key_slot <slot>: prints the other slot name.
inactive_key_slot() {
  local slot="$1"
  if [[ "$slot" == "key1" ]]; then
    printf 'key2'
  else
    printf 'key1'
  fi
}

# rotate_key_main: `--rotate-key` mode (AC-BI-014). Reads the stored AZURE-API-KEY secret,
# compares it against the account's live key1/key2 to find the active slot, and regenerates the
# inactive one. Never prints an old or new key value (AC-BI-013).
rotate_key_main() {
  local subscription_id account_name vault_name
  subscription_id="$(fetch_subscription_id)"
  account_name="$(llm_account_name "$subscription_id")"
  vault_name="$(llm_keyvault_name "$subscription_id")"

  log_step "Checking AIServices account and Key Vault exist"
  require_account_exists "$account_name"
  require_keyvault_exists "$vault_name"

  local stored_value keys_json key2_value active_slot inactive_slot new_value
  log_step "Determining active key slot"
  stored_value="$(read_secret_value "$vault_name" "AZURE-API-KEY")"
  keys_json="$(az cognitiveservices account keys list --name "$account_name" \
    --resource-group "$RESOURCE_GROUP_NAME")"
  key2_value="$(jq -r '.key2' <<< "$keys_json")"
  active_slot="$(active_key_slot "$stored_value" "$key2_value")"
  inactive_slot="$(inactive_key_slot "$active_slot")"

  log_step "Regenerating inactive key slot ($inactive_slot)"
  keys_json="$(az cognitiveservices account keys regenerate --name "$account_name" \
    --resource-group "$RESOURCE_GROUP_NAME" --key-name "$inactive_slot")"
  new_value="$(jq -r --arg slot "$inactive_slot" '.[$slot]' <<< "$keys_json")"

  log_step "Writing rotated key to $vault_name"
  az keyvault secret set --vault-name "$vault_name" --name "AZURE-API-KEY" \
    --value "$new_value" >/dev/null

  printf 'Rotated %s (was inactive); %s remains active. AZURE-API-KEY updated in Key Vault.\n' \
    "$inactive_slot" "$active_slot"
}

main() {
  parse_args "$@"

  if [[ "$rotate_key" == true ]]; then
    rotate_key_main
    return
  fi

  log_step "Loading and validating $CONFIG_FILE_DISPLAY_PATH"
  load_config
  validate_config

  local subscription_id
  subscription_id="$(fetch_subscription_id)"

  local account_name vault_name
  account_name="$(llm_account_name "$subscription_id")"
  vault_name="$(llm_keyvault_name "$subscription_id")"

  print_confirmation_table "$account_name" "$vault_name"
  confirm_or_exit

  log_step "Checking RBAC role assignment on subscription $subscription_id"
  rbac_preflight "$subscription_id"

  local region="$LLM_REGION"
  local model_list
  log_step "Checking model availability in $region"
  model_list="$(verify_target_region "$region")"
  log_step "Validating configured capacity against $region's reported ranges"
  validate_capacity_range "$model_list" "$region"
  log_step "Checking remaining Azure quota in $region"
  check_quota "$region" "$account_name"

  log_step "Provisioning Azure resources in $region"
  provision_resources "$region" "$account_name" "$vault_name"
  print_provisioning_summary
}

main "$@"
