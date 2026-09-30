#!/usr/bin/env bash
# Deploys the evaluator profile of Policy System onto the local kind cluster and creates the
# first user (issue #165): the bundled Authentik served over locally trusted TLS (passkeys need a
# secure context), the owner created as an Authentik admin, and a single-use passkey-enrolment
# link printed. No password is ever set, prompted for, or logged.
#
# Usage:
#   scripts/deploy-ps-eval.sh [--owner-email <address>] [--hostname <name>] [--yes]
#
#   --owner-email  The first SystemOwner. Prompted for when omitted. The address is both the
#                  Authentik username and the OIDC `sub` PS Service expects, so the deploy sets
#                  the owner identity in one pass.
#   --hostname     The name Authentik is served under (default authentik.local). It must resolve
#                  to this machine on every machine that logs in (/etc/hosts); the script never
#                  edits /etc/hosts itself.
#   --yes          Non-interactive: never prompt (an owner email is then required).
#
# Environment: PS_CHART_REF (chart path or OCI ref; default the published OCI chart),
# PS_EVAL_STATE_DIR (local CA and certificate; default ~/.config/policy-system/eval-tls),
# PS_OWNER_LINK_TTL (minutes the enrolment link stays valid; default 30), PS_ROLLOUT_TIMEOUT.
#
# Re-running is the owner-recovery path (gated by cluster access): an owner with no passkey gets a
# fresh link, an owner with one is left untouched. Certificate verification is never disabled:
# every call to Authentik trusts the local CA explicitly.
#
# Exit codes: 2 usage error, 1 validation/preflight/business failure, 0 success.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail

# Hard-stop failure messages go through print_error: red only on a terminal, plain in a CI log
# (respects NO_COLOR, https://no-color.org). Same shape as scripts/deploy-ps-prod.sh.
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
  # shellcheck disable=SC2059
  printf "${COLOR_RED}${format}${COLOR_RESET}" "$@" >&2
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/authentik-owner.sh
source "${SCRIPT_DIR}/lib/authentik-owner.sh"
# shellcheck source=lib/local-tls.sh
source "${SCRIPT_DIR}/lib/local-tls.sh"

readonly EXIT_USAGE=2
readonly EXIT_FAILURE=1
USAGE="usage: $(basename "$0") [--owner-email <address>] [--hostname <name>] [--yes]"
readonly USAGE

readonly HELM_RELEASE_NAME="policy-system"
readonly DEFAULT_CHART_REF="oci://ghcr.io/mindovermachine-dev/charts/policy-system"
readonly DEFAULT_HOSTNAME="authentik.local"
readonly LLM_SECRET_NAME="policy-system-llm-credentials"
readonly TLS_SECRET_NAME="policy-system-local-tls"
readonly API_TOKEN_SECRET_NAME="policy-system-authentik-api-token"
readonly AUTHENTIK_SERVER_SERVICE="${HELM_RELEASE_NAME}-authentik-server"
# The kind cluster maps this host port to Authentik's HTTPS NodePort (deploy/kind/cluster.yaml,
# authentik.server.service.nodePortHttps in the chart): the only Authentik port on the host.
readonly AUTHENTIK_HTTPS_PORT=30443
readonly AUTHENTIK_APP_SLUG="ps-cli"
readonly AUTHENTIK_SCOPES="openid profile email offline_access"
readonly PS_SERVICE_LOCAL_URL="http://127.0.0.1:8000"
readonly INSTALL_GUIDE="docs/artifacts/installation-guide.md"

# Process-wide state, plain assignment only (never inside $(...), so writes reach the caller).
# OWNER_EMAIL, SKIP_OWNER_PROMPT, AUTHENTIK_API_BASE, AUTHENTIK_LINK_BASE and AUTHENTIK_CURL_ARGS
# are the interface lib/authentik-owner.sh reads.
OWNER_EMAIL=""
SKIP_OWNER_PROMPT=false
AUTHENTIK_API_BASE=""
AUTHENTIK_LINK_BASE=""
AUTHENTIK_CURL_ARGS=()
hostname_arg="$DEFAULT_HOSTNAME"
kind_context=""
namespace="default"
node_ip=""
made_release_change=false
cert_refreshed=false

# parse_args <args...>: sets OWNER_EMAIL/hostname_arg/SKIP_OWNER_PROMPT from the flags.
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes) SKIP_OWNER_PROMPT=true ;;
      --owner-email | --hostname)
        if [[ $# -lt 2 || -z "$2" ]]; then
          print_error '%s needs a value\n%s\n' "$1" "$USAGE"
          exit "$EXIT_USAGE"
        fi
        if [[ "$1" == "--owner-email" ]]; then
          OWNER_EMAIL="$2"
        else
          hostname_arg="$2"
        fi
        shift
        ;;
      -h | --help)
        printf '%s\n' "$USAGE"
        exit 0
        ;;
      *)
        print_error 'unknown flag: %s\n%s\n' "$1" "$USAGE"
        exit "$EXIT_USAGE"
        ;;
    esac
    shift
  done
}

log_step() {
  printf '==> %s\n' "$1" >&2
}

# check_required_tools: hard-stops, before any state change, unless every prerequisite CLI is on
# PATH, listing all the missing ones (and the install guide) in one message.
check_required_tools() {
  local tool missing=()
  for tool in kubectl helm openssl jq curl; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
  done
  if ! command -v podman >/dev/null 2>&1 && ! command -v docker >/dev/null 2>&1; then
    missing+=("podman (or docker)")
  fi
  if [[ ${#missing[@]} -gt 0 ]]; then
    local joined="" item
    for item in "${missing[@]}"; do
      joined="${joined:+$joined, }$item"
    done
    print_error 'Missing required tool(s): %s\n' "$joined"
    print_error 'Fix: install them (see %s, prerequisites) and re-run.\n' "$INSTALL_GUIDE"
    exit "$EXIT_FAILURE"
  fi
}

# require_kind_context: refuses to run unless the active kubectl context is a kind-* context, so
# a deploy can never land on the wrong cluster. Sets kind_context and namespace.
require_kind_context() {
  kind_context="$(kubectl config current-context)"
  if [[ "$kind_context" != kind-* ]]; then
    print_error 'Current kubectl context "%s" is not a kind-* context. Fix: switch to your kind cluster (kubectl config use-context kind-<name>) and re-run.\n' \
      "$kind_context"
    exit "$EXIT_FAILURE"
  fi
  namespace="$(current_kube_namespace)"
}

# require_llm_secret: the chart's PS Service reads its LLM credentials from this Secret.
require_llm_secret() {
  if ! kubectl -n "$namespace" get secret "$LLM_SECRET_NAME" -o name >/dev/null 2>&1; then
    print_error 'Secret %s not found in namespace %s. Fix: create it first (scripts/sync-llm-secrets-to-kind.sh, see %s step 5), then re-run.\n' \
      "$LLM_SECRET_NAME" "$namespace" "$INSTALL_GUIDE"
    exit "$EXIT_FAILURE"
  fi
}

# detect_node_ip: the kind node container's own IP on the `kind` network (PS Service's pod must
# resolve the Authentik hostname to it). Works under podman and docker; the node's name follows
# the cluster name (context kind-<name> -> <name>-control-plane).
detect_node_ip() {
  local node="${kind_context#kind-}-control-plane" engine
  node_ip=""
  for engine in podman docker; do
    command -v "$engine" >/dev/null 2>&1 || continue
    node_ip="$("$engine" inspect "$node" \
      --format '{{ (index .NetworkSettings.Networks "kind").IPAddress }}' 2>/dev/null || true)"
    [[ -n "$node_ip" ]] && break
  done
  if [[ -z "$node_ip" ]]; then
    print_error 'Cannot find the kind node container "%s". Fix: check the cluster is running (kind get clusters, podman ps) and that kubectl points at it, then re-run.\n' \
      "$node"
    exit "$EXIT_FAILURE"
  fi
}

# release_values_json <host> <issuer> <checksum>: prints the complete set of user values this
# script gives the chart, in the JSON shape `helm get values -o json` returns (so the release
# comparison is exact). Sets, in ONE deploy: the local-TLS Secret, the Authentik pods' mount of
# it (the chart cannot make that conditional), the issuer/audience/client for ps-cli, the
# bootstrap owner (subject == the owner's email, issuer == the issuer above) and the in-reach
# Authentik base URL PS Service calls for invitations.
release_values_json() {
  local host="$1" issuer="$2" checksum="$3"
  jq -n --arg llm "$LLM_SECRET_NAME" --arg tls "$TLS_SECRET_NAME" --arg host "$host" \
    --arg ip "$node_ip" --arg issuer "$issuer" --arg app "$AUTHENTIK_APP_SLUG" \
    --arg scopes "$AUTHENTIK_SCOPES" --arg owner "$OWNER_EMAIL" \
    --arg base "https://${host}:${AUTHENTIK_HTTPS_PORT}" --arg sum "$checksum" \
    '{llm: {existingSecret: $llm},
      localTls: {secretName: $tls},
      authentik: {enabled: true,
        global: {volumes: [{name: "ps-tls", secret: {secretName: $tls}}],
          volumeMounts: [{name: "ps-tls", mountPath: "/ps-tls", readOnly: true}],
          podAnnotations: {"checksum/local-tls": $sum}}},
      psService: {localTestBypass: {enabled: false},
        authentikHostname: $host, authentikHostAliasIP: $ip,
        auth: {issuer: $issuer, audience: $app, cliClientId: $app, scopes: $scopes},
        authzBootstrapOwner: {subject: $owner, issuer: $issuer},
        authentik: {baseUrl: $base}}}'
}

# ensure_release <values-json>: write-if-changed. Plain `helm upgrade --install` always creates a
# new revision, so the desired values are compared with `helm get values` first; the script
# passes every value it sets in one file and no other, so the comparison is on the whole object.
ensure_release() {
  local desired_json="$1" chart_ref="${PS_CHART_REF:-$DEFAULT_CHART_REF}"
  local values_file current_json
  if helm status "$HELM_RELEASE_NAME" >/dev/null 2>&1; then
    current_json="$(helm get values "$HELM_RELEASE_NAME" -o json)"
    if [[ "$(jq -S . <<<"$current_json")" == "$(jq -S . <<<"$desired_json")" ]]; then
      printf 'Helm release %s unchanged.\n' "$HELM_RELEASE_NAME" >&2
      return 0
    fi
  fi
  values_file="$(local_tls_state_dir)/release-values.json"
  printf '%s\n' "$desired_json" >"$values_file"
  helm upgrade --install "$HELM_RELEASE_NAME" "$chart_ref" -f "$values_file" >/dev/null
  made_release_change=true
}

# wait_for_rollouts <name...>: blocks until each named Deployment of the release is Ready. Image
# pulls on a first install can take minutes, hence the generous default.
wait_for_rollouts() {
  local name timeout="${PS_ROLLOUT_TIMEOUT:-900s}"
  for name in "$@"; do
    if ! kubectl -n "$namespace" rollout status "deployment/${HELM_RELEASE_NAME}-${name}" \
      --timeout="$timeout" >/dev/null; then
      print_error 'deployment/%s-%s did not become Ready within %s. Fix: look at the pod events (kubectl get pods; kubectl describe pod ...; kubectl logs deploy/%s-%s), then re-run.\n' \
        "$HELM_RELEASE_NAME" "$name" "$timeout" "$HELM_RELEASE_NAME" "$name"
      exit "$EXIT_FAILURE"
    fi
  done
}

# ensure_authentik_serves_local_cert <host>: Authentik must serve the local leaf on the HTTPS
# NodePort before the owner is provisioned through it. Nothing is done when it already does; when
# it does not (a regenerated certificate), the local-TLS blueprint is applied through the loopback
# HTTP port-forward (the HTTPS listener cannot be trusted yet) and the new certificate awaited,
# without restarting Authentik. Sets cert_refreshed when it had to act.
ensure_authentik_serves_local_cert() {
  local host="$1" ca want have
  ca="$(local_tls_state_dir)/ca.pem"
  want="$(leaf_fingerprint)"
  have="$(served_fingerprint "$host" "$AUTHENTIK_HTTPS_PORT" "$ca" 127.0.0.1)"
  if [[ "$have" == "$want" ]]; then
    return 0
  fi
  log_step "Refreshing the certificate Authentik serves"
  cert_refreshed=true
  start_authentik_port_forward "$namespace" "$AUTHENTIK_SERVER_SERVICE" 80 || exit "$EXIT_FAILURE"
  AUTHENTIK_API_BASE="http://127.0.0.1:${AUTHENTIK_PF_LOCAL_PORT}"
  AUTHENTIK_CURL_ARGS=()
  if ! check_token_accepted \
    || ! refresh_authentik_cert_if_differs "$host" "$AUTHENTIK_HTTPS_PORT" "$ca" 127.0.0.1; then
    stop_authentik_port_forward
    exit "$EXIT_FAILURE"
  fi
  stop_authentik_port_forward
}

# print_closing_summary <host>: what the evaluator does next. The enrolment link is printed last,
# exactly once (it is a single-use credential-equivalent).
print_closing_summary() {
  local host="$1" ca
  ca="$(local_tls_state_dir)/ca.pem"
  if [[ "$made_release_change" == true ]]; then
    printf 'Policy System (evaluator profile) deployed.\n'
  else
    printf 'Policy System (evaluator profile) already up to date.\n'
  fi
  cat <<EOF

Authentik:  https://${host}:${AUTHENTIK_HTTPS_PORT}/
PS Service: ${PS_SERVICE_LOCAL_URL}

Before opening the enrolment link in a browser on this machine:
  1. Map the hostname:     echo '127.0.0.1 ${host}' | sudo tee -a /etc/hosts
  2. Trust the local CA:   ${ca}
     (macOS: sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ${ca})

For ps-cli and the MCP bridge (they read SSL_CERT_FILE, not the operating system trust store;
the variable REPLACES the default CA bundle for that process, so set it for ps-cli only):
  export SSL_CERT_FILE=${ca}
  ps-cli config set-context eval --url ${PS_SERVICE_LOCAL_URL}
  ps-cli config use-context eval
  ps-cli auth login

LAN colleagues: copy ${ca} to their machine and trust it there, and map ${host} to this
machine's LAN IP in their hosts file. Authentik's HTTPS login (port ${AUTHENTIK_HTTPS_PORT}) is
reachable from your LAN by design in this profile, admin UI included.

EOF
  print_owner_link_message
}

main() {
  parse_args "$@"

  check_required_tools
  require_kind_context
  require_llm_secret
  prompt_owner_email || exit "$EXIT_FAILURE"
  detect_node_ip

  trap stop_authentik_port_forward EXIT

  log_step "Preparing local TLS for ${hostname_arg}"
  ensure_local_leaf "$hostname_arg" || exit "$EXIT_FAILURE"
  apply_local_tls_secret "$namespace" "$TLS_SECRET_NAME"

  local issuer checksum
  issuer="https://${hostname_arg}:${AUTHENTIK_HTTPS_PORT}/application/o/${AUTHENTIK_APP_SLUG}/"
  checksum="$(leaf_fingerprint | tr -d ':' | tr '[:upper:]' '[:lower:]')"
  log_step "Reconciling the Helm release"
  ensure_release "$(release_values_json "$hostname_arg" "$issuer" "$checksum")"

  # Authentik first: PS Service discovers the OIDC issuer at startup and crash-loops until
  # Authentik serves the local certificate, so it is awaited only after that.
  log_step "Waiting for Authentik to become Ready"
  wait_for_rollouts authentik-server authentik-worker

  read_bootstrap_token "$namespace" "$API_TOKEN_SECRET_NAME" || exit "$EXIT_FAILURE"
  ensure_authentik_serves_local_cert "$hostname_arg"

  if [[ "$cert_refreshed" == true ]]; then
    # It may sit in CrashLoopBackOff from the window before the certificate was served; a new
    # pod skips the remaining back-off.
    kubectl -n "$namespace" rollout restart "deployment/${HELM_RELEASE_NAME}-ps-service" >/dev/null
  fi
  log_step "Waiting for PS Service to become Ready"
  wait_for_rollouts ps-service

  log_step "Creating the owner and issuing the enrolment link"
  AUTHENTIK_API_BASE="https://${hostname_arg}:${AUTHENTIK_HTTPS_PORT}"
  AUTHENTIK_LINK_BASE="$AUTHENTIK_API_BASE"
  AUTHENTIK_CURL_ARGS=(--cacert "$(local_tls_state_dir)/ca.pem"
    --resolve "${hostname_arg}:${AUTHENTIK_HTTPS_PORT}:127.0.0.1")
  provision_owner "$OWNER_EMAIL" || exit "$EXIT_FAILURE"

  print_closing_summary "$hostname_arg"
}

main "$@"
