# shellcheck shell=bash
# Local TLS for the evaluator profile (issue #165): a local CA plus a leaf certificate for the
# Authentik hostname, kept outside the repo, delivered to the cluster as a Secret, and served by
# Authentik through the `authentik-local-tls-blueprint.yaml` blueprint. No mkcert; only `openssl`
# (OpenSSL or LibreSSL: config files, never `-addext`).
#
# Sourced, never executed (mode 100644):
#   source "$script_dir/lib/local-tls.sh"
#
# State lives in ${PS_EVAL_STATE_DIR:-$HOME/.config/policy-system/eval-tls} (dir 0700, keys 0600):
#   ca.pem ca.key leaf.pem leaf.key. The CA is created once and reused; the leaf is regenerated
# when missing, when the hostname no longer matches, or when less than 30 days remain.
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
readonly LOCAL_TLS_CA_VALID_DAYS=3650
readonly LOCAL_TLS_RENEW_BEFORE_SECONDS=$((30 * 86400))
readonly LOCAL_TLS_BLUEPRINT_NAME="Policy System local TLS"

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

# ensure_local_ca: creates the CA once (CA:TRUE, subject/authority key ids, keyCertSign).
ensure_local_ca() {
  local dir cnf
  dir="$(local_tls_state_dir)"
  _tls_prepare_state_dir
  if [[ -s "$dir/ca.pem" && -s "$dir/ca.key" ]]; then
    return 0
  fi
  cnf="$dir/ca.cnf"
  cat >"$cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
CN = ${LOCAL_TLS_CA_SUBJECT}
[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
CNF
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

# _leaf_current_for_host <host>: 0 when a leaf exists, names <host>, chains to the current CA
# and has more than LOCAL_TLS_RENEW_BEFORE_SECONDS left.
_leaf_current_for_host() {
  local host="$1" dir
  dir="$(local_tls_state_dir)"
  [[ -s "$dir/leaf.pem" && -s "$dir/leaf.key" ]] || return 1
  openssl x509 -in "$dir/leaf.pem" -noout -text 2>/dev/null | grep -Eq "(DNS|IP Address):${host}([^A-Za-z0-9.-]|$)" || return 1
  openssl verify -CAfile "$dir/ca.pem" "$dir/leaf.pem" >/dev/null 2>&1 || return 1
  openssl x509 -in "$dir/leaf.pem" -noout -checkend "$LOCAL_TLS_RENEW_BEFORE_SECONDS" >/dev/null 2>&1
}

# ensure_local_leaf <host>: (re)generates the leaf for <host> signed by the local CA: SKI, AKI,
# SAN, serverAuth. Python 3.13+ and current browsers verify strictly and require SKI/AKI.
ensure_local_leaf() {
  local host="$1" dir cnf san valid_days="${PS_EVAL_LEAF_VALID_DAYS:-397}"
  dir="$(local_tls_state_dir)"
  ensure_local_ca || return 1
  if _leaf_current_for_host "$host"; then
    return 0
  fi
  if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    san="IP:${host}"
  else
    san="DNS:${host}"
  fi
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
