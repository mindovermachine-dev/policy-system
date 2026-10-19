# shellcheck shell=bash disable=SC2034
# Local TLS for the evaluator profile: a name-constrained, short-lived local CA plus a leaf
# certificate for the evaluator hostname, kept outside the repo, delivered to the cluster as a
# Secret, and served by Authentik through the `authentik-local-tls-blueprint.yaml` blueprint. No
# mkcert; only `openssl` (OpenSSL or LibreSSL: config files, never `-addext`).
#
# Sourced, never executed (mode 100644):
#   source "$script_dir/lib/local-tls.sh"
#
# State lives in ${PS_EVAL_STATE_DIR:-$HOME/.config/policy-system/eval-tls} (dir 0700, keys 0600):
#   ca.pem ca.key leaf.pem leaf.key. The CA carries a critical name constraint that permits only
# the evaluator hostname, is valid for at most 730 days and is limited to TLS server
# authentication. The CA is replaced (with its leaf) when it lacks the constraint, permits another
# name, outlives 730 days or has less than 30 days left; the replaced CA is kept as
# ca.replaced-<sha1 prefix>.pem and its fingerprints are left in LOCAL_TLS_REPLACED_CA_*. The leaf
# names exactly the hostname and is regenerated when its names differ, when it does not chain to
# the CA, or when less than 30 days remain.
# Certificate verification is never disabled anywhere in this file.
#
# Bash 3.2 compatible. Needs the caller's print_error; refresh_authentik_cert_if_differs also
# needs lib/authentik-owner.sh sourced first (authentik_api).

if [[ -n "${LOCAL_TLS_LIB_LOADED:-}" ]]; then
  return 0
fi
LOCAL_TLS_LIB_LOADED=1

if ! declare -F print_error >/dev/null 2>&1; then
  print_error() {
    local format="$1"
    shift
    # shellcheck disable=SC2059
    printf "$format" "$@" >&2
  }
fi

readonly LOCAL_TLS_CA_SUBJECT="Policy System local CA"
readonly LOCAL_TLS_CA_VALID_DAYS=730
readonly LOCAL_TLS_RENEW_BEFORE_SECONDS=$((30 * 86400))
readonly LOCAL_TLS_BLUEPRINT_NAME="Policy System local TLS"
# One day of slack above the maximum validity: a CA valid longer than this is not ours.
readonly LOCAL_TLS_CA_MAX_REMAINING_SECONDS=$(((LOCAL_TLS_CA_VALID_DAYS + 1) * 86400))

# Facts about the CA that ensure_local_ca replaced in this process, for the caller to print:
# colon-free uppercase SHA-1 and SHA-256, the subject, and the path of the kept copy. Empty
# when no CA was replaced. The sourcing script reads them (hence the file-level SC2034 disable).
LOCAL_TLS_REPLACED_CA_SHA1=""
LOCAL_TLS_REPLACED_CA_SHA256=""
LOCAL_TLS_REPLACED_CA_SUBJECT=""
LOCAL_TLS_REPLACED_CA_PEM=""

# local_tls_state_dir: prints the state directory.
local_tls_state_dir() {
  printf '%s' "${PS_EVAL_STATE_DIR:-$HOME/.config/policy-system/eval-tls}"
}

# require_tls_tools: openssl must be present, with an actionable hint.
require_tls_tools() {
  if ! command -v openssl >/dev/null 2>&1; then
    print_error 'openssl not found. Fix: install it (macOS ships LibreSSL at /usr/bin/openssl; Linux: apt install openssl / dnf install openssl) and re-run.\n'
    return 1
  fi
}

# _tls_prepare_state_dir: creates the state directory with mode 0700.
_tls_prepare_state_dir() {
  local dir
  dir="$(local_tls_state_dir)"
  mkdir -p "$dir"
  chmod 700 "$dir"
}

# local_tls_validate_hostname <host>: 0 for a DNS name made only of letters, digits, dots and
# hyphens (labels neither start nor end with a hyphen). Anything that could reach an openssl
# config as another directive, and IP addresses, are refused with a fix. The CA constraint is a
# DNS name constraint and cannot express an address.
local_tls_validate_hostname() {
  local host="$1"
  local label='[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?'
  local dns_name="^${label}(\\.${label})*\$"
  if [[ ! "$host" =~ $dns_name ]]; then
    print_error 'Invalid --hostname "%s": use a DNS name of letters, digits, dots and hyphens (IP addresses are not accepted). Fix: pass a name such as ps.test and re-run.\n' "$host"
    return 1
  fi
  if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    print_error 'Invalid --hostname "%s": it is an IP address, and the CA permits a DNS name only. Fix: use a DNS name such as ps.test, mapped to the address in the hosts file, and re-run.\n' "$host"
    return 1
  fi
}

# _ca_permitted_names <ca.pem>: prints the permitted name constraints of a CA that holds a
# critical name constraint extension, space separated; prints nothing otherwise.
_ca_permitted_names() {
  local text permitted
  text="$(openssl x509 -in "$1" -noout -text 2>/dev/null | tr -s ' \n' ' ')" || return 0
  permitted="${text#*X509v3 Name Constraints: critical Permitted: }"
  [[ "$permitted" != "$text" ]] || return 0
  permitted="${permitted%% X509v3*}"
  permitted="${permitted%% Signature Algorithm*}"
  printf '%s' "${permitted% }"
}

# local_tls_ca_is_current <host>: 0 when the CA in the state directory permits exactly DNS:<host>
# through a critical name constraint, lives at most LOCAL_TLS_CA_VALID_DAYS and has more than
# LOCAL_TLS_RENEW_BEFORE_SECONDS left.
local_tls_ca_is_current() {
  local host="$1" ca
  ca="$(local_tls_state_dir)/ca.pem"
  [[ -s "$ca" && -s "$(local_tls_state_dir)/ca.key" ]] || return 1
  [[ "$(_ca_permitted_names "$ca")" == "DNS:${host}" ]] || return 1
  # -checkend succeeds when the certificate is still valid after that many seconds.
  ! openssl x509 -in "$ca" -noout -checkend "$LOCAL_TLS_CA_MAX_REMAINING_SECONDS" >/dev/null 2>&1 || return 1
  openssl x509 -in "$ca" -noout -checkend "$LOCAL_TLS_RENEW_BEFORE_SECONDS" >/dev/null 2>&1
}

# _cert_hash <pem> <sha1|sha256>: prints the fingerprint as colon-free uppercase hex.
_cert_hash() {
  openssl x509 -in "$1" -noout -fingerprint "-$2" 2>/dev/null | sed 's/.*=//; s/://g' | tr '[:lower:]' '[:upper:]'
}

# _retire_local_ca: keeps a copy of the current CA as ca.replaced-<sha1 prefix>.pem, records its
# fingerprints and subject in LOCAL_TLS_REPLACED_CA_*, and removes the CA, its key, serial and
# the leaf signed by it. A missing or unreadable CA records nothing.
_retire_local_ca() {
  local dir sha1
  dir="$(local_tls_state_dir)"
  sha1="$(_cert_hash "$dir/ca.pem" sha1)"
  if [[ -n "$sha1" ]]; then
    LOCAL_TLS_REPLACED_CA_SHA1="$sha1"
    LOCAL_TLS_REPLACED_CA_SHA256="$(_cert_hash "$dir/ca.pem" sha256)"
    LOCAL_TLS_REPLACED_CA_SUBJECT="$(openssl x509 -in "$dir/ca.pem" -noout -subject 2>/dev/null | sed 's/^subject= *//')"
    LOCAL_TLS_REPLACED_CA_PEM="$dir/ca.replaced-${sha1:0:8}.pem"
    cp "$dir/ca.pem" "$LOCAL_TLS_REPLACED_CA_PEM"
  fi
  rm -f "$dir/ca.pem" "$dir/ca.key" "$dir/ca.srl" "$dir/leaf.pem" "$dir/leaf.key"
}

# _write_ca_config <cnf> <host>: writes the openssl config of the CA: CA:TRUE, key ids,
# keyCertSign, a critical name constraint permitting only <host> and serverAuth as the only
# purpose.
_write_ca_config() {
  cat >"$1" <<CNF
[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
CN = ${LOCAL_TLS_CA_SUBJECT} (${2})
[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
extendedKeyUsage = serverAuth
nameConstraints = critical, permitted;DNS:${2}
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
CNF
}

# ensure_local_ca <host>: leaves a CA that permits only <host> in the state directory. A current
# CA is kept; any other (missing, unconstrained, constrained to another name, too long-lived or
# near expiry) is retired and replaced, together with its leaf.
ensure_local_ca() {
  local host="$1" dir cnf
  local_tls_validate_hostname "$host" || return 1
  dir="$(local_tls_state_dir)"
  _tls_prepare_state_dir
  if local_tls_ca_is_current "$host"; then
    return 0
  fi
  _retire_local_ca
  cnf="$dir/ca.cnf"
  _write_ca_config "$cnf" "$host"
  (
    umask 077
    openssl req -x509 -new -newkey rsa:2048 -nodes -sha256 -days "$LOCAL_TLS_CA_VALID_DAYS" \
      -config "$cnf" -keyout "$dir/ca.key" -out "$dir/ca.pem" >/dev/null 2>&1
  ) || {
    print_error 'Failed to create the local CA with openssl. Fix: check that %s is writable and that openssl works (openssl version).\n' "$dir"
    return 1
  }
  chmod 600 "$dir/ca.key"
  chmod 644 "$dir/ca.pem"
}

# _leaf_names <leaf.pem>: prints the subject alternative name entries, one per line.
_leaf_names() {
  openssl x509 -in "$1" -noout -text 2>/dev/null \
    | awk '/X509v3 Subject Alternative Name:/ { getline; print; exit }' \
    | tr ',' '\n' | sed 's/^ *//; s/ *$//'
}

# _leaf_current_for_host <host>: 0 when a leaf exists whose only name is DNS:<host>, chains to the
# current CA and has more than LOCAL_TLS_RENEW_BEFORE_SECONDS left.
_leaf_current_for_host() {
  local host="$1" dir
  dir="$(local_tls_state_dir)"
  [[ -s "$dir/leaf.pem" && -s "$dir/leaf.key" ]] || return 1
  [[ "$(_leaf_names "$dir/leaf.pem")" == "DNS:${host}" ]] || return 1
  openssl verify -CAfile "$dir/ca.pem" "$dir/leaf.pem" >/dev/null 2>&1 || return 1
  openssl x509 -in "$dir/leaf.pem" -noout -checkend "$LOCAL_TLS_RENEW_BEFORE_SECONDS" >/dev/null 2>&1
}

# ensure_local_leaf <host>: (re)generates the leaf for <host> signed by the local CA: SKI, AKI,
# SAN, serverAuth. Python 3.13+ and current browsers verify strictly and require SKI/AKI.
ensure_local_leaf() {
  local host="$1" dir cnf san valid_days="${PS_EVAL_LEAF_VALID_DAYS:-397}"
  dir="$(local_tls_state_dir)"
  ensure_local_ca "$host" || return 1
  if _leaf_current_for_host "$host"; then
    return 0
  fi
  san="DNS:${host}"
  cnf="$dir/leaf.cnf"
  cat >"$cnf" <<CNF
[req]
distinguished_name = dn
prompt = no
[dn]
CN = ${host}
[v3_leaf]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = ${san}
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid, issuer
CNF
  (
    umask 077
    openssl req -new -newkey rsa:2048 -nodes -config "$cnf" \
      -keyout "$dir/leaf.key" -out "$dir/leaf.csr" >/dev/null 2>&1 \
      && openssl x509 -req -sha256 -days "$valid_days" -in "$dir/leaf.csr" \
        -CA "$dir/ca.pem" -CAkey "$dir/ca.key" -CAserial "$dir/ca.srl" -CAcreateserial \
        -extfile "$cnf" -extensions v3_leaf -out "$dir/leaf.pem" >/dev/null 2>&1
  ) || {
    print_error 'Failed to create the local TLS certificate for %s with openssl. Fix: check %s is writable, then re-run.\n' "$host" "$dir"
    return 1
  }
  chmod 600 "$dir/leaf.key"
  chmod 644 "$dir/leaf.pem"
  rm -f "$dir/leaf.csr"
}

# apply_local_tls_secret <namespace> <secret-name>: (over)writes the Secret holding tls.crt,
# tls.key (Authentik) and ca.crt (PS Service trust bundle), via the create --dry-run | apply idiom.
apply_local_tls_secret() {
  local namespace="$1" secret_name="$2" dir
  dir="$(local_tls_state_dir)"
  kubectl -n "$namespace" create secret generic "$secret_name" \
    --from-file=tls.crt="$dir/leaf.pem" \
    --from-file=tls.key="$dir/leaf.key" \
    --from-file=ca.crt="$dir/ca.pem" \
    --dry-run=client -o yaml | kubectl -n "$namespace" apply -f - >/dev/null
}

# leaf_fingerprint: prints the sha256 fingerprint of the local leaf.
leaf_fingerprint() {
  openssl x509 -in "$(local_tls_state_dir)/leaf.pem" -noout -fingerprint -sha256 | sed 's/.*=//'
}

# served_fingerprint <host> <port> <ca> [connect-address]: prints the sha256 fingerprint of the
# certificate a TLS endpoint presents (empty when nothing answers). The handshake passes -CAfile;
# the security decision is the fingerprint comparison against our own leaf, not this handshake.
served_fingerprint() {
  local host="$1" port="$2" ca="$3" address="${4:-$1}"
  # `|| true`: with nothing answering, the `x509` stage fails; under the callers' `set -e -o pipefail`
  # that would abort the whole script silently instead of yielding the documented empty value.
  { openssl s_client -connect "${address}:${port}" -servername "$host" -CAfile "$ca" </dev/null 2>/dev/null \
    | openssl x509 -noout -fingerprint -sha256 2>/dev/null | sed 's/.*=//'; } || true
}

# refresh_authentik_cert_if_differs <host> <port> <ca> [connect-address]: when Authentik serves
# something other than the local leaf, applies the local-TLS blueprint through the API and waits
# (bounded) for the new certificate. No restart needed (verified on Authentik 2026.8.3, D-C); a
# matching certificate makes no API call. Needs authentik_api (lib/authentik-owner.sh).
refresh_authentik_cert_if_differs() {
  local host="$1" port="$2" ca="$3" address="${4:-$1}"
  local attempts="${PS_TLS_REFRESH_ATTEMPTS:-60}" interval="${PS_TLS_REFRESH_INTERVAL_SECONDS:-2}"
  local want have pk i
  want="$(leaf_fingerprint)"
  have="$(served_fingerprint "$host" "$port" "$ca" "$address")"
  if [[ "$have" == "$want" ]]; then
    return 0
  fi

  pk=""
  for ((i = 0; i < attempts; i++)); do
    if authentik_api GET "/managed/blueprints/?name=$(jq -rn --arg v "$LOCAL_TLS_BLUEPRINT_NAME" '$v | @uri')" \
      && [[ "$AUTHENTIK_HTTP_STATUS" == "200" ]]; then
      pk="$(printf '%s' "$AUTHENTIK_HTTP_BODY" | jq -r --arg n "$LOCAL_TLS_BLUEPRINT_NAME" \
        '[.results[]? | select(.name == $n)][0].pk // empty')"
      [[ -n "$pk" ]] && break
    fi
    sleep "$interval"
  done
  if [[ -z "$pk" ]]; then
    print_error 'Authentik has no "%s" blueprint instance yet, so its certificate cannot be refreshed. Fix: check the chart was installed with localTls.secretName set and the Authentik worker is Running (kubectl get pods), then re-run.\n' "$LOCAL_TLS_BLUEPRINT_NAME"
    return 1
  fi

  if ! authentik_api POST "/managed/blueprints/${pk}/apply/" || [[ "$AUTHENTIK_HTTP_STATUS" != "200" ]]; then
    print_error 'Applying the "%s" blueprint failed (HTTP %s). Fix: check the Authentik worker logs (kubectl logs deploy/policy-system-authentik-worker), then re-run.\n' \
      "$LOCAL_TLS_BLUEPRINT_NAME" "$AUTHENTIK_HTTP_STATUS"
    return 1
  fi

  for ((i = 0; i < attempts; i++)); do
    have="$(served_fingerprint "$host" "$port" "$ca" "$address")"
    if [[ "$have" == "$want" ]]; then
      return 0
    fi
    sleep "$interval"
  done
  print_error 'Authentik is still not serving the local certificate on %s:%s after applying the blueprint. Fix: inspect the blueprint status in the Authentik admin UI and the Secret mount (/ps-tls), then re-run.\n' "$host" "$port"
  return 1
}
