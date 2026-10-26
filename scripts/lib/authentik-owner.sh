# shellcheck shell=bash
# Shared first-owner provisioning for scripts/deploy-ps-eval.sh and scripts/deploy-ps-prod.sh:
# creates the Authentik owner user (username == email, member of the admin group,
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
# Optional: OWNER_IGNORE_EXISTING_DEVICES=true (set when the WebAuthn RP ID changed, so existing
# devices are unusable) makes provision_owner issue a link even when devices exist.
# and the caller's print_error/EXIT_FAILURE convention (a fallback print_error is defined here
# when the caller has none). Functions return non-zero rather than exiting; every
# state-changing API call happens only after input validation and the token preflight, and a
# user created by this run is deleted again if a later step fails, so a failed run leaves no
# half-created user. A pre-existing user is never deleted.
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
# The --help description of --reset-owner-passkey, printed by both deploy scripts after USAGE.
# shellcheck disable=SC2034  # read by the sourcing scripts
readonly OWNER_PASSKEY_RESET_HELP="  --reset-owner-passkey  Recover the owner after a lost or stolen device: deletes every
                         Authentik device, session, OAuth and app-password/API token of the
                         owner, clears the owner's signing passkeys and voids their pending
                         approvals in PS Service, then prints one fresh
                         single-use enrolment link. Changes nothing else. Asks for the owner
                         email and a confirmation; --yes needs --owner-email."

# Process-wide results (plain assignment; never call these functions inside $(...)).
AUTHENTIK_HTTP_STATUS=""
AUTHENTIK_HTTP_BODY=""
AUTHENTIK_CURL_EXIT=0
OWNER_PK=""
OWNER_CREATED=false
OWNER_LINK=""
OWNER_STATE=""
OWNER_DEVICE_COUNT=0
# The Authentik user pk the shared API token belongs to (set by check_token_accepted).
TOKEN_USER_PK=""
# The closing clause of an owner_api_failed message: what the failed run left behind.
OWNER_API_FAILURE_NOTE="nothing was created"

# Owner passkey reset (run_owner_passkey_reset): the installed release's owner identity and
# public Authentik base, the kube context it runs against, and the counts its record line reports.
RESET_OWNER_SUBJECT=""
RESET_OWNER_ISSUER=""
RESET_LINK_BASE=""
RESET_WEB_PATH=""
RESET_HOSTNAME=""
RESET_KUBE_CONTEXT=""
# The owner's devices to delete: a JSON array of {path, pk} (set by plan_owner_devices).
OWNER_DEVICE_PLAN="[]"
RESET_DEVICES_DELETED=0
RESET_SESSIONS_DELETED=0
RESET_OAUTH_TOKENS_REVOKED=0
RESET_CORE_TOKENS_DELETED=0
RESET_CORE_TOKENS_KEPT=0
RESET_CREDENTIALS_DELETED=0
RESET_APPROVALS_VOIDED=0
# The rows of the last authentik_list_pages call, every page, as one JSON array.
AUTHENTIK_LIST_JSON="[]"
readonly OWNER_RESET_PAGE_SIZE=100
readonly OWNER_RESET_MAX_PAGES=1000
# The owner's signing passkeys live in PS Service's own database; the reset reads and revokes
# them by running this module in the PS Service pod (namespace and deployment set by
# run_owner_passkey_reset).
readonly PS_SIGNING_REVOKE_MODULE="ps_service.passkey_signing.revoke"
RESET_NAMESPACE=""
RESET_PS_DEPLOYMENT=""
# The one JSON line of the last ps_signing_exec call, and the counts read_signing_state set.
SIGNING_RESULT=""
SIGNING_CREDENTIALS=0
SIGNING_PENDING_APPROVALS=0

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
# Secret into AUTHENTIK_API_TOKEN: a shell variable only, never echoed or exported, so the token
# reaches no output, child environment or log.
# Shell tracing (set -x) is suspended while the token is in a command, so a traced run never
# prints it.
read_bootstrap_token() {
  local namespace="$1" secret_name="$2" encoded xtrace_was_on=false
  case $- in *x*)
    xtrace_was_on=true
    set +x
    ;;
  esac
  if ! encoded="$(kubectl -n "$namespace" get secret "$secret_name" \
    -o 'jsonpath={.data.PS_AUTHENTIK_API_TOKEN}' 2>/dev/null)" || [[ -z "$encoded" ]]; then
    if [[ "$xtrace_was_on" == true ]]; then set -x; fi
    print_error 'Cannot read key PS_AUTHENTIK_API_TOKEN from Secret %s in namespace %s. Fix: deploy the chart first (helm upgrade --install creates it), then re-run; %s.\n' \
      "$secret_name" "$namespace" "$OWNER_API_FAILURE_NOTE"
    return 1
  fi
  AUTHENTIK_API_TOKEN="$(printf '%s' "$encoded" | base64 -d 2>/dev/null || printf '%s' "$encoded" | base64 -D)"
  if [[ "$xtrace_was_on" == true ]]; then set -x; fi
}

# authentik_api <METHOD> <path under /api/v3> [json-body]: one API call. Sets
# AUTHENTIK_HTTP_STATUS/AUTHENTIK_HTTP_BODY/AUTHENTIK_CURL_EXIT; returns curl's exit code (0 when
# a response arrived, whatever its HTTP status). TLS is always verified (caller-supplied --cacert).
# Shell tracing (set -x) is suspended around the call that carries the token.
authentik_api() {
  local method="$1" path="$2" body="${3:-}" out rc xtrace_was_on=false
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
  case $- in *x*)
    xtrace_was_on=true
    set +x
    ;;
  esac
  # The token travels on curl's stdin config, so it is in no argv (ps) and no argv log.
  out="$(printf 'header = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\n' \
    "$AUTHENTIK_API_TOKEN" | curl "${args[@]}")"
  rc=$?
  if [[ "$xtrace_was_on" == true ]]; then set -x; fi
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
  print_error 'kubectl port-forward to svc/%s failed (%s). Fix: check the pods are Ready (kubectl get pods) and that the Service exists, then re-run; %s.\n' \
    "$service" "$(head -c 200 "$AUTHENTIK_PF_LOG" 2>/dev/null | tr '\n' ' ')" "$OWNER_API_FAILURE_NOTE"
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
    print_error 'Cannot reach the Authentik API at %s while %s (curl exit %s). Fix: check the pods are Ready (kubectl get pods), the hostname/port mapping and the CA file, then re-run; %s.\n' \
      "$AUTHENTIK_API_BASE" "$what" "$AUTHENTIK_CURL_EXIT" "$OWNER_API_FAILURE_NOTE"
  else
    print_error 'Authentik API call failed while %s: HTTP %s %s; %s.\n' \
      "$what" "$AUTHENTIK_HTTP_STATUS" "${AUTHENTIK_HTTP_BODY:0:200}" "$OWNER_API_FAILURE_NOTE"
  fi
}

# check_token_accepted: preflight. Authentik creates its bootstrap token once, from the value of
# AUTHENTIK_BOOTSTRAP_TOKEN it first saw (an install that had no token adopts it on upgrade),
# and never rotates it, so a Secret whose value later changed no longer matches. Live Authentik answers
# a rejected token with 403, some paths with 401; both are the same fix. Runs before any owner call.
# On success sets TOKEN_USER_PK to the pk of the user the token belongs to; a response that does
# not name one fails, because the reset decides which tokens to keep by it.
check_token_accepted() {
  TOKEN_USER_PK=""
  authentik_api GET "/core/users/me/" || {
    owner_api_failed "checking the shared API token"
    return 1
  }
  case "$AUTHENTIK_HTTP_STATUS" in
    200)
      TOKEN_USER_PK="$(printf '%s' "$AUTHENTIK_HTTP_BODY" | jq -r '.user.pk // empty' 2>/dev/null || true)"
      if [[ -z "$TOKEN_USER_PK" ]]; then
        owner_api_failed "reading which user the shared API token belongs to"
        return 1
      fi
      return 0
      ;;
    401 | 403)
      print_error 'Authentik rejected the shared API token (HTTP %s). Authentik created its bootstrap token from an earlier value of this Secret, and AUTHENTIK_BOOTSTRAP_TOKEN does not rotate it, so the Secret and Authentik now disagree. Fix: sign in to the Authentik admin UI as the superuser akadmin with a single-use recovery key (a passkey does not sign in through a port-forward): run kubectl port-forward svc/policy-system-authentik-server 9000:80, then kubectl exec deploy/policy-system-authentik-worker -- ak create_recovery_key 10 akadmin, which prints a path starting /auth/recovery/use-token/; open that path on http://127.0.0.1:9000 within 10 minutes, then open http://127.0.0.1:9000/auth/if/admin/. Create an API token there for a user other than the owner, store it in a Secret with key PS_AUTHENTIK_API_TOKEN, set psService.authentik.existingSecret to that Secret, re-run. On an evaluator cluster with no data to keep, delete the cluster and start again.\n' \
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
# `successful`. A blueprint that lands in `error` is re-applied (up to PS_BLUEPRINT_REAPPLY_ATTEMPTS,
# default 3): on a fresh install the worker applies several blueprints at once and Postgres can abort
# one with a deadlock, and Authentik re-applies a blueprint only when its file changes, so an
# `error` left alone stays `error` forever. On a fresh install the API answers as soon as the server is Ready, but the worker
# discovers and applies blueprints about a minute later; a recovery link requested earlier fails
# with "No recovery flow set.". PS_BLUEPRINT_WAIT_ATTEMPTS/_INTERVAL_SECONDS
# (default 90 x 2s) bound the wait.
wait_for_authentik_blueprint() {
  local attempts="${PS_BLUEPRINT_WAIT_ATTEMPTS:-90}" interval="${PS_BLUEPRINT_WAIT_INTERVAL_SECONDS:-2}"
  local reapplies_left="${PS_BLUEPRINT_REAPPLY_ATTEMPTS:-3}"
  local i state="" encoded pk
  encoded="$(jq -rn --arg v "$AUTHENTIK_BLUEPRINT_NAME" '$v | @uri')"
  for ((i = 0; i < attempts; i++)); do
    if authentik_api GET "/managed/blueprints/?name=${encoded}" && [[ "$AUTHENTIK_HTTP_STATUS" == "200" ]]; then
      state="$(printf '%s' "$AUTHENTIK_HTTP_BODY" | jq -r --arg n "$AUTHENTIK_BLUEPRINT_NAME" \
        '[.results[]? | select(.name == $n)][0].status // empty')"
      if [[ "$state" == "successful" ]]; then
        return 0
      fi
      if [[ "$state" == "error" && "$reapplies_left" -gt 0 ]]; then
        pk="$(printf '%s' "$AUTHENTIK_HTTP_BODY" | jq -r --arg n "$AUTHENTIK_BLUEPRINT_NAME" \
          '[.results[]? | select(.name == $n)][0].pk // empty')"
        if [[ -n "$pk" ]]; then
          reapplies_left=$((reapplies_left - 1))
          authentik_api POST "/managed/blueprints/${pk}/apply/" || true
        fi
      fi
    fi
    sleep "$interval"
  done
  print_error 'Authentik has not applied its "%s" blueprint (status: %s). Fix: check the Authentik worker is Running and look at its log (kubectl logs deploy/policy-system-authentik-worker; a status of error usually has its cause in the task log), then re-run; nothing was created.\n' \
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
# of that user. Uses /authenticators/admin/all/?user=<pk>: the webauthn-specific list ignores the
# `user` filter and would count other users' devices.
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
# AUTHENTIK_LINK_BASE (Authentik builds the link from the request it received, so behind a
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
  if [[ "$OWNER_DEVICE_COUNT" -gt 0 && "${OWNER_IGNORE_EXISTING_DEVICES:-false}" != "true" ]]; then
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
    reset-link-issued)
      printf 'Open this single-use link in a browser to register a new passkey (valid %s minutes):\n%s\n' \
        "$ttl" "$OWNER_LINK"
      ;;
    already-enrolled)
      printf 'Owner %s already has a passkey registered; no new link was issued.\n' "${OWNER_EMAIL:-the owner}"
      ;;
  esac
}

# owner_reset_require_tools: the tools the owner passkey reset runs (no az, openssl or podman).
owner_reset_require_tools() {
  local tool missing=""
  for tool in kubectl helm jq curl; do
    command -v "$tool" >/dev/null 2>&1 || missing="${missing:+$missing, }$tool"
  done
  if [[ -n "$missing" ]]; then
    print_error 'Missing required tool(s): %s. Fix: install them and re-run; nothing was changed.\n' "$missing"
    return 1
  fi
}

# owner_reset_kube_context: sets RESET_KUBE_CONTEXT to the current kube context.
owner_reset_kube_context() {
  RESET_KUBE_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
  if [[ -z "$RESET_KUBE_CONTEXT" ]]; then
    print_error 'No current kube context. Fix: kubectl config use-context <name>, then re-run; nothing was changed.\n'
    return 1
  fi
}

# require_cluster_reachable <namespace>: lists the namespace's deployments (bounded), which fails
# when the cluster is unreachable or the caller may not read the namespace. An empty namespace
# passes; the release check reports a missing install.
require_cluster_reachable() {
  local namespace="$1"
  if ! kubectl -n "$namespace" get deployments -o name --request-timeout=15s >/dev/null 2>&1; then
    print_error 'Cannot reach the cluster of kube context %s, or cannot list deployments in namespace %s. Fix: check the context, your network or VPN and your role in that namespace (kubectl -n %s get deployments), then re-run; nothing was changed.\n' \
      "$RESET_KUBE_CONTEXT" "$namespace" "$namespace"
    return 1
  fi
}

# read_reset_release <release>: reads the installed release's owner identity and public Authentik
# URL from its Helm values. Sets RESET_OWNER_SUBJECT, RESET_OWNER_ISSUER, RESET_LINK_BASE (the
# publicUrl), RESET_WEB_PATH (its path, e.g. /auth) and RESET_HOSTNAME (its host, no port).
read_reset_release() {
  local release="$1" values="" missing="" rest authority path
  RESET_OWNER_SUBJECT=""
  RESET_OWNER_ISSUER=""
  RESET_LINK_BASE=""
  if ! helm status "$release" >/dev/null 2>&1; then
    print_error 'No Helm release %s in kube context %s. Fix: run the full deploy first (the deploy script without --reset-owner-passkey); nothing was changed.\n' \
      "$release" "$RESET_KUBE_CONTEXT"
    return 1
  fi
  values="$(helm get values "$release" -o json 2>/dev/null || true)"
  RESET_OWNER_SUBJECT="$(printf '%s' "$values" | jq -r '.psService.authzBootstrapOwner.subject // empty' 2>/dev/null || true)"
  RESET_OWNER_ISSUER="$(printf '%s' "$values" | jq -r '.psService.authzBootstrapOwner.issuer // empty' 2>/dev/null || true)"
  RESET_LINK_BASE="$(printf '%s' "$values" | jq -r '.psService.authentik.publicUrl // empty' 2>/dev/null || true)"
  [[ -n "$RESET_OWNER_SUBJECT" ]] || missing="psService.authzBootstrapOwner.subject"
  [[ -n "$RESET_OWNER_ISSUER" ]] || missing="${missing:+$missing, }psService.authzBootstrapOwner.issuer"
  if [[ ! "$RESET_LINK_BASE" =~ ^https://[^/]+(/|$) ]]; then
    missing="${missing:+$missing, }psService.authentik.publicUrl"
  fi
  if [[ -n "$missing" ]]; then
    print_error 'Helm release %s does not record %s. Fix: run the full deploy first (the deploy script without --reset-owner-passkey); nothing was changed.\n' \
      "$release" "$missing"
    return 1
  fi
  rest="${RESET_LINK_BASE#https://}"
  authority="${rest%%/*}"
  path=""
  if [[ "$rest" == */* ]]; then
    path="/${rest#*/}"
  fi
  RESET_WEB_PATH="${path%/}"
  RESET_HOSTNAME="${authority%%:*}"
}

# check_reset_owner_matches_release <email>: the reset acts only on the owner the release was
# installed for (exact match with psService.authzBootstrapOwner.subject).
check_reset_owner_matches_release() {
  local email="$1"
  if [[ "$email" != "$RESET_OWNER_SUBJECT" ]]; then
    print_error 'The owner email "%s" is not the owner this release was installed for ("%s", psService.authzBootstrapOwner.subject). Fix: pass --owner-email %s. Nothing was changed.\n' \
      "$email" "$RESET_OWNER_SUBJECT" "$RESET_OWNER_SUBJECT"
    return 1
  fi
}

# find_owner_user <email>: looks the owner up (username == email) and sets OWNER_PK. Never
# creates a user.
find_owner_user() {
  local email="$1" encoded
  OWNER_PK=""
  encoded="$(jq -rn --arg v "$email" '$v | @uri')"
  if ! authentik_api GET "/core/users/?username=${encoded}" || [[ "$AUTHENTIK_HTTP_STATUS" != "200" ]]; then
    owner_api_failed "looking up the owner user"
    return 1
  fi
  OWNER_PK="$(printf '%s' "$AUTHENTIK_HTTP_BODY" | jq -r --arg u "$email" \
    '[.results[]? | select(.username == $u)][0].pk // empty')"
  if [[ -z "$OWNER_PK" ]]; then
    print_error 'No Authentik user "%s" exists. Fix: run the full deploy (it creates the owner); nothing was changed.\n' "$email"
    return 1
  fi
}

# device_delete_path <meta_model_name>: prints the API path a device of that type is retrieved
# and deleted under (a device pk is unique only within its type). Returns 1 for an unknown type.
device_delete_path() {
  case "$1" in
    authentik_stages_authenticator_webauthn.webauthndevice) printf '/authenticators/admin/webauthn/' ;;
    authentik_stages_authenticator_totp.totpdevice) printf '/authenticators/admin/totp/' ;;
    authentik_stages_authenticator_static.staticdevice) printf '/authenticators/admin/static/' ;;
    authentik_stages_authenticator_duo.duodevice) printf '/authenticators/admin/duo/' ;;
    authentik_stages_authenticator_sms.smsdevice) printf '/authenticators/admin/sms/' ;;
    authentik_stages_authenticator_email.emaildevice) printf '/authenticators/admin/email/' ;;
    authentik_stages_authenticator_endpoint_gdtc.endpointdevice) printf '/authenticators/admin/endpoint/' ;;
    authentik_endpoints_connectors_agent.appleindependentsecureenclave) printf '/endpoints/agents/psso/ise/' ;;
    *) return 1 ;;
  esac
}

# owner_row_mismatch <what>: the error for a listed row that belongs to another user; the reset
# refuses to act on a listing whose per-user filter Authentik did not apply.
owner_row_mismatch() {
  print_error "Authentik returned another user's %s for the owner's listing (the per-user filter was not applied); refusing to delete it. Fix: check that Authentik runs the version the chart pins (helm get values / kubectl get pods -o wide), then re-run; %s.\n" \
    "$1" "$OWNER_API_FAILURE_NOTE"
}

# plan_owner_devices <pk>: lists every Authentik device of the user (all types, from the one
# listing Authentik filters per user), resolves each to its per-type path and re-reads it there to
# confirm it belongs to the user (a type that carries no user, the endpoint device, is taken from
# the listing alone). Sets OWNER_DEVICE_PLAN and OWNER_DEVICE_COUNT. Changes nothing.
plan_owner_devices() {
  local pk="$1" listing kind device_pk path owner
  OWNER_DEVICE_COUNT=0
  OWNER_DEVICE_PLAN="[]"
  if ! authentik_api GET "/authenticators/admin/all/?user=${pk}" || [[ "$AUTHENTIK_HTTP_STATUS" != "200" ]]; then
    owner_api_failed "listing the owner's devices"
    return 1
  fi
  listing="$AUTHENTIK_HTTP_BODY"
  while IFS=$'\t' read -r -u 3 kind device_pk; do
    if ! path="$(device_delete_path "$kind")"; then
      print_error 'The owner has an Authentik device of a type this script does not know (%s), so it cannot delete it. Fix: use a deploy script that supports this Authentik version, then re-run; nothing was changed.\n' \
        "$kind"
      return 1
    fi
    device_pk="$(jq -rn --arg v "$device_pk" '$v | @uri')"
    if ! authentik_api GET "${path}${device_pk}/" || [[ "$AUTHENTIK_HTTP_STATUS" != "200" ]]; then
      owner_api_failed "checking the owner's devices"
      return 1
    fi
    owner="$(printf '%s' "$AUTHENTIK_HTTP_BODY" | jq -r \
      '.user | if type == "object" then .pk elif type == "number" then . else null end | if . == null then "" else tostring end')"
    if [[ -n "$owner" && "$owner" != "$pk" ]]; then
      owner_row_mismatch "device"
      return 1
    fi
    OWNER_DEVICE_PLAN="$(printf '%s' "$OWNER_DEVICE_PLAN" | jq -c --arg path "$path" --arg pk "$device_pk" \
      '. + [{path: $path, pk: $pk}]')"
  done 3< <(printf '%s' "$listing" | jq -r '.[] | [.meta_model_name, (.pk | tostring)] | @tsv')
  OWNER_DEVICE_COUNT="$(printf '%s' "$OWNER_DEVICE_PLAN" | jq -r 'length')"
}

# delete_owner_devices: deletes every device in OWNER_DEVICE_PLAN at its per-type path. A device
# already gone (404) is not counted. Sets RESET_DEVICES_DELETED.
delete_owner_devices() {
  local path device_pk
  RESET_DEVICES_DELETED=0
  while IFS=$'\t' read -r -u 3 path device_pk; do
    authentik_api DELETE "${path}${device_pk}/" || {
      owner_api_failed "deleting the owner's devices"
      return 1
    }
    case "$AUTHENTIK_HTTP_STATUS" in
      204) RESET_DEVICES_DELETED=$((RESET_DEVICES_DELETED + 1)) ;;
      404) ;;
      *)
        owner_api_failed "deleting the owner's devices"
        return 1
        ;;
    esac
  done 3< <(printf '%s' "$OWNER_DEVICE_PLAN" | jq -r '.[] | [.path, .pk] | @tsv')
}

# authentik_list_pages <path-with-query> <what>: reads every page of a paginated Authentik list
# (page_size 100, following pagination.next, the next page number, until it is 0) into
# AUTHENTIK_LIST_JSON. A failed page, a next page that does not advance or more than
# OWNER_RESET_MAX_PAGES pages print an error naming <what> and return 1.
authentik_list_pages() {
  local path="$1" what="$2" separator="?" page=1 pages=0 next
  AUTHENTIK_LIST_JSON="[]"
  if [[ "$path" == *\?* ]]; then
    separator="&"
  fi
  while :; do
    pages=$((pages + 1))
    if [[ "$pages" -gt "$OWNER_RESET_MAX_PAGES" ]]; then
      print_error 'Authentik returned more than %s pages while %s; refusing to continue. Fix: check that Authentik runs the version the chart pins, then re-run; %s.\n' \
        "$OWNER_RESET_MAX_PAGES" "$what" "$OWNER_API_FAILURE_NOTE"
      return 1
    fi
    if ! authentik_api GET "${path}${separator}page_size=${OWNER_RESET_PAGE_SIZE}&page=${page}" ||
      [[ "$AUTHENTIK_HTTP_STATUS" != "200" ]]; then
      owner_api_failed "$what"
      return 1
    fi
    if ! AUTHENTIK_LIST_JSON="$(printf '%s\n%s' "$AUTHENTIK_LIST_JSON" "$AUTHENTIK_HTTP_BODY" |
      jq -cs '.[0] + (.[1].results | if type == "array" then . else error("no results") end)' 2>/dev/null)"; then
      owner_api_failed "$what"
      return 1
    fi
    next="$(printf '%s' "$AUTHENTIK_HTTP_BODY" | jq -r '.pagination.next // 0' 2>/dev/null || true)"
    if [[ ! "$next" =~ ^[0-9]+$ ]]; then
      owner_api_failed "$what"
      return 1
    fi
    if [[ "$next" -eq 0 ]]; then
      return 0
    fi
    if [[ "$next" -le "$page" ]]; then
      print_error "Authentik's pagination did not advance past page %s while %s (next page %s); refusing to continue. Fix: check that Authentik runs the version the chart pins, then re-run; %s.\n" \
        "$page" "$what" "$next" "$OWNER_API_FAILURE_NOTE"
      return 1
    fi
    page="$next"
  done
}

# owner_rows_checked <what> <owner-jq>: every row of AUTHENTIK_LIST_JSON must belong to the owner
# (<owner-jq> picks a row's user pk). A listing whose per-user filter Authentik did not apply
# fails here, before anything is deleted.
owner_rows_checked() {
  local what="$1" owner_jq="$2" foreign
  foreign="$(printf '%s' "$AUTHENTIK_LIST_JSON" | jq -r --arg pk "$OWNER_PK" \
    "[.[] | select((($owner_jq) | tostring) != \$pk)] | length" 2>/dev/null || printf 'unreadable')"
  if [[ "$foreign" != "0" ]]; then
    owner_row_mismatch "$what"
    return 1
  fi
}

# delete_owner_listed <what> <path-prefix> <id-jq>: deletes every row of AUTHENTIK_LIST_JSON at
# <path-prefix><id>/ and sets OWNER_LISTED_DELETED to how many went (204); a 404 is already gone
# and not counted.
OWNER_LISTED_DELETED=0
delete_owner_listed() {
  local what="$1" prefix="$2" id_jq="$3" id
  OWNER_LISTED_DELETED=0
  while IFS= read -r -u 3 id; do
    authentik_api DELETE "${prefix}${id}/" || {
      owner_api_failed "$what"
      return 1
    }
    case "$AUTHENTIK_HTTP_STATUS" in
      204) OWNER_LISTED_DELETED=$((OWNER_LISTED_DELETED + 1)) ;;
      404) ;;
      *)
        owner_api_failed "$what"
        return 1
        ;;
    esac
  done 3< <(printf '%s' "$AUTHENTIK_LIST_JSON" | jq -r ".[] | ($id_jq) | tostring | @uri")
}

# delete_owner_sessions <email>: deletes every Authentik session of the owner. Sessions are listed
# by user__username (Authentik ignores a `user` pk filter on this list) and never bulk-deleted, so
# every deleted id comes from the owner's own listing. Sets RESET_SESSIONS_DELETED.
delete_owner_sessions() {
  local encoded status=0
  RESET_SESSIONS_DELETED=0
  encoded="$(jq -rn --arg v "$1" '$v | @uri')"
  authentik_list_pages "/core/authenticated_sessions/?user__username=${encoded}" \
    "listing the owner's sessions" || return 1
  owner_rows_checked "session" '.user' || return 1
  delete_owner_listed "deleting the owner's sessions" "/core/authenticated_sessions/" '.uuid' || status=1
  RESET_SESSIONS_DELETED="$OWNER_LISTED_DELETED"
  return "$status"
}

# revoke_owner_oauth_tokens <pk>: deletes the owner's OAuth refresh tokens, then access tokens
# (deleting is revoking; with the refresh tokens gone no new access token is issued meanwhile).
# Listed by `user` pk (Authentik ignores user__username on these lists). Sets
# RESET_OAUTH_TOKENS_REVOKED to both together.
revoke_owner_oauth_tokens() {
  local pk="$1" kind status
  RESET_OAUTH_TOKENS_REVOKED=0
  for kind in refresh_tokens access_tokens; do
    authentik_list_pages "/oauth2/${kind}/?user=${pk}" "listing the owner's OAuth tokens" || return 1
    owner_rows_checked "OAuth ${kind%_tokens} token" '.user.pk' || return 1
    status=0
    delete_owner_listed "revoking the owner's OAuth tokens" "/oauth2/${kind}/" '.pk' || status=1
    RESET_OAUTH_TOKENS_REVOKED=$((RESET_OAUTH_TOKENS_REVOKED + OWNER_LISTED_DELETED))
    [[ "$status" -eq 0 ]] || return 1
  done
}

# token_is_owners: true when the shared API token belongs to the owner. Then the owner's
# `api`-intent tokens are kept, since the one in use cannot be told apart from the others.
token_is_owners() {
  [[ -n "$TOKEN_USER_PK" && "$TOKEN_USER_PK" == "$OWNER_PK" ]]
}

# owner_core_tokens_jq: prints the jq filter selecting the owner's core tokens a reset deletes.
owner_core_tokens_jq() {
  if token_is_owners; then
    printf '[.[] | select(.intent != "api")]'
  else
    printf '.'
  fi
}

# delete_owner_core_tokens <email>: deletes the owner's Authentik core tokens of every intent
# (app passwords, API tokens, earlier recovery links), listed by user__username (Authentik ignores
# a `user` pk filter here). When the shared API token is the owner's, its `api` tokens are kept
# and counted. Runs before the new link is issued, which is itself a recovery token. Sets
# RESET_CORE_TOKENS_DELETED and RESET_CORE_TOKENS_KEPT.
delete_owner_core_tokens() {
  local encoded listed status=0
  RESET_CORE_TOKENS_DELETED=0
  RESET_CORE_TOKENS_KEPT=0
  encoded="$(jq -rn --arg v "$1" '$v | @uri')"
  authentik_list_pages "/core/tokens/?user__username=${encoded}" \
    "listing the owner's app-password and API tokens" || return 1
  owner_rows_checked "token" '.user' || return 1
  listed="$(printf '%s' "$AUTHENTIK_LIST_JSON" | jq -r 'length')"
  AUTHENTIK_LIST_JSON="$(printf '%s' "$AUTHENTIK_LIST_JSON" | jq -c "$(owner_core_tokens_jq)")"
  RESET_CORE_TOKENS_KEPT=$((listed - $(printf '%s' "$AUTHENTIK_LIST_JSON" | jq -r 'length')))
  delete_owner_listed "deleting the owner's app-password and API tokens" "/core/tokens/" \
    '.identifier' || status=1
  RESET_CORE_TOKENS_DELETED="$OWNER_LISTED_DELETED"
  return "$status"
}

# warn_owner_tokens_kept <token-secret>: when the shared API token is the owner's, states how many
# of the owner's API tokens were kept and how to let the next reset delete them (stderr).
warn_owner_tokens_kept() {
  if token_is_owners; then
    print_error "Warning: the shared Authentik API token belongs to the owner, so %s of the owner's API tokens were kept (the one in use cannot be told apart). Fix: move PS_AUTHENTIK_API_TOKEN in Secret %s to a token of a non-owner Authentik user, then re-run --reset-owner-passkey; it then deletes them.\n" \
      "$RESET_CORE_TOKENS_KEPT" "$1"
  fi
}

# count_owner_devices: prints the number of the owner's devices, or "unknown" when the listing
# cannot be read. Safe inside $(...): it changes no global the caller reads.
count_owner_devices() {
  if authentik_api GET "/authenticators/admin/all/?user=${OWNER_PK}" && [[ "$AUTHENTIK_HTTP_STATUS" == "200" ]]; then
    printf '%s' "$AUTHENTIK_HTTP_BODY" | jq -r 'length' 2>/dev/null || printf 'unknown'
  else
    printf 'unknown'
  fi
}

# count_owner_listed <path-with-query> [jq-filter]: prints how many rows the paginated listing
# holds (after the optional filter), or "unknown" when it cannot be read. Safe inside $(...).
count_owner_listed() {
  if authentik_list_pages "$1" "counting" 2>/dev/null; then
    printf '%s' "$AUTHENTIK_LIST_JSON" | jq -r "${2:-.} | length" 2>/dev/null || printf 'unknown'
  else
    printf 'unknown'
  fi
}

# count_owner_oauth_tokens: prints the owner's OAuth refresh plus access tokens, or "unknown".
count_owner_oauth_tokens() {
  local refresh access
  refresh="$(count_owner_listed "/oauth2/refresh_tokens/?user=${OWNER_PK}")"
  access="$(count_owner_listed "/oauth2/access_tokens/?user=${OWNER_PK}")"
  if [[ "$refresh" =~ ^[0-9]+$ && "$access" =~ ^[0-9]+$ ]]; then
    printf '%s' "$((refresh + access))"
  else
    printf 'unknown'
  fi
}

# ps_signing_exec <expected-status> [--count-only]: runs the signing revoke module for the
# release's owner (RESET_OWNER_SUBJECT/RESET_OWNER_ISSUER) in the PS Service pod and sets
# SIGNING_RESULT to its one JSON line. Anything but exactly one JSON line with the expected
# status (revoked/counted) fails, naming the command's reason_code. The Authentik token never
# reaches this command.
ps_signing_exec() {
  local expected="$1" output="" status="" reason="" rc=0
  shift
  output="$(kubectl -n "$RESET_NAMESPACE" exec "deploy/${RESET_PS_DEPLOYMENT}" -c ps-service \
    --pod-running-timeout=60s -- python -m "$PS_SIGNING_REVOKE_MODULE" \
    --subject "$RESET_OWNER_SUBJECT" --issuer "$RESET_OWNER_ISSUER" "$@" 2>/dev/null)" || rc=$?
  if [[ "$output" != *$'\n'* ]]; then
    status="$(printf '%s' "$output" | jq -r '.status // empty' 2>/dev/null)" || status=""
    reason="$(printf '%s' "$output" | jq -r '.reason_code // empty' 2>/dev/null)" || reason=""
  fi
  if [[ "$rc" -eq 0 && -n "$status" && "$status" == "$expected" ]]; then
    SIGNING_RESULT="$output"
    return 0
  fi
  [[ "$reason" =~ ^[a-z_]+$ ]] || reason="no result"
  print_error 'The signing passkey step in the PS Service pod failed (%s). Fix: check the pod (kubectl -n %s get pods; kubectl -n %s logs deploy/%s), then re-run; %s.\n' \
    "$reason" "$RESET_NAMESPACE" "$RESET_NAMESPACE" "$RESET_PS_DEPLOYMENT" "$OWNER_API_FAILURE_NOTE"
  return 1
}

# read_signing_state: reads (changes nothing) how many signing credentials and pending approvals
# the owner has in PS Service; sets SIGNING_CREDENTIALS and SIGNING_PENDING_APPROVALS.
read_signing_state() {
  ps_signing_exec counted --count-only || return 1
  SIGNING_CREDENTIALS="$(printf '%s' "$SIGNING_RESULT" | jq -r '.credentials')"
  SIGNING_PENDING_APPROVALS="$(printf '%s' "$SIGNING_RESULT" | jq -r '.pending_approvals')"
}

# revoke_owner_signing: voids every pending approval of the owner and deletes every signing
# passkey of the owner in PS Service, which records its own audit row; sets
# RESET_CREDENTIALS_DELETED and RESET_APPROVALS_VOIDED.
revoke_owner_signing() {
  ps_signing_exec revoked || return 1
  RESET_CREDENTIALS_DELETED="$(printf '%s' "$SIGNING_RESULT" | jq -r '.credentials_deleted')"
  RESET_APPROVALS_VOIDED="$(printf '%s' "$SIGNING_RESULT" | jq -r '.voided_approval_ids | length')"
}

# count_owner_signing: prints "signing_credentials=N pending_approvals=M", each "unknown" when
# the pod cannot be read. Safe inside $(...).
count_owner_signing() {
  if [[ -n "$RESET_PS_DEPLOYMENT" ]] && read_signing_state 2>/dev/null; then
    printf 'signing_credentials=%s pending_approvals=%s' "$SIGNING_CREDENTIALS" "$SIGNING_PENDING_APPROVALS"
  else
    printf 'signing_credentials=unknown pending_approvals=unknown'
  fi
}

# owner_remaining_counts <email>: prints what still authenticates as the owner, one key=value per source;
# a source that cannot be read shows "unknown". API tokens kept because the shared token is the
# owner's are not counted.
owner_remaining_counts() {
  local encoded
  encoded="$(jq -rn --arg v "$1" '$v | @uri')"
  printf 'devices=%s sessions=%s oauth_tokens=%s core_tokens=%s %s' \
    "$(count_owner_devices)" \
    "$(count_owner_listed "/core/authenticated_sessions/?user__username=${encoded}")" \
    "$(count_owner_oauth_tokens)" \
    "$(count_owner_listed "/core/tokens/?user__username=${encoded}" "$(owner_core_tokens_jq)")" \
    "$(count_owner_signing)"
}

# owner_remaining_report <email> [counts]: after a failed step, states what remains for the owner
# (recounted unless given), names every count that could not be recounted, and says a re-run is
# safe (stderr). No link has been issued then.
owner_remaining_report() {
  local counts="${2:-}" pair unknown=""
  [[ -n "$counts" ]] || counts="$(owner_remaining_counts "$1")"
  for pair in $counts; do
    if [[ "$pair" == *=unknown ]]; then
      unknown="${unknown:+$unknown, }${pair%=unknown}"
    fi
  done
  if [[ -n "$unknown" ]]; then
    unknown=" Could not recount: ${unknown}."
  fi
  print_error 'Remaining for %s: %s.%s No link was issued. Re-running --reset-owner-passkey is safe and finishes the reset.\n' \
    "$1" "$counts" "$unknown"
}

# owner_reset_postcondition <email>: recounts every source before the link is issued; anything
# left (e.g. a device, session, signing passkey or pending approval added while the reset ran)
# or unreadable fails the reset.
owner_reset_postcondition() {
  local counts pair
  counts="$(owner_remaining_counts "$1")"
  for pair in $counts; do
    case "$pair" in
      *=0) ;;
      *)
        print_error 'The owner still has credentials or pending approvals after the reset (%s).\n' "$pair"
        owner_remaining_report "$1" "$counts"
        return 1
        ;;
    esac
  done
}

# confirm_owner_reset <profile>: shows what the reset changes and asks for confirmation on stdin
# (skipped when SKIP_OWNER_PROMPT=true). eval accepts y/yes; prod accepts only the owner email
# typed in full, because a production owner reset locks out every device the owner signed in on.
confirm_owner_reset() {
  local profile="$1" answer=""
  if [[ "${SKIP_OWNER_PROMPT:-false}" == "true" ]]; then
    return 0
  fi
  {
    printf "Reset the owner's passkeys (nothing else is changed):\n"
    printf '  Owner:                %s\n' "$OWNER_EMAIL"
    printf '  Authentik devices:    %s (deleted)\n' "$OWNER_DEVICE_COUNT"
    printf '  Signing credentials:  %s (deleted)\n' "$SIGNING_CREDENTIALS"
    printf '  Pending approvals:    %s (voided)\n' "$SIGNING_PENDING_APPROVALS"
    printf '  Kube context:         %s\n' "$RESET_KUBE_CONTEXT"
    printf '  Hostname:             %s\n' "$RESET_HOSTNAME"
    if token_is_owners; then
      printf "  Shared API token:     belongs to this owner; the owner's API tokens are kept\n"
    fi
    printf "All of the owner's Authentik sessions, OAuth tokens and app-password/API tokens are also\n"
    printf 'revoked, then one new single-use enrolment link is printed.\n'
    if [[ "$profile" == "prod" ]]; then
      printf 'Type the owner email (%s) to proceed: ' "$OWNER_EMAIL"
    else
      printf 'Proceed? [y/N] '
    fi
  } >&2
  read -r answer || true
  if [[ "$profile" == "prod" ]]; then
    [[ "$answer" == "$OWNER_EMAIL" ]] && return 0
  else
    case "$answer" in
      [yY] | [yY][eE][sS]) return 0 ;;
    esac
  fi
  print_error 'Declined; nothing was changed.\n'
  return 1
}

# print_owner_reset_record <email>: one UTC-timestamped line on stdout recording what the reset
# removed. Authentik's own event log does not record every one of these deletions, so this line is
# the complete record.
print_owner_reset_record() {
  local email="$1"
  printf 'owner-passkey-reset %s owner=%s kube_context=%s devices_deleted=%s sessions_deleted=%s oauth_tokens_revoked=%s core_tokens_deleted=%s core_tokens_kept=%s signing_credentials_deleted=%s approvals_voided=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$email" "$RESET_KUBE_CONTEXT" \
    "$RESET_DEVICES_DELETED" "$RESET_SESSIONS_DELETED" "$RESET_OAUTH_TOKENS_REVOKED" \
    "$RESET_CORE_TOKENS_DELETED" "$RESET_CORE_TOKENS_KEPT" "$RESET_CREDENTIALS_DELETED" \
    "$RESET_APPROVALS_VOIDED"
}

# run_owner_passkey_reset <profile> <namespace> <release> <token-secret> <authentik-service>
#   <ps-deployment>: the owner passkey reset. Reads and checks everything before any change, then
# issues one fresh single-use enrolment link on the release's public hostname and prints the
# record line followed by the link, last. Returns 0 or 1. The caller sets OWNER_EMAIL and
# SKIP_OWNER_PROMPT and traps stop_authentik_port_forward on EXIT.
run_owner_passkey_reset() {
  local profile="$1" namespace="$2" release="$3" token_secret="$4" authentik_service="$5"
  RESET_NAMESPACE="$namespace"
  RESET_PS_DEPLOYMENT="$6"
  OWNER_API_FAILURE_NOTE="nothing was changed"
  OWNER_LINK=""
  OWNER_STATE=""
  RESET_DEVICES_DELETED=0
  RESET_SESSIONS_DELETED=0
  RESET_OAUTH_TOKENS_REVOKED=0
  RESET_CORE_TOKENS_DELETED=0
  RESET_CORE_TOKENS_KEPT=0
  RESET_CREDENTIALS_DELETED=0
  RESET_APPROVALS_VOIDED=0

  owner_reset_kube_context || return 1
  require_cluster_reachable "$namespace" || return 1
  prompt_owner_email || return 1
  read_reset_release "$release" || return 1
  check_reset_owner_matches_release "$OWNER_EMAIL" || return 1
  read_bootstrap_token "$namespace" "$token_secret" || return 1
  start_authentik_port_forward "$namespace" "$authentik_service" 80 || return 1
  AUTHENTIK_API_BASE="http://127.0.0.1:${AUTHENTIK_PF_LOCAL_PORT}${RESET_WEB_PATH}"
  AUTHENTIK_LINK_BASE="$RESET_LINK_BASE"
  AUTHENTIK_CURL_ARGS=()
  check_token_accepted || return 1
  find_owner_user "$OWNER_EMAIL" || return 1
  plan_owner_devices "$OWNER_PK" || return 1
  read_signing_state || return 1
  confirm_owner_reset "$profile" || return 1

  OWNER_API_FAILURE_NOTE="the remaining counts follow"
  if ! delete_owner_devices ||
    ! delete_owner_sessions "$OWNER_EMAIL" ||
    ! revoke_owner_oauth_tokens "$OWNER_PK" ||
    ! delete_owner_core_tokens "$OWNER_EMAIL" ||
    ! revoke_owner_signing; then
    owner_remaining_report "$OWNER_EMAIL"
    return 1
  fi
  owner_reset_postcondition "$OWNER_EMAIL" || return 1
  issue_owner_link "$OWNER_PK" || {
    owner_remaining_report "$OWNER_EMAIL"
    return 1
  }
  stop_authentik_port_forward
  if [[ "$OWNER_DEVICE_COUNT" -eq 0 ]]; then
    printf 'No Authentik device was found for %s; the sessions, tokens and signing passkeys were still cleared.\n' "$OWNER_EMAIL"
  fi
  print_owner_reset_record "$OWNER_EMAIL"
  warn_owner_tokens_kept "$token_secret"
  OWNER_STATE="reset-link-issued"
  print_owner_link_message
}
