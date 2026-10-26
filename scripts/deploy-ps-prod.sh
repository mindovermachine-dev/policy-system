#!/usr/bin/env bash
# Provisions a full customer-managed Azure deployment of Policy System.
#
# Usage:
#   scripts/deploy-ps-prod.sh [--yes] [--owner-email <address>]
#   scripts/deploy-ps-prod.sh --rotate-key
#   scripts/deploy-ps-prod.sh --reset-owner-passkey [--owner-email <address>] [--yes]
#
#   --yes         Skip the "Proceed with these values? [Y/n]" prompt (the table still prints).
#                 The owner email is then never prompted for: pass --owner-email (or set
#                 AUTHZ_OWNER_EMAIL in scripts/ps-defaults.conf).
#   --owner-email The first SystemOwner. Prompted for when neither this flag nor
#                 AUTHZ_OWNER_EMAIL gives one. The address becomes an Authentik administrator
#                 (username == email, no password) and the OIDC `sub` PS Service expects, so the
#                 deploy sets the whole bootstrap identity in one pass; a single-use passkey
#                 enrolment link is printed at the end. Re-running issues a fresh link to an
#                 owner with no passkey; a lost or stolen device is recovered with
#                 --reset-owner-passkey.
#   --rotate-key  Rotate the Azure Cognitive Services API key currently NOT stored in Key Vault
#                 (the "inactive" slot) and write its new value back. Branches immediately after
#                 flag parsing -- skips config validation, the confirmation table, RBAC
#                 preflight, and region/quota/AKS/Helm provisioning entirely (none of those
#                 matter for rotating an already-provisioned account's key). Fails clearly if run
#                 before a first successful deploy (require_account_exists/require_keyvault_exists
#                 below).
#   --reset-owner-passkey
#                 Lost or stolen device: deletes every Authentik device, session, OAuth and
#                 app-password/API token of the owner, clears the owner's signing passkeys and
#                 voids their pending approvals in PS Service, then prints one fresh single-use
#                 enrolment link. Changes nothing else. Works on the release in the current kube
#                 context with no Azure call and no config file; cannot be combined with
#                 --rotate-key, and --yes needs --owner-email.
#
# Authentik's own Django `secret_key`/Postgres password and the PS Postgres
# (admin/state/signing) passwords are not generated or synced by this script -- the
# Helm chart generates and persists them (a `lookup`+`randAlphaNum` idiom in
# charts/policy-system/templates/authentik-credentials-secret.yaml/ps-postgres-secret.yaml),
# created/updated as ordinary chart-rendered resources by `helm upgrade --install` (below, in
# ensure_release). This script makes no Key Vault or kubectl calls for either secret.
#
# Exit codes: 2 usage error, 1 validation/preflight/business failure, 0 success -- including
# the evaluator declining at the confirmation prompt and a fully-idempotent no-op rerun.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail

# Every hard-stop failure message goes through print_error (below), which is red only when
# stderr is a terminal -- piping to a file/CI log leaves plain text, no stray ANSI codes
# (respects NO_COLOR, https://no-color.org). Same few lines as scripts/deploy-llm.sh: small and
# self-contained, so each script keeps its own copy rather than sourcing a shared lib.
if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
  readonly COLOR_RED=$'\033[31m'
  readonly COLOR_RESET=$'\033[0m'
else
  readonly COLOR_RED=""
  readonly COLOR_RESET=""
fi

chart_version_args=()
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/deploy-llm-common.sh
source "${SCRIPT_DIR}/lib/deploy-llm-common.sh"
# Shared with scripts/deploy-ps-eval.sh: creates the first Authentik owner and
# issues the passkey-enrolment link.
# shellcheck source=lib/authentik-owner.sh
source "${SCRIPT_DIR}/lib/authentik-owner.sh"
# shellcheck source=lib/chart-version.sh
source "${SCRIPT_DIR}/lib/chart-version.sh"

readonly EXIT_USAGE=2
readonly EXIT_FAILURE=1
readonly CONFIG_FILE="${SCRIPT_DIR}/ps-defaults.conf"
readonly CONFIG_FILE_DISPLAY_PATH="scripts/ps-defaults.conf"
readonly SUPPORTED_REGIONS=(swedencentral francecentral westeurope germanywestcentral)
readonly POSITIVE_INTEGER_PATTERN='^[1-9][0-9]*$'
# Matches scripts/deploy-llm.sh's own AZURE_API_VERSION_LITERAL -- not evaluator-
# tunable there either; the API version is a platform constant, not a per-subscription choice.
readonly AZURE_API_VERSION_LITERAL="preview"
USAGE="usage: $(basename "$0") [--yes] [--owner-email <address>] | --rotate-key | --reset-owner-passkey [--owner-email <address>] [--yes]"
readonly USAGE

skip_confirmation=false
rotate_key=false
reset_owner_passkey=false
# Read by lib/authentik-owner.sh: the owner's email, the --yes switch that turns its
# prompt off, and the API/link bases + curl arguments for the owner-creation calls.
OWNER_EMAIL=""
SKIP_OWNER_PROMPT=false
AUTHENTIK_API_BASE=""
AUTHENTIK_LINK_BASE=""
AUTHENTIK_CURL_ARGS=()
# account_endpoint / made_changes are process-wide state written by ensure_account/ensure_*
# below. Set via plain assignment inside functions that are always called as a plain
# statement, never wrapped in a `$(...)` command substitution -- that would fork a subshell
# whose variable writes never reach the caller (same discipline as scripts/deploy-llm.sh's own
# top-of-file note). made_changes is set by every ensure_*/adopt_* function that changes
# something and read by the closing summary.
account_endpoint=""
made_changes=false
# public_hostname is set by main() below (fetch_public_ip_fqdn); the release values and the
# closing summary line derive from it. Same plain-assignment state-sharing
# discipline as every other process-wide variable above.
public_hostname=""

# parse_args <args...>: sets skip_confirmation/rotate_key/reset_owner_passkey from CLI flags;
# fails fast otherwise. --rotate-key and --reset-owner-passkey are checked in main() BEFORE any
# provisioning step runs, the same shape as scripts/deploy-llm.sh's parse_args/main() -- see
# rotate_key_main and reset_owner_passkey_main below.
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes) skip_confirmation=true ;;
      --rotate-key) rotate_key=true ;;
      --reset-owner-passkey) reset_owner_passkey=true ;;
      -h | --help)
        printf '%s\n\n%s\n' "$USAGE" "$OWNER_PASSKEY_RESET_HELP"
        exit 0
        ;;
      --owner-email)
        if [[ $# -lt 2 || -z "$2" ]]; then
          print_error '--owner-email needs a value\n%s\n' "$USAGE"
          exit "$EXIT_USAGE"
        fi
        OWNER_EMAIL="$2"
        shift
        ;;
      *)
        print_error 'unknown flag: %s\n%s\n' "$1" "$USAGE"
        exit "$EXIT_USAGE"
        ;;
    esac
    shift
  done
  if [[ "$reset_owner_passkey" == true && "$rotate_key" == true ]]; then
    print_error '%s cannot be combined with %s\n%s\n' --reset-owner-passkey --rotate-key "$USAGE"
    exit "$EXIT_USAGE"
  fi
  if [[ "$reset_owner_passkey" == true && "$skip_confirmation" == true && -z "$OWNER_EMAIL" ]]; then
    print_error '%s --yes needs --owner-email <address>\n%s\n' --reset-owner-passkey "$USAGE"
    exit "$EXIT_USAGE"
  fi
}

# print_experimental_notice: states on stderr that this production path is experimental and not
# verified on AKS, and where the evidence lives. Printed before any Azure resource is
# created; the wording matches the production banner in docs/artifacts/installation-guide.md.
print_experimental_notice() {
  {
    printf 'EXPERIMENTAL: this production path is not verified on AKS.\n'
    printf 'It was verified on kind only: the AKS managed NGINX add-on, real Let'"'"'s Encrypt issuance\n'
    printf 'and real Azure resources were not. Do not rely on it for a real deployment.\n'
    printf 'See docs/artifacts/installation-guide.md#verification-status\n\n'
  } >&2
}

# load_config: sources the checked-in defaults file, failing fast if it is missing.
load_config() {
  if [[ ! -f "$CONFIG_FILE" ]]; then
    print_error '%s: file not found\n' "$CONFIG_FILE_DISPLAY_PATH"
    exit "$EXIT_FAILURE"
  fi
  # shellcheck source=ps-defaults.conf
  source "$CONFIG_FILE"
  # A config file may lack AUTHZ_OWNER_EMAIL (AUTHZ_BOOTSTRAP_OWNER_* keys in it are ignored);
  # default it blank so the prompt (rather than `set -u`'s unbound-variable error) is what an
  # operator with such a file sees.
  : "${AUTHZ_OWNER_EMAIL:=}"
}

# print_error <format> [args...]: like `printf <format> >&2`, wrapped in COLOR_RED/COLOR_RESET
# (empty strings when stderr isn't a terminal, so this degrades to plain printf). Every hard-stop
# failure message in this script goes through this instead of a bare `printf ... >&2`.
print_error() {
  local format="$1"
  shift
  # shellcheck disable=SC2059 # the caller's format is deliberately wrapped in the colour codes
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

# check_required_tools <tool...>: hard-stops unless every named CLI is on PATH, listing all
# missing tools in one message rather than failing on the first -- so a missing binary is caught
# before any Azure resource is touched instead of partway through provisioning (kubectl/helm
# are first used only after the AKS cluster and everything before it exist). See docs/artifacts/installation-guide.md#prerequisites-production for install links.
check_required_tools() {
  local tool missing=()
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    print_error 'Missing required tool(s): %s\n' "$(join_comma_space "${missing[@]}")"
    print_error 'See docs/artifacts/installation-guide.md#prerequisites-production for install links.\n'
    exit "$EXIT_FAILURE"
  fi
}

# fail_validation <message>: prints a field+file-scoped config error and exits before any Azure
# call.
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

# validate_region_candidates: every LLM_REGION_CANDIDATES entry must be a supported EU region.
# Unlike deploy-llm.sh, ps-defaults.conf has no single LLM_REGION field -- region *selection*
# runs later (select_region); this only validates the candidate pool itself.
validate_region_candidates() {
  local index region
  for index in "${!LLM_REGION_CANDIDATES[@]}"; do
    region="${LLM_REGION_CANDIDATES[$index]}"
    if ! is_supported_region "$region"; then
      fail_validation \
        "LLM_REGION_CANDIDATES[$index] \"$region\" is not one of the supported EU regions: $(join_comma_space "${SUPPORTED_REGIONS[@]}")"
    fi
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

# prompt_for_tls_contact_email: interactively asks the evaluator for TLS_CONTACT_EMAIL when the
# config file left it blank -- unlike the LLM_* defaults, there's no sensible value to bake into
# a checked-in file (it's the operator's own address, used only for Let's Encrypt's
# expiry/revocation notices). Skipped entirely if the config already set it, so a fully
# pre-filled config (or --yes in a non-interactive context, once TLS_CONTACT_EMAIL is filled in)
# never blocks on stdin. Runs before validate_config so the prompted value is validated too.
prompt_for_tls_contact_email() {
  if [[ -n "$TLS_CONTACT_EMAIL" ]]; then
    return 0
  fi
  printf "Contact email for Let's Encrypt certificate notices (TLS_CONTACT_EMAIL): "
  read -r TLS_CONTACT_EMAIL || true
}

# resolve_owner_email: the owner email comes from --owner-email, else the config's
# AUTHZ_OWNER_EMAIL, else an interactive prompt (never with --yes) -- like TLS_CONTACT_EMAIL there
# is no sensible value to bake into a checked-in file. The address is validated up front, before any
# `az` call. It is both the Authentik username and the `sub` PS Service's bootstrap expects
# (`sub_mode: user_username`), so no first login is needed to learn it and one deploy sets the
# whole bootstrap identity.
resolve_owner_email() {
  if [[ -z "$OWNER_EMAIL" ]]; then
    OWNER_EMAIL="$AUTHZ_OWNER_EMAIL"
  fi
  SKIP_OWNER_PROMPT="$skip_confirmation"
  prompt_owner_email || exit "$EXIT_FAILURE"
}

# validate_config: runs every config validation rule against the loaded config, in order.
# LLM_CHAT_MODEL_SKU/LLM_EMBED_MODEL_SKU are new fields vs. deploy-llm.sh (whose SKUs are
# hardcoded literals, not evaluator-tunable there) -- SKU availability depends on the
# subscription's quota, so it is tunable, and non-emptiness must be checked here.
validate_config() {
  validate_region_candidates
  validate_non_empty "LLM_CHAT_MODEL_NAME" "$LLM_CHAT_MODEL_NAME"
  validate_non_empty "LLM_CHAT_MODEL_SKU" "$LLM_CHAT_MODEL_SKU"
  validate_positive_integer "LLM_CHAT_MODEL_CAPACITY" "$LLM_CHAT_MODEL_CAPACITY"
  validate_non_empty "LLM_EMBED_MODEL_NAME" "$LLM_EMBED_MODEL_NAME"
  validate_non_empty "LLM_EMBED_MODEL_SKU" "$LLM_EMBED_MODEL_SKU"
  validate_positive_integer "LLM_EMBED_MODEL_CAPACITY" "$LLM_EMBED_MODEL_CAPACITY"
  validate_non_empty "TLS_CONTACT_EMAIL" "$TLS_CONTACT_EMAIL"
}

# fetch_subscription_id: prints the signed-in az session's subscription id. Called exactly once
# by main() -- every resource name below derives from this single value (no repeated
# subscription lookups, same discipline as deploy-llm.sh).
fetch_subscription_id() {
  az account show --query id -o tsv
}

# print_confirmation_table <account_name> <vault_name> <cluster_name> <dns_label>: prints every
# deterministically-computed resource name, before any Azure change. The region row shows the
# configured candidate list, not a single resolved region -- region selection runs after the
# confirmation (select_region), same convention as deploy-llm.sh's own table.
print_confirmation_table() {
  local account_name="$1"
  local vault_name="$2"
  local cluster_name="$3"
  local dns_label="$4"

  printf 'The following Azure resources will be used:\n\n'
  printf '  Region candidates (in order): %s\n' "$(join_comma_space "${LLM_REGION_CANDIDATES[@]}")"
  printf '  Resource group:                %s\n' "$RESOURCE_GROUP_NAME"
  printf '  AIServices account:            %s\n' "$account_name"
  printf '  Chat deployment:               %s (%s, capacity %s)\n' \
    "$LLM_CHAT_MODEL_NAME" "$LLM_CHAT_MODEL_SKU" "$LLM_CHAT_MODEL_CAPACITY"
  printf '  Embedding deployment:          %s (%s, capacity %s)\n' \
    "$LLM_EMBED_MODEL_NAME" "$LLM_EMBED_MODEL_SKU" "$LLM_EMBED_MODEL_CAPACITY"
  printf '  Key Vault:                     %s\n' "$vault_name"
  printf '  AKS cluster:                   %s\n' "$cluster_name"
  printf '  Public DNS label:              %s\n\n' "$dns_label"
  printf 'Proceed with these values? [Y/n] '
}

# confirm_or_exit: reads the [Y/n] prompt's answer unless --yes was given; on "N"/"n", prints
# where to edit and exits 0 without doing anything else -- nothing past this point in main() runs.
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
# --assignee and in its failure message). Same shape as scripts/deploy-llm.sh's: user-only, no
# service-principal branch.
fetch_signed_in_user_upn() {
  az account show --query user.name -o tsv
}

# az_no_pathconv <args...>: runs `az` with Git Bash's MSYS path conversion disabled for this one
# call. Git Bash auto-rewrites any argument that looks like a POSIX absolute path (leading "/")
# into a Windows path before a non-MSYS binary ever sees it -- helpful for a real filesystem path,
# but it mangles an Azure resource ID/scope like "/subscriptions/..." into garbage. This script has
# exactly one real filesystem path (VALUES_PROD_FILE, passed to `helm -f` in ensure_release) that
# depends on that conversion happening, so MSYS_NO_PATHCONV can't be exported globally (that
# breaks the `helm -f` lookup under Git Bash on Windows) -- it must be scoped to only the
# `az` calls below that take a leading-"/" resource ID: fetch_role_assignments,
# grant_aks_rbac_access's role assignment create, and the `--ids`-based public-IP calls
# (ensure_dns_label/fetch_public_ip_fqdn).
az_no_pathconv() {
  MSYS_NO_PATHCONV=1 az "$@"
}

# fetch_role_assignments <assignee> <scope>: prints the newline-separated role names assigned to
# <assignee> at <scope>. Takes an explicit scope rather than building a subscription-root scope
# internally -- `az role assignment list --scope` accepts any resource ID (subscription, resource
# group, or a single resource such as an AKS cluster), and the AKS RBAC grant
# (grant_aks_rbac_access below) needs this same read at a cluster-scoped resource ID, a different
# scope than rbac_preflight's subscription-root check just below; both callers share this one
# function rather than duplicating the `az role assignment list` call shape.
fetch_role_assignments() {
  local assignee="$1" scope="$2"
  az_no_pathconv role assignment list --assignee "$assignee" --scope "$scope" \
    --query "[].roleDefinitionName" -o tsv
}

# has_sufficient_role <roles>: true if the newline-separated <roles> contains Owner or
# Contributor.
has_sufficient_role() {
  local roles="$1"
  local role
  while IFS= read -r role; do
    [[ "$role" == "Owner" || "$role" == "Contributor" ]] && return 0
  done <<< "$roles"
  return 1
}

# rbac_preflight <subscription_id>: hard-stops unless the signed-in user has Owner or
# Contributor at subscription scope, with an actionable fix command -- same user-only pattern as
# deploy-llm.sh's own preflight (no service-principal branch). Runs before any
# other Azure resource is touched, including provider registration below.
rbac_preflight() {
  local subscription_id="$1"
  local upn roles
  upn="$(fetch_signed_in_user_upn)"
  roles="$(fetch_role_assignments "$upn" "/subscriptions/$subscription_id")"
  if ! has_sufficient_role "$roles"; then
    print_error 'RBAC preflight failed: %s has neither Owner nor Contributor at subscription scope.\n' \
      "$upn"
    print_error 'Fix: az role assignment create --assignee %s --role Contributor --scope /subscriptions/%s\n' \
      "$upn" "$subscription_id"
    exit "$EXIT_FAILURE"
  fi
}

# Resource providers this script's resources need registered on the subscription -- the 9
# namespaces backing every Azure resource type this script creates (the Cognitive Services
# account, AKS cluster, Key Vault, virtual-network resources, VMs, managed identities, and the
# Container Insights/Log Analytics monitoring stack). On a fresh subscription only
# Microsoft.Authorization is registered by default; any of these left NotRegistered fails
# resource creation with MissingSubscriptionRegistration.
readonly REQUIRED_PROVIDERS=(
  Microsoft.CognitiveServices Microsoft.ContainerService Microsoft.KeyVault Microsoft.Network
  Microsoft.Compute Microsoft.ManagedIdentity Microsoft.OperationsManagement
  Microsoft.OperationalInsights Microsoft.Insights
)
# Bundled Authentik, as broker -- one fixed OAuth2 Provider + Application, both named "ps-cli"
# (no separate API-app/CLI-app split; the blueprint,
# charts/policy-system/files/authentik-blueprint.yaml, is the single source of truth for this
# literal on the chart side). AUTHENTIK_SCOPES matches that blueprint's own
# `property_mappings` (openid/profile/email/offline_access): a Provider with no `offline_access`
# scope mapping never issues a `refresh_token`, breaking every ps-cli command after the very
# first `auth login`.
readonly AUTHENTIK_APP_SLUG="ps-cli"
readonly AUTHENTIK_SCOPES="openid profile email offline_access"

# Fixed AKS node shape -- not evaluator-tunable: one tested deployment shape
# (scripts/ps-defaults.conf has no AKS-size field). These literals and the check_aks_vm_size
# function below follow the documented Azure CLI surfaces and are not verified against a live
# subscription. AKS_NODE_VM_SIZE_VCPUS is Standard_D4as_v7's own vCPU
# count (the Dasv7-series specification), used only to compute the total vCPUs this fixed 2-node
# shape needs: AKS_NODE_COUNT x AKS_NODE_VM_SIZE_VCPUS = 2 x 4 = 8.
readonly AKS_NODE_VM_SIZE="Standard_D4as_v7"
readonly AKS_NODE_VM_SIZE_FAMILY="StandardDasv7Family"
readonly AKS_NODE_COUNT=2
readonly AKS_NODE_VM_SIZE_VCPUS=4

# Built-in role granted at cluster scope so the deploying identity's own
# kubectl/helm calls can authenticate against an --enable-azure-rbac cluster -- matches
# Azure's built-in "Azure Kubernetes Service RBAC Cluster Admin" role name exactly, the role that
# grants full access to Kubernetes APIs when Azure RBAC authorization is enabled on the cluster.
readonly AKS_RBAC_ADMIN_ROLE="Azure Kubernetes Service RBAC Cluster Admin"

# LLM credentials Secret name and Helm release identity: LLM_SECRET_NAME is the
# Kubernetes Secret name ensure_llm_secret below creates/reads; CHART_REF is this project's own
# published OCI chart (charts/policy-system, published to ghcr.io); HELM_RELEASE_NAME is the Helm
# release name ensure_release below installs/upgrades.
readonly LLM_SECRET_NAME="policy-system-llm-credentials"
# PS_CHART_REF (a chart path or OCI ref) overrides the published chart, e.g. to deploy an
# unreleased local chart for a live check; unset, the published chart is used.
readonly CHART_REF="${PS_CHART_REF:-oci://ghcr.io/mindovermachine-dev/charts/policy-system}"
readonly HELM_RELEASE_NAME="policy-system"
# PS Service name -- equals the chart's rendered PS Service Service and PS Service Ingress name
# ("{{ include "policy-system.fullname" . }}-ps-service"): fullname resolves to the bare release
# name whenever the chart name is already contained in the release name, which is the case here
# (both "policy-system").
readonly PS_SERVICE_NAME="${HELM_RELEASE_NAME}-ps-service"
# Authentik server Service name -- equals the upstream `authentik` dependency chart's rendered
# server Service and the chart's Authentik Ingress name: its `authentik.fullname` template renders
# "<release-name>-authentik" (the release name "policy-system" does not already contain
# "authentik", so it is not collapsed to the bare release name the way PS_SERVICE_NAME's fullname
# is), and its `authentik.server.fullname` template appends "-server" to that.
readonly AUTHENTIK_SERVICE_NAME="${HELM_RELEASE_NAME}-authentik-server"
# PS Service calls Authentik in-cluster: the Ingress routes only the login/enrolment paths (the
# chart's files/authentik-public-paths.txt), so the invitation API PS Service needs for
# `invite_user` is reachable only through the Service. `/auth` is Authentik's web path (AUTHENTIK_WEB__PATH in
# values-prod.yaml); the Service's port 80 is the default and needs no port here.
readonly AUTHENTIK_IN_CLUSTER_BASE_URL="http://${AUTHENTIK_SERVICE_NAME}/auth"
# The Secret holding the one shared API token (chart-generated: templates/authentik-api-token-
# secret.yaml) that Authentik also reads as its bootstrap token.
readonly AUTHENTIK_API_TOKEN_SECRET_NAME="${HELM_RELEASE_NAME}-authentik-api-token"
readonly AUTHENTIK_ROLLOUT_TIMEOUT="${AUTHENTIK_ROLLOUT_TIMEOUT:-900s}"
# Resolves the REAL chart file directly -- reads charts/policy-system/values-prod.yaml from the
# repo's actual chart directory, never a local copy that could silently drift from it. This
# script (scripts/deploy-ps-prod.sh) lives ONE directory below the repo root (scripts/), so one
# "../" from SCRIPT_DIR is correct here; a path-existence test reads this exact assignment back
# out of this file, so a wrong "../" count fails it.
readonly VALUES_PROD_FILE="${SCRIPT_DIR}/../charts/policy-system/values-prod.yaml"

# Env-var-overridable so tests can poll on a millisecond timescale instead of the real ~5-minute
# worst case; the defaults are the values that cover a real registration. Other polling loops in
# this script use the same `${VAR:-default}` shape.
readonly PROVIDER_REGISTRATION_WAIT_ATTEMPTS="${PROVIDER_REGISTRATION_WAIT_ATTEMPTS:-30}"
readonly PROVIDER_REGISTRATION_WAIT_INTERVAL_SECONDS="${PROVIDER_REGISTRATION_WAIT_INTERVAL_SECONDS:-10}"

# provider_registered <namespace>: true if already Registered.
provider_registered() {
  [[ "$(az provider show --namespace "$1" --query registrationState -o tsv 2>/dev/null)" == "Registered" ]]
}

# ensure_providers_registered: register-if-absent every namespace in REQUIRED_PROVIDERS, then
# poll each to Registered before returning -- every dependent resource create runs after this,
# so none fails with MissingSubscriptionRegistration. Kicks off every missing registration first, then polls each in
# turn, rather than register-then-poll one at a time -- registration is asynchronous on Azure's
# side regardless of when the request lands, so the wait for provider 2 already overlaps
# provider 1's.
ensure_providers_registered() {
  local ns unregistered=()
  for ns in "${REQUIRED_PROVIDERS[@]}"; do
    provider_registered "$ns" || unregistered+=("$ns")
  done
  if [[ ${#unregistered[@]} -eq 0 ]]; then
    return 0
  fi
  for ns in "${unregistered[@]}"; do
    az provider register --namespace "$ns" >/dev/null
  done

  local _attempt
  for ns in "${unregistered[@]}"; do
    for _attempt in $(seq 1 "$PROVIDER_REGISTRATION_WAIT_ATTEMPTS"); do
      provider_registered "$ns" && continue 2
      sleep "$PROVIDER_REGISTRATION_WAIT_INTERVAL_SECONDS"
    done
    print_error 'Timed out waiting for resource provider %s to finish registering.\n' "$ns"
    exit "$EXIT_FAILURE"
  done
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
  model_generally_available "$model_list" "$LLM_CHAT_MODEL_NAME" "$LLM_CHAT_MODEL_SKU" \
    && model_generally_available "$model_list" "$LLM_EMBED_MODEL_NAME" "$LLM_EMBED_MODEL_SKU"
}

# fail_no_region_available: hard-stops when no candidate region has both models Generally
# Available at the required SKU -- reached only after every candidate was tried.
fail_no_region_available() {
  print_error 'No candidate region has both %s (%s) and %s (%s) Generally Available. Tried: %s\n' \
    "$LLM_CHAT_MODEL_NAME" "$LLM_CHAT_MODEL_SKU" "$LLM_EMBED_MODEL_NAME" "$LLM_EMBED_MODEL_SKU" \
    "$(join_comma_space "${LLM_REGION_CANDIDATES[@]}")"
  exit "$EXIT_FAILURE"
}

# select_region: probes LLM_REGION_CANDIDATES in configured order, stopping at the first
# candidate where both models are Generally Available -- a flat loop with an early
# exit, not nested conditionals (docs/coding-standards/level1-coding-principles.md, cyclomatic
# complexity).
# Prints "<region>\n<model_list_json>" so a caller capturing this via command substitution
# (which runs in a subshell -- a plain variable set here would not survive back to the caller)
# gets both the selected region and its already-fetched `model list` response, letting later
# steps (capacity validation, model version lookup) reuse it instead of re-querying the same
# region. Fails explicitly if no candidate qualifies.
select_region() {
  local candidate model_list
  for candidate in "${LLM_REGION_CANDIDATES[@]}"; do
    model_list="$(az cognitiveservices model list --location "$candidate")"
    if both_models_generally_available "$model_list"; then
      printf '%s\n%s' "$candidate" "$model_list"
      return 0
    fi
  done
  fail_no_region_available
}

# model_capacity_range <model_list_json> <model_name> <sku>: prints "<minimum> <maximum>" for
# <model_name>'s <sku> SKU, per that region's `model list` response. Coalesces a null
# capacity.minimum to 0 -- Azure's `az cognitiveservices model list` reports `capacity.minimum`
# as `null`, not a number, for SKUs with no enforced floor (e.g. `GlobalStandard`/
# `DataZoneStandard`); left uncoalesced, the bash arithmetic capacity check below would crash on
# the literal string "null". ps-defaults.conf's own LLM_CHAT_MODEL_SKU/LLM_EMBED_MODEL_SKU
# defaults (DataZoneStandard/Standard) are exactly this null-minimum case.
model_capacity_range() {
  local model_list="$1" model_name="$2" sku="$3"
  jq -r --arg name "$model_name" --arg sku "$sku" \
    '.[] | select(.model.name == $name) | .model.skus[]? | select(.name == $sku)
      | "\(.capacity.minimum // 0) \(.capacity.maximum)"' \
    <<< "$model_list" | head -n1
}

# validate_model_capacity <model_list_json> <field_name> <model_name> <sku> <capacity>: hard
# stops unless <capacity> falls within the [minimum, maximum] range <model_name>'s <sku> SKU
# reports in <model_list_json>.
validate_model_capacity() {
  local model_list="$1" field_name="$2" model_name="$3" sku="$4" capacity="$5"
  local range minimum maximum
  range="$(model_capacity_range "$model_list" "$model_name" "$sku")"
  minimum="${range%% *}"
  maximum="${range##* }"
  if (( capacity < minimum || capacity > maximum )); then
    print_error '%s: %s "%s" is outside the allowed range for %s (%s) in this region: %s-%s\n' \
      "$CONFIG_FILE_DISPLAY_PATH" "$field_name" "$capacity" "$model_name" "$sku" \
      "$minimum" "$maximum"
    exit "$EXIT_FAILURE"
  fi
}

# validate_capacity_range <model_list_json>: validates the configured chat/embed capacities
# against the selected region's live-reported ranges -- reuses the `model list`
# response select_region already fetched, no second call for the same region. Unlike
# deploy-llm.sh's fail_region_not_viable, a failure here is a hard stop: the
# selected region is never swapped for another once select_region has already picked one.
validate_capacity_range() {
  local model_list="$1"
  validate_model_capacity "$model_list" "LLM_CHAT_MODEL_CAPACITY" "$LLM_CHAT_MODEL_NAME" \
    "$LLM_CHAT_MODEL_SKU" "$LLM_CHAT_MODEL_CAPACITY"
  validate_model_capacity "$model_list" "LLM_EMBED_MODEL_CAPACITY" "$LLM_EMBED_MODEL_NAME" \
    "$LLM_EMBED_MODEL_SKU" "$LLM_EMBED_MODEL_CAPACITY"
}

# quota_usage_key <sku> <model_name>: prints the usage-list entry name Azure actually reports for
# a model+SKU pair, e.g. "OpenAI.DataZoneStandard.gpt-5.4-mini". `az cognitiveservices usage
# list` names each quota entry "OpenAI.<sku>.<model_name>"; querying by any other key (e.g. the
# literal strings "chat"/"embed") never matches a real entry, so a quota preflight built on those
# keys would silently never fire on any subscription. scripts/deploy-llm.sh still queries by
# those literal keys.
quota_usage_key() {
  local sku="$1" model_name="$2"
  printf 'OpenAI.%s.%s' "$sku" "$model_name"
}

# model_usage_entry_exists <usage_json> <usage_key>: true if Azure's usage endpoint reported an
# entry for <usage_key>. Needed because `az cognitiveservices usage list` reports no usage
# entries at all for a subscription/region until something has actually been deployed there;
# treating that absence as "remaining = 0" would hard-fail the very first deployment in a region.
# validate_model_quota below uses this to skip the check (with a printed note) when no entry
# exists yet -- the account/deployment create calls still enforce the real limit.
model_usage_entry_exists() {
  local usage="$1" usage_key="$2"
  jq -e --arg key "$usage_key" 'any(.[]; .name.value == $key)' <<< "$usage" >/dev/null
}

# model_remaining_quota <usage_json> <usage_key>: prints the remaining quota (limit minus
# current usage) for <usage_key> in <usage_json>, as an integer. Caller must already know the
# entry exists (model_usage_entry_exists). Azure reports `currentValue`/`limit` as floats (e.g.
# `200.0`), which bash's `$(( ))` cannot parse -- the subtraction is done in `jq` (`floor`)
# instead of bash arithmetic.
model_remaining_quota() {
  local usage="$1" usage_key="$2"
  jq -r --arg key "$usage_key" \
    '[.[] | select(.name.value == $key)][0] | ((.limit - .currentValue) | floor)' \
    <<< "$usage"
}

# validate_model_quota <usage_json> <usage_key> <field_name> <requested_capacity>: hard stops
# with a quota-increase message unless <usage_key>'s remaining quota covers
# <requested_capacity>. Skips the check (with a note, not silently) when Azure
# reports no usage entry for <usage_key> at all -- see model_usage_entry_exists's own
# comment; the account/deployment create calls still enforce the real limit and
# will fail clearly if it's actually insufficient.
validate_model_quota() {
  local usage="$1" usage_key="$2" field_name="$3" requested_capacity="$4"
  if ! model_usage_entry_exists "$usage" "$usage_key"; then
    printf 'Note: Azure reports no usage/quota entry for "%s" in this region yet (expected on a subscription with no prior deployment here) -- skipping this preflight check for %s; the account/deployment create calls below still enforce the real limit.\n' \
      "$usage_key" "$field_name"
    return 0
  fi
  local remaining
  remaining="$(model_remaining_quota "$usage" "$usage_key")"
  if (( remaining < requested_capacity )); then
    print_error '%s: insufficient Azure quota for %s: requested %s, only %s remaining in this region (usage key: %s).\n' \
      "$CONFIG_FILE_DISPLAY_PATH" "$field_name" "$requested_capacity" "$remaining" "$usage_key"
    print_error 'Request a quota increase for this subscription/region and re-run -- no other region is tried.\n'
    exit "$EXIT_FAILURE"
  fi
}

# deployment_exists <account_name> <deployment_name>: true if it already exists. Defined here,
# ahead of the provisioning chain, because check_quota below uses it to skip an existing
# deployment; this function only ever reads.
deployment_exists() {
  local account_name="$1" deployment_name="$2"
  az cognitiveservices account deployment show --name "$account_name" \
    --resource-group "$RESOURCE_GROUP_NAME" --deployment-name "$deployment_name" \
    >/dev/null 2>&1
}

# check_quota <region> <account_name>: hard-stops if either model's remaining quota at <region>
# is less than its configured capacity -- runs once, at the already-selected region
# only; never tried against a different region. Skips a model's check entirely when its
# deployment already exists: once a deployment exists, its own allocated capacity counts against
# Azure's reported `currentValue`, so re-requesting the same capacity on a rerun would read as
# "0 remaining" even though no *new* capacity is actually needed (ensure_deployment below never
# resizes an existing deployment).
check_quota() {
  local region="$1" account_name="$2"
  local usage
  usage="$(az cognitiveservices usage list --location "$region")"
  if deployment_exists "$account_name" "$LLM_CHAT_MODEL_NAME"; then
    printf 'Chat deployment %s already exists -- skipping quota preflight (no new capacity requested).\n' \
      "$LLM_CHAT_MODEL_NAME"
  else
    validate_model_quota "$usage" "$(quota_usage_key "$LLM_CHAT_MODEL_SKU" "$LLM_CHAT_MODEL_NAME")" \
      "LLM_CHAT_MODEL_CAPACITY" "$LLM_CHAT_MODEL_CAPACITY"
  fi
  if deployment_exists "$account_name" "$LLM_EMBED_MODEL_NAME"; then
    printf 'Embedding deployment %s already exists -- skipping quota preflight (no new capacity requested).\n' \
      "$LLM_EMBED_MODEL_NAME"
  else
    validate_model_quota "$usage" "$(quota_usage_key "$LLM_EMBED_MODEL_SKU" "$LLM_EMBED_MODEL_NAME")" \
      "LLM_EMBED_MODEL_CAPACITY" "$LLM_EMBED_MODEL_CAPACITY"
  fi
}

# model_version <model_list_json> <model_name>: prints the version string Azure's `model list`
# reports for <model_name> in this region. Needed because `az cognitiveservices account
# deployment create` hard-requires an explicit `--model-version` -- reads `.model.version` from
# the already-fetched `model list` response rather than omitting the flag. Reuses
# select_region's already-fetched response rather than a second `model list` call for the same
# region. ensure_deployment consumes this value (feeds --model-version); main() resolves and
# logs it at its "Selected region" step, before any deployment is created.
model_version() {
  local model_list="$1" model_name="$2"
  jq -r --arg name "$model_name" '.[] | select(.model.name == $name) | .model.version' \
    <<< "$model_list" | head -n1
}

# resource_group_exists: true if the fixed-name resource group already exists.
resource_group_exists() {
  az group show --name "$RESOURCE_GROUP_NAME" >/dev/null 2>&1
}

# ensure_resource_group <region>: create-if-absent -- the first link in the provisioning chain,
# this script's own copy under $RESOURCE_GROUP_NAME (not shared/sourced from
# scripts/deploy-llm.sh). Checks resource_group_exists first rather than
# calling `az group create` unconditionally -- `az group create` is itself idempotent (a no-op if
# the group already exists), but only the explicit check lets this function set
# made_changes=true exclusively when a create actually happened, the check-before-act idiom the
# idempotent-rerun summary depends on.
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

# ensure_account <account_name> <region>: create-if-absent, kind AIServices, SKU S0 -- same shape
# as scripts/deploy-llm.sh's own ensure_account. Writes the resolved endpoint into the
# process-wide `account_endpoint` (top-of-file note) rather than returning it on stdout, since
# this function must also set `made_changes`.
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

# ensure_deployment <account_name> <deployment_name> <sku> <capacity> <model_version>:
# create-if-absent, using the deployment name as the model name (design doc's own reference
# deployment does the same, e.g. "Deployment: gpt-5.4-mini"). <model_version> is required
# (--model-version is hard-required, see model_version's own comment above) -- the caller
# passes $chat_model_version/$embed_model_version, already resolved in main() before this point.
# deployment_exists is defined above, next to check_quota, which also uses it.
ensure_deployment() {
  local account_name="$1" deployment_name="$2" sku="$3" capacity="$4" version="$5"
  if deployment_exists "$account_name" "$deployment_name"; then
    return 0
  fi
  az cognitiveservices account deployment create --name "$account_name" \
    --resource-group "$RESOURCE_GROUP_NAME" --deployment-name "$deployment_name" \
    --model-name "$deployment_name" --model-version "$version" --model-format OpenAI \
    --sku-name "$sku" --sku-capacity "$capacity" >/dev/null
  made_changes=true
}

# keyvault_exists <vault_name>: true if it already exists.
keyvault_exists() {
  local vault_name="$1"
  az keyvault show --name "$vault_name" >/dev/null 2>&1
}

# ensure_keyvault <vault_name> <region>: create-if-absent, standard SKU, access-policy based
# (enableRbacAuthorization false) -- same shape as scripts/deploy-llm.sh's own ensure_keyvault.
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
# access policy's --object-id). Same shape as scripts/deploy-llm.sh's -- user-only, no
# service-principal branch.
fetch_signed_in_user_object_id() {
  az ad signed-in-user show --query id -o tsv
}

# grant_keyvault_access <vault_name>: grants the deploying identity get/list/set on secrets,
# scoped to this vault only. Azure's set-policy is itself idempotent and there is no "read
# current policy" call to check against first, so this always runs on every provisioning pass --
# it is not a "create" and does not affect a later idempotent-rerun's zero-create-calls proof.
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
# unchanged, so this read-before-write comparison is what makes an unchanged rerun a true no-op.
# <desired_value> is never printed or logged, only compared and forwarded to `az`.
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

# fetch_vm_sku_json <region>: prints az's `vm list-skus` response for AKS_NODE_VM_SIZE at
# <region> (server-side filtered by --size, so this is a handful of tier/zone-variant entries for
# one VM size, not the whole SKU catalog).
fetch_vm_sku_json() {
  local region="$1"
  az vm list-skus --location "$region" --size "$AKS_NODE_VM_SIZE" --all -o json
}

# vm_size_restricted <sku_json> <region>: true if any returned SKU entry's restrictions[] mark
# AKS_NODE_VM_SIZE unavailable to this subscription -- either an explicit
# "NotAvailableForSubscription" reasonCode (any restriction type), or a "Location"-type
# restriction whose values cover <region> specifically.
vm_size_restricted() {
  local sku_json="$1" region="$2"
  jq -e --arg region "$region" \
    'any(.[]? | .restrictions[]?;
      .reasonCode == "NotAvailableForSubscription"
      or (.type == "Location" and ((.values // []) | index($region) != null)))' \
    <<< "$sku_json" >/dev/null 2>&1
}

# vm_size_restriction_reason <sku_json>: prints the first restriction's reasonCode -- the
# preflight failure message shows this actual code, never a generic "not allowed" (this AC's
# literal wording).
vm_size_restriction_reason() {
  local sku_json="$1"
  jq -r '[.[]? | .restrictions[]? | .reasonCode] | first // "restricted"' <<< "$sku_json"
}

# vm_size_allowed <region>: true unless AKS_NODE_VM_SIZE is restricted for this subscription in
# <region> (vm_size_restricted above) -- the allowlist half of check_aks_vm_size, independent of
# quota.
vm_size_allowed() {
  local region="$1"
  ! vm_size_restricted "$(fetch_vm_sku_json "$region")" "$region"
}

# fetch_vm_usage_json <region>: prints az's `vm list-usage` response for <region> -- the
# subscription's vCPU quota per VM-size family.
fetch_vm_usage_json() {
  local region="$1"
  az vm list-usage --location "$region" -o json
}

# vm_family_quota_remaining <usage_json>: prints AKS_NODE_VM_SIZE_FAMILY's remaining vCPU quota
# (limit minus current usage) in <usage_json>, as an integer -- jq `floor`, same float-safety
# discipline as model_remaining_quota above (Azure reports these as floats too, per that
# function's own comment). Prints 0 if the family has no entry at all -- unlike the Cognitive
# Services usage-list gap (model_usage_entry_exists's own comment), a subscription's
# core-count quota families are a fixed catalog Azure always reports in `vm list-usage`, so an
# absent entry here means genuinely no quota, not "never deployed here yet".
vm_family_quota_remaining() {
  local usage_json="$1"
  jq -r --arg family "$AKS_NODE_VM_SIZE_FAMILY" \
    '([.[] | select(.name.value == $family)][0]
      | (((.limit // 0) | tonumber) - ((.currentValue // 0) | tonumber) | floor)) // 0' \
    <<< "$usage_json"
}

# vm_family_quota_sufficient <usage_json> <needed_vcpus>: true if AKS_NODE_VM_SIZE_FAMILY's
# remaining vCPU quota in <usage_json> covers <needed_vcpus> -- the quota half of
# check_aks_vm_size,
# checked only once the allowlist check above has already passed.
vm_family_quota_sufficient() {
  local usage_json="$1" needed_vcpus="$2"
  local remaining
  remaining="$(vm_family_quota_remaining "$usage_json")"
  (( remaining >= needed_vcpus ))
}

# check_aks_vm_size <cluster_name> <region>: hard-stops before any AKS creation
# (ensure_aks_cluster) unless AKS_NODE_VM_SIZE is both allowed for this subscription in <region>
# and this subscription's vCPU family quota there covers AKS_NODE_COUNT x AKS_NODE_VM_SIZE_VCPUS.
# Not verified against a live subscription. Fails with the actual allowlist/quota numbers shown,
# mirroring validate_model_quota's own message-style convention (explicit numbers, never a
# generic message) -- never left to `az aks create`'s own error text.
#
# Skips entirely when <cluster_name> already exists, same reasoning as check_quota's own
# deployment_exists guard above: ensure_aks_cluster is create-if-absent and never resizes an
# existing cluster, so an existing cluster's own nodes are what the quota preflight would be
# measuring against -- re-requesting their already-allocated vCPUs on a rerun reads as
# insufficient quota even though no *new* capacity is actually needed.
check_aks_vm_size() {
  local cluster_name="$1" region="$2"
  if aks_cluster_exists "$cluster_name"; then
    printf 'AKS cluster %s already exists -- skipping vCPU quota preflight (no new capacity requested).\n' \
      "$cluster_name"
    return
  fi
  if ! vm_size_allowed "$region"; then
    # Re-fetches (failure path only, not the common case) -- vm_size_allowed only returns a
    # boolean; the actual restriction reason for the message comes from the same live query.
    print_error 'AKS node VM size %s is not available to this subscription in %s (reason: %s). Choose a different subscription or request the restriction lifted, then re-run.\n' \
      "$AKS_NODE_VM_SIZE" "$region" "$(vm_size_restriction_reason "$(fetch_vm_sku_json "$region")")"
    exit "$EXIT_FAILURE"
  fi

  local usage_json needed_vcpus remaining
  usage_json="$(fetch_vm_usage_json "$region")"
  needed_vcpus=$((AKS_NODE_COUNT * AKS_NODE_VM_SIZE_VCPUS))
  if ! vm_family_quota_sufficient "$usage_json" "$needed_vcpus"; then
    remaining="$(vm_family_quota_remaining "$usage_json")"
    print_error 'Insufficient vCPU quota for AKS node size %s (%s) in %s: need %s vCPUs (%s nodes x %s vCPUs each), only %s remaining.\n' \
      "$AKS_NODE_VM_SIZE" "$AKS_NODE_VM_SIZE_FAMILY" "$region" "$needed_vcpus" "$AKS_NODE_COUNT" \
      "$AKS_NODE_VM_SIZE_VCPUS" "$remaining"
    print_error 'Request a vCPU quota increase for this subscription/region and re-run.\n'
    exit "$EXIT_FAILURE"
  fi
}

# has_role <roles> <role_name>: true if the newline-separated <roles> contains <role_name>
# exactly. has_sufficient_role above checks for either-of-two roles at subscription scope with
# its own small loop; this checks for exactly one role at an arbitrary scope
# (grant_aks_rbac_access below), so it is kept as its own narrow function.
has_role() {
  local roles="$1" wanted="$2"
  local role
  while IFS= read -r role; do
    [[ "$role" == "$wanted" ]] && return 0
  done <<< "$roles"
  return 1
}

# aks_cluster_exists <cluster_name>: true if it already exists.
aks_cluster_exists() {
  local cluster_name="$1"
  az aks show --name "$cluster_name" --resource-group "$RESOURCE_GROUP_NAME" >/dev/null 2>&1
}

# fetch_aks_cluster_id <cluster_name>: prints the cluster's own resource ID -- the --scope
# grant_aks_rbac_access below grants the deploying identity's role at, deliberately never the
# subscription root: the grant is scoped to the cluster, never the subscription.
fetch_aks_cluster_id() {
  local cluster_name="$1"
  az aks show --name "$cluster_name" --resource-group "$RESOURCE_GROUP_NAME" --query id -o tsv
}

# ensure_aks_cluster <cluster_name> <region>: create-if-absent, with every cluster hardening
# flag: AAD-integrated auth, Azure RBAC authorization, local (cert-based) accounts
# disabled, and Azure CNI network policy. --network-plugin azure MUST be passed ALONGSIDE
# --network-policy azure -- `az aks create --network-policy azure` requires an explicit
# `--network-plugin azure`; Azure network policy enforcement is only supported on top of the
# Azure CNI plugin (not kubenet), and omitting the plugin flag fails `az aks create` outright.
ensure_aks_cluster() {
  local cluster_name="$1" region="$2"
  if aks_cluster_exists "$cluster_name"; then
    return 0
  fi
  az aks create --name "$cluster_name" --resource-group "$RESOURCE_GROUP_NAME" --location "$region" \
    --node-count "$AKS_NODE_COUNT" --node-vm-size "$AKS_NODE_VM_SIZE" --tier free \
    --enable-aad --enable-azure-rbac --disable-local-accounts \
    --network-plugin azure --network-policy azure --node-os-upgrade-channel SecurityPatch \
    --generate-ssh-keys >/dev/null
  made_changes=true
}

# grant_aks_rbac_access <cluster_name>: grants the deploying identity -- reusing
# fetch_signed_in_user_object_id's result, the SAME identity resolver the Key Vault access grant
# uses, not a separate service-principal-aware resolver -- the built-in
# "Azure Kubernetes Service RBAC Cluster Admin" role, scoped to this cluster's own resource ID
# only, never the subscription root. Without this, --enable-azure-rbac rejects every kubectl/helm
# call after this regardless of the operator's subscription-level Owner/Contributor role
# rbac_preflight already checked -- that is a different scope for a different concern. Read-
# before-write, like grant_keyvault_access: unlike keyvault set-policy, `az role
# assignment create` errors on a duplicate assignment instead of being silently idempotent.
grant_aks_rbac_access() {
  local cluster_name="$1"
  local scope object_id roles
  scope="$(fetch_aks_cluster_id "$cluster_name")"
  object_id="$(fetch_signed_in_user_object_id)"
  roles="$(fetch_role_assignments "$object_id" "$scope")"
  if has_role "$roles" "$AKS_RBAC_ADMIN_ROLE"; then
    return 0
  fi
  az_no_pathconv role assignment create --assignee "$object_id" --role "$AKS_RBAC_ADMIN_ROLE" \
    --scope "$scope" >/dev/null
  made_changes=true
}

# ensure_aks_credentials <cluster_name>: points the local kubectl/helm at the cluster.
# --overwrite-existing so a rerun never silently keeps a stale prior kubeconfig entry -- without
# it, `az aks get-credentials` refuses to merge new credentials over an existing kubeconfig
# context of the same name.
ensure_aks_credentials() {
  local cluster_name="$1"
  az aks get-credentials --name "$cluster_name" --resource-group "$RESOURCE_GROUP_NAME" \
    --overwrite-existing >/dev/null
}

# ---------------------------------------------------------------------------------------------
# LLM credentials -> Kubernetes Secret, and the Helm release itself.
# ---------------------------------------------------------------------------------------------

# apply_output_changed <apply_output>: true unless kubectl reported the object unchanged --
# `kubectl apply` is idempotent by construction but doesn't otherwise expose a machine-readable
# "nothing changed" signal, so this greps its own stdout the way the rest of this script tracks
# made_changes.
apply_output_changed() {
  [[ "$1" != *unchanged* ]]
}

# ensure_llm_secret <vault_name>: reads the three LLM credentials already written to Key Vault
# and writes them into the cluster as a Kubernetes Secret, underscore-keyed (K8s env-var
# convention) from the dash-named Key Vault secret names -- same create --dry-run=client -o yaml
# | apply idiom as scripts/sync-llm-secrets-to-kind.sh, minus its kind-only context guard (this
# script already pointed kubectl at the right cluster via ensure_aks_credentials).
ensure_llm_secret() {
  local vault_name="$1"
  local api_key api_base api_version apply_output
  api_key="$(read_secret_value "$vault_name" "AZURE-API-KEY")"
  api_base="$(read_secret_value "$vault_name" "AZURE-API-BASE")"
  api_version="$(read_secret_value "$vault_name" "AZURE-API-VERSION")"
  apply_output="$(kubectl create secret generic "$LLM_SECRET_NAME" \
    --from-literal="AZURE_API_KEY=$api_key" \
    --from-literal="AZURE_API_BASE=$api_base" \
    --from-literal="AZURE_API_VERSION=$api_version" \
    --dry-run=client -o yaml | kubectl apply -f -)"
  apply_output_changed "$apply_output" && made_changes=true
  return 0
}

# release_values_json <issuer> <audience> <cli_client_id> <scopes> <owner_subject> <owner_issuer>
# <authentik_base_url> <authentik_public_url> <public_url>: prints the JSON shape of the --set
# values this script passes to `helm upgrade --install`, in the same structure `helm get values
# -o json` returns -- lets ensure_release compare desired vs. deployed. Exactly 14 leaf fields:
# llm.existingSecret, psService.auth.{issuer,audience,cliClientId,scopes},
# psService.authzBootstrapOwner.{subject,issuer} -- the owner's email and the issuer, both known
# before Authentik exists -- psService.authentik.{baseUrl,publicUrl}: the in-cluster API address
# PS Service calls, and the public address used only for the invitee-facing link --
# psService.publicUrl, the origin PS Service publishes for itself, and the chart-owned Ingress:
# ingress.{enabled,className,tls.secretName} and the cert-manager issuer annotation.
release_values_json() {
  local issuer="$1" audience="$2" cli_client_id="$3" scopes="$4" owner_subject="$5" owner_issuer="$6"
  local authentik_base_url="$7" authentik_public_url="$8" public_url="$9"
  jq -n --arg secret "$LLM_SECRET_NAME" --arg issuer "$issuer" --arg audience "$audience" \
    --arg cli "$cli_client_id" --arg scopes "$scopes" \
    --arg owner_subject "$owner_subject" --arg owner_issuer "$owner_issuer" \
    --arg base "$authentik_base_url" --arg public "$authentik_public_url" \
    --arg ps_public "$public_url" --arg ingress_class "$INGRESS_CLASS" \
    --arg cluster_issuer "$CLUSTER_ISSUER_NAME" --arg tls_secret "${PS_SERVICE_NAME}-tls" \
    '{llm: {existingSecret: $secret},
      psService: {auth: {issuer: $issuer, audience: $audience, cliClientId: $cli, scopes: $scopes},
        authzBootstrapOwner: {subject: $owner_subject, issuer: $owner_issuer},
        authentik: {baseUrl: $base, publicUrl: $public}, publicUrl: $ps_public},
      ingress: {enabled: true, className: $ingress_class,
        annotations: {"cert-manager.io/cluster-issuer": $cluster_issuer},
        tls: {secretName: $tls_secret}}}'
}

# ensure_release <issuer> <audience> <cli_client_id> <scopes> <owner_subject> <owner_issuer>
# <authentik_base_url> <authentik_public_url> <public_url>:
# write-if-changed against the currently deployed release's values (`helm get values`), since
# plain `helm upgrade --install` has no built-in no-op detection of its own -- it creates a new
# revision even when nothing changed. <audience> is the fixed Authentik application slug,
# same literal as <cli_client_id>. <owner_subject> is the owner's email and
# <owner_issuer> the issuer: rerunning with the same values is a no-op here, and
# ps_service's own "grantable once" bootstrap is unaffected either way.
#
# Compares only the 14 fields release_values_json sets, extracted from `helm get values`'s output
# via the same jq shape, rather than the whole object -- `helm get values` also echoes back
# everything from `-f values-prod.yaml` (falkordb.*, llm.provider, psService.service.type), which
# this script never sets itself via --set and doesn't need to compare; a whole-object comparison
# against a JSON built from only the --set flags would never match, since that JSON never
# contains those values-file-only fields at all -- permanently defeating this idempotency check
# and causing `helm upgrade` to run on every rerun regardless of whether anything changed.
ensure_release() {
  local issuer="$1" audience="$2" cli_client_id="$3" scopes="$4"
  local owner_subject="$5" owner_issuer="$6" authentik_base_url="$7" authentik_public_url="$8"
  local public_url="$9"
  local desired_json
  local -a reuse_args=()
  set_chart_version_args "$SCRIPT_DIR" "$CHART_REF"
  desired_json="$(release_values_json "$issuer" "$audience" "$cli_client_id" "$scopes" \
    "$owner_subject" "$owner_issuer" "$authentik_base_url" "$authentik_public_url" "$public_url")"
  if helm status "$HELM_RELEASE_NAME" >/dev/null 2>&1; then
    local current_json current_subset_json
    current_json="$(helm get values "$HELM_RELEASE_NAME" -o json)"
    current_subset_json="$(jq \
      '{llm: {existingSecret: .llm.existingSecret},
        psService: {auth: {issuer: .psService.auth.issuer, audience: .psService.auth.audience,
          cliClientId: .psService.auth.cliClientId, scopes: .psService.auth.scopes},
          authzBootstrapOwner: {subject: .psService.authzBootstrapOwner.subject,
            issuer: .psService.authzBootstrapOwner.issuer},
          authentik: {baseUrl: .psService.authentik.baseUrl,
            publicUrl: .psService.authentik.publicUrl},
          publicUrl: .psService.publicUrl},
        ingress: {enabled: .ingress.enabled, className: .ingress.className,
          annotations: {"cert-manager.io/cluster-issuer":
            .ingress.annotations["cert-manager.io/cluster-issuer"]},
          tls: {secretName: .ingress.tls.secretName}}}' \
      <<< "$current_json")"
    if [[ "$(jq -S . <<< "$current_subset_json")" == "$(jq -S . <<< "$desired_json")" ]]; then
      [[ "$chart_version_action" == upgrade ]] || return 0
      # Only the chart version differs: keep the values the release holds beyond the ones set here.
      reuse_args=(--reset-then-reuse-values)
    fi
  fi
  helm upgrade --install "$HELM_RELEASE_NAME" "$CHART_REF" \
    ${chart_version_args[@]+"${chart_version_args[@]}"} \
    ${reuse_args[@]+"${reuse_args[@]}"} -f "$VALUES_PROD_FILE" \
    --set llm.existingSecret="$LLM_SECRET_NAME" \
    --set psService.auth.issuer="$issuer" \
    --set psService.auth.audience="$audience" \
    --set psService.auth.cliClientId="$cli_client_id" \
    --set psService.auth.scopes="$scopes" \
    --set psService.authzBootstrapOwner.subject="$owner_subject" \
    --set psService.authzBootstrapOwner.issuer="$owner_issuer" \
    --set psService.authentik.baseUrl="$authentik_base_url" \
    --set psService.authentik.publicUrl="$authentik_public_url" \
    --set psService.publicUrl="$public_url" \
    --set ingress.enabled=true \
    --set ingress.className="$INGRESS_CLASS" \
    --set 'ingress.annotations.cert-manager\.io/cluster-issuer'="$CLUSTER_ISSUER_NAME" \
    --set ingress.tls.secretName="${PS_SERVICE_NAME}-tls" \
    --wait --wait-for-jobs --timeout 10m >/dev/null
  made_changes=true
}

# adopt_legacy_ingress <name>: Takes over an Ingress that exists without Helm ownership so the
# release can manage it in place; an Ingress already owned by this release is skipped, an absent
# one is left for the chart to create, and an Ingress owned by another release stops the run.
# Only the ownership annotations and label are written; the Ingress is never deleted or applied.
adopt_legacy_ingress() {
  local name="$1" ownership label release_name release_namespace namespace
  if ! ownership="$(kubectl get ingress "$name" --ignore-not-found -o \
    jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}{"|"}{.metadata.annotations.meta\.helm\.sh/release-name}{"|"}{.metadata.annotations.meta\.helm\.sh/release-namespace}' \
    2>&1)"; then
    print_error 'Could not read Ingress %s: %s. Fix: check cluster access (kubectl auth can-i get ingress), then re-run.\n' \
      "$name" "$ownership"
    exit "$EXIT_FAILURE"
  fi
  [[ -n "$ownership" ]] || return 0
  IFS='|' read -r label release_name release_namespace <<<"$ownership"
  namespace="$(current_kube_namespace)"
  if [[ -n "$release_name" && "$release_name" != "$HELM_RELEASE_NAME" ]]; then
    print_error 'Ingress %s is owned by Helm release %s, not %s. Fix: remove or rename that Ingress, then re-run.\n' \
      "$name" "$release_name" "$HELM_RELEASE_NAME"
    exit "$EXIT_FAILURE"
  fi
  if [[ "$label" == "Helm" && "$release_name" == "$HELM_RELEASE_NAME" \
    && "$release_namespace" == "$namespace" ]]; then
    return 0
  fi
  kubectl annotate ingress "$name" "meta.helm.sh/release-name=${HELM_RELEASE_NAME}" \
    "meta.helm.sh/release-namespace=${namespace}" --overwrite >/dev/null
  kubectl label ingress "$name" app.kubernetes.io/managed-by=Helm --overwrite >/dev/null
  made_changes=true
}

# adopt_legacy_ingresses: runs adopt_legacy_ingress for the PS Service and Authentik Ingresses.
adopt_legacy_ingresses() {
  adopt_legacy_ingress "$PS_SERVICE_NAME"
  adopt_legacy_ingress "$AUTHENTIK_SERVICE_NAME"
}

# ---------------------------------------------------------------------------------------------
# Public exposure: AKS application-routing add-on (managed NGINX ingress controller) + Azure's
# own public-IP DNS label -- gives a "<label>.<region>.cloudapp.azure.com" hostname with no
# customer-owned domain or DNS zone required.
# ---------------------------------------------------------------------------------------------

# Env-var-overridable so tests can poll on a millisecond timescale instead of the real worst
# case -- same `${VAR:-default}` shape as PROVIDER_REGISTRATION_WAIT_ATTEMPTS/_INTERVAL_SECONDS
# above.
readonly INGRESS_IP_WAIT_ATTEMPTS="${INGRESS_IP_WAIT_ATTEMPTS:-30}"
readonly INGRESS_IP_WAIT_INTERVAL_SECONDS="${INGRESS_IP_WAIT_INTERVAL_SECONDS:-10}"

# The AKS application-routing add-on's own fixed ingress-class name, set by Azure itself on every
# cluster with the add-on enabled -- used by the ClusterIssuer HTTP-01 solver below and as the
# chart Ingress class.
readonly INGRESS_CLASS="webapprouting.kubernetes.azure.com"

# approuting_enabled <cluster_name>: true if the add-on is already on.
approuting_enabled() {
  local cluster_name="$1"
  [[ "$(az aks show --name "$cluster_name" --resource-group "$RESOURCE_GROUP_NAME" \
    --query "ingressProfile.webAppRouting.enabled" -o tsv)" == "true" ]]
}

# ensure_approuting <cluster_name>: create-if-absent the managed NGINX ingress controller add-on
# that publishes PS Service and Authentik.
ensure_approuting() {
  local cluster_name="$1"
  if approuting_enabled "$cluster_name"; then
    return 0
  fi
  az aks approuting enable --name "$cluster_name" --resource-group "$RESOURCE_GROUP_NAME" >/dev/null
  made_changes=true
}

# fetch_ingress_public_ip: polls the add-on's managed ingress-nginx Service for its LoadBalancer
# external IP -- namespace/Service name (app-routing-system/nginx) are the add-on's own fixed
# names, set by AKS itself whenever application-routing is enabled. Fails explicitly, naming the
# resource it was waiting on, rather than leaving a hard-to-diagnose empty hostname downstream.
fetch_ingress_public_ip() {
  local ip="" _attempt
  for _attempt in $(seq 1 "$INGRESS_IP_WAIT_ATTEMPTS"); do
    ip="$(kubectl get service nginx --namespace app-routing-system \
      --output jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
    [[ -n "$ip" ]] && break
    sleep "$INGRESS_IP_WAIT_INTERVAL_SECONDS"
  done
  if [[ -z "$ip" ]]; then
    print_error 'Timed out waiting for the app-routing ingress controller to get a public IP.\n'
    exit "$EXIT_FAILURE"
  fi
  printf '%s' "$ip"
}

# fetch_public_ip_resource_id <ip_address> <node_resource_group>: the add-on's public IP lives in
# the AKS-managed node resource group (the auto-generated "MC_*" resource group where AKS
# creates load-balancer and public-IP resources), not $RESOURCE_GROUP_NAME.
fetch_public_ip_resource_id() {
  local ip_address="$1" node_resource_group="$2"
  az network public-ip list --resource-group "$node_resource_group" \
    --query "[?ipAddress=='$ip_address'].id | [0]" -o tsv
}

# ensure_dns_label <public_ip_id> <label>: create-if-absent Azure's own public-IP DNS label
# -- no external registrar or Azure DNS zone.
# Read-before-write like grant_keyvault_access/grant_aks_rbac_access's sibling functions above.
ensure_dns_label() {
  local public_ip_id="$1" label="$2"
  local current_label
  current_label="$(az_no_pathconv network public-ip show --ids "$public_ip_id" \
    --query "dnsSettings.domainNameLabel" -o tsv 2>/dev/null || true)"
  if [[ "$current_label" == "$label" ]]; then
    return 0
  fi
  az_no_pathconv network public-ip update --ids "$public_ip_id" --dns-name "$label" >/dev/null
  made_changes=true
}

# fetch_public_ip_fqdn <public_ip_id>: prints the resulting "<label>.<region>.cloudapp.azure.com"
# hostname.
fetch_public_ip_fqdn() {
  az_no_pathconv network public-ip show --ids "$1" --query "dnsSettings.fqdn" -o tsv
}

# ---------------------------------------------------------------------------------------------
# cert-manager install + wait-for-Available, then ClusterIssuer.
# ---------------------------------------------------------------------------------------------

readonly CERT_MANAGER_CHART_REF="oci://quay.io/jetstack/charts/cert-manager"
readonly CERT_MANAGER_RELEASE_NAME="cert-manager"
readonly CERT_MANAGER_NAMESPACE="cert-manager"
readonly CERT_MANAGER_READY_TIMEOUT="180s"
readonly CLUSTER_ISSUER_NAME="letsencrypt-prod"

# ensure_cert_manager: create-if-absent install of cert-manager itself, via its own published OCI
# chart -- the AKS application-routing add-on installs only the NGINX ingress controller, NOT
# cert-manager: there is no bundled cert-manager for a ClusterIssuer to use unless this script
# installs one itself. Waits for the controller/webhook/cainjector deployments to report
# Available BEFORE returning -- a fresh install's admission webhook needs its own
# cert issued before it can admit the ClusterIssuer ensure_cluster_issuer creates next; applying
# one immediately after a fresh install can intermittently fail webhook admission otherwise.
ensure_cert_manager() {
  if helm status "$CERT_MANAGER_RELEASE_NAME" --namespace "$CERT_MANAGER_NAMESPACE" \
      >/dev/null 2>&1; then
    return 0
  fi
  helm upgrade --install "$CERT_MANAGER_RELEASE_NAME" "$CERT_MANAGER_CHART_REF" \
    --namespace "$CERT_MANAGER_NAMESPACE" --create-namespace --set crds.enabled=true >/dev/null
  made_changes=true
  kubectl wait --for=condition=Available --timeout="${CERT_MANAGER_READY_TIMEOUT}" \
    --namespace "$CERT_MANAGER_NAMESPACE" \
    deployment/cert-manager deployment/cert-manager-webhook deployment/cert-manager-cainjector \
    >/dev/null
}

# ensure_cluster_issuer <email>: create-if-absent (via kubectl apply, itself idempotent) a Let's
# Encrypt ClusterIssuer using the cert-manager install above, HTTP-01 solved through the
# app-routing add-on's own nginx ingress class -- MUST run only after ensure_cert_manager's own
# wait-for-Available has already returned (main() below calls these in that fixed order, never
# the reverse).
ensure_cluster_issuer() {
  local email="$1" apply_output
  apply_output="$(kubectl apply -f - <<EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: ${CLUSTER_ISSUER_NAME}
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ${email}
    privateKeySecretRef:
      name: ${CLUSTER_ISSUER_NAME}-key
    solvers:
      - http01:
          ingress:
            ingressClassName: ${INGRESS_CLASS}
EOF
)"
  apply_output_changed "$apply_output" && made_changes=true
  return 0
}

# ---------------------------------------------------------------------------------------------
# Authentik readiness, the closing provisioning summary, and `--rotate-key` mode carried over
# from scripts/deploy-llm.sh's own proven implementation.
# ---------------------------------------------------------------------------------------------

# wait_for_authentik: blocks until Authentik's server Deployment is Ready -- the owner-creation
# step below talks to its API. A first install pulls large images, hence the generous default.
wait_for_authentik() {
  if ! kubectl rollout status "deployment/${AUTHENTIK_SERVICE_NAME}" \
      --timeout="$AUTHENTIK_ROLLOUT_TIMEOUT" >/dev/null; then
    print_error 'deployment/%s did not become Ready within %s. Fix: look at the pod events (kubectl get pods; kubectl describe pod ...; kubectl logs deploy/%s), then re-run.\n' \
      "$AUTHENTIK_SERVICE_NAME" "$AUTHENTIK_ROLLOUT_TIMEOUT" "$AUTHENTIK_SERVICE_NAME"
    exit "$EXIT_FAILURE"
  fi
}

# provision_authentik_owner <hostname>: creates the owner in Authentik and issues the enrolment
# link (via lib/authentik-owner.sh). The admin API is deliberately NOT on the public
# Ingress, so the calls go through a loopback `kubectl port-forward` to the Authentik Service; the
# link Authentik returns names that loopback address, so the lib rewrites it onto the public
# `https://<hostname>/auth` authority. A failure leaves no half-created owner.
provision_authentik_owner() {
  local hostname="$1" kube_namespace
  kube_namespace="$(current_kube_namespace)"
  read_bootstrap_token "$kube_namespace" "$AUTHENTIK_API_TOKEN_SECRET_NAME" \
    || exit "$EXIT_FAILURE"
  start_authentik_port_forward "$kube_namespace" "$AUTHENTIK_SERVICE_NAME" 80 \
    || exit "$EXIT_FAILURE"
  AUTHENTIK_API_BASE="http://127.0.0.1:${AUTHENTIK_PF_LOCAL_PORT}/auth"
  AUTHENTIK_LINK_BASE="https://${hostname}/auth"
  AUTHENTIK_CURL_ARGS=()
  if ! provision_owner "$OWNER_EMAIL"; then
    stop_authentik_port_forward
    exit "$EXIT_FAILURE"
  fi
  stop_authentik_port_forward
}

# print_provisioning_summary: evaluator-visible closing message -- names which secrets now
# exist (never their values -- only the fixed secret NAMES already written, never read back), whether anything actually changed anywhere in the whole chain,
# and the resulting HTTPS URL. Reads $made_changes/$public_hostname directly (both already
# process-wide state by this point, top-of-file note) rather than taking parameters, matching
# scripts/deploy-llm.sh's own print_provisioning_summary's no-argument shape. Also surfaces
# $TLS_CONTACT_EMAIL when set (ps-defaults.conf or the interactive prompt in
# prompt_for_tls_contact_email) so the evaluator can see which address cert-expiry notices go to
# without having to dig through the conf file.
print_provisioning_summary() {
  if [[ "$made_changes" == true ]]; then
    printf 'Policy System provisioned. Wrote secrets: AZURE-API-BASE, AZURE-API-KEY, AZURE-API-VERSION.\n'
  else
    printf 'Policy System already up to date -- no changes made.\n'
  fi
  printf 'PS Service: https://%s\n' "$public_hostname"
  if [[ -n "$TLS_CONTACT_EMAIL" ]]; then
    printf 'TLS contact email: %s\n' "$TLS_CONTACT_EMAIL"
  fi
  # The enrolment link is a single-use credential-equivalent: printed here, once, last.
  print_owner_link_message
}

# require_account_exists <account_name>: fails clearly if the AIServices account has not been
# provisioned yet -- --rotate-key has nothing to rotate otherwise. Same check as
# scripts/deploy-llm.sh's require_account_exists, with a message that names this script's own
# flag.
require_account_exists() {
  local account_name="$1"
  if ! az cognitiveservices account show --name "$account_name" \
      --resource-group "$RESOURCE_GROUP_NAME" >/dev/null 2>&1; then
    print_error 'Azure AIServices account %s not found. Run scripts/deploy-ps-prod.sh first (without --rotate-key).\n' \
      "$account_name"
    exit "$EXIT_FAILURE"
  fi
}

# require_keyvault_exists <vault_name> <flag_name>: fails clearly if the Key Vault has not been
# created yet. <flag_name> (e.g. "--rotate-key") names the calling rotation mode's own flag in
# the error message.
require_keyvault_exists() {
  local vault_name="$1" flag_name="$2"
  if ! keyvault_exists "$vault_name"; then
    print_error 'Key Vault %s not found. Run scripts/deploy-ps-prod.sh first (without %s).\n' \
      "$vault_name" "$flag_name"
    exit "$EXIT_FAILURE"
  fi
}

# active_key_slot <stored_value> <key2_value>: prints "key2" if <stored_value> (the currently
# persisted AZURE-API-KEY secret) equals the account's current key2 value, "key1" otherwise --
# including when neither key matches (a from-scratch rotation), matching this script's own
# initial secret write in ensure_account/the write_secret_if_changed call in main(), which always
# writes key1's value first. Same logic as scripts/deploy-llm.sh's active_key_slot.
active_key_slot() {
  local stored_value="$1" key2_value="$2"
  if [[ "$stored_value" == "$key2_value" ]]; then
    printf 'key2'
  else
    printf 'key1'
  fi
}

# inactive_key_slot <slot>: prints the other slot name. Same logic as
# scripts/deploy-llm.sh's inactive_key_slot.
inactive_key_slot() {
  local slot="$1"
  if [[ "$slot" == "key1" ]]; then
    printf 'key2'
  else
    printf 'key1'
  fi
}

# rotate_key_main: `--rotate-key` mode, the same flow as scripts/deploy-llm.sh's (main()
# branches into this immediately after flag parsing, before any provisioning step runs). Reads the stored AZURE-API-KEY secret, compares it against the
# account's live key1/key2 to find the active slot, and regenerates the inactive one. Never prints
# an old or new key value -- new_value/stored_value/key2_value only ever feed the next
# `az`/comparison, matching read_secret_value's own never-log discipline.
rotate_key_main() {
  local subscription_id account_name vault_name
  subscription_id="$(fetch_subscription_id)"
  account_name="$(llm_account_name "$subscription_id")"
  vault_name="$(llm_keyvault_name "$subscription_id")"

  log_step "Checking AIServices account and Key Vault exist"
  require_account_exists "$account_name"
  require_keyvault_exists "$vault_name" "--rotate-key"

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

# reset_owner_passkey_main: the --reset-owner-passkey mode against the current kube context. No
# Azure call and no config file: it needs only cluster access. Changes only the owner's passkeys
# and what authenticates as the owner, then prints one fresh enrolment link.
reset_owner_passkey_main() {
  owner_reset_require_tools || exit "$EXIT_FAILURE"
  SKIP_OWNER_PROMPT="$skip_confirmation"
  trap stop_authentik_port_forward EXIT
  # The bundle's VERSION describes the published chart only; a local chart is not checked.
  local version_file="${SCRIPT_DIR}/../VERSION"
  [[ "$CHART_REF" == oci://* ]] || version_file=""
  run_owner_passkey_reset prod "$(current_kube_namespace)" "$HELM_RELEASE_NAME" \
    "$AUTHENTIK_API_TOKEN_SECRET_NAME" "$AUTHENTIK_SERVICE_NAME" "$PS_SERVICE_NAME" \
    "$version_file" || exit "$EXIT_FAILURE"
}

main() {
  parse_args "$@"

  if [[ "$reset_owner_passkey" == true ]]; then
    reset_owner_passkey_main
    return
  fi

  check_required_tools az jq

  if [[ "$rotate_key" == true ]]; then
    rotate_key_main
    return
  fi

  print_experimental_notice

  check_required_tools kubectl helm curl
  read_bundle_version "$SCRIPT_DIR" "$CHART_REF" || exit "$EXIT_FAILURE"

  log_step "Loading and validating $CONFIG_FILE_DISPLAY_PATH"
  load_config
  prompt_for_tls_contact_email
  resolve_owner_email
  validate_config

  local subscription_id
  subscription_id="$(fetch_subscription_id)"

  local account_name vault_name cluster_name label
  account_name="$(llm_account_name "$subscription_id")"
  vault_name="$(llm_keyvault_name "$subscription_id")"
  cluster_name="$(aks_cluster_name "$subscription_id")"
  label="$(dns_label "$subscription_id")"

  print_confirmation_table "$account_name" "$vault_name" "$cluster_name" "$label"
  confirm_or_exit

  log_step "Checking subscription-level RBAC"
  rbac_preflight "$subscription_id"

  log_step "Registering required resource providers"
  ensure_providers_registered

  local region_selection selected_region selected_region_model_list
  region_selection="$(select_region)"
  selected_region="${region_selection%%$'\n'*}"
  selected_region_model_list="${region_selection#*$'\n'}"
  validate_capacity_range "$selected_region_model_list"

  local chat_model_version embed_model_version
  chat_model_version="$(model_version "$selected_region_model_list" "$LLM_CHAT_MODEL_NAME")"
  embed_model_version="$(model_version "$selected_region_model_list" "$LLM_EMBED_MODEL_NAME")"
  log_step "Selected region: $selected_region (chat model version $chat_model_version, embed model version $embed_model_version)"

  log_step "Checking Azure quota in $selected_region"
  check_quota "$selected_region" "$account_name"

  log_step "Ensuring resource group $RESOURCE_GROUP_NAME"
  ensure_resource_group "$selected_region"
  log_step "Ensuring AIServices account $account_name"
  ensure_account "$account_name" "$selected_region"
  log_step "Ensuring chat deployment $LLM_CHAT_MODEL_NAME"
  ensure_deployment "$account_name" "$LLM_CHAT_MODEL_NAME" "$LLM_CHAT_MODEL_SKU" \
    "$LLM_CHAT_MODEL_CAPACITY" "$chat_model_version"
  log_step "Ensuring embedding deployment $LLM_EMBED_MODEL_NAME"
  ensure_deployment "$account_name" "$LLM_EMBED_MODEL_NAME" "$LLM_EMBED_MODEL_SKU" \
    "$LLM_EMBED_MODEL_CAPACITY" "$embed_model_version"
  log_step "Ensuring Key Vault $vault_name"
  ensure_keyvault "$vault_name" "$selected_region"
  log_step "Granting Key Vault access to the signed-in identity"
  grant_keyvault_access "$vault_name"

  log_step "Writing LLM secrets to $vault_name"
  local key1
  key1="$(fetch_account_key1 "$account_name")"
  write_secret_if_changed "$vault_name" "AZURE-API-BASE" "$account_endpoint"
  write_secret_if_changed "$vault_name" "AZURE-API-KEY" "$key1"
  write_secret_if_changed "$vault_name" "AZURE-API-VERSION" "$AZURE_API_VERSION_LITERAL"

  log_step "Checking AKS node VM size and quota in $selected_region"
  check_aks_vm_size "$cluster_name" "$selected_region"

  log_step "Ensuring AKS cluster $cluster_name"
  ensure_aks_cluster "$cluster_name" "$selected_region"
  log_step "Granting AKS RBAC access to the signed-in identity"
  grant_aks_rbac_access "$cluster_name"
  log_step "Fetching AKS credentials for $cluster_name"
  ensure_aks_credentials "$cluster_name"
  plan_chart_version_action "$HELM_RELEASE_NAME" || exit "$EXIT_FAILURE"

  log_step "Syncing LLM credentials into the cluster"
  ensure_llm_secret "$vault_name"

  # The ingress/DNS-label/hostname-resolution steps come before "Reconciling the Helm release":
  # Authentik's issuer is a path under PS Service's own hostname
  # ("https://<hostname>/auth/application/o/ps-cli/"), so $public_hostname must be resolved before
  # ensure_release computes it.
  log_step "Enabling the application-routing ingress add-on on $cluster_name"
  ensure_approuting "$cluster_name"

  log_step "Waiting for the ingress controller's public IP"
  local node_resource_group ingress_ip public_ip_id
  node_resource_group="$(az aks show --name "$cluster_name" --resource-group "$RESOURCE_GROUP_NAME" \
    --query nodeResourceGroup -o tsv)"
  ingress_ip="$(fetch_ingress_public_ip)"
  public_ip_id="$(fetch_public_ip_resource_id "$ingress_ip" "$node_resource_group")"

  log_step "Setting public DNS label $label"
  ensure_dns_label "$public_ip_id" "$label"
  public_hostname="$(fetch_public_ip_fqdn "$public_ip_id")"
  log_step "Public hostname: $public_hostname"

  # cert-manager and the ClusterIssuer exist before the Helm release: the chart's Ingress carries
  # the issuer annotation, and PS Service's startup does OIDC discovery against the public
  # https://<hostname>/auth/... issuer, which needs the certificate cert-manager issues.
  log_step "Installing cert-manager"
  ensure_cert_manager

  log_step "Ensuring the Let's Encrypt ClusterIssuer"
  ensure_cluster_issuer "$TLS_CONTACT_EMAIL"

  log_step "Adopting existing Ingresses into the Helm release"
  adopt_legacy_ingresses

  log_step "Reconciling the Helm release"
  # Fixed constants, never an `az ad app show`-derived value (no tenant app registrations): the
  # blueprint defines exactly one OAuth2 Provider/Application, both named "ps-cli", so audience
  # and cliClientId are the same literal, not two distinct app IDs. issuer is path-based under PS
  # Service's own already-resolved $public_hostname -- never a second, Authentik-only hostname,
  # never an external IdP's host.
  local issuer
  issuer="https://${public_hostname}/auth/application/o/${AUTHENTIK_APP_SLUG}/"
  ensure_release "$issuer" "$AUTHENTIK_APP_SLUG" "$AUTHENTIK_APP_SLUG" "$AUTHENTIK_SCOPES" \
    "$OWNER_EMAIL" "$issuer" "$AUTHENTIK_IN_CLUSTER_BASE_URL" "https://${public_hostname}/auth" \
    "https://${public_hostname}"

  # The owner (an Authentik admin, username == email, no password) and the
  # single-use passkey-enrolment link. The port-forward is killed however the script ends.
  trap stop_authentik_port_forward EXIT
  log_step "Waiting for Authentik to become Ready"
  wait_for_authentik
  log_step "Creating the owner and issuing the passkey-enrolment link"
  provision_authentik_owner "$public_hostname"

  print_provisioning_summary
}

main "$@"
