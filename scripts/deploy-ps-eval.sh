#!/usr/bin/env bash
# Deploys the evaluator profile of Policy System onto the local kind cluster and creates the
# first user (issue #165): the bundled Authentik served over locally trusted TLS (passkeys need a
# secure context), the owner created as an Authentik admin, and a single-use passkey-enrolment
# link printed. No password is ever set, prompted for, or logged.
#
# Usage:
#   scripts/deploy-ps-eval.sh [--owner-email <address>] [--hostname <name>] [--lan-address <ip>]
#                             [--apply-host-setup] [--allow-public-host] [--yes]
#   scripts/deploy-ps-eval.sh --check-exposure [--allow-public-host]
#
#   --owner-email  The first SystemOwner. Prompted for when omitted. The address is both the
#                  Authentik username and the OIDC `sub` PS Service expects, so the deploy sets
#                  the owner identity in one pass.
#   --hostname     The name Authentik is served under (default ps.test). Every machine that
#                  logs in, this one included, maps it to this machine's LAN address in its
#                  hosts file (/etc/hosts). Names ending in
#                  .local are rejected: Claude Code cannot resolve them. IP addresses and
#                  names with characters other than letters, digits, dots and hyphens are
#                  rejected too. The local CA permits only this name, so a different name
#                  replaces the CA (trust the new one and remove the old).
#   --lan-address  This machine's LAN address, the one the hostname maps to. Detected when
#                  omitted: the address of the default-route interface, else the only private
#                  IPv4 address of the machine. The run stops before any change, naming this flag,
#                  when neither identifies one address.
#   --apply-host-setup
#                  Run the two sudo steps this machine needs when they are not already done
#                  (map the hostname to the LAN address in /etc/hosts; trust the local CA for TLS
#                  server authentication in the macOS system keychain) without asking. Without it,
#                  an interactive run asks first and a non-interactive run only prints the
#                  commands. Nothing is touched when both are already in place, and a hosts line
#                  that maps the hostname to another address is reported, never edited.
#   --allow-public-host
#                  Accept a machine that has a non-private address. The cluster publishes
#                  the proxy on every interface of the host (port 30443), so the run stops
#                  before any cluster call when an interface holds an address outside the
#                  loopback, link-local and private ranges (10/8, 172.16/12, 192.168/16,
#                  169.254/16, fc00::/7, fe80::/10). VPN addresses such as Tailscale's
#                  100.64/10 and global IPv6 addresses count as public. Pass the flag on every
#                  run, `--check-exposure` included, on such a machine.
#   --check-exposure
#                  Run only that address check and exit (0 passed, 1 failed), with no cluster
#                  or tool requirement. Run it before `kind create cluster`.
#   --yes          Non-interactive: never prompt (an owner email is then required).
#
# Environment: PS_CHART_REF (chart path or OCI ref; default the published OCI chart),
# PS_EVAL_STATE_DIR (local CA and certificate; default ~/.config/policy-system/eval-tls),
# PS_OWNER_LINK_TTL (minutes the enrolment link stays valid; default 30), PS_ROLLOUT_TIMEOUT,
# PS_HOSTS_FILE (hosts file checked and edited; default /etc/hosts).
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
# shellcheck source=lib/chart-version.sh
source "${SCRIPT_DIR}/lib/chart-version.sh"

readonly EXIT_USAGE=2
readonly EXIT_FAILURE=1
USAGE="usage: $(basename "$0") [--owner-email <address>] [--hostname <name>] [--lan-address <ip>] [--apply-host-setup] [--allow-public-host] [--yes] | --check-exposure [--allow-public-host]"
readonly USAGE

readonly HELM_RELEASE_NAME="policy-system"
readonly DEFAULT_CHART_REF="oci://ghcr.io/mindovermachine-dev/charts/policy-system"
readonly EVALUATOR_HOSTNAME="ps.test"
readonly LLM_SECRET_NAME="policy-system-llm-credentials"
readonly TLS_SECRET_NAME="policy-system-local-tls"
readonly CA_SECRET_NAME="policy-system-local-ca"
readonly API_TOKEN_SECRET_NAME="policy-system-authentik-api-token"
readonly AUTHENTIK_SERVER_SERVICE="${HELM_RELEASE_NAME}-authentik-server"
# The kind cluster maps this host port to the proxy's NodePort (deploy/kind/cluster.yaml,
# proxy.service.nodePort in the chart): the proxy serves PS Service and Authentik on it.
readonly PROXY_PORT=30443
# Authentik serves every page under this path (the chart's AUTHENTIK_WEB__PATH), so the issuer,
# the API base, the enrolment link and the admin UI all sit below it.
readonly AUTHENTIK_WEB_PATH="/auth"
readonly AUTHENTIK_APP_SLUG="ps-cli"
readonly AUTHENTIK_SCOPES="openid profile email offline_access"
readonly PS_CLI_CONTEXT="eval"
readonly INSTALL_GUIDE="docs/artifacts/installation-guide.md"

# Process-wide state, plain assignment only (never inside $(...), so writes reach the caller).
# OWNER_EMAIL, SKIP_OWNER_PROMPT, AUTHENTIK_API_BASE, AUTHENTIK_LINK_BASE and AUTHENTIK_CURL_ARGS
# are the interface lib/authentik-owner.sh reads.
OWNER_EMAIL=""
SKIP_OWNER_PROMPT=false
AUTHENTIK_API_BASE=""
AUTHENTIK_LINK_BASE=""
AUTHENTIK_CURL_ARGS=()
chart_version_args=()
hostname_arg="$EVALUATOR_HOSTNAME"
kind_context=""
namespace="default"
node_ip=""
made_release_change=false
apply_host_setup=false
allow_public_host=false
check_exposure_only=false
lan_address=""
needs_hosts=false
needs_trust=false
old_ca_still_trusted=false
# Read by lib/authentik-owner.sh: true when the hostname changed on an existing install, because
# the owner's passkeys are bound to the old name and are unusable (see announce_migration).
OWNER_IGNORE_EXISTING_DEVICES=false
installed_host=""
installed_issuer=""

# parse_args <args...>: sets OWNER_EMAIL/hostname_arg/SKIP_OWNER_PROMPT from the flags.
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes) SKIP_OWNER_PROMPT=true ;;
      --apply-host-setup) apply_host_setup=true ;;
      --allow-public-host) allow_public_host=true ;;
      --check-exposure) check_exposure_only=true ;;
      --owner-email | --hostname | --lan-address)
        if [[ $# -lt 2 || -z "$2" ]]; then
          print_error '%s needs a value\n%s\n' "$1" "$USAGE"
          exit "$EXIT_USAGE"
        fi
        set_value_flag "$1" "$2"
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
  reject_local_suffix_hostname
  local_tls_validate_hostname "$hostname_arg" || exit "$EXIT_USAGE"
  [[ -z "$lan_address" ]] || validate_lan_address_flag "$lan_address"
}

# set_value_flag <flag> <value>: stores the value of --owner-email, --hostname or --lan-address.
set_value_flag() {
  case "$1" in
    --owner-email) OWNER_EMAIL="$2" ;;
    --hostname) hostname_arg="$2" ;;
    --lan-address) lan_address="$2" ;;
    *) ;;
  esac
}

# is_ipv4_literal <value>: 0 for four dot-separated decimal fields, each at most 255.
is_ipv4_literal() {
  local shape='^[0-9]{1,3}(\.[0-9]{1,3}){3}$' octet
  [[ "$1" =~ $shape ]] || return 1
  for octet in ${1//./ }; do
    [[ $((10#$octet)) -le 255 ]] || return 1
  done
}

# validate_lan_address_flag <value>: hard-stops with a usage error unless the value is an IPv4
# address that is neither loopback nor unspecified.
validate_lan_address_flag() {
  if is_ipv4_literal "$1"; then
    case "$1" in
      127.* | 0.*) ;;
      *) return 0 ;;
    esac
  fi
  print_error 'Invalid --lan-address "%s": use this machine'"'"'s LAN IPv4 address, such as 192.168.1.5 (not a loopback address).\n%s\n' "$1" "$USAGE"
  exit "$EXIT_USAGE"
}

# reject_local_suffix_hostname: hard-stops (before any tool check or cluster call) on a --hostname
# ending in .local (case-insensitive, trailing dot allowed). Claude Code treats .local as mDNS and
# fails with ENOTFOUND even when /etc/hosts maps the name, so the evaluator could never sign in.
reject_local_suffix_hostname() {
  local lowered
  lowered="$(printf '%s' "$hostname_arg" | tr '[:upper:]' '[:lower:]')"
  case "$lowered" in
    *.local | *.local.)
      print_error 'Invalid --hostname "%s": it ends in .local. Claude Code cannot resolve .local names (it treats them as mDNS and fails with ENOTFOUND, even when /etc/hosts maps them), so you could not sign in. Fix: use another name, such as %s (the default), and re-run.\n' \
        "$hostname_arg" "$EVALUATOR_HOSTNAME"
      exit "$EXIT_USAGE"
      ;;
    *) ;;
  esac
}

log_step() {
  printf '==> %s\n' "$1" >&2
}

# enumerate_host_addresses: prints every IPv4 and IPv6 address of this machine's interfaces, one per
# line, from `ip` (Linux) or else `ifconfig` (macOS, BSD). Returns 1 when neither tool exists.
enumerate_host_addresses() {
  if command -v ip >/dev/null 2>&1; then
    ip -o addr show | awk '{ for (i = 1; i < NF; i++) if ($i == "inet" || $i == "inet6") print $(i + 1) }'
  elif command -v ifconfig >/dev/null 2>&1; then
    ifconfig | awk '{ for (i = 1; i < NF; i++) if ($i == "inet" || $i == "inet6") print $(i + 1) }'
  else
    return 1
  fi
}

# is_private_ipv4 <a.b.c.d>: 0 for loopback, private and link-local IPv4 addresses.
is_private_ipv4() {
  local first second rest
  IFS=. read -r first second rest <<<"$1"
  first=$((10#${first})) second=$((10#${second}))
  case "$first" in
    10 | 127) return 0 ;;
    172) [[ "$second" -ge 16 && "$second" -le 31 ]] ;;
    192) [[ "$second" -eq 168 ]] ;;
    169) [[ "$second" -eq 254 ]] ;;
    *) return 1 ;;
  esac
}

# is_private_address <address>: 0 when the address is loopback, private or link-local, so the
# listener on this interface is not reachable from outside the local network. Anything else,
# including CGNAT 100.64/10 and global IPv6, is public. Scope ids and prefix lengths are ignored;
# IPv4-mapped IPv6 addresses are classified by the IPv4 address they carry.
is_private_address() {
  local address hextet high low
  address="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  address="${address%%%*}"
  address="${address%%/*}"
  if [[ "$address" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    is_private_ipv4 "$address"
  elif [[ "$address" =~ ^::ffff:([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
    is_private_ipv4 "${BASH_REMATCH[1]}"
  elif [[ "$address" =~ ^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$ ]]; then
    high=$((16#${BASH_REMATCH[1]})) low=$((16#${BASH_REMATCH[2]}))
    is_private_ipv4 "$((high >> 8)).$((high & 255)).$((low >> 8)).$((low & 255))"
  elif [[ "$address" == "::1" ]]; then
    return 0
  else
    hextet="${address%%:*}"
    [[ "$hextet" =~ ^(f[cd][0-9a-f]{2}|fe[89ab][0-9a-f])$ ]]
  fi
}

# is_lan_ipv4 <a.b.c.d>: 0 for a private IPv4 address that is neither loopback nor link-local.
is_lan_ipv4() {
  is_private_ipv4 "$1" || return 1
  case "$1" in
    127.* | 169.254.*) return 1 ;;
    *) return 0 ;;
  esac
}

# private_lan_addresses: prints the LAN IPv4 addresses of this machine's interfaces, one per line.
private_lan_addresses() {
  local address
  while IFS= read -r address; do
    address="${address%%/*}"
    [[ "$address" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
    if is_lan_ipv4 "$address"; then
      printf '%s\n' "$address"
    fi
  done < <(enumerate_host_addresses || true)
}

# default_route_address: prints the address the default route leaves from: the `src` of
# `ip route get` on Linux, the address of the `route -n get default` interface on macOS. Prints
# nothing when no route or neither tool can be found.
default_route_address() {
  local interface
  if command -v ip >/dev/null 2>&1; then
    { ip route get 1.1.1.1 2>/dev/null \
      | awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }'; } || true
  elif command -v route >/dev/null 2>&1 && command -v ipconfig >/dev/null 2>&1; then
    interface="$(route -n get default 2>/dev/null | awk '$1 == "interface:" { print $2; exit }' || true)"
    [[ -z "$interface" ]] || ipconfig getifaddr "$interface" 2>/dev/null || true
  fi
}

# detect_lan_address: sets lan_address unless --lan-address gave it: the default-route address,
# else the only private IPv4 address. Hard-stops, before any change, naming --lan-address when
# no address or several addresses qualify.
detect_lan_address() {
  local candidates first="" second="" found
  [[ -z "$lan_address" ]] || return 0
  lan_address="$(default_route_address)"
  if [[ -n "$lan_address" ]] && is_lan_ipv4 "$lan_address"; then
    return 0
  fi
  lan_address=""
  candidates="$(private_lan_addresses)"
  { read -r first || true; read -r second || true; } <<<"$candidates"
  if [[ -n "$first" && -z "$second" ]]; then
    lan_address="$first"
    return 0
  fi
  found="found no private address"
  [[ -z "$candidates" ]] || found="found $(printf '%s' "$candidates" | tr '\n' ' ')"
  print_error 'Cannot tell which LAN address of this machine %s should map to (%s). Fix: re-run with --lan-address <ip>, the address colleagues reach this machine on (for example 192.168.1.5).\n' \
    "$hostname_arg" "$found"
  exit "$EXIT_FAILURE"
}

# check_host_exposure: hard-stops (before any tool check or cluster call) when an interface of this
# machine holds a public address, because the kind cluster publishes the proxy on all of them.
# --allow-public-host accepts that. Fails closed when the addresses cannot be listed.
check_host_exposure() {
  local addresses address offending=()
  if [[ "$allow_public_host" == true ]]; then
    log_step "Exposure check skipped (--allow-public-host)"
    return 0
  fi
  if ! addresses="$(enumerate_host_addresses)"; then
    print_error 'Cannot verify this machine'"'"'s network exposure: neither ip nor ifconfig is available. Fix: install one of them, or re-run with --allow-public-host to accept the risk.\n'
    exit "$EXIT_FAILURE"
  fi
  while IFS= read -r address; do
    [[ -n "$address" ]] || continue
    is_private_address "$address" || offending+=("$address")
  done <<<"$addresses"
  if [[ ${#offending[@]} -gt 0 ]]; then
    for address in "${offending[@]}"; do
      print_error 'exposure: public address %s on this machine\n' "$address"
    done
    print_error 'The cluster publishes the proxy (port 30443) on every interface of this machine, so it would be reachable from those addresses. Fix: disconnect or firewall them, or re-run with --allow-public-host to accept the exposure (see %s, What is exposed).\n' \
      "$INSTALL_GUIDE"
    exit "$EXIT_FAILURE"
  fi
  log_step "Exposure check passed: no public address on this machine"
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
    print_error 'Secret %s not found in namespace %s. Fix: create it first (scripts/sync-llm-secrets-to-kind.sh, see %s step 4), then re-run.\n' \
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

# print_hosts_steps <host>: prints the hosts-file command this machine still needs, or, when a
# line maps <host> to another address, what is wrong and how to fix it. Prints nothing when the
# mapping is in place.
print_hosts_steps() {
  local mapping
  mapping="$(missing_host_mapping "$1")"
  if [[ -n "$mapping" ]]; then
    printf "  echo '%s' | sudo tee -a %s\n" "$mapping" "$(hosts_file)"
  fi
  print_hosts_conflict "$1"
}

# print_hosts_conflict <host>: reports a hosts line that maps <host> to an address other than this
# machine's LAN address. The line is never edited or duplicated by the script.
print_hosts_conflict() {
  local state
  state="$(host_mapping_state "$1")"
  [[ "$state" == conflict* ]] || return 0
  printf '  %s already maps %s to %s, not to this machine'"'"'s LAN address %s; the script never edits existing lines.\n' \
    "$(hosts_file)" "$1" "${state#conflict }" "$lan_address"
  printf '  Fix: edit or remove that line so that %s maps to %s, then re-run.\n' "$1" "$lan_address"
}

# read_installed_release: sets installed_host and installed_issuer to the values the installed
# release recorded (the hostname PS Service resolves and its OIDC issuer). Both stay empty when
# there is no release or it recorded neither.
read_installed_release() {
  local values
  installed_host=""
  installed_issuer=""
  helm status "$HELM_RELEASE_NAME" >/dev/null 2>&1 || return 0
  values="$(helm get values "$HELM_RELEASE_NAME" -o json 2>/dev/null || true)"
  installed_host="$(jq -r '.psService.authentikHostname // empty' <<<"$values" 2>/dev/null || true)"
  installed_issuer="$(jq -r '.psService.auth.issuer // empty' <<<"$values" 2>/dev/null || true)"
}

# migration_kind <host> <issuer>: prints `host` when the installed release was made for another
# hostname, `issuer` when only its recorded issuer differs, and `none` otherwise (a fresh install,
# nothing recorded, or nothing changed).
migration_kind() {
  if [[ -n "$installed_host" && "$installed_host" != "$1" ]]; then
    printf 'host\n'
  elif [[ -n "$installed_issuer" && "$installed_issuer" != "$2" ]]; then
    printf 'issuer\n'
  else
    printf 'none\n'
  fi
}

# old_host_lines <host>: prints the hosts-file lines (comments excluded) that name <host>.
old_host_lines() {
  awk -v host="$1" '
    { line = $0; sub(/#.*/, "") }
    { for (i = 2; i <= NF; i++) if ($i == host) { print line; next } }
  ' "$(hosts_file)" 2>/dev/null || true
}

# print_old_host_lines <host>: names the hosts-file lines of the former name to remove. Prints
# nothing when none exist. The script never edits the hosts file for this.
print_old_host_lines() {
  local lines line
  lines="$(old_host_lines "$1")"
  [[ -n "$lines" ]] || return 0
  printf 'Remove these lines from %s (the former name no longer reaches this evaluator):\n' "$(hosts_file)"
  while IFS= read -r line; do
    printf '  %s\n' "$line"
  done <<<"$lines"
}

# print_replaced_ca_steps: when the local CA was replaced in this run, names the old CA (subject,
# SHA-1 and SHA-256, kept copy) and prints how to remove it on macOS (admin trust settings first,
# then the keychain entry) and on each Linux family, so no machine keeps trusting the old one.
print_replaced_ca_steps() {
  [[ -n "$LOCAL_TLS_REPLACED_CA_SHA1" ]] || return 0
  printf 'The local CA was replaced: the previous one is unconstrained or permits another name.\n'
  printf 'On every machine that trusts the old CA, remove it, then trust the new one below.\n'
  printf '  Old CA subject: %s\n  SHA-1:          %s\n  SHA-256:        %s\n  Kept at:        %s\n' \
    "$LOCAL_TLS_REPLACED_CA_SUBJECT" "$LOCAL_TLS_REPLACED_CA_SHA1" "$LOCAL_TLS_REPLACED_CA_SHA256" \
    "$LOCAL_TLS_REPLACED_CA_PEM"
  if [[ "$(host_os)" == Darwin ]]; then
    printf '  macOS (the first command clears the admin trust settings, the second deletes the certificate):\n'
    printf '    sudo security remove-trusted-cert -d %s\n' "$LOCAL_TLS_REPLACED_CA_PEM"
    printf '    sudo security delete-certificate -Z %s -t /Library/Keychains/System.keychain\n' \
      "$LOCAL_TLS_REPLACED_CA_SHA1"
  fi
  printf '  Debian/Ubuntu: sudo rm /usr/local/share/ca-certificates/ps-local-ca.crt && sudo update-ca-certificates --fresh\n'
  printf '  Fedora/RHEL:   sudo rm /etc/pki/ca-trust/source/anchors/ps-local-ca.pem && sudo update-ca-trust\n'
  printf 'New CA to trust: %s/ca.pem\n\n' "$(local_tls_state_dir)"
}

# print_machine_move_steps <host> <ca.pem>: what this machine still needs after a hostname change:
# the former name's hosts lines to remove, the new mapping, and trust for the new CA.
print_machine_move_steps() {
  printf 'Before the cluster changes, this machine still needs:\n'
  print_old_host_lines "$installed_host"
  print_hosts_steps "$1"
  if [[ ! -f "$2" ]] || ! ca_is_trusted "$2"; then
    print_trust_commands "$2"
  fi
  printf '(or re-run with --apply-host-setup to have the script do the sudo steps)\n\n'
}

# print_passkey_notice <host>: every passkey is bound to the hostname, so all must be registered
# again. The owner gets a fresh single-use link although the old passkey is still listed.
print_passkey_notice() {
  printf 'Passkeys are bound to the hostname, so all passkeys must be registered again: the owner\n'
  printf 'registers a new passkey with a fresh enrolment link (issued even though the old passkey is\n'
  printf 'still listed), and every invitee registers a new passkey too.\n'
  printf 'LAN colleagues must change their hosts line to %s %s and trust the new CA.\n\n' "$lan_address" "$1"
}

# print_role_reset: the evaluator-only command that clears the role assignments recorded under the
# former issuer, which must run before the owner signs in. The $ signs are shown verbatim.
# shellcheck disable=SC2016
print_role_reset() {
  printf 'After the script finishes and BEFORE the owner signs in, clear the role assignments\n'
  printf 'recorded under the old issuer (evaluator only, never a production install):\n'
  printf '  POD=$(kubectl get pod -l app.kubernetes.io/component=ps-postgres -o jsonpath=%s)\n' \
    "'{.items[0].metadata.name}'"
  printf '  STATE_PW=$(kubectl get secret policy-system-ps-postgres-state-credentials \\\n'
  printf "    -o jsonpath='{.data.PS_STATE_POSTGRES_PASSWORD}' | base64 -d)\n"
  printf '  kubectl exec "$POD" -- env PGPASSWORD="$STATE_PW" psql -h 127.0.0.1 -U ps_state -d ps_state \\\n'
  printf '    -c "DELETE FROM access_role_assignments;"\n'
  printf 'Do not open the enrolment link before running this command.\n'
  printf 'Details: %s#upgrading-an-existing-install\n\n' "$INSTALL_GUIDE"
}

# print_migration_intro <kind> <host> <issuer>: what changes, naming the recorded and the new
# hostname or issuer.
print_migration_intro() {
  case "$1" in
    host)
      printf '\nThis evaluator was installed for %s; switching to %s.\n' "$installed_host" "$2"
      [[ -z "$installed_issuer" ]] \
        || printf 'Its OIDC issuer moves from %s to %s.\n' "$installed_issuer" "$3"
      ;;
    issuer)
      printf '\nThe OIDC issuer changes from %s to %s on the same host; passkeys stay valid.\n' \
        "$installed_issuer" "$3"
      ;;
    *) ;;
  esac
}

# announce_migration <host> <issuer> <ca.pem>: before the first cluster change, prints (stderr,
# informational only: no sudo, no prompt, no mutation) what a move of an existing evaluator needs:
# the replaced CA to remove, the hosts lines, the passkey re-registration and the role reset.
# A hostname change also makes provision_owner issue a fresh enrolment link although the old
# passkey is still listed; an issuer-only change keeps the passkeys and issues none. Nothing is
# printed for a fresh install, an unchanged release, or one that recorded neither value.
announce_migration() {
  local host="$1" issuer="$2" ca="$3" kind
  read_installed_release
  kind="$(migration_kind "$host" "$issuer")"
  [[ "$kind" != host ]] || OWNER_IGNORE_EXISTING_DEVICES=true
  {
    print_migration_intro "$kind" "$host" "$issuer"
    print_replaced_ca_steps
    [[ "$kind" != host ]] || print_machine_move_steps "$host" "$ca"
    [[ "$kind" != host ]] || print_passkey_notice "$host"
    [[ "$kind" == none ]] || print_role_reset
  } >&2
}

# authentik_base_url <host>: prints the externally reachable Authentik address, with its web path
# and without a trailing slash. The issuer, PS Service's Authentik base URL and the owner API
# base are all this value plus a suffix.
authentik_base_url() {
  printf 'https://%s:%s%s' "$1" "$PROXY_PORT" "$AUTHENTIK_WEB_PATH"
}

# ps_service_url <host>: prints the address clients use for PS Service (REST and MCP), the proxy
# on the configured name, without a trailing slash.
ps_service_url() {
  printf 'https://%s:%s' "$1" "$PROXY_PORT"
}

# authentik_cluster_base_url: prints the in-cluster Authentik address PS Service calls, with the
# web path and without a trailing slash.
authentik_cluster_base_url() {
  printf 'http://%s%s' "$AUTHENTIK_SERVER_SERVICE" "$AUTHENTIK_WEB_PATH"
}

# authentik_issuer <host>: prints the OIDC issuer of the ps-cli application, which ends in a slash.
authentik_issuer() {
  printf '%s/application/o/%s/' "$(authentik_base_url "$1")" "$AUTHENTIK_APP_SLUG"
}

# release_values_json <host> <issuer> <checksum>: prints the complete set of user values this
# script gives the chart, in the JSON shape `helm get values -o json` returns (so the release
# comparison is exact). Sets, in ONE deploy: the proxy and its TLS Secret (with a certificate
# checksum that restarts it on a rotated leaf), PS Service's CA-only Secret, the issuer/audience/
# client for ps-cli, the bootstrap owner (subject == the owner's email, issuer == the issuer
# above), the in-cluster Authentik base URL PS Service calls for invitations with the public
# address handed to invitees, and the one origin the approval page is served from (the proxy
# on the configured name).
release_values_json() {
  local host="$1" issuer="$2" checksum="$3"
  jq -n --arg llm "$LLM_SECRET_NAME" --arg tls "$TLS_SECRET_NAME" --arg ca "$CA_SECRET_NAME" \
    --arg host "$host" --arg ip "$node_ip" --arg issuer "$issuer" --arg app "$AUTHENTIK_APP_SLUG" \
    --arg scopes "$AUTHENTIK_SCOPES" --arg owner "$OWNER_EMAIL" \
    --arg base "$(authentik_cluster_base_url)" --arg public "$(authentik_base_url "$host")" \
    --arg sum "$checksum" --arg origin "$(ps_service_url "$host")" \
    '{llm: {existingSecret: $llm},
      proxy: {enabled: true, lanExposure: {enabled: true}, tls: {secretName: $tls},
        podAnnotations: {"checksum/local-tls": $sum}},
      authentik: {enabled: true},
      psService: {localTestBypass: {enabled: false},
        localCa: {secretName: $ca},
        passkeySigning: {allowedOrigins: [$origin]},
        authentikHostname: $host, authentikHostAliasIP: $ip,
        auth: {issuer: $issuer, audience: $app, cliClientId: $app, scopes: $scopes},
        authzBootstrapOwner: {subject: $owner, issuer: $issuer},
        authentik: {baseUrl: $base, publicUrl: $public}}}'
}

# ensure_release <values-json>: write-if-changed. Plain `helm upgrade --install` always creates a
# new revision, so the desired values are compared with `helm get values` first; the script
# passes every value it sets in one file and no other, so the comparison is on the whole object.
ensure_release() {
  local desired_json="$1" chart_ref="${PS_CHART_REF:-$DEFAULT_CHART_REF}"
  local values_file current_json
  set_chart_version_args "$SCRIPT_DIR" "$chart_ref"
  if helm status "$HELM_RELEASE_NAME" >/dev/null 2>&1; then
    current_json="$(helm get values "$HELM_RELEASE_NAME" -o json)"
    if [[ "$(jq -S . <<<"$current_json")" == "$(jq -S . <<<"$desired_json")" ]]; then
      printf 'Helm release %s unchanged.\n' "$HELM_RELEASE_NAME" >&2
      return 0
    fi
  fi
  values_file="$(local_tls_state_dir)/release-values.json"
  printf '%s\n' "$desired_json" >"$values_file"
  helm upgrade --install "$HELM_RELEASE_NAME" "$chart_ref" ${chart_version_args[@]+"${chart_version_args[@]}"} \
    -f "$values_file" >/dev/null
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

# probe_issuer_discovery <issuer>: PS Service's own pod fetches the issuer's discovery document
# through the proxy, verifying the certificate against the pod's trust bundle. This is the path
# PS Service needs at startup, so a failure here is reported with its cause instead of surfacing
# later as a crash loop. Bounded by PS_DISCOVERY_ATTEMPTS (default 5) two seconds apart.
probe_issuer_discovery() {
  local url="${1}.well-known/openid-configuration" i
  local attempts="${PS_DISCOVERY_ATTEMPTS:-5}"
  local code='import ssl, sys, urllib.request; urllib.request.urlopen(sys.argv[1], context=ssl.create_default_context(cafile="/ps-trust/bundle.crt"), timeout=10).read()'
  for ((i = 0; i < attempts; i++)); do
    if kubectl -n "$namespace" exec "deploy/${HELM_RELEASE_NAME}-ps-service" -- \
      python -c "$code" "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  print_error 'PS Service cannot reach the OIDC issuer %s through the proxy. Fix: check the proxy and Authentik pods (kubectl get pods; kubectl logs deploy/%s-proxy), that %s resolves to the node inside the PS Service pod, and that the local CA is in its trust bundle, then re-run.\n' \
    "$1" "$HELM_RELEASE_NAME" "$hostname_arg"
  return 1
}

# proxy_get <host> <url>: one request to the proxy from this machine, resolving <host> to the
# loopback address the cluster publishes the proxy on and verifying against the evaluator CA. Sets
# proxy_status (000 when nothing answered) and proxy_body. No credentials are sent.
proxy_get() {
  local out
  proxy_status=000
  proxy_body=""
  out="$(curl -sS --max-time 10 --cacert "$(local_tls_state_dir)/ca.pem" \
    --resolve "${1}:${PROXY_PORT}:127.0.0.1" -w $'\n%{http_code}' "$2" 2>/dev/null)" || return 0
  proxy_status="${out##*$'\n'}"
  proxy_body="${out%$'\n'*}"
}

# proxy_expect <host> <url> <status> <what>: requests <url> through the proxy until it answers
# <status> (PS_PROXY_PROBE_ATTEMPTS tries, default 5, two seconds apart). Leaves proxy_body
# of the last answer. Prints the failure with its fix and returns 1 otherwise.
proxy_expect() {
  local i attempts="${PS_PROXY_PROBE_ATTEMPTS:-5}"
  for ((i = 0; i < attempts; i++)); do
    proxy_get "$1" "$2"
    [[ "$proxy_status" == "$3" ]] && return 0
    sleep 2
  done
  print_error '%s through the proxy answered %s, expected %s (%s). Fix: check the proxy and PS Service pods (kubectl get pods; kubectl logs deploy/%s-proxy) and that port %s of this machine reaches the cluster, then re-run.\n' \
    "$4" "$proxy_status" "$3" "$2" "$HELM_RELEASE_NAME" "$PROXY_PORT"
  return 1
}

# probe_proxy_from_host <host> <issuer>: the closing end-to-end check, through the published proxy
# port with the evaluator CA verifying the certificate: PS Service answers /health, the issuer
# discovery document reports exactly the issuer the release was given, and PS Service refuses a
# request that carries no bearer token with 401.
probe_proxy_from_host() {
  local host="$1" issuer="$2" reported
  proxy_expect "$host" "$(ps_service_url "$host")/health" 200 "/health" || return 1
  proxy_expect "$host" "${issuer}.well-known/openid-configuration" 200 "The issuer discovery document" \
    || return 1
  reported="$(jq -r '.issuer // empty' <<<"$proxy_body" 2>/dev/null || true)"
  if [[ "$reported" != "$issuer" ]]; then
    print_error 'The issuer discovery document through the proxy reports issuer "%s", not "%s". Fix: the proxy must pass the Host and port the browser used to Authentik; check the proxy ConfigMap and the Authentik trusted-proxy setting, then re-run.\n' \
      "$reported" "$issuer"
    return 1
  fi
  proxy_expect "$host" "$(ps_service_url "$host")/mcp/" 401 "An unauthenticated request to /mcp/" || return 1
}

# warn_stale_tls_keypair: an earlier version of this script registered the local certificate in
# Authentik and set it as the default brand's certificate. Nothing serves it now; when the keypair
# is still there, print how to remove it (a brand pointing at a deleted certificate is not safe to
# edit automatically). Needs the loopback API base; never fails the run.
warn_stale_tls_keypair() {
  authentik_api GET "/crypto/certificatekeypairs/?name=ps-local-tls" || return 0
  [[ "$AUTHENTIK_HTTP_STATUS" == "200" ]] || return 0
  jq -e '(.results | length) > 0' <<<"$AUTHENTIK_HTTP_BODY" >/dev/null 2>&1 || return 0
  printf 'Authentik still holds the certificate keypair ps-local-tls from an earlier install, which nothing serves now. Fix: open the admin UI through a port-forward (kubectl port-forward svc/%s 9000:80, then http://127.0.0.1:9000%s/if/admin/), set System > Brands > the default brand'"'"'s Default certificate to empty, then delete the keypair under System > Certificates.\n' \
    "$AUTHENTIK_SERVER_SERVICE" "$AUTHENTIK_WEB_PATH" >&2
}

# hosts_file: the file checked and edited for the hostname mapping.
hosts_file() {
  printf '%s' "${PS_HOSTS_FILE:-/etc/hosts}"
}

# host_mapping_state <host>: prints how the hosts file relates <host> to this machine's LAN
# address: `present` (a line maps it there and no other line maps it elsewhere), `missing` (no
# line maps it) or `conflict <address...>` (lines map it to other addresses).
host_mapping_state() {
  awk -v host="$1" -v addr="$lan_address" '
    { sub(/#.*/, "") }
    { for (i = 2; i <= NF; i++) if ($i == host) {
        if ($1 == addr) found = 1
        else other = (other == "" ? $1 : other " " $1)
      } }
    END {
      if (other != "") print "conflict " other
      else if (found) print "present"
      else print "missing"
    }' "$(hosts_file)" 2>/dev/null || printf 'missing\n'
}

# missing_host_mapping <host>: prints the one hosts-file line that maps <host> to this machine's
# LAN address, only when no line maps <host> at all.
missing_host_mapping() {
  [[ "$(host_mapping_state "$1")" == missing ]] || return 0
  printf '%s %s\n' "$lan_address" "$1"
}

# host_os: prints the operating system name (`uname -s`: Darwin, Linux, ...).
host_os() {
  uname -s
}

# ca_is_trusted <ca.pem>: 0 when the macOS system trust settings accept the CA for TLS server
# authentication. Other systems cannot be checked, so they report "not trusted" and get the
# commands to run.
ca_is_trusted() {
  [[ "$(host_os)" == Darwin ]] && command -v security >/dev/null 2>&1 \
    && security verify-cert -c "$1" -p ssl >/dev/null 2>&1
}

# print_trust_commands <ca.pem>: the commands that trust the CA on this operating system, limited
# to TLS server authentication, with what bounds the trust where the system has no switch for it.
print_trust_commands() {
  local ca="$1"
  if [[ "$(host_os)" == Darwin ]]; then
    printf '  sudo security add-trusted-cert -d -r trustRoot -p ssl -k /Library/Keychains/System.keychain %s\n' "$ca"
    return 0
  fi
  printf '  Debian/Ubuntu: sudo cp %s /usr/local/share/ca-certificates/ps-local-ca.crt && sudo update-ca-certificates\n' "$ca"
  printf '    (this store ignores key usage, so only the name constraint limits what the CA can vouch for)\n'
  printf '  Fedora/RHEL:   sudo cp %s /etc/pki/ca-trust/source/anchors/ps-local-ca.pem && sudo update-ca-trust\n' "$ca"
  printf '    (this store takes the purposes from the CA'"'"'s extended key usage, TLS server authentication only, and enforces the name constraint)\n'
  printf '  Other systems: import %s as a trusted root for TLS server authentication in the system or browser store.\n' "$ca"
}

# check_local_access <host> <ca>: sets needs_hosts/needs_trust to what this machine still lacks.
check_local_access() {
  needs_hosts=false
  needs_trust=false
  [[ "$(host_mapping_state "$1")" != missing ]] || needs_hosts=true
  ca_is_trusted "$2" || needs_trust=true
}

# confirm_local_access_setup: 0 when the sudo steps may run now: --apply-host-setup, or a yes at
# the prompt of an interactive run. A non-interactive run without the flag runs none of them.
confirm_local_access_setup() {
  local answer=""
  [[ "$apply_host_setup" == true ]] && return 0
  [[ "$SKIP_OWNER_PROMPT" == true || ! -t 0 ]] && return 1
  printf 'This machine still needs sudo steps (hostname mapping, CA trust and/or removal of the replaced CA). Run them now? [y/N] ' >&2
  read -r answer || true
  [[ "$answer" == [yY] || "$answer" == [yY][eE][sS] ]]
}

# debian_ca_anchor / fedora_ca_anchor: where the trust store of each Linux family holds the
# local CA (overridable for tests).
debian_ca_anchor() {
  printf '%s' "${PS_DEBIAN_CA_ANCHOR:-/usr/local/share/ca-certificates/ps-local-ca.crt}"
}
fedora_ca_anchor() {
  printf '%s' "${PS_FEDORA_CA_ANCHOR:-/etc/pki/ca-trust/source/anchors/ps-local-ca.pem}"
}

# replaced_ca_anchor: prints the Linux trust-store file that holds the replaced local CA (matched
# by SHA-1, so a file holding any other certificate is never named), or nothing.
replaced_ca_anchor() {
  local anchor
  for anchor in "$(debian_ca_anchor)" "$(fedora_ca_anchor)"; do
    [[ -f "$anchor" ]] || continue
    if [[ "$(_cert_hash "$anchor" sha1)" == "$LOCAL_TLS_REPLACED_CA_SHA1" ]]; then
      printf '%s' "$anchor"
      return 0
    fi
  done
}

# replaced_ca_is_trusted: 0 when this machine's trust store still holds the replaced local CA
# (macOS: the system trust settings accept the kept copy; Linux: a trust-store file holds it).
replaced_ca_is_trusted() {
  [[ -n "$LOCAL_TLS_REPLACED_CA_SHA1" ]] || return 1
  if [[ "$(host_os)" == Darwin ]]; then
    ca_is_trusted "$LOCAL_TLS_REPLACED_CA_PEM"
  else
    [[ -n "$(replaced_ca_anchor)" ]]
  fi
}

# remove_replaced_ca: removes the replaced local CA, identified by its SHA-1, from this machine's
# trust store (sudo). Returns 1 when a command fails (sudo declined); the old CA then stays.
remove_replaced_ca() {
  local anchor
  if [[ "$(host_os)" == Darwin ]]; then
    sudo security remove-trusted-cert -d "$LOCAL_TLS_REPLACED_CA_PEM" || return 1
    sudo security delete-certificate -Z "$LOCAL_TLS_REPLACED_CA_SHA1" -t /Library/Keychains/System.keychain
    return
  fi
  anchor="$(replaced_ca_anchor)"
  sudo rm "$anchor" || return 1
  if [[ "$anchor" == "$(debian_ca_anchor)" ]]; then
    sudo update-ca-certificates --fresh
  else
    sudo update-ca-trust
  fi
}

# setup_local_access <host>: when this machine lacks the hostname mapping or CA trust, or still
# trusts the CA this run replaced, runs the sudo steps if the evaluator agreed, then re-checks.
# A step that fails (sudo declined) stays outstanding: a missing mapping is not retried, and the
# closing output carries the pointer for a replaced CA that is still trusted.
setup_local_access() {
  local host="$1" ca remove_old=false
  ca="$(local_tls_state_dir)/ca.pem"
  check_local_access "$host" "$ca"
  old_ca_still_trusted=false
  ! replaced_ca_is_trusted || remove_old=true
  [[ "$remove_old" == false ]] || old_ca_still_trusted=true
  [[ "$needs_hosts" == false && "$needs_trust" == false && "$remove_old" == false ]] && return 0
  if confirm_local_access_setup; then
    if [[ "$needs_hosts" == true ]]; then
      log_step "Mapping ${host} to ${lan_address} in $(hosts_file) (sudo)"
      missing_host_mapping "$host" | sudo tee -a "$(hosts_file)" >/dev/null || true
    fi
    if [[ "$needs_trust" == true && "$(host_os)" == Darwin ]]; then
      log_step "Trusting the local CA for TLS server authentication in the system keychain (sudo)"
      sudo security add-trusted-cert -d -r trustRoot -p ssl -k /Library/Keychains/System.keychain "$ca" || true
    fi
    if [[ "$remove_old" == true ]]; then
      log_step "Removing the replaced local CA (SHA-1 ${LOCAL_TLS_REPLACED_CA_SHA1}) from this machine's trust store (sudo)"
      ! remove_replaced_ca || old_ca_still_trusted=false
    fi
    check_local_access "$host" "$ca"
  fi
}

# configure_ps_cli_context <host>: points ps-cli's "eval" context at PS Service and selects it, unless it
# already is (set-context deletes the context's stored login, so a re-run must not repeat it).
# Any failure is ignored; the installation guide covers the manual commands.
configure_ps_cli_context() {
  command -v ps-cli >/dev/null 2>&1 || return 0
  local contexts url
  url="$(ps_service_url "$1")"
  contexts="$(ps-cli config get-contexts 2>/dev/null || true)"
  grep -Eq "\*.*[[:space:]]${PS_CLI_CONTEXT}[[:space:]].*${url//./\\.}[[:space:]]" <<<"$contexts" && return 0
  log_step "Pointing ps-cli's ${PS_CLI_CONTEXT} context at ${url}"
  if ps-cli config set-context "$PS_CLI_CONTEXT" --url "$url" >/dev/null; then
    ps-cli config use-context "$PS_CLI_CONTEXT" >/dev/null || true
  fi
}

# print_closing_summary <host>: the three things to carry to a client machine, in this order: the
# hosts line (with a conflicting hosts line and its fix right after it), the absolute path of
# ca.pem, and the owner's enrolment link or the already-enrolled message. A pointer line sits
# before the link when the replaced CA is still trusted here. Per-OS, per-client and ps-cli steps
# live in the installation guide. The enrolment link is printed last, exactly once (it is a
# single-use credential-equivalent).
print_closing_summary() {
  local host="$1"
  printf '%s %s\n' "$lan_address" "$host"
  print_hosts_conflict "$host"
  printf '%s\n' "$(local_tls_state_dir)/ca.pem"
  if [[ "$old_ca_still_trusted" == true ]]; then
    printf 'The replaced local CA is still trusted on this machine; re-run with --apply-host-setup to remove it.\n'
  fi
  print_owner_link_message
}

main() {
  parse_args "$@"

  check_host_exposure
  [[ "$check_exposure_only" == true ]] && exit 0
  check_required_tools
  detect_lan_address
  require_kind_context
  require_llm_secret
  prompt_owner_email || exit "$EXIT_FAILURE"
  detect_node_ip

  trap stop_authentik_port_forward EXIT

  local issuer checksum
  issuer="$(authentik_issuer "$hostname_arg")"
  log_step "Preparing local TLS for ${hostname_arg}"
  ensure_local_leaf "$hostname_arg" || exit "$EXIT_FAILURE"
  announce_migration "$hostname_arg" "$issuer" "$(local_tls_state_dir)/ca.pem"
  apply_local_tls_secret "$namespace" "$TLS_SECRET_NAME" "$CA_SECRET_NAME"

  checksum="$(leaf_fingerprint | tr -d ':' | tr '[:upper:]' '[:lower:]')"
  log_step "Reconciling the Helm release"
  ensure_release "$(release_values_json "$hostname_arg" "$issuer" "$checksum")"

  # Authentik and the proxy first: PS Service discovers the OIDC issuer through the proxy at
  # startup and crash-loops until both answer.
  log_step "Waiting for Authentik and the proxy to become Ready"
  wait_for_rollouts authentik-server authentik-worker proxy

  read_bootstrap_token "$namespace" "$API_TOKEN_SECRET_NAME" || exit "$EXIT_FAILURE"
  log_step "Waiting for PS Service to become Ready"
  wait_for_rollouts ps-service
  log_step "Checking OIDC discovery from inside the cluster"
  probe_issuer_discovery "$issuer" || exit "$EXIT_FAILURE"
  log_step "Checking the proxy from this machine"
  probe_proxy_from_host "$hostname_arg" "$issuer" || exit "$EXIT_FAILURE"

  log_step "Creating the owner and issuing the enrolment link"
  start_authentik_port_forward "$namespace" "$AUTHENTIK_SERVER_SERVICE" 80 || exit "$EXIT_FAILURE"
  AUTHENTIK_API_BASE="http://127.0.0.1:${AUTHENTIK_PF_LOCAL_PORT}${AUTHENTIK_WEB_PATH}"
  AUTHENTIK_LINK_BASE="$(authentik_base_url "$hostname_arg")"
  AUTHENTIK_CURL_ARGS=()
  warn_stale_tls_keypair
  provision_owner "$OWNER_EMAIL" || exit "$EXIT_FAILURE"
  stop_authentik_port_forward

  setup_local_access "$hostname_arg"
  configure_ps_cli_context "$hostname_arg"
  print_closing_summary "$hostname_arg"
}

main "$@"
