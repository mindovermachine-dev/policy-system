# shellcheck shell=bash
# Shared first-owner provisioning for scripts/deploy-ps-eval.sh and scripts/deploy-ps-prod.sh
# (issue #165): creates the Authentik owner user (username == email, member of the admin group,
# no password) and issues a single-use passkey-enrolment (recovery) link, through Authentik's
# admin API with the shared bootstrap token.
#
# Sourced, never executed (mode 100644):
#   source "$script_dir/lib/authentik-owner.sh"
#
# The caller supplies (plain shell variables, none exported):
#   AUTHENTIK_API_BASE   scheme://authority[/path prefix] the API is reached at, WITHOUT /api/v3
#   AUTHENTIK_LINK_BASE  scheme://authority[/path prefix] the printed link must carry (the address
#                        the owner's browser uses; rewritten onto the API's returned link)
#   AUTHENTIK_CURL_ARGS  optional array of extra curl args (--cacert, --resolve ...); never -k
#   AUTHENTIK_API_TOKEN  the shared token; set by read_bootstrap_token, never printed, never
#                        placed in argv (handed to curl on stdin via `--config -`)
# and the caller's print_error/EXIT_FAILURE convention (a fallback print_error is defined here
# when the caller has none). Functions return non-zero rather than exiting; every
# state-changing API call happens only after input validation and the token preflight, and a
# user created by this run is deleted again if a later step fails (AC-BI-016: no half-created
# user). A pre-existing user is never deleted.
#
# Bash 3.2 compatible (macOS /bin/bash): no associative arrays, no ${var,,}, no mapfile.

if [[ -n "${AUTHENTIK_OWNER_LIB_LOADED:-}" ]]; then
  return 0
fi
AUTHENTIK_OWNER_LIB_LOADED=1

if ! declare -F print_error >/dev/null 2>&1; then
  print_error() {
    local format="$1"
    shift
    # shellcheck disable=SC2059
    printf "$format" "$@" >&2
  }
fi

readonly AUTHENTIK_ADMIN_GROUP_NAME="authentik Admins"
readonly OWNER_EMAIL_PATTERN='^[^[:space:]@]+@[^[:space:]@]+$'
readonly OWNER_LINK_TTL_DEFAULT_MINUTES=30
readonly AUTHENTIK_API_TIMEOUT_SECONDS=30
# The chart's bundled blueprint (charts/policy-system/files/authentik-blueprint.yaml, its
# metadata.name): it creates the recovery flow and patches the brand the owner link needs.
readonly AUTHENTIK_BLUEPRINT_NAME="PS Service — bundled Authentik setup (issue #129)"

# Process-wide results (plain assignment; never call these functions inside $(...)).
AUTHENTIK_HTTP_STATUS=""
AUTHENTIK_HTTP_BODY=""
AUTHENTIK_CURL_EXIT=0
OWNER_PK=""
OWNER_CREATED=false
OWNER_LINK=""
OWNER_STATE=""
OWNER_DEVICE_COUNT=0

# validate_owner_email <email>: lenient shape check (one @, no whitespace, both sides non-empty).
validate_owner_email() {
  local email="$1"
  if [[ ! "$email" =~ $OWNER_EMAIL_PATTERN ]]; then
    print_error 'Owner email "%s" is not a valid address. Fix: pass a real address, e.g. --owner-email you@example.com.\n' "$email"
    return 1
  fi
}

# prompt_owner_email: fills OWNER_EMAIL from stdin when blank (never when SKIP_OWNER_PROMPT=true,
# the --yes case), then validates it.
prompt_owner_email() {
  if [[ -z "${OWNER_EMAIL:-}" ]]; then
    if [[ "${SKIP_OWNER_PROMPT:-false}" == "true" ]]; then
      print_error 'No owner email given and prompting is disabled. Fix: pass --owner-email <address>.\n'
      return 1
    fi
    printf 'Owner email (becomes the first SystemOwner; --owner-email): ' >&2
    read -r OWNER_EMAIL || true
  fi
  validate_owner_email "${OWNER_EMAIL:-}"
}

# read_bootstrap_token <namespace> <secret-name>: loads PS_AUTHENTIK_API_TOKEN from the cluster
# Secret into AUTHENTIK_API_TOKEN (D-21: a shell variable only, never echoed or exported).
read_bootstrap_token() {
  local namespace="$1" secret_name="$2" encoded
  if ! encoded="$(kubectl -n "$namespace" get secret "$secret_name" \
    -o 'jsonpath={.data.PS_AUTHENTIK_API_TOKEN}' 2>/dev/null)" || [[ -z "$encoded" ]]; then
    print_error 'Cannot read key PS_AUTHENTIK_API_TOKEN from Secret %s in namespace %s. Fix: deploy the chart first (helm upgrade --install creates it), then re-run.\n' \
      "$secret_name" "$namespace"
    return 1
  fi
  AUTHENTIK_API_TOKEN="$(printf '%s' "$encoded" | base64 -d 2>/dev/null || printf '%s' "$encoded" | base64 -D)"
}

# authentik_api <METHOD> <path under /api/v3> [json-body]: one API call. Sets
# AUTHENTIK_HTTP_STATUS/AUTHENTIK_HTTP_BODY/AUTHENTIK_CURL_EXIT; returns curl's exit code (0 when
# a response arrived, whatever its HTTP status). TLS is always verified (caller-supplied --cacert).
authentik_api() {
  local method="$1" path="$2" body="${3:-}" out rc
  local args=(-sS --max-time "$AUTHENTIK_API_TIMEOUT_SECONDS" -w $'\n%{http_code}' -X "$method")
  if [[ ${#AUTHENTIK_CURL_ARGS[@]} -gt 0 ]]; then
    args+=("${AUTHENTIK_CURL_ARGS[@]}")
  fi
  if [[ -n "$body" ]]; then
    args+=(--data "$body")
  fi
  args+=(--config - "${AUTHENTIK_API_BASE}/api/v3${path}")
  AUTHENTIK_HTTP_STATUS=0
  AUTHENTIK_HTTP_BODY=""
  # The token travels on curl's stdin config, so it is in no argv (ps) and no argv log (D-21).
  out="$(printf 'header = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\n' \
    "$AUTHENTIK_API_TOKEN" | curl "${args[@]}")"
  rc=$?
  AUTHENTIK_CURL_EXIT=$rc
  if [[ $rc -ne 0 ]]; then
    return "$rc"
  fi
  AUTHENTIK_HTTP_STATUS="${out##*$'\n'}"
  AUTHENTIK_HTTP_BODY="${out%$'\n'*}"
}

# current_kube_namespace: prints the active kubectl context's namespace, "default" when it has none
# (the namespace `helm upgrade --install` without --namespace installs into).
current_kube_namespace() {
  local ns
  ns="$(kubectl config view --minify --output 'jsonpath={..namespace}' 2>/dev/null || true)"
  printf '%s' "${ns:-default}"
}

# start_authentik_port_forward <namespace> <service> <service-port>: forwards a random loopback
# port to the Service in the background (kubectl picks the local port, so it never collides) and
# sets AUTHENTIK_PF_LOCAL_PORT once kubectl reports it; AUTHENTIK_PF_PID/AUTHENTIK_PF_LOG track the
# process so stop_authentik_port_forward can end it (callers also call it from an EXIT trap).
# Loopback only; the forwarded port speaks plain HTTP to the Service, never TLS.
AUTHENTIK_PF_PID=""
AUTHENTIK_PF_LOG=""
AUTHENTIK_PF_LOCAL_PORT=""
# shellcheck disable=SC2034  # AUTHENTIK_PF_LOCAL_PORT is read by the sourcing scripts
start_authentik_port_forward() {
  local namespace="$1" service="$2" port="$3" i line
  local attempts="${PS_PORT_FORWARD_ATTEMPTS:-100}"
  local interval="${PS_PORT_FORWARD_INTERVAL_SECONDS:-0.2}"
  AUTHENTIK_PF_LOCAL_PORT=""
  AUTHENTIK_PF_LOG="$(mktemp)"
  kubectl -n "$namespace" port-forward "svc/${service}" ":${port}" --address 127.0.0.1 \
    >"$AUTHENTIK_PF_LOG" 2>&1 &
  AUTHENTIK_PF_PID=$!
  for ((i = 0; i < attempts; i++)); do
    line="$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9][0-9]*\) .*/\1/p' "$AUTHENTIK_PF_LOG" | head -n 1)"
    if [[ -n "$line" ]]; then
      AUTHENTIK_PF_LOCAL_PORT="$line"
      return 0
    fi
    if ! kill -0 "$AUTHENTIK_PF_PID" 2>/dev/null; then
      break
    fi
    sleep "$interval"
  done
  print_error 'kubectl port-forward to svc/%s failed (%s). Fix: check the pods are Ready (kubectl get pods) and that the Service exists, then re-run; nothing was created.\n' \
    "$service" "$(head -c 200 "$AUTHENTIK_PF_LOG" 2>/dev/null | tr '\n' ' ')"
  stop_authentik_port_forward
  return 1
}

# stop_authentik_port_forward: ends the background port-forward (safe to call when none runs).
stop_authentik_port_forward() {
  if [[ -n "$AUTHENTIK_PF_PID" ]]; then
    kill "$AUTHENTIK_PF_PID" 2>/dev/null || true
    wait "$AUTHENTIK_PF_PID" 2>/dev/null || true
  fi
  if [[ -n "$AUTHENTIK_PF_LOG" ]]; then
    rm -f "$AUTHENTIK_PF_LOG"
  fi
  AUTHENTIK_PF_PID=""
  AUTHENTIK_PF_LOG=""
}

# owner_api_failed <what>: prints the actionable error for the last authentik_api result.
owner_api_failed() {
  local what="$1"
  if [[ "$AUTHENTIK_CURL_EXIT" -ne 0 ]]; then
    print_error 'Cannot reach the Authentik API at %s while %s (curl exit %s). Fix: check the pods are Ready (kubectl get pods), the hostname/port mapping and the CA file, then re-run; nothing was created.\n' \
      "$AUTHENTIK_API_BASE" "$what" "$AUTHENTIK_CURL_EXIT"
  else
    print_error 'Authentik API call failed while %s: HTTP %s %s\n' \
      "$what" "$AUTHENTIK_HTTP_STATUS" "${AUTHENTIK_HTTP_BODY:0:200}"
  fi
}

# check_token_accepted: preflight (F-1/D-A). Authentik creates its bootstrap token once, from the value
# of AUTHENTIK_BOOTSTRAP_TOKEN it first saw (an install predating the token adopts it on upgrade; LC2),
# and never rotates it, so a Secret whose value later changed no longer matches. Live Authentik answers
# a rejected token with 403, some paths with 401; both are the same fix. Runs before any owner call.
check_token_accepted() {
  authentik_api GET "/core/users/me/" || {
    owner_api_failed "checking the shared API token"
    return 1
  }
  case "$AUTHENTIK_HTTP_STATUS" in
    200) return 0 ;;
    401 | 403)
      print_error 'Authentik rejected the shared API token (HTTP %s). Authentik created its bootstrap token from an earlier value of this Secret, and AUTHENTIK_BOOTSTRAP_TOKEN does not rotate it, so the Secret and Authentik now disagree. Fix: create an API token in the Authentik admin UI (kubectl port-forward svc/policy-system-authentik-server 9000:80, then open http://127.0.0.1:9000/if/admin/ ; production serves it under /auth/if/admin/), store it in a Secret with key PS_AUTHENTIK_API_TOKEN, set psService.authentik.existingSecret to that Secret, re-run. On an evaluator cluster with no data to keep, delete the cluster and start again.\n' \
        "$AUTHENTIK_HTTP_STATUS"
      return 1
      ;;
    *)
      owner_api_failed "checking the shared API token"
      return 1
      ;;
  esac
}

# wait_for_authentik_blueprint: blocks (bounded) until the bundled blueprint instance is
# `successful`. On a fresh install the API answers as soon as the server is Ready, but the worker
# discovers and applies blueprints about a minute later; a recovery link requested earlier fails
# with "No recovery flow set." (found live, LC3). PS_BLUEPRINT_WAIT_ATTEMPTS/_INTERVAL_SECONDS
# (default 90 x 2s) bound the wait.
wait_for_authentik_blueprint() {
  local attempts="${PS_BLUEPRINT_WAIT_ATTEMPTS:-90}" interval="${PS_BLUEPRINT_WAIT_INTERVAL_SECONDS:-2}"
  local i state="" encoded
  encoded="$(jq -rn --arg v "$AUTHENTIK_BLUEPRINT_NAME" '$v | @uri')"
  for ((i = 0; i < attempts; i++)); do
    if authentik_api GET "/managed/blueprints/?name=${encoded}" && [[ "$AUTHENTIK_HTTP_STATUS" == "200" ]]; then
      state="$(printf '%s' "$AUTHENTIK_HTTP_BODY" | jq -r --arg n "$AUTHENTIK_BLUEPRINT_NAME" \
        '[.results[]? | select(.name == $n)][0].status // empty')"
      if [[ "$state" == "successful" ]]; then
        return 0
      fi
    fi
    sleep "$interval"
  done
  print_error 'Authentik has not applied its "%s" blueprint (status: %s). Fix: check the Authentik worker is Running and look at its log (kubectl logs deploy/policy-system-authentik-worker), then re-run; nothing was created.\n' \
    "$AUTHENTIK_BLUEPRINT_NAME" "${state:-not found yet}"
  return 1
}

# ensure_owner_user <email>: finds the user (username == email) or creates it in the admin group
# with no password. Sets OWNER_PK and OWNER_CREATED (true only when this run created it).
ensure_owner_user() {
  local email="$1" encoded group_pk payload
  OWNER_PK=""
  OWNER_CREATED=false
  encoded="$(jq -rn --arg v "$email" '$v | @uri')"
  authentik_api GET "/core/users/?username=${encoded}" || {
    owner_api_failed "looking up the owner user"
    return 1
  }
  if [[ "$AUTHENTIK_HTTP_STATUS" != "200" ]]; then
    owner_api_failed "looking up the owner user"
    return 1
  fi
  OWNER_PK="$(printf '%s' "$AUTHENTIK_HTTP_BODY" | jq -r --arg u "$email" \
    '[.results[] | select(.username == $u)][0].pk // empty')"
  if [[ -n "$OWNER_PK" ]]; then
    return 0
  fi

  authentik_api GET "/core/groups/?name=$(jq -rn --arg v "$AUTHENTIK_ADMIN_GROUP_NAME" '$v | @uri')" || {
    owner_api_failed "looking up the admin group"
    return 1
  }
  group_pk="$(printf '%s' "$AUTHENTIK_HTTP_BODY" | jq -r --arg n "$AUTHENTIK_ADMIN_GROUP_NAME" \
    '[.results[]? | select(.name == $n)][0].pk // empty')"
  if [[ "$AUTHENTIK_HTTP_STATUS" != "200" || -z "$group_pk" ]]; then
    owner_api_failed "looking up the admin group"
    return 1
  fi

  payload="$(jq -cn --arg e "$email" --arg g "$group_pk" \
    '{username: $e, name: $e, email: $e, type: "internal", is_active: true, groups: [$g]}')"
  authentik_api POST "/core/users/" "$payload" || {
    owner_api_failed "creating the owner user"
    return 1
  }
  if [[ "$AUTHENTIK_HTTP_STATUS" != "201" ]]; then
    owner_api_failed "creating the owner user"
    return 1
  fi
  OWNER_PK="$(printf '%s' "$AUTHENTIK_HTTP_BODY" | jq -r '.pk // empty')"
  if [[ -z "$OWNER_PK" ]]; then
    owner_api_failed "creating the owner user"
    return 1
  fi
  OWNER_CREATED=true
}

# owner_webauthn_device_count <pk>: sets OWNER_DEVICE_COUNT to the number of authenticator devices
# of that user (AC-BI-004). Uses /authenticators/admin/all/?user=<pk>: the webauthn-specific list ignores the
# `user` filter and would count other users' devices (DECISIONS D-B).
owner_webauthn_device_count() {
  local pk="$1"
  authentik_api GET "/authenticators/admin/all/?user=${pk}" || {
    owner_api_failed "checking the owner's passkeys"
    return 1
  }
  if [[ "$AUTHENTIK_HTTP_STATUS" != "200" ]]; then
    owner_api_failed "checking the owner's passkeys"
    return 1
  fi
  OWNER_DEVICE_COUNT="$(printf '%s' "$AUTHENTIK_HTTP_BODY" | jq -r 'length')"
}

# rewrite_owner_link <link>: prints <link> with its scheme, authority and path prefix replaced by
# AUTHENTIK_LINK_BASE (OD-2: Authentik builds the link from the request it received, so behind a
# port-forward it names 127.0.0.1). A path prefix already present is not doubled.
rewrite_owner_link() {
  local link="$1" base="${AUTHENTIK_LINK_BASE%/}" rest path authority_path prefix=""
  rest="${link#*://}"
  path="/${rest#*/}"
  authority_path="${base#*://}"
  if [[ "$authority_path" == */* ]]; then
    prefix="/${authority_path#*/}"
  fi
  if [[ -n "$prefix" && "$path" == "$prefix"/* ]]; then
    path="${path#"$prefix"}"
  fi
  printf '%s%s' "$base" "$path"
}

# issue_owner_link <pk>: creates a single-use recovery link (PS_OWNER_LINK_TTL minutes, default
# 30) and stores the public form in OWNER_LINK.
issue_owner_link() {
  local pk="$1" ttl="${PS_OWNER_LINK_TTL:-$OWNER_LINK_TTL_DEFAULT_MINUTES}" raw
  if [[ ! "$ttl" =~ ^[1-9][0-9]*$ ]]; then
    print_error 'PS_OWNER_LINK_TTL must be a positive number of minutes, got "%s".\n' "$ttl"
    return 1
  fi
  OWNER_LINK=""
  authentik_api POST "/core/users/${pk}/recovery/?token_duration=minutes=${ttl}" || {
    owner_api_failed "creating the passkey enrolment link"
    return 1
  }
  if [[ "$AUTHENTIK_HTTP_STATUS" != "200" ]]; then
    owner_api_failed "creating the passkey enrolment link"
    return 1
  fi
  raw="$(printf '%s' "$AUTHENTIK_HTTP_BODY" | jq -r '.link // empty')"
  if [[ -z "$raw" ]]; then
    owner_api_failed "creating the passkey enrolment link"
    return 1
  fi
  OWNER_LINK="$(rewrite_owner_link "$raw")"
}

# rollback_owner_user: deletes the owner user, but only when THIS run created it.
rollback_owner_user() {
  if [[ "$OWNER_CREATED" == "true" && -n "$OWNER_PK" ]]; then
    authentik_api DELETE "/core/users/${OWNER_PK}/" || true
    OWNER_CREATED=false
  fi
}

# provision_owner <email>: validate -> token preflight -> ensure user -> (link | already
# enrolled). Sets OWNER_STATE to link-issued or already-enrolled and OWNER_LINK. On any failure
# a user created by this run is removed and the function returns 1.
provision_owner() {
  local email="$1"
  OWNER_LINK=""
  OWNER_STATE=""
  validate_owner_email "$email" || return 1
  check_token_accepted || return 1
  wait_for_authentik_blueprint || return 1
  ensure_owner_user "$email" || return 1
  if ! owner_webauthn_device_count "$OWNER_PK"; then
    rollback_owner_user
    return 1
  fi
  if [[ "$OWNER_DEVICE_COUNT" -gt 0 ]]; then
    OWNER_STATE="already-enrolled"
    return 0
  fi
  if ! issue_owner_link "$OWNER_PK"; then
    rollback_owner_user
    return 1
  fi
  OWNER_STATE="link-issued"
}

# print_owner_link_message: prints the outcome of provision_owner to stdout. The link is a
# credential-equivalent (single use, short lived) and is printed exactly once, here.
print_owner_link_message() {
  local ttl="${PS_OWNER_LINK_TTL:-$OWNER_LINK_TTL_DEFAULT_MINUTES}"
  case "$OWNER_STATE" in
    link-issued)
      printf 'Open this single-use link in a browser to register your passkey (valid %s minutes):\n%s\n' \
        "$ttl" "$OWNER_LINK"
      ;;
    already-enrolled)
      printf 'Owner %s already has a passkey registered; no new link was issued.\n' "${OWNER_EMAIL:-the owner}"
      ;;
  esac
}
